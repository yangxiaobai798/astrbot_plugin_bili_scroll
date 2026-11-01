"""刷B站核心流程：选视频 -> 读字幕 -> LLM判断 -> 写日报。

复刻 dsh-bili-scroll 的 duty.md 章程，把其中依赖"AI 自由 agent"的部分
改成确定性的流水线，保证每次跑法一致、可预算。
"""

import asyncio
import datetime
import json
import os
import random
import re

from astrbot.api import logger


# ----------判重表 ----------

class Ledger:
	"""记录看过哪些视频，避免重复推送。"""

	def __init__(self, data_dir: str):
		self.path = os.path.join(data_dir, "seen.jsonl")
		self._seen = set()
		self._load()

	def _load(self):
		if not os.path.isfile(self.path):
			return
		try:
			with open(self.path, encoding="utf-8") as f:
				for line in f:
					line = line.strip()
					if not line:
						continue
					try:
						rec = json.loads(line)
						if rec.get("bvid"):
							self._seen.add(rec["bvid"])
					except json.JSONDecodeError:
						continue
		except OSError as e:
			logger.warning(f"[bili_scroll] 判重表读取失败: {e}")

	def has(self, bvid: str) -> bool:
		return bvid in self._seen

	def add(self, bvid: str, title: str, status: str, note: str = ""):
		rec = {
			"bvid": bvid,
			"title": (title or "")[:120],
			"status": status,
			"note": note[:200],
			"ts": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
		}
		try:
			with open(self.path, "a", encoding="utf-8") as f:
				f.write(json.dumps(rec, ensure_ascii=False) + "\n")
			self._seen.add(bvid)
		except OSError as e:
			logger.warning(f"[bili_scroll] 判重表写入失败: {e}")

	def recent(self, n: int = 20) -> list:
		if not os.path.isfile(self.path):
			return []
		out = []
		try:
			with open(self.path, encoding="utf-8") as f:
				lines = [x.strip() for x in f if x.strip()]
			for line in lines[-n:]:
				try:
					out.append(json.loads(line))
				except json.JSONDecodeError:
					continue
		except OSError:
			pass
		return out


# ----------LLM判断 ----------

JUDGE_PROMPT = """你是一个挑剔的视频筛子。给我一条B站视频的信息，你判断它值不值得占用我十几分钟。

判断标准：
- **值得看**：有真东西（新技术、实操细节、一手经验），或者跟我关心的东西相关；我看了能学到或会用上
- **可以看**：有信息量，但不看也不亏
- **不值当**：标题党、口水课、复读机、纯流量内容、三小时讲一个入门概念

严格要求：
1. 只有真读过字幕才允许给"值得看"。只看到标题/简介，最多给"可以看"。
2. 不要因为标题写得好看就给高分。
3. 教程类、合集类（比如"全748集"）多数是凑数，倾向于"不值当"。
4. 回答必须是严格的 JSON，不要任何额外文字。

输出格式：
{"level": "值得看|可以看|不值当", "reason": "两三句为什么，要指出具体内容", "one_liner": "你自己的一句话，像跟朋友聊天"}

视频信息：
- 标题：{title}
- UP主：{author}
- 时长：{duration}秒
- 播放：{view}
- 简介：{desc}
- 字幕内容：
{subtitle}
"""


class Judge:
	"""调用 LLM 判断视频价值。"""

	def __init__(self, get_provider, prompt_cache: bool = True, cache_probe: bool = True):
		self._get_provider = get_provider
		self.prompt_cache = prompt_cache
		self.cache_probe = cache_probe

	def _parse(self, raw: str, info: dict | None = None) -> dict:
		"""从模型输出里抽出 JSON。"""
		raw = (raw or "").strip()
		# 去掉 ```json 包裹
		raw = re.sub(r"^```(?:json)?\s*", "", raw)
		raw = re.sub(r"\s*```$", "", raw)
		try:
			d = json.loads(raw)
			if isinstance(d, dict) and "level" in d:
				return d
		except json.JSONDecodeError:
			pass
		# 退而求其次：找第一个 {...}
		m = re.search(r"\{.*\}", raw, re.S)
		if m:
			try:
				d = json.loads(m.group(0))
				if isinstance(d, dict) and "level" in d:
					return d
			except json.JSONDecodeError:
				pass
		# 模型没吐 JSON 时别把"解析失败"直接推给人，
		# 用标题本身当理由，宁可信息少也别露出内部错误
		title = str((info or {}).get("title") or "").strip()
		fallback = f"标题看着有戏：{title[:40]}" if title else "没读到判词，点进去看看"
		return {"level": "可以看", "reason": fallback, "one_liner": ""}

	def _compose(self, info: dict, subtitle_text: str, persona: str = "") -> str:
		"""把章程/人设 + 本条视频拼成一条完整的 user 消息。

		刻意把所有内容都塞进 user，不走 system/instructions：
		部分第三方中转站会丢弃 system（实测 instructions 恒被忽略），
		章程一旦走 system 就等于没写，判断标准会整体失效。
		"""
		sub = (subtitle_text or "").strip()
		if len(sub) > 6000:
			sub = sub[:6000] + "\n...（已截断）"
		if not sub:
			sub = "（没有字幕，只能看标题和简介判断）"

		persona = (persona or "").strip()
		head = JUDGE_PROMPT
		if persona:
			head = "【执行准则】\n" + persona + "\n\n" + head
		if not getattr(self, "prompt_cache", True):
			# 关掉前缀稳定化：恢复成"每处都填真值"的老拼法
			return (head
				.replace("{title}", str(info.get("title", "")))
				.replace("{author}", str(info.get("author", "")))
				.replace("{duration}", str(info.get("duration", 0)))
				.replace("{view}", str(info.get("view", 0)))
				.replace("{desc}", (info.get("desc") or "")[:400])
				.replace("{subtitle}", sub))
		# 只把稳定部分做替换；变化量（本条视频）统一留在最后，
		# 让前面那一大段规则逐字节不变 —— 前缀缓存要靠这个才可能命中。
		stable = (head
			.replace("{title}", "（见下方本条视频）")
			.replace("{author}", "（见下方本条视频）")
			.replace("{duration}", "-")
			.replace("{view}", "-")
			.replace("{desc}", "（见下方本条视频）")
			.replace("{subtitle}", ""))
		body = (
			"【本条视频】\n"
			+ "- 标题：" + str(info.get("title", "")) + "\n"
			+ "- UP主：" + str(info.get("author", "")) + "\n"
			+ "- 时长：" + str(info.get("duration", 0)) + "秒\n"
			+ "- 播放：" + str(info.get("view", 0)) + "\n"
			+ "- 简介：" + (info.get("desc") or "")[:400] + "\n"
			+ "- 字幕内容：\n" + sub + "\n"
		)
		return stable.rstrip() + "\n\n" + body

	async def judge(self, info: dict, subtitle_text: str, persona: str = "") -> dict:
		provider = await self._get_provider()
		if provider is None:
			return {"level": "可以看", "reason": "无可用模型", "one_liner": ""}

		prompt = self._compose(info, subtitle_text, persona)
		try:
			resp = await provider.text_chat(prompt=prompt, system_prompt="")
			text = getattr(resp, "completion_text", "") or ""
			d = self._parse(text, info)
			# 把命中缓存量记进日志，便于判断中转站到底有没有吃缓存
			u = getattr(resp, "usage", None)
			cached = getattr(u, "input_cached", 0) or 0
			other = getattr(u, "input_other", 0) or 0
			if getattr(self, "cache_probe", True):
				if cached:
					logger.info(
						f"[bili_scroll] LLM 缓存命中 {cached} tok（未命中 {other} tok）"
					)
				else:
					logger.info(f"[bili_scroll] LLM 无缓存命中（输入 {other} tok）")
			return d
		except Exception as e:
			logger.warning(f"[bili_scroll] 判断失败: {type(e).__name__}: {e}")
			return {"level": "可以看", "reason": f"判断失败: {str(e)[:80]}", "one_liner": ""}


