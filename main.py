"""刷B站 v2.0 —— 完整移植 dsh-bili-scroll。

原项目 github.com/c1overzz/dsh-bili-scroll 的核心是：
  · vendor/bilibili-mcp/mcp_server.py —— 34 个 B站工具
  · prompts/duty.md —— 判断章程
本插件把工具原样移植（bili_tools.py），并把章程改写成 AstrBot 里的自动流程。
"""

import asyncio
import datetime
import importlib.util
import json
import os
import random
import sys

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger

try:
	from astrbot.api.provider import ProviderRequest
except Exception:  # 兼容旧版本
	ProviderRequest = None


_HERE = os.path.dirname(os.path.abspath(__file__))


_MEM_BRIDGE = None


def _load_memory_bridge():
	"""加载记忆桥。失败不影响主流程。"""
	global _MEM_BRIDGE
	if _MEM_BRIDGE is not None:
		return _MEM_BRIDGE or None
	try:
		_MEM_BRIDGE = _load("memory_bridge")
	except Exception as e:
		logger.warning(f"[bili_scroll] 记忆桥加载失败: {type(e).__name__}: {e}")
		_MEM_BRIDGE = False
	return _MEM_BRIDGE or None


def _load(name: str, filename: str = None):
	"""加载同目录模块。"""
	path = os.path.join(_HERE, filename or f"{name}.py")
	spec = importlib.util.spec_from_file_location(f"biliscroll_{name}", path)
	mod = importlib.util.module_from_spec(spec)
	if _HERE not in sys.path:
		sys.path.insert(0, _HERE)
	spec.loader.exec_module(mod)
	return mod


