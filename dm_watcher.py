"""B站私信双向监听。

原版 dsh-bili-scroll 靠一个常驻 agent 自己轮询私信，AstrBot 这个移植版
当时只搬了「每日刷视频」，`dmWatch` 这个开关一直是空壳 —— 界面上写着
「有人私信它就回一句」，代码里没有任何东西读它。

这个模块补上那段：
  · 每 poll_interval 秒拉一次会话列表，找有未读的私聊会话
  · 只回白名单里的 UID（默认 = B站自己 + mentionName 对应的那位）
  · 每条私信走一次 LLM 生成回复，用 session.send_msg 发回去
  · 已回过的 msg_seqno 存进 dm_state.json，重启不重复回

三条硬约束，都来自原版踩过的坑（见 dsh_ref/duty.md）：
  1. 私信正文上限 500 字符，超了接口返回 code 21007 —— 发之前必须截。
  2. 一轮只发一条，别把几条分开发。
  3. 查私信失败（风控/网络）不能把循环打死，睡一会儿接着来。
"""

import asyncio
import datetime
import json
import os

from astrbot.api import logger

DM_MAX_CHARS = 480          # 留 20 字余量，实测 490 能过、再长 code 21007
SEEN_KEEP = 300             # 状态文件里最多留多少条已回记录