# ----------日报 ----------

def render_report(date: str, picks: list, skipped: list, total_seen: int) -> str:
	"""生成 Markdown 日报。每条的标题行固定写成 ### BV号。"""
	lines = [f"# B站日报 {date}", ""]
	if not picks:
		lines.append("今天没刷到什么值得看的。")
		if skipped:
			lines.append("")
			lines.append(f"（看了 {total_seen} 条，跳过 {len(skipped)} 条）")
		return "\n".join(lines)

	lines.append(f"今天挑了 {len(picks)} 条：")
	lines.append("")
	for p in picks:
		info = p["info"]
		j = p["judge"]
		lines.append(f"### {info['bvid']}")
		lines.append("")
		lines.append(f"- **标题**：{info.get('title','')}")
		lines.append(f"- **UP**：{info.get('author','')}")
		lines.append(f"- **链接**：https://www.bilibili.com/video/{info['bvid']}")
		lines.append(f"- **播放**：{info.get('view',0)}")
		lines.append(f"- **档位**：{j.get('level','可以看')}")
		lines.append(f"- **为什么值得**：{j.get('reason','')}")
		if j.get("one_liner"):
			lines.append(f"- **一句话**：{j['one_liner']}")
		lines.append("")

	lines.append("---")
	lines.append("")
	lines.append(f"看了 {total_seen} 条，因无字幕跳过 {len(skipped)} 条。")
	return "\n".join(lines)


def render_push(picks: list) -> str:
	"""生成推送用的短文本（适配 QQ，不用 Markdown）。"""
	if not picks:
		return "今天B站没刷到什么值得看的。"
	out = [f"今天给你挑了 {len(picks)} 条："]
	for i, p in enumerate(picks, 1):
		info = p["info"]
		j = p["judge"]
		out.append("")
		out.append(f"{i}. {info.get('title','')}")
		out.append(f"   {j.get('level','')}｜{j.get('one_liner') or j.get('reason','')}")
		out.append(f"   https://www.bilibili.com/video/{info['bvid']}")
	return "\n".join(out)


def render_single(info: dict, judge: dict, has_sub: bool) -> str:
	"""单条视频的即时分析结果，用于分享链接时回复。"""
	lines = []
	title = info.get("title", "")
	author = info.get("author", "")
	level = judge.get("level", "")
	lines.append(f"《{title}》")
	meta = []
	if author:
		meta.append(f"UP：{author}")
	dur = info.get("duration") or 0
	if dur:
		try:
			d = int(dur)
			meta.append(f"时长：{d // 60}分{d % 60}秒" if d >= 60 else f"时长：{d}秒")
		except (TypeError, ValueError):
			meta.append(f"时长：{dur}")
	view = info.get("view") or 0
	if view:
		meta.append(f"播放：{view}")
	if meta:
		lines.append("　".join(meta))
	lines.append("")

	if level:
		lines.append(f"【{level}】")
	reason = judge.get("reason", "")
	if reason:
		lines.append(reason)
	one = judge.get("one_liner", "")
	if one:
		lines.append(f"——{one}")

	if not has_sub:
		lines.append("")
		lines.append("（这条没读到字幕，判断只基于标题和简介）")

	lines.append("")
	lines.append(f"https://www.bilibili.com/video/{info.get('bvid', '')}")
	return "\n".join(lines)
