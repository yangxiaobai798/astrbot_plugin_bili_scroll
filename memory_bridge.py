"""把刷B站的结果和聊天内容写进「天使之魂」的记忆库。

设计原则：
- 不直接碰 p-memory 的 sqlite，一律走 angel_memory 暴露的 memory_runtime，
  这样衰减、去重、标签索引、作用域都由天使之魂自己管。
- 天使之魂没装 / 没初始化好时安静降级，绝不影响刷B站主流程。
"""

import json
import os

from astrbot.api import logger


ANGEL_PLUGIN = "astrbot_plugin_angel_memory"
# 记忆作用域：""或 "public" = 公共记忆，所有会话都能召回
DEFAULT_SCOPE = "public"

# 查询串里出现这些就没必要送进全文索引（p-memory 对中文按整段切词，
# "你今天刷到什么视频了" 这种问句整句搜必然 0 命中）
_STOPWORDS = (
	"的", "了", "吗", "呢", "吧", "啊", "是", "在", "有", "和", "跟", "就",
	"什么", "怎么", "为什么", "哪个", "哪些", "多少", "是不是", "有没有",
	"你", "我", "他", "她", "它", "我们", "你们", "他们",
	"今天", "昨天", "前天", "现在", "刚才", "最近", "上次",
	"记得", "知道", "说过", "讲过", "提到", "那个", "这个",
	"能", "会", "要", "想", "可以", "帮", "给", "把", "被", "还",
)


def _get_angel(context):
	"""拿到 angel_memory 插件实例。拿不到返回 None。"""
	try:
		meta = context.get_registered_star(ANGEL_PLUGIN)
	except Exception:
		return None
	if not meta:
		return None
	return getattr(meta, "star_cls", None)


def _get_runtime(context):
	angel = _get_angel(context)
	if angel is None:
		return None
	rt = getattr(angel, "memory_runtime", None)
	if rt is None:
		# 后台初始化可能还没完成，退回问一次 PluginContext
		try:
			pc = getattr(angel, "plugin_context", None)
			if pc is not None:
				rt = pc.get_component("memory_runtime")
		except Exception:
			rt = None
	return rt


async def resolve_scope(context, event=None, fallback: str = DEFAULT_SCOPE) -> str:
	"""取当前会话在天使之魂里的记忆作用域。

	必须和天使之魂注入/整理时用同一个 scope，否则写进去的记忆在对话里永远捞不出来
	（这正是"大鲨鱼不记得之前的事"的根因之一）。
	拿不到事件或解析失败时退回 fallback。
	"""
	if event is None:
		return fallback
	angel = _get_angel(context)
	if angel is None:
		return fallback
	pc = getattr(angel, "plugin_context", None)
	if pc is None:
		return fallback
	try:
		scope = await pc.resolve_memory_scope_from_event(event)
		return str(scope or fallback).strip() or fallback
	except Exception as e:
		logger.debug(f"[bili_scroll] 解析记忆 scope 失败，退回 {fallback}: {e}")
		return fallback


async def remember(context, judgment: str, reasoning: str = "", tags=None,
				   memory_type: str = "event", scope: str = DEFAULT_SCOPE) -> str:
	"""写一条记忆。返回记忆 ID；失败返回空串。"""
	judgment = (judgment or "").strip()
	if not judgment:
		return ""
	tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
	if not tags:
		tags = ["未分类"]

	rt = _get_runtime(context)
	if rt is None:
		logger.debug("[bili_scroll] 天使之魂未就绪，跳过记忆写入")
		return ""
	try:
		mid = await rt.remember(
			memory_type=memory_type,
			judgment=judgment,
			reasoning=(reasoning or "")[:1000],
			tags=tags,
			is_active=True,
			strength=60,
			memory_scope=scope,
		)
		logger.info(f"[bili_scroll] 已写入天使之魂记忆 id={mid} scope={scope}「{judgment[:40]}」")
		return str(mid or "")
	except Exception as e:
		logger.warning(f"[bili_scroll] 写天使之魂记忆失败: {type(e).__name__}: {e}")
		return ""


async def remember_video(context, info: dict, judge: dict, report_date: str = "",
						 scope: str = DEFAULT_SCOPE) -> str:
	"""把一条刷到的视频写成记忆。"""
	bvid = str(info.get("bvid") or "").strip()
	title = str(info.get("title") or "").strip()
	if not title:
		return ""
	up = str(info.get("author") or "").strip()
	level = str((judge or {}).get("level") or "").strip()
	reason = str((judge or {}).get("reason") or "").strip()

	judgment = f"在B站刷到视频《{title}》"
	if up:
		judgment += f"（UP主：{up}）"
	if level:
		judgment += f"，判断为「{level}」"
	if bvid:
		judgment += f"，BV号 {bvid}"

	bits = []
	if report_date:
		bits.append(f"刷到日期：{report_date}")
	if reason:
		bits.append(f"判断理由：{reason[:400]}")
	bits.append("来源：刷B站插件自动记录")
	reasoning = "\n".join(bits)

	tags = ["B站", "刷视频", "视频记忆"]
	if up:
		tags.append(up)
	if level:
		tags.append(level)
	if bvid:
		tags.append(bvid)
	if report_date:
		tags.append(report_date)

	# 标题里的关键片段也当检索锚点，方便"上次那个洋垃圾视频"这类模糊问法
	for frag in _title_fragments(title):
		if frag not in tags:
			tags.append(frag)
	return await remember(context, judgment, reasoning, tags,
						  memory_type="event", scope=scope)