@register(
	"astrbot_plugin_bili_scroll",
	"local",
	"发链接就能看懂B站视频；每天自动刷B站挑好货推给你",
	"2.0.0",
)
class BiliScrollPlugin(Star):
	def __init__(self, context: Context, config: dict | None = None):
		super().__init__(context)
		self.conf = config or {}
		self._tools_mod = None
		self._core = None
		self._links = None
		self._client_mod = None
		self._task = None
		self._running = False
		self._recent_bv = {}
		self._data_dir = None
		self._llm_tools = None
		self._dm = None
		self._self_uid = None
		self._mention_uid = None

	# -------------配置 ------------

	def _cfg(self):
		cfg = dict(self.conf or {})
		if not cfg:
			extra = getattr(self, "config", None)
			if isinstance(extra, dict):
				cfg = dict(extra)
		return cfg

	def _get(self, k, d=None):
		return self._cfg().get(k, d)

	def _data(self):
		if self._data_dir is None:
			try:
				self._data_dir = str(StarTools.get_data_dir("astrbot_plugin_bili_scroll"))
			except Exception:
				self._data_dir = os.path.join(_HERE, "data")
		os.makedirs(self._data_dir, exist_ok=True)
		return self._data_dir

	# -------------模块加载 ------------

	def _ensure_tools(self):
		if self._tools_mod is not None:
			return self._tools_mod
		data = self._data()
		os.environ["BILI_DUTY_HOME"] = os.path.join(data, "duty")
		os.environ.pop("BILI_MCP_TOOLS", None)
		mod = _load("bili_tools")
		from pathlib import Path
		# 凭证优先读 plugin_data，没有就回落到插件目录。
		# 原来无条件改成 plugin_data/bili_credential.json，而扫码登录
		# 写的一直是插件目录那份 —— 结果运行时读到一个不存在的文件，
		# _has_cred() 恒为 False，字幕/转写/私信全部走不了登录态。
		pd_cred = Path(data) / "bili_credential.json"
		if not pd_cred.exists():
			legacy = Path(__file__).parent / "bili_credential.json"
			if legacy.exists():
				logger.info(f"[bili_scroll] 凭证沿用插件目录那份：{legacy}")
				pd_cred = legacy
		mod.CRED_FILE = pd_cred
		self._tools_mod = mod
		logger.info(f"[bili_scroll] 已加载 {len(mod.list_tools())} 个B站工具（凭证={pd_cred.name}, 存在={pd_cred.exists()}）")
		return mod

	def _ensure_core(self):
		if self._core is None:
			self._core = _load("scroll_core")
		if self._links is None:
			self._links = _load("link_parser")
		if self._client_mod is None:
			self._client_mod = _load("bili_client")
		return self._core

	def _client(self):
		"""只用凭证文件（扫码登录写入）里的 cookie，配置里不再提供。"""
		self._ensure_core()
		sess = ""
		jct = ""
		try:
			mod = self._ensure_tools()
			cred = mod.load_credential()
			if cred is not None:
				sess = getattr(cred, "sessdata", "") or ""
				jct = getattr(cred, "bili_jct", "") or ""
		except Exception:
			pass
		return self._client_mod.BiliClient(sessdata=sess, bili_jct=jct)

	# -------------凭证 ------------

	def _sync_credential_from_config(self):
		"""已废弃：登录一律走扫码，配置里不再有 cookie 字段。

		保留此方法只是为了让老配置文件里残留的值能迁移一次，然后清掉。
		"""
		mod = self._ensure_tools()
		sess = (self._get("bili_sessdata") or "").strip()
		jct = (self._get("bili_bili_jct") or "").strip()
		if not sess:
			return
		try:
			cred_path = mod.CRED_FILE
			existing = {}
			if cred_path.exists():
				existing = json.loads(cred_path.read_text(encoding="utf-8"))
			if existing.get("sessdata") == sess:
				logger.info("[bili_scroll] 凭证已在扫码记录里，跳过迁移")
				return
			existing["sessdata"] = sess
			if jct:
				existing["bili_jct"] = jct
			cred_path.parent.mkdir(parents=True, exist_ok=True)
			tmp = cred_path.with_suffix(".tmp")
			tmp.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
			os.replace(tmp, cred_path)
			logger.info("[bili_scroll] 已把旧配置里的 cookie 迁移到凭证文件（今后请用扫码登录）")
		except Exception as e:
			logger.warning(f"[bili_scroll] 凭证迁移失败: {e}")

	def _has_cred(self) -> bool:
		mod = self._ensure_tools()
		try:
			return mod.load_credential() is not None
		except Exception:
			return False

	# -------------工具调用 ------------

	async def _call(self, name: str, **kw):
		mod = self._ensure_tools()
		try:
			r = await mod.call_tool(name, **kw)
		except Exception as e:
			return f"调用 {name} 失败：{type(e).__name__}: {str(e)[:150]}"
		if isinstance(r, str):
			return r
		parts = []
		for b in (r or []):
			if getattr(b, "type", "") == "text":
				parts.append(getattr(b, "text", ""))
			elif getattr(b, "type", "") == "image":
				parts.append(f"[图片 {getattr(b, 'mimeType', '')} {len(getattr(b, 'data', ''))}字节]")
		return "\n".join(parts)

	def _build_llm_tools(self):
		"""把移植的工具包装成 LLM FunctionTool。"""
		if self._llm_tools is not None:
			return self._llm_tools
		try:
			reg = _load("tool_registrar")
			mod = self._ensure_tools()
			self._llm_tools = reg.build_function_tools(mod, self._call)
		except Exception as e:
			logger.warning(f"[bili_scroll] 构建LLM工具失败: {type(e).__name__}: {e}")
			self._llm_tools = []
		return self._llm_tools

	async def _provider(self):
		pid = (self._get("llm_provider_id") or "").strip()
		try:
			if pid:
				p = self.context.get_provider_by_id(pid)
				if p:
					return p
			return self.context.get_using_provider()
		except Exception as e:
			logger.warning(f"[bili_scroll] provider获取失败: {e}")
			return None

	# -------------分析一条 ------------

	async def analyze_one(self, bvid: str, with_frames: bool = True) -> dict:
		core = self._ensure_core()
		bvid = (bvid or "").strip()
		if not bvid:
			return {"ok": False, "error": "没认出 BV号"}

		client = self._client()
		try:
			info = await client.video_info(bvid)
		except Exception as e:
			return {"ok": False, "error": f"取详情失败：{type(e).__name__}: {str(e)[:100]}"}

		sub_text = ""
		sub_reason = ""
		if self._has_cred():
			r = await self._call("bili_subtitle", bvid=bvid)
			try:
				d = json.loads(r)
				if d.get("text"):
					sub_text = d["text"]
				else:
					sub_reason = d.get("message") or d.get("error") or "无字幕"
			except Exception:
				sub_reason = "字幕解析失败"
		else:
			sub_reason = "未登录B站"

		frames_note = ""
		if with_frames and not sub_text and self._has_cred():
			r = await self._call("bili_frames", bvid=bvid, num=3)
			if "[图片" in r:
				frames_note = "（已抽取画面帧，但当前模型无法接收图片，未做视觉判断）"

		# 没有字幕、且开了本地转写 ⇒ 用 whisper 转一遍
		asr_note = ""
		if not sub_text and self._get("transcribe", False):
			logger.info(f"[bili_scroll] {bvid} 无字幕，走本地转写")
			try:
				r = await self._call("bili_transcribe", bvid=bvid)
				txt = r if isinstance(r, str) else str(r)
				# 转写结果如果像正常文本（不是错误提示），就用它当字幕
				bad_markers = ("转写环境没装好", "找不到 asr.py", "转写启动失败",
					"转写超时", "转写没产出文件", "（空）", "只有署名", "读转写结果失败")
				if txt and not any(m in txt for m in bad_markers) and len(txt.strip()) > 20:
					sub_text = txt
					sub_reason = ""
					asr_note = "\n（本地转写）"
					logger.info(f"[bili_scroll] {bvid} 转写成功，{len(txt)} 字")
				else:
					asr_note = f"\n（转写没拿到内容：{txt[:80]}）"
					logger.info(f"[bili_scroll] {bvid} 转写失败: {txt[:100]}")
			except Exception as e:
				asr_note = f"\n（转写异常：{type(e).__name__}）"
				logger.warning(f"[bili_scroll] 转写异常: {e}")

		judge = core.Judge(
			self._provider,
			prompt_cache=bool(self._get("prompt_cache", True)),
			cache_probe=bool(self._get("cache_probe", True)),
		)
		extra = frames_note + asr_note
		j = await judge.judge(
			info,
			sub_text + ("\n" + extra if extra else ""),
			persona=self._get("personaPrompt") or "",
		)

		text = core.render_single(info, j, bool(sub_text))
		if not sub_text and sub_reason:
			text = text.replace(
				"（这条没读到字幕，判断只基于标题和简介）",
				f"（没读到字幕：{sub_reason}）")
		# 顺手把这条视频写进天使之魂，以后被问到"你上次看那个视频"能想起来
		try:
			bridge = _load_memory_bridge()
			if bridge:
				await bridge.remember_video(
					self.context, info, j, datetime.date.today().isoformat())
		except Exception as e:
			logger.debug(f"[bili_scroll] 写入视频记忆失败（不影响主流程）: {e}")
		return {"ok": True, "info": info, "judge": j,
			"has_sub": bool(sub_text), "subtitle_reason": sub_reason, "text": text}

	# -------------事件 ------------

	def _today_digest(self, limit: int = 6) -> str:
		"""把今天日报里挑中的视频压成一小段，喂给聊天模型。

		没有这段，聊天模型完全不知道插件刷到了什么 ——
		它只有自己的对话上下文，用户问"你今天看了什么"它只能瞎答。
		"""
		try:
			today = datetime.date.today().isoformat()
			path = os.path.join(self._data(), "out", f"{today}.md")
			if not os.path.isfile(path):
				return ""
			with open(path, encoding="utf-8") as f:
				raw = f.read()
			# 同一天可能被追加了多份日报，全部合并后按 BV 号去重
			items = []
			seen = set()
			for block in raw.split("### ")[1:]:
				lines = block.splitlines()
				if not lines or not lines[0].strip().startswith("BV"):
					continue
				bvid = lines[0].strip()
				if bvid in seen:
					continue
				title = up = level = ""
				for ln in lines[1:]:
					ln = ln.strip()
					if ln.startswith("- **标题**："):
						title = ln.split("：", 1)[1]
					elif ln.startswith("- **UP**："):
						up = ln.split("：", 1)[1]
					elif ln.startswith("- **档位**："):
						level = ln.split("：", 1)[1]
				if title:
					seen.add(bvid)
					items.append(f"{title}（{up}，{level}，{bvid}）")
			# 日报文件是「先写的在前」，取前 N 条等于永远只喂早上的旧结果，
			# 当天后来挑中的全被截掉——模型就会答「还是那几个」。
			# 这里取最新的一批，并倒序，让最近刷到的排在最前。
			items = items[-limit:][::-1]
			if not items:
				return ""
			return "【今天刷B站的成果】\n" + "\n".join("- " + x for x in items)
		except Exception as e:
			logger.debug(f"[bili_scroll] 读取日报失败: {e}")
			return ""

	@filter.on_llm_request(priority=45)
	async def inject_digest(self, event: AstrMessageEvent, req):
		"""让聊天模型知道今天刷到了什么，否则它回答"看了什么"只能瞎编。"""
		if ProviderRequest is not None and not isinstance(req, ProviderRequest):
			return
		digest = self._today_digest()
		if not digest:
			return
		base = req.system_prompt or ""
		marker = "【今天刷B站的成果】"
		if marker in base:
			return
		req.system_prompt = (
			(base.rstrip() + "\n\n" if base else "")
			+ digest
			+ "\n\n【关于上面的刷B站成果，必须遵守】\n"
			"1. 这是我（大鲨鱼）今天实际刷到并挑过的视频，是最新的，"
			"优先于你记忆里的旧信息和你自己的猜测。\n"
			"2. 用户问「你刷到什么了」「今天看了什么视频」，就报这里的标题，"
			"不要报热门榜、不要报你想当然的东西。\n"
			"3. 一次说 2-4 条就够，别把整张单子念完。\n"
			"4. 这个列表是空的，才说今天还没刷到；绝不许编造标题和 BV 号。\n"
		)
		logger.info(f"[bili_scroll] 已把今日日报注入聊天上下文（{len(digest)} 字）")

	def _session_id(self, event) -> str:
		try:
			return str(getattr(event, "unified_msg_origin", "") or "")
		except Exception:
			return ""

	@filter.on_llm_request(priority=44)
	async def inject_memories(self, event: AstrMessageEvent, req):
		"""从天使之魂召回相关记忆，塞进本轮 system_prompt。

		和 inject_digest 分开是因为职责不同：日报只管今天，
		记忆管"以前聊过什么、刷到过什么"。
		"""
		if ProviderRequest is not None and not isinstance(req, ProviderRequest):
			return
		# 记下这个私聊对象，push_target 没配时用它兜底
		self._remember_peer(event)
		bridge = _load_memory_bridge()
		if not bridge:
			return
		query = str(getattr(req, "prompt", "") or "").strip()
		if not query:
			return
		# scope 必须跟着会话走，否则写进 personal 的记忆在这里永远查不到
		try:
			scope = await bridge.resolve_scope(self.context, event)
		except Exception:
			scope = "public"
		try:
			hits = await bridge.search(self.context, query, limit=6, scope=scope)
		except Exception as e:
			logger.debug(f"[bili_scroll] 记忆召回失败: {e}")
			return
		if not hits:
			return
		base = req.system_prompt or ""
		if "【你记得的事】" in base:
			return
		lines = []
		for judgment, reasoning in hits:
			line = "- " + judgment
			if reasoning:
				first = reasoning.splitlines()[0].strip()
				if first:
					line += "（" + first[:80] + "）"
			lines.append(line)
		header = base.rstrip() + "\n\n【你记得的事】\n" if base else "【你记得的事】\n"
		tail = (
			"\n\n上面是你以前记下的事，聊天时自然地用上；"
			"用户提到以前的事就照这些回答，别装不记得，也别编造没记过的内容。\n"
		)
		req.system_prompt = header + "\n".join(lines) + tail
		logger.info(f"[bili_scroll] 已注入 {len(hits)} 条天使之魂记忆 scope={scope}")

	@filter.on_llm_response()
	async def save_chat_memory(self, event: AstrMessageEvent, response):
		"""把这一轮对话写进天使之魂。"""
		bridge = _load_memory_bridge()
		if not bridge:
			return
		user_text = str(getattr(event, "message_str", "") or "").strip()
		reply = str(getattr(response, "completion_text", "") or "").strip()
		if not user_text:
			return
		try:
			scope = await bridge.resolve_scope(self.context, event)
			await bridge.remember_chat(
				self.context, user_text, reply, self._session_id(event), scope=scope
			)
		except Exception as e:
			logger.debug(f"[bili_scroll] 写聊天记忆失败: {e}")

	@filter.event_message_type(filter.EventMessageType.ALL)
	async def on_message(self, event: AstrMessageEvent):
		if not self._get("auto_on_link", True):
			return
		text = event.message_str or ""
		self._ensure_core()
		# 必须是机器人自己发的消息就跳过 —— 否则回复里带的 "看一下 BVxxx…"
		# 会被当成新链接再次触发分析，形成无限循环。
		try:
			sender = str(event.get_sender_id() or "")
			me = str(event.get_self_id() or "")
			if me and sender and sender == me:
				logger.debug(f"[bili_scroll] 跳过自己发的消息: {text[:40]}")
				return
		except Exception:
			pass
		if not self._links.has_bili_link(text):
			return

		try:
			from astrbot.core.platform.message_type import MessageType
			is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
		except Exception:
			is_group = False

		if is_group and self._get("group_need_at", True):
			self_id = ""
			try:
				self_id = str(event.get_self_id() or "")
			except Exception:
				pass
			hit = False
			try:
				for comp in (event.get_messages() or []):
					if comp.__class__.__name__ == "At" and self_id and str(getattr(comp, "qq", "")) == self_id:
						hit = True
						break
			except Exception:
				pass
			if not hit and not (self_id and self_id in text):
				return

		bvid = self._links.extract_bvid(text)
		if not bvid:
			short = self._links.extract_short_url(text)
			if short:
				bvid = await self._links.resolve_short_url(short)
			if not bvid:
				av = self._links.extract_av(text)
				if av:
					try:
						c = self._client()
						from bilibili_api import video as _v
						d = await _v.Video(aid=av, credential=c.credential).get_info()
						bvid = d.get("bvid", "")
					except Exception:
						bvid = ""
		if not bvid:
			return

		logger.info(f"[bili_scroll] 分析 {bvid}")
		# 防重复：同一条视频一分钟内只分析一次，避免任何意外的回声循环
		now_ts = asyncio.get_event_loop().time()
		last = self._recent_bv.get(bvid, 0)
		if now_ts - last < 60:
			logger.debug(f"[bili_scroll] {bvid} 刚刚分析过，跳过")
			return
		self._recent_bv[bvid] = now_ts
		if len(self._recent_bv) > 200:
			self._recent_bv.clear()
		yield event.plain_result(f"看一下这条…")
		res = await self.analyze_one(bvid)
		if not res.get("ok"):
			yield event.plain_result(f"没看成：{res.get('error')}")
			return
		yield event.plain_result(res["text"])

	# -------------命令 ------------

	@filter.command("看看")
	async def cmd_look(self, event: AstrMessageEvent, arg: str = ""):
		if not arg:
			yield event.plain_result("用法：/看看 BV号或链接")
			return
		self._ensure_core()
		bvid = self._links.extract_bvid(arg) or arg.strip()
		yield event.plain_result("看一下这条…")
		res = await self.analyze_one(bvid)
		yield event.plain_result(res["text"] if res.get("ok") else f"没看成：{res.get('error')}")

	@filter.command("bili登录")
	async def cmd_login(self, event: AstrMessageEvent):
		"""扫码登录：二维码直接发图片，然后自动等扫码结果。"""
		from astrbot.core.message.message_event_result import MessageChain

		r = await self._call("bili_login")
		try:
			d = json.loads(r)
		except json.JSONDecodeError:
			yield event.plain_result(r[:1200])
			return

		st = d.get("status")
		if st == "already_logged_in":
			yield event.plain_result("已经登录过了（B站），可以直接用。想换号先删凭证文件再扫。")
			return
		if st != "qrcode_ready":
			yield event.plain_result(d.get("message") or r[:400])
			return

		# 优先把二维码当图片发出去
		img_b64 = d.get("qrcode_image") or ""
		sent_img = False
		if img_b64:
			try:
				data_url = img_b64 if img_b64.startswith("data:") else f"data:image/png;base64,{img_b64}"
				chain = MessageChain().url_image(data_url)
				yield event.chain_result([chain])
				sent_img = True
			except Exception as e:
				logger.warning(f"[bili_scroll] 发二维码图片失败: {e}")

		if not sent_img:
			# 退回文本二维码
			term = d.get("qrcode_terminal") or ""
			if term:
				yield event.plain_result("用B站App扫下面这个码：\n" + term)
			else:
				yield event.plain_result(
					"二维码生成好了，但没法当图片发。请打开这个链接扫码：\n"
					+ (d.get("qrcode_url") or "（无）"))

		yield event.plain_result("扫完会在手机上点确认。我这边等着，好了告诉你。")

		# 自动轮询扫码结果（最多 100 秒）
		for _ in range(20):
			await asyncio.sleep(5)
			chk = await self._call("bili_login_check")
			try:
				cd = json.loads(chk)
			except json.JSONDecodeError:
				continue
			s = cd.get("status")
			if s == "done" or s == "already_logged_in":
				yield event.plain_result("登录成功，现在能读字幕、弹幕、评论了。")
				return
			if s == "timeout":
				yield event.plain_result("二维码过期了，重新发 /bili登录 再来一次。")
				return
			# scanning / confirming 继续等
		yield event.plain_result("等了 100 秒还没确认。确认完了发 /bili登录状态 看看。")

	@filter.command("bili登录状态")
	async def cmd_login_status(self, event: AstrMessageEvent):
		r = await self._call("bili_login_check")
		try:
			d = json.loads(r)
			s = d.get("status")
			m = d.get("message") or ""
			if s in ("done", "already_logged_in"):
				yield event.plain_result("已登录：" + (m or "可以正常用"))
			elif s in ("scanning", "confirming"):
				yield event.plain_result("扫码中：" + (m or "等你在手机上确认"))
			elif s == "timeout":
				yield event.plain_result("二维码过期了，重新发 /bili登录")
			else:
				yield event.plain_result(m or r[:400])
		except json.JSONDecodeError:
			yield event.plain_result(r[:600])

	@filter.command("bili工具")
	async def cmd_tools(self, event: AstrMessageEvent):
		mod = self._ensure_tools()
		names = sorted(mod.list_tools())
		yield event.plain_result(f"共 {len(names)} 个工具：\n" + "\n".join(names))

	@filter.command("刷b站")
	async def cmd_scroll(self, event: AstrMessageEvent):
		res = await self.run_round(force=True)
		if res.get("error"):
			yield event.plain_result(res["error"])
			return
		yield event.plain_result(res.get("push_text", "跑完了"))

	@filter.command("bili状态")
	async def cmd_status(self, event: AstrMessageEvent):
		per_day = int(self._get("perDay", 5) or 0)
		lines = [
			f"登录：{'已登录' if self._has_cred() else '未登录（用 /bili登录）'}",
			f"关键词：{self._get('keywords') or '空'}",
			f"每天：{'不自动刷（perDay=0）' if per_day <= 0 else str(per_day) + ' 条'}",
			f"自动刷：{'开' if per_day > 0 else '关'} {self._get('cron_hour', 9)}点",
			f"推送：{self._get('push_target') or '未设置'}",
		]
		yield event.plain_result("\n".join(lines))

	@filter.command("bili私信")
	async def cmd_dm_check(self, event: AstrMessageEvent):
		"""立刻查一轮B站私信，并把未读内容念出来（不自动回）。"""
		mod = self._ensure_tools()
		try:
			from bilibili_api import session
			cred = mod.get_cred()
			if cred is None:
				yield event.plain_result("还没登录B站，先 /bili登录")
				return
			res = await session.get_sessions(cred, session_type=4)
			data = res.get("data", res)
			sessions = data.get("session_list") or []
			lines = []
			for s in sessions:
				talker = s.get("talker_id")
				if not talker or int(talker) > 840000000000000:
					continue
				unread = s.get("unread_count") or 0
				last = (s.get("last_msg") or {}).get("content")
				lines.append(f"UID {talker}｜未读 {unread}｜最近：{str(last)[:60]}")
			if not lines:
				yield event.plain_result("没有私聊会话")
				return
			whitelist = self._dm._allowed_uids() if self._dm else set()
			lines.insert(0, f"监听：{'开' if self._dm else '关'}")
			lines.insert(1, f"白名单：{','.join(whitelist) if whitelist else '空（不会回任何人）'}")
			yield event.plain_result("\n".join(lines))
		except Exception as e:
			yield event.plain_result(f"查私信失败：{type(e).__name__}: {e}")

	# -------------自动刷 ------------

	def _push_targets(self) -> list:
		"""算出推送目标列表（unified_msg_origin 形式）。

		优先用配置里的 push_target；没配就退回"最近跟我说过话的人"，
		这样即使忘了配置，挑到好视频也能主动发出去 ——
		之前 push_target 为空就把整段推送逻辑跳过了，等于永远收不到通知。
		"""
		raw = str(self._get("push_target") or "").strip()
		out = []
		if raw:
			for piece in raw.replace("\n", ",").split(","):
				t = piece.strip()
				if not t:
					continue
				# 裸 QQ 号 / 裸群号补成完整会话 ID（send_message 只认 unified_msg_origin）
				if ":" not in t:
					t = f"default:FriendMessage:{t}"
				out.append(t)
		if out:
			return out
		# 没有配置：发给最近活跃的私聊对象
		peer = self._last_peer()
		if peer:
			logger.info(f"[bili_scroll] push_target 未设置，回落到最近的对话对象 {peer}")
			return [peer]
		return []

	def _last_peer(self) -> str:
		"""找最近一个跟我说过话的私聊对象。

		两条来源，先看插件自己记的（最准），再看会话历史库。
		只找私聊：群里主动刷日报会招人烦。
		"""
		# 1) 插件运行期记下的最近私聊会话
		peer = str(getattr(self, "_last_private_umo", "") or "").strip()
		if peer:
			return peer
		# 2) 落下盘的记录（重启后仍然有效）
		try:
			f = os.path.join(self._data(), "last_peer.txt")
			if os.path.isfile(f):
				with open(f, encoding="utf-8") as fp:
					saved = fp.read().strip()
				if saved and "FriendMessage" in saved:
					return saved
		except Exception:
			pass
		# 3) 退到会话历史库
		try:
			from astrbot.core.utils.astrbot_path import get_astrbot_data_path
			import sqlite3
			db = os.path.join(str(get_astrbot_data_path()), "data_v4.db")
			if not os.path.isfile(db):
				return ""
			conn = sqlite3.connect(db)
			try:
				row = conn.execute(
					"SELECT user_id FROM conversations "
					"WHERE user_id LIKE '%FriendMessage%' "
					"ORDER BY updated_at DESC LIMIT 1"
				).fetchone()
			finally:
				conn.close()
			return str(row[0]) if row and row[0] else ""
		except Exception as e:
			logger.debug(f"[bili_scroll] 查最近对话对象失败: {e}")
			return ""

	def _remember_peer(self, event) -> None:
		"""记下这个私聊会话，供推送兜底用。"""
		try:
			umo = str(getattr(event, "unified_msg_origin", "") or "").strip()
			if not umo or "FriendMessage" not in umo:
				return
			self._last_private_umo = umo
			f = os.path.join(self._data(), "last_peer.txt")
			with open(f, "w", encoding="utf-8") as fp:
				fp.write(umo)
		except Exception as e:
			logger.debug(f"[bili_scroll] 记录最近私聊对象失败: {e}")

	async def _push_out(self, text: str, level: str = "") -> bool:
		"""把挑好的视频推给人。返回是否至少发出去一条。

		分级：
		- 一般情况下只走私聊，别打扰群。
		- 只有判级为「值得看」（不是「可以看」）并且在群里 @ 过你，
		  才额外往群里发一条，让人知道这次真挖到东西了。
		"""
		if not text:
			return False
		targets = self._push_targets()
		if not targets:
			logger.warning("[bili_scroll] 挑到了视频但没有推送目标，跳过通知")
			return False
		from astrbot.core.message.message_event_result import MessageChain
		ok_any = False
		for target in targets:
			try:
				ok = await self.context.send_message(target, MessageChain().message(text))
				if ok:
					ok_any = True
					logger.info(f"[bili_scroll] 已推送 {len(text)} 字到 {target}")
				else:
					logger.warning(f"[bili_scroll] 推送到 {target} 返回失败")
			except Exception as e:
				logger.warning(f"[bili_scroll] 推送到 {target} 失败: {type(e).__name__}: {e}")
		# 只有「值得看」才额外往群里喊一声
		if ok_any and str(level or "").strip() == "值得看":
			await self._push_group_shout(text)
		return ok_any

	async def _push_group_shout(self, text: str) -> None:
		"""特别好的视频才往群里 @ 一下。出错不影响私聊那条。"""
		raw = str(self._get("push_group") or "").strip()
		if not raw:
			return
		target = raw if ":" in raw else f"default:GroupMessage:{raw}"
		mention = str(self._get("mentionName") or "").strip()
		try:
			from astrbot.core.message.message_event_result import MessageChain
			chain = MessageChain()
			if mention:
				# 优先 @ 配置里写的人
				try:
					chain = chain.at(mention) if hasattr(chain, "at") else chain
				except Exception:
					pass
			body = (f"@{mention} " if mention else "") + text
			await self.context.send_message(target, MessageChain().message(body))
			logger.info(f"[bili_scroll] 值得看，已额外喊一声到 {target}")
		except Exception as e:
			logger.debug(f"[bili_scroll] 群内喊话失败（不影响私聊）: {e}")

	def _top_level(self, picks: list) -> str:
		"""这一轮里最高的档位。只有出现「值得看」才值得去群里喊一声。"""
		levels = [str((p.get("judge") or {}).get("level") or "") for p in picks or []]
		if "值得看" in levels:
			return "值得看"
		if "可以看" in levels:
			return "可以看"
		return ""

	async def _notify_one(self, info: dict, judge: dict, pick_index: int) -> None:
		"""逐条通知主人。按 dmMode 决定走评论@还是B站私信。

		pick_index 是这一轮里第几条（从 0 开始），用来执行 commentPerRound 上限。
		"""
		mode = str(self._get("dmMode", "off") or "off").strip()
		full = self._render_notice(info, judge)

		# 公开评论@主人
		if mode in ("off", "both"):
			try:
				cap = int(self._get("commentPerRound", 1) or 0)
			except (TypeError, ValueError):
				cap = 1
			if cap > 0 and pick_index >= cap:
				logger.info(f"[bili_scroll] 本轮评论数已达上限 {cap}，不再评论")
			else:
				await self._comment_at_owner(info, judge)

		# B站私信给主人
		if mode in ("only", "both"):
			await self._dm_to_owner(full)

	def _render_notice(self, info: dict, judge: dict) -> str:
		"""拼一条给主人看的短消息（私信用，控制在500字以内）。"""
		length = str(self._get("commentLength", "medium") or "medium").strip()
		title = str(info.get("title") or "")[:60]
		bv = str(info.get("bvid") or "")
		reason = str((judge or {}).get("reason") or "")
		one = str((judge or {}).get("one_liner") or "")
		level = str((judge or {}).get("level") or "")
		body = one or reason
		if length == "short":
			body = body[:40]
		elif length == "long":
			body = body[:160]
		else:
			body = body[:90]
		txt = f"《{title}》\n{level}｜{body}\nhttps://www.bilibili.com/video/{bv}"
		return txt[:480]

	async def _dm_to_owner(self, text: str) -> bool:
		"""把通知私信发给主人（B站私信，500字上限）。"""
		uid = str(self._get("owner_bili_uid", "") or "").strip()
		if not uid:
			uid = str(getattr(self, "_mention_uid", "") or "")
		if not uid:
			logger.debug("[bili_scroll] 没有 owner_bili_uid，跳过B站私信")
			return False
		try:
			r = await self._call("bili_send_message", receiver_uid=int(uid), text=text)
			d = json.loads(r) if isinstance(r, str) and r.startswith("{") else {}
			if d.get("success") is False or d.get("error"):
				logger.warning(f"[bili_scroll] B站私信发送失败: {str(r)[:140]}")
				return False
			logger.info(f"[bili_scroll] 已私信给 {uid}")
			return True
		except Exception as e:
			logger.warning(f"[bili_scroll] B站私信异常: {type(e).__name__}: {e}")
			return False

	async def _comment_at_owner(self, info: dict, judge: dict) -> bool:
		"""在B站视频评论区发一条评论并 @ 主人，让B站 App 推送到他手机。

		按 owner_comment_rate 的概率决定发不发（默认 0.5 = 50%）。
		只在登录了B站账号时才能发；失败静默，不影响主流程。
		"""
		if not self._get("autoComment", True):
			return False
		if not self._has_cred():
			logger.debug("[bili_scroll] 未登录B站，跳过评论区@")
			return False
		bvid = str(info.get("bvid") or "").strip()
		title = str(info.get("title") or "").strip()
		if not bvid or not title:
			return False

		# 概率闸门
		try:
			rate = float(self._get("owner_comment_rate", 0.5) or 0)
		except (TypeError, ValueError):
			rate = 0.5
		rate = max(0.0, min(1.0, rate))
		if rate <= 0:
			return False
		if random.random() > rate:
			logger.info(f"[bili_scroll] 这条没抽中@{self._get('mentionName')}（概率 {rate:.0%}）")
			return False

		who = str(self._get("mentionName") or "").strip()
		if not who:
			return False

		level = str((judge or {}).get("level") or "").strip()
		reason = str((judge or {}).get("reason") or "").strip()
		one = str((judge or {}).get("one_liner") or "").strip()

		# 评论正文：@主人 + 一句自己的话。必须带上昵称，B站才会真的提醒他。
		body_bits = [f"@{who}"]
		if one:
			body_bits.append(one)
		elif reason:
			body_bits.append(reason[:80])
		elif level:
			body_bits.append(f"这条{level}")
		text = " ".join(body_bits).strip()
		if len(text) > 200:
			text = text[:200]

		try:
			r = await self._call("bili_reply", bvid=bvid, text=text)
			try:
				d = json.loads(r)
			except (TypeError, json.JSONDecodeError):
				d = {}
			if d.get("success"):
				logger.info(f"[bili_scroll] 已在《{title[:20]}》评论区@{who}：{text[:40]}")
				return True
			logger.warning(f"[bili_scroll] 评论区@失败：{str(r)[:160]}")
			return False
		except Exception as e:
			logger.warning(f"[bili_scroll] 评论区@异常: {type(e).__name__}: {e}")
			return False

	def _append_pick(self, report_path: str, today: str, info: dict, judge: dict):
		"""把刚挑中的一条追加进今天的日报文件。

		_today_digest() 每一轮对话都重读这个文件，所以只要这里写进去，
		下一次提问就能看见——不必等整轮跑完。
		"""
		block = [
			f"### {info.get('bvid', '')}",
			"",
			f"- **标题**：{info.get('title', '')}",
			f"- **UP**：{info.get('author', '')}",
			f"- **链接**：https://www.bilibili.com/video/{info.get('bvid', '')}",
			f"- **播放**：{info.get('view', 0)}",
			f"- **档位**：{judge.get('level', '可以看')}",
		]
		# reportLevel: brief 只留标题和档位 / normal 加一句理由 / full 全都要
		level_opt = str(self._get("reportLevel", "full") or "full").strip()
		if level_opt != "brief":
			reason = str(judge.get("reason") or "")
			if level_opt == "normal":
				reason = reason[:80]
			block.append(f"- **为什么值得**：{reason}")
		if level_opt == "full" and judge.get("one_liner"):
			block.append(f"- **一句话**：{judge['one_liner']}")
		block.append("")
		new_file = not os.path.isfile(report_path)
		with open(report_path, "a", encoding="utf-8") as f:
			if new_file:
				f.write(f"# B站日报 {today}\n\n")
			f.write("\n".join(block) + "\n")

	async def run_round(self, force: bool = False) -> dict:
		if self._running and not force:
			return {"error": "上一轮还在跑"}
		self._running = True
		core = self._ensure_core()
		try:
			client = self._client()
			judge = core.Judge(
				self._provider,
				prompt_cache=bool(self._get("prompt_cache", True)),
				cache_probe=bool(self._get("cache_probe", True)),
			)
			limit = int(self._get("perDay", 5) or 0)
			today = datetime.date.today().isoformat()

			out_dir = os.path.join(self._data(), "out")
			os.makedirs(out_dir, exist_ok=True)
			report_path = os.path.join(out_dir, f"{today}.md")
			already = 0
			if os.path.isfile(report_path):
				with open(report_path, encoding="utf-8") as f:
					already = sum(1 for L in f if L.startswith("### BV"))
			if limit <= 0:
				return {"error": "perDay=0，已关闭自动刷"}
			budget = max(0, limit - already)
			if budget == 0:
				return {"error": f"今天已经挑够 {limit} 条了"}

			kws = [x.strip() for x in (self._get("keywords") or "").replace("，", ",").split(",") if x.strip()]
			# 搜索排序：pubdate=最新发布 / click=最多播放 / totalrank=综合 / dm=弹幕多
			sort = str(self._get("searchSort", "pubdate") or "pubdate").strip()
			if sort not in ("pubdate", "click", "totalrank", "dm"):
				sort = "pubdate"
			# 排除词：标题里带这些词的一律当候选丢掉（省掉后面的字幕+模型开销）
			excl = [x.strip() for x in str(self._get("excludeWords", "") or "").replace("，", ",").split(",") if x.strip()]
			candidates = []
			# 关键词全都要用上（原来只取前3个，配多了会被静默丢掉）
			for kw in kws:
				try:
					candidates.extend(await client.search_videos(kw, num=8, order=sort))
					logger.info(f"[bili_scroll] 搜索 {kw} 完成（{sort}），候选累计 {len(candidates)}")
				except Exception as e:
					logger.warning(f"[bili_scroll] 搜索 {kw} 失败: {e}")
			if excl:
				before = len(candidates)
				candidates = [c for c in candidates
					if not any(w.lower() in str(c.get("title", "")).lower() for w in excl)]
				drop = before - len(candidates)
				if drop:
					logger.info(f"[bili_scroll] 排除词过滤掉 {drop} 条（{','.join(excl)}）")
			if not candidates:
				return {"error": "没搜到候选视频"}

			ledger = core.Ledger(self._data())
			random.shuffle(candidates)
			picks, skipped, inspected = [], [], 0
			for c in candidates:
				if len(picks) >= budget:
					break
				bv = c.get("bvid")
				if not bv or ledger.has(bv):
					continue
				inspected += 1
				res = await self.analyze_one(bv, with_frames=False)
				if not res.get("ok"):
					ledger.add(bv, c.get("title", ""), "跳过", "分析失败")
					continue
				j = res["judge"]
				# 不管判级如何，这条视频都先写进天使之魂。
				# 「不值当」的也是信息：以后你说「别再给我推这种」它能想起来为什么。
				try:
					bridge0 = _load_memory_bridge()
					if bridge0:
						await bridge0.remember_video(
							self.context, res["info"], j, today)
				except Exception as e:
					logger.debug(f"[bili_scroll] 写入视频记忆失败: {e}")
				if j.get("level") == "不值当":
					ledger.add(bv, res["info"].get("title", ""), "跳过", j.get("reason", "")[:80])
					skipped.append({"bvid": bv, "reason": j.get("reason", "")})
					continue
				picks.append({"info": res["info"], "judge": j, "has_sub": res["has_sub"]})
				ledger.add(bv, res["info"].get("title", ""), "挑了", j.get("level", ""))
				# 立刻把这挑中的一条写进今天的日报。
				# 原来只在整轮跑完后写一次，perDay=50 时一轮要跑一个多小时，
				# 中途问「你今天刷到什么」读到的还是上一轮（甚至上半天）的旧文件，
				# 看起来就像根本没在刷。增量写让 _today_digest 随时都能读到新结果。
				try:
					self._append_pick(report_path, today, res["info"], j)
				except Exception as e:
					logger.warning(f"[bili_scroll] 增量写日报失败: {e}")
				# 按概率在评论区@主人，让B站 App 主动提醒他来看。
				# dmMode 决定这一轮走哪条路：
				#   only —— 只发B站私信（不公开评论）
				#   off  —— 只发公开评论@（默认，最省事）
				#   both —— 两条都发
				# commentPerRound 限制一轮最多在几个视频下留评论，免得刷屏。
				try:
					await self._notify_one(res["info"], j, len(picks))
				except Exception as e:
					logger.warning(f"[bili_scroll] 通知流程失败: {e}")

			# 每条已经在上面增量写过了，这里只补一行汇总，
			# 否则整份日报会被再写一遍，BV 块翻倍。
			if picks or skipped:
				with open(report_path, "a", encoding="utf-8") as f:
					f.write(
						f"\n---\n\n本轮看了 {inspected} 条，挑了 {len(picks)} 条，"
						f"跳过 {len(skipped)} 条。\n"
					)

			push = core.render_push(picks)
			# 一轮结束后按 notify_mode 决定通知方式。
			# 三档：
			#   bili_at_only —— 只在B站评论区@主人（默认；不往QQ发消息）
			#   both         —— 评论区@ + QQ 推一条汇总
			#   qq_only      —— 只发 QQ（老行为）
			mode = str(self._get("notify_mode", "bili_at_only") or "").strip()
			if picks and mode in ("both", "qq_only"):
				try:
					await self._push_out(push, level=self._top_level(picks))
				except Exception as e:
					logger.warning(f"[bili_scroll] 推送失败: {e}")
			elif picks and mode == "bili_at_only":
				logger.info(
					f"[bili_scroll] 本轮挑了 {len(picks)} 条，按 notify_mode=bili_at_only "
					f"只在B站评论区@，不发QQ")

			# 视频记忆已在上面每条分析完就写进去了，这里不再重复写。
			return {"picks": picks, "push_text": push, "report_path": report_path,
				"stats": {"inspected": inspected, "picked": len(picks), "skipped": len(skipped)}}
		finally:
			self._running = False

	async def initialize(self):
		self._ensure_tools()
		self._sync_credential_from_config()
		# 把34个B站工具注册给 LLM，让它能自己调
		tools = self._build_llm_tools()
		if tools:
			try:
				self.context.add_llm_tools(*tools)
				logger.info(f"[bili_scroll] 已注册 {len(tools)} 个LLM工具给模型")
			except Exception as e:
				logger.warning(f"[bili_scroll] 注册LLM工具失败: {type(e).__name__}: {e}")
		# 注册 Web API（给管理页面用）
		self._register_web_api()
		if int(self._get("perDay", 5) or 0) > 0:
			hour = int(self._get("cron_hour", 9) or 9)
			self._task = asyncio.create_task(self._daily(hour))
			logger.info(f"[bili_scroll] 每日 {hour}:00 自动刷已注册")
		# 私信双向：dmWatch 一直是个空壳开关，这里补上真正的监听
		if self._get("dmWatch", True):
			try:
				self._dm = _load("dm_watcher").DmWatcher(self)
				await self._init_bili_ids()
				self._dm.start()
			except Exception as e:
				logger.warning(f"[bili_scroll] 私信监听启动失败: {type(e).__name__}: {e}")

	async def _init_bili_ids(self):
		"""记下自己 UID 和 mentionName 对应的 UID，私信白名单要用。"""
		try:
			mod = self._ensure_tools()
			cred = mod.load_credential()
			if cred is None:
				logger.warning("[bili_scroll] 私信监听拿不到B站凭证，白名单将为空")
				return
			from bilibili_api import user as bili_user
			me = await bili_user.get_self_info(cred)
			mid = me.get("mid")
			if mid:
				self._self_uid = int(mid)
			if self._dm:
				uid = await self._dm._resolve_mention_uid()
				if uid:
					self._mention_uid = int(uid)
				logger.info(
					f"[bili_scroll] 私信白名单: 自己={self._self_uid} 对方={self._mention_uid}")
		except Exception as e:
			logger.warning(f"[bili_scroll] 取B站UID失败: {type(e).__name__}: {e}")

	def _register_web_api(self):
		"""注册页面接口。路径前缀必须是插件名。"""
		try:
			api_mod = _load("web_api")
			api = api_mod.BiliScrollApi(self)
			prefix = "/astrbot_plugin_bili_scroll"
			routes = [
				("/status", api.get_status, ["GET"], "刷B站-状态"),
				("/history", api.get_history, ["GET"], "刷B站-浏览记录"),
				("/dialogs", api.get_dialogs, ["GET"], "刷B站-对话记录"),
				("/reports", api.get_reports, ["GET"], "刷B站-日报列表"),
				("/report", api.get_report, ["GET"], "刷B站-日报内容"),
				("/run", api.post_run, ["POST"], "刷B站-立刻跑"),
				("/config", api.post_config, ["POST"], "刷B站-保存配置"),
				("/login/start", api.post_login_start, ["POST"], "刷B站-生成二维码"),
				("/login/poll", api.post_login_poll, ["POST"], "刷B站-查询扫码"),
				("/logout", api.post_logout, ["POST"], "刷B站-退出登录"),
			]
			for route, handler, methods, desc in routes:
				self.context.register_web_api(prefix + route, handler, methods, desc)
			logger.info(f"[bili_scroll] 已注册 {len(routes)} 个页面接口")
		except Exception as e:
			logger.warning(f"[bili_scroll] 注册页面接口失败: {type(e).__name__}: {e}")

	async def _daily(self, hour: int):
		while True:
			try:
				now = datetime.datetime.now()
				t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
				if t <= now:
					t += datetime.timedelta(days=1)
				await asyncio.sleep((t - now).total_seconds())
				logger.info("[bili_scroll] 开始每日刷")
				await self.run_round(force=True)
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.warning(f"[bili_scroll] 定时异常: {e}")
				await asyncio.sleep(600)

	async def terminate(self):
		# CancelledError 是 BaseException 子类，必须显式接住；
		# 否则它会被抛回 star_manager.reload()，把 dashboard 那次保存请求的协程
		# 一起标记成 cancelled，前端只看到 Internal server error。
		if self._dm is not None:
			try:
				await self._dm.stop()
			except (asyncio.CancelledError, Exception):
				pass
			self._dm = None
		t = self._task
		self._task = None
		if t is None or t.done():
			return
		t.cancel()
		try:
			await t
		except (asyncio.CancelledError, Exception):
			pass