class DmWatcher:
	def __init__(self, plugin):
		self.p = plugin
		self._task = None
		self._state_path = os.path.join(plugin._data(), "dm_state.json")
		self._seen = self._load_seen()

	# ---------------- 状态 ----------------

	def _load_seen(self) -> list:
		try:
			with open(self._state_path, encoding="utf-8") as f:
				d = json.load(f)
			v = d.get("seen")
			return list(v) if isinstance(v, list) else []
		except (OSError, json.JSONDecodeError, AttributeError):
			return []

	def _save_seen(self):
		self._seen = self._seen[-SEEN_KEEP:]
		tmp = self._state_path + ".tmp"
		try:
			with open(tmp, "w", encoding="utf-8") as f:
				json.dump({"seen": self._seen,
					"updated": datetime.datetime.now().isoformat(timespec="seconds")},
					f, ensure_ascii=False)
			os.replace(tmp, self._state_path)
		except OSError as e:
			logger.warning(f"[bili_scroll] 私信状态写入失败: {e}")

	def _mark(self, key: str):
		if key not in self._seen:
			self._seen.append(key)
			self._save_seen()

	# ---------------- 启动/停止 ----------------

	def start(self):
		if self._task and not self._task.done():
			return
		self._task = asyncio.create_task(self._loop())
		logger.info("[bili_scroll] 私信监听已启动")

	async def stop(self):
		t, self._task = self._task, None
		if t is None or t.done():
			return
		t.cancel()
		try:
			await t
		except (asyncio.CancelledError, Exception):
			pass

	# ---------------- 主循环 ----------------

	async def _loop(self):
		# 启动后先等一下，别和插件初始化抢资源
		await asyncio.sleep(15)
		while True:
			try:
				await self.poll_once()
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.warning(f"[bili_scroll] 私信轮询异常: {type(e).__name__}: {e}")
			interval = int(self.p._get("dm_poll_seconds", 60) or 60)
			await asyncio.sleep(max(20, interval))

	async def poll_once(self) -> int:
		"""查一轮，返回这轮回复了几条。"""
		from bilibili_api import session

		mod = self.p._ensure_tools()
		cred = mod.get_cred()
		if cred is None:
			return 0

		res = await session.get_sessions(cred, session_type=4)
		data = res.get("data", res)
		sessions = data.get("session_list") or []
		if not sessions:
			return 0

		allowed = self._allowed_uids()
		if not allowed:
			logger.debug("[bili_scroll] 私信白名单为空，跳过（填 mentionName 或 dm_allowed_uids）")
			return 0

		replied = 0
		for s in sessions:
			talker = s.get("talker_id")
			if not talker:
				continue
			# 系统通知号是超大 UID（844424930131964 这类），不参与自动回复
			if int(talker) > 840000000000000:
				continue
			if str(talker) not in allowed:
				continue
			try:
				n = await self._handle_session(session, cred, int(talker))
				replied += n
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.warning(
					f"[bili_scroll] 处理私信会话 {talker} 失败: {type(e).__name__}: {e}")
		if replied:
			logger.info(f"[bili_scroll] 本轮回复了 {replied} 条私信")
		return replied

	def _allowed_uids(self) -> set:
		"""白名单：显式配置优先，否则回退到 mentionName 查出来的 UID。"""
		raw = str(self.p._get("dm_allowed_uids", "") or "")
		uids = {x.strip() for x in raw.replace("，", ",").split(",") if x.strip()}
		if uids:
			return uids
		# mentionName 是 B站昵称，缓存过一次就不重复查
		uid = getattr(self.p, "_mention_uid", None)
		if uid:
			return {str(uid)}
		return set()

	async def _resolve_mention_uid(self):
		"""把 mentionName 解析成 UID，供白名单用。

		bilibili_api 没有公开的 search_user，原来那段 try 必然失败。
		改成：先用配置里显式给的 dm_allowed_uids（已在 _allowed_uids 处理），
		再回退到「当前会话里那个真实对话者」——对单人使用的场景，
		会话列表里除自己以外的那个非系统号就是主人。
		"""
		name = str(self.p._get("mentionName", "") or "").strip()
		if name:
			try:
				mod = self.p._ensure_tools()
				cred = mod.get_cred()
				from bilibili_api import user as bili_user
				fn = getattr(bili_user, "search_user", None)
				if fn:
					r = await fn(name, credential=cred)
					for item in (r.get("result") or [])[:5]:
						if item.get("uname") == name:
							return item.get("mid")
			except Exception as e:
				logger.debug(f"[bili_scroll] 按昵称查UID失败（回退到会话列表）: {e}")

		# 回退：会话列表里第一个非系统、非自己的会话，就是私信对话者
		try:
			from bilibili_api import session as bili_session
			mod = self.p._ensure_tools()
			cred = mod.get_cred()
			res = await bili_session.get_sessions(cred, session_type=4)
			data = res.get("data", res)
			self_uid = int(getattr(self.p, "_self_uid", 0) or 0)
			for s in (data.get("session_list") or []):
				talker = int(s.get("talker_id") or 0)
				if not talker or talker > 840000000000000:
					continue
				if self_uid and talker == self_uid:
					continue
				return talker
		except Exception as e:
			logger.debug(f"[bili_scroll] 从会话列表推UID失败: {e}")
		return None

	# ---------------- 单个会话 ----------------

	async def _handle_session(self, session_mod, cred, talker: int) -> int:
		mod = self.p._ensure_tools()
		from bilibili_api import session

		r = await session.fetch_session_msgs(
			talker_id=talker, credential=cred, session_type=1)
		msgs = r.get("messages") or []
		if not msgs:
			return 0

		# 按 seqno 从旧到新处理，只挑「对方发来、我还没回」的那条
		msgs = sorted(msgs, key=lambda m: m.get("msg_seqno") or 0)

		# 找到最后一条自己发的消息的序号，之后的对方消息才算未回
		last_mine = 0
		for m in msgs:
			if int(m.get("sender_uid") or 0) == int(self._self_uid()):
				last_mine = max(last_mine, m.get("msg_seqno") or 0)

		pending = []
		for m in msgs:
			seq = m.get("msg_seqno") or 0
			if int(m.get("sender_uid") or 0) == int(self._self_uid()):
				continue
			if seq <= last_mine:
				continue
			pending.append(m)

		if not pending:
			return 0

		# 合并所有未回内容，一次生成、一次发出（一轮只发一条）
		texts = []
		keys = []
		for m in pending:
			txt = self._content_text(m)
			if txt:
				texts.append(txt)
			keys.append(f"{talker}:{m.get('msg_seqno')}")

		if not texts:
			for k in keys:
				self._mark(k)
			return 0

		joined = "\n".join(texts)
		reply = await self._generate_reply(joined)
		if not reply:
			logger.info("[bili_scroll] 私信没生成出回复，本轮跳过")
			return 0

		reply = reply.strip()
		if len(reply) > DM_MAX_CHARS:
			reply = reply[:DM_MAX_CHARS - 1] + "…"

		try:
			# 用 bili_tools 里已验证的封装：它带 msg_key 回执校验，
			# 能区分「接口回 200 但其实没送到」和真发出去。
			out = await mod.bili_send_message(receiver_uid=talker, text=reply)
			d = json.loads(out) if isinstance(out, str) and out.startswith("{") else {}
			if d.get("success") is False or d.get("error"):
				logger.warning(f"[bili_scroll] 私信发送失败: {out[:150]}")
				return 0
		except Exception as e:
			logger.warning(f"[bili_scroll] 私信发送异常: {type(e).__name__}: {e}")
			return 0

		for k in keys:
			self._mark(k)
		logger.info(
			f"[bili_scroll] 已回私信给 {talker}（收到 {len(texts)} 条，回 {len(reply)} 字）")
		return 1

	def _self_uid(self):
		uid = getattr(self.p, "_self_uid", None)
		return int(uid) if uid else 0

	@staticmethod
	def _content_text(m: dict) -> str:
		"""私信 content 是一层 JSON 字符串，取出真正的文字。"""
		raw = m.get("content") or ""
		try:
			d = json.loads(raw)
			if isinstance(d, dict):
				return str(d.get("content") or d.get("title") or "").strip()
			return str(d).strip()
		except (json.JSONDecodeError, TypeError):
			return str(raw).strip()

	# ---------------- 生成回复 ----------------

	async def _generate_reply(self, incoming: str) -> str:
		provider = await self.p._provider()
		if provider is None:
			return ""
		persona = str(self.p._get("dmPersona", "") or "").strip()
		if not persona:
			persona = str(self.p._get("personaPrompt", "") or "").strip()

		# 规则走 user turn：部分中转站会丢 system/instructions，
		# 塞进 system 就等于没写（见 references/provider-relay-quirks.md）。
		charter = (
			"【身份】你是大鲨鱼，一个在B站上刷视频的普通用户，不是客服。\n"
			"【场景】这是B站私信，对方是熟人，说话像随手打字。\n"
			"【规矩】\n"
			"1. 一两句就够，别写小作文，不用列表、不加粗。\n"
			"2. 不用「您好」「有什么可以帮您」这类客服腔。\n"
			"3. 不解释自己是AI，被问就含糊过去。\n"
			"4. 别编造你刷到的视频；不确定就说还没看。\n"
			"5. 直接说要说的话，不要加任何前缀或标记。\n"
		)
		prompt = charter + "\n"
		if persona:
			prompt += "【人设补充】\n" + persona + "\n\n"
		prompt += "【对方刚发来的私信】\n" + incoming[:2000] + "\n\n直接回："
		try:
			resp = await provider.text_chat(prompt=prompt, system_prompt="")
			text = getattr(resp, "completion_text", "") or ""
			if not text.strip():
				# 推理模型可能把预算烧在 reasoning_content 上，重试一次
				resp = await provider.text_chat(
					prompt=prompt + "\n（直接给一句话，不要思考过程）",
					system_prompt="")
				text = getattr(resp, "completion_text", "") or ""
			return text.strip()
		except Exception as e:
			logger.warning(f"[bili_scroll] 私信生成回复失败: {type(e).__name__}: {e}")
			return ""