def _title_fragments(title: str, limit: int = 6) -> list:
	"""从标题里挑几个 2~8 字的词当检索锚点。"""
	out = []
	for seg in title.replace("｜", "|").replace("，", ",").replace("：", ":").split():
		seg = seg.strip("【】[]()（）|,。！？!? 「」《》")
		if 2 <= len(seg) <= 8 and seg not in out:
			out.append(seg)
		if len(out) >= limit:
			break
	return out


def _is_question_only(text: str) -> bool:
	"""这句话是不是「只在问、没说自己的事」。

	判断标准：有疑问标记，而且没有第一人称。含「我」说明用户在讲自己的情况，
	哪怕带问号（"我昨天买了显卡，好不好用"）也值得记。
	纯「你…吗」这类问句记成「用户说过：你今天刷到什么视频了」是纯噪声。
	"""
	t = text.strip()
	if not t:
		return True
	marks = ("?", "？", "吗", "呢", "什么", "怎么", "为什么", "多少", "哪")
	if not any(m in t for m in marks):
		return False
	return "我" not in t


def _keywords(query: str, limit: int = 8) -> str:
	"""把一句问话压成检索关键词。

	p-memory 的全文索引对中文按整段切词，原句直接搜常常一个都命中不了
	（"你今天刷到什么B站视频了" → 0 条）。去掉虚词、只留实词片段后召回明显变好。
	"""
	text = str(query or "").strip()
	if not text:
		return ""
	for ch in "，。！？；、,.!?;:：\n	\"'“”‘’（）()【】[]":
		text = text.replace(ch, " ")
	segs = [s.strip() for s in text.split() if s.strip()]
	kept = []
	for seg in segs:
		# 只削句首句尾的虚词，不动中间内容
		changed = True
		while changed and seg:
			changed = False
			for w in _STOPWORDS:
				if seg.startswith(w) and len(seg) > len(w):
					seg = seg[len(w):]
					changed = True
				elif seg.endswith(w) and len(seg) > len(w):
					seg = seg[: -len(w)]
					changed = True
		seg = seg.strip()
		if len(seg) >= 2 and seg not in _STOPWORDS:
			kept.append(seg)
		if len(kept) >= limit:
			break
	if not kept:
		# 全被削掉了就退回原文，宁可多搜不可漏搜
		return text
	return " ".join(kept)


async def remember_chat(context, user_text: str, reply_text: str,
						session_id: str = "", scope: str = DEFAULT_SCOPE) -> str:
	"""把一轮对话写成记忆（只在内容够长、有信息量时写）。"""
	user_text = (user_text or "").strip()
	reply_text = (reply_text or "").strip()
	# 太短的寒暄不值得记，避免把记忆库塞满"嗯""哈哈"
	if len(user_text) < 8:
		return ""
	# 提问句本身不含事实，记成「用户说过：你今天刷到什么视频了」纯属噪声
	if _is_question_only(user_text):
		logger.debug(f"[bili_scroll] 跳过提问式对话的记忆写入：{user_text[:30]}")
		return ""
	judgment = f"用户说过：{user_text[:300]}"
	reasoning = f"大鲨鱼的回复：{reply_text[:400]}" if reply_text else ""
	if session_id:
		reasoning = (reasoning + f"\n会话：{session_id}").strip()
	reasoning = (reasoning + "\n来源：聊天自动记录").strip()

	tags = ["聊天", "对话记忆"]
	for frag in _title_fragments(user_text):
		if frag not in tags:
			tags.append(frag)
	if session_id:
		tags.append(session_id)
	return await remember(context, judgment, reasoning, tags,
						  memory_type="event", scope=scope)


async def search(context, query: str, limit: int = 8,
				 scope: str = DEFAULT_SCOPE) -> list:
	"""检索记忆，返回 [(judgment, reasoning), ...]。失败返回空列表。

	三轮检索：原句 → 去虚词关键词 → 单实词兜底。
	中文问句式查询靠这一步才能真正把旧记忆捞上来。
	"""
	rt = _get_runtime(context)
	if rt is None or not (query or "").strip():
		return []

	out = []
	seen = set()

	async def _run(q: str) -> None:
		try:
			# 综合检索不按 memory_type 过滤，能同时拿到 event/knowledge 各类记忆
			memories = await rt.comprehensive_recall(
				query=q, limit=limit, memory_scope=scope
			)
		except Exception as e:
			logger.debug(f"[bili_scroll] 天使之魂检索失败: {e}")
			return
		for m in memories or []:
			j = str(getattr(m, "judgment", "") or "").strip()
			r = str(getattr(m, "reasoning", "") or "").strip()
			if not j or j in seen:
				continue
			seen.add(j)
			out.append((j, r))

	raw = (query or "").strip()
	await _run(raw)

	kw = _keywords(raw)
	if kw and kw != raw and len(out) < limit:
		await _run(kw)

	# 仍然一条都没有：拆成单个实词再试
	if not out:
		for p in [p for p in kw.split() if len(p) >= 2][:4]:
			await _run(p)
			if len(out) >= limit:
				break

	logger.info(
		f"[bili_scroll] 记忆检索「{raw[:30]}」→ 关键词「{kw[:30]}」命中 {len(out)} 条"
	)
	return out[:limit]
