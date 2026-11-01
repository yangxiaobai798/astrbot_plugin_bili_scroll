"""刷B站插件的 Web API：给管理页面提供浏览记录、对话记录、配置读写。"""

import json
import os
from datetime import datetime


def _json(data, status=200):
	from quart import jsonify
	return jsonify(data), status


class BiliScrollApi:
	"""把插件的状态以 HTTP API 暴露给 pages/bili-scroll 页面。"""

	def __init__(self, plugin):
		self.plugin = plugin

	@property
	def _p(self):
		return self.plugin

	def _data(self):
		return self._p._data()

	# ----------状态 ----------

	async def get_status(self):
		"""总览：登录、配额、上次跑、今日已挑。"""
		p = self._p
		today = datetime.now().strftime("%Y-%m-%d")
		out_dir = os.path.join(self._data(), "out")
		report = os.path.join(out_dir, f"{today}.md")

		picked_today = 0
		last_run = ""
		if os.path.isfile(report):
			try:
				with open(report, encoding="utf-8") as f:
					txt = f.read()
				picked_today = sum(1 for L in txt.splitlines() if L.startswith("### BV"))
				last_run = datetime.fromtimestamp(os.path.getmtime(report)).strftime("%Y-%m-%d %H:%M:%S")
			except OSError:
				pass

		ledger_count = 0
		seen = os.path.join(self._data(), "seen.jsonl")
		if os.path.isfile(seen):
			try:
				with open(seen, encoding="utf-8") as f:
					ledger_count = sum(1 for L in f if L.strip())
			except OSError:
				pass

		cfg = p._cfg()
		return _json({
			"ok": True,
			"loggedIn": p._has_cred(),
			"perDay": cfg.get("perDay", 5),
			"pickedToday": picked_today,
			"lastRunAt": last_run,
			"ledgerCount": ledger_count,
			"reportPath": report,
			"uptime": True,
		})

	# ----------浏览记录 ----------

	async def get_history(self):
		"""看过的视频（判重表）。"""
		seen = os.path.join(self._data(), "seen.jsonl")
		rows = []
		if os.path.isfile(seen):
			try:
				with open(seen, encoding="utf-8") as f:
					lines = [x.strip() for x in f if x.strip()]
				for line in reversed(lines[-500:]):
					try:
						rows.append(json.loads(line))
					except json.JSONDecodeError:
						continue
			except OSError:
				pass
		return _json({"ok": True, "count": len(rows), "items": rows})

	# ----------对话记录 ----------

	async def get_dialogs(self):
		"""它跟人聊过什么（从 AstrBot 的会话表读，可按插件相关过滤）。"""
		try:
			import sqlite3
			root = os.path.dirname(os.path.dirname(self._data()))
			# self._data() 形如 .../data/plugin_data/<plugin>，向上两级到 AstrBot 根
			for _ in range(4):
				cand = os.path.join(root, "data", "data_v4.db")
				if os.path.isfile(cand):
					db = cand
					break
				root = os.path.dirname(root)
			else:
				return _json({"ok": False, "error": "找不到 data_v4.db"}, 500)

			con = sqlite3.connect(db)
			con.row_factory = sqlite3.Row
			rows = []
			try:
				cur = con.execute(
					"select conversation_id, user_id, platform_id, content, updated_at "
					"from conversations order by updated_at desc limit 40")
				for r in cur:
					content = r["content"] or "[]"
					msgs = []
					try:
						arr = json.loads(content)
						for m in arr[-6:]:
							role = m.get("role", "")
							txt = ""
							c = m.get("content")
							if isinstance(c, str):
								txt = c
							elif isinstance(c, list):
								parts = []
								for seg in c:
									if isinstance(seg, dict) and seg.get("type") == "text":
										parts.append(seg.get("text", ""))
								txt = " ".join(parts)
							if txt.strip():
								msgs.append({"role": role, "text": txt[:400]})
					except json.JSONDecodeError:
						pass
					rows.append({
						"umo": r["user_id"],
						"platform": r["platform_id"],
						"updated": r["updated_at"],
						"messages": msgs,
					})
			finally:
				con.close()
			return _json({"ok": True, "count": len(rows), "items": rows})
		except Exception as e:
			return _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

	# ----------日报 ----------

	async def get_reports(self):
		"""列出已有日报。"""
		out_dir = os.path.join(self._data(), "out")
		items = []
		if os.path.isdir(out_dir):
			for f in sorted(os.listdir(out_dir), reverse=True):
				if not f.endswith(".md"):
					continue
				p = os.path.join(out_dir, f)
				items.append({
					"date": f[:-3],
					"size": os.path.getsize(p),
					"mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M"),
				})
		return _json({"ok": True, "items": items[:60]})

	async def get_report(self):
		"""读某天的日报全文（?date=YYYY-MM-DD）。"""
		try:
			from quart import request
			date = request.args.get("date", "")
		except Exception:
			date = ""
		safe = "".join(ch for ch in (date or "") if ch.isdigit() or ch == "-")
		if not safe:
			return _json({"ok": False, "error": "缺少 date 参数"}, 400)
		p = os.path.join(self._data(), "out", f"{safe}.md")
		if not os.path.isfile(p):
			return _json({"ok": False, "error": "没有这天的日报"}, 404)
		try:
			txt = open(p, encoding="utf-8").read()
			return _json({"ok": True, "date": safe, "content": txt})
		except OSError as e:
			return _json({"ok": False, "error": str(e)}, 500)

	# ----------动作 ----------

	async def post_run(self):
		"""立刻跑一轮。"""
		try:
			res = await self._p.run_round(force=True)
			return _json({"ok": True, "result": {
				"stats": res.get("stats"),
				"pushText": res.get("push_text"),
				"error": res.get("error"),
			}})
		except Exception as e:
			return _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

	# ----------扫码登录（对齐原版 /login/start + /login/poll） ----------

	async def post_login_start(self):
		"""生成登录二维码，返回 {ok, key, qr}。qr 是 data URL，直接给 <img src>。"""
		try:
			mod = self._p._ensure_tools()
			# 已登录就直接告诉前端
			if mod.load_credential() is not None:
				return _json({"ok": True, "already": True, "qr": "",
					"message": "已经登录过了"})
			r = await mod.call_tool("bili_login")
			d = json.loads(r) if isinstance(r, str) else {}
			st = d.get("status")
			if st == "already_logged_in":
				return _json({"ok": True, "already": True, "qr": "",
					"message": "已经登录过了"})
			qr = d.get("qrcode_image") or ""
			if not qr:
				return _json({"ok": False, "error": "没画出二维码（qrcode 依赖缺失？）"})
			# 用插件内存里的会话标识作为 key
			key = f"login-{int(__import__('time').time())}"
			self._p._login_key = key
			return _json({"ok": True, "key": key, "qr": qr,
				"message": "用手机 B站 扫这张码"})
		except Exception as e:
			return _json({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}, 500)

	async def post_login_poll(self):
		"""查询扫码状态，返回 {state: ok|scanned|expired|unknown|waiting}。"""
		try:
			mod = self._p._ensure_tools()
			# 已经登录了（扫码成功后 bili_login_check 会写凭证）
			if mod.load_credential() is not None:
				return _json({"state": "ok"})
			r = await mod.call_tool("bili_login_check")
			d = json.loads(r) if isinstance(r, str) else {}
			st = d.get("status")
			if st in ("done", "already_logged_in"):
				return _json({"state": "ok"})
			if st in ("scanning", "confirming"):
				return _json({"state": "scanned", "message": d.get("message", "")})
			if st == "timeout":
				return _json({"state": "expired"})
			if st == "no_session":
				return _json({"state": "expired"})
			return _json({"state": "waiting"})
		except Exception as e:
			return _json({"state": "unknown",
				"message": f"{type(e).__name__}: {str(e)[:120]}"}, 500)

	async def post_logout(self):
		"""退出登录：删掉凭证文件。"""
		try:
			mod = self._p._ensure_tools()
			p = mod.CRED_FILE
			if p.exists():
				p.unlink()
			return _json({"ok": True})
		except Exception as e:
			return _json({"ok": False, "error": str(e)}, 500)

	async def post_config(self):
		"""保存配置到运行时配置文件。"""
		try:
			from quart import request
			body = await request.get_json(force=True, silent=True) or {}
			root = self._data()
			for _ in range(4):
				cand = os.path.join(root, "data", "config",
					"astrbot_plugin_bili_scroll_config.json")
				if os.path.isfile(cand):
					path = cand
					break
				root = os.path.dirname(root)
			else:
				return _json({"ok": False, "error": "找不到配置文件"}, 500)

			cur = {}
			try:
				cur = json.load(open(path, encoding="utf-8-sig"))
			except Exception:
				cur = {}
			# 只允许改已知字段
			allowed = {
				"keywords", "perDay", "reportLevel", "personaPrompt", "searchSort",
				"excludeWords", "dmMode", "mentionName", "autoComment", "commentPerRound",
				"commentLength", "replyToComments", "replyPerRound", "dmWatch",
				"favFolderId", "transcribe", "bili_sessdata", "bili_bili_jct",
				"auto_on_link", "group_need_at", "push_target", "cron_hour", "llm_provider_id",
			}
			for k, v in body.items():
				if k in allowed:
					cur[k] = v
			tmp = path + ".tmp"
			with open(tmp, "w", encoding="utf-8-sig") as f:
				json.dump(cur, f, ensure_ascii=False, indent=2)
			os.replace(tmp, path)
			# 同步到插件内存
			if isinstance(getattr(self._p, "conf", None), dict):
				self._p.conf.update({k: v for k, v in body.items() if k in allowed})
			return _json({"ok": True, "saved": [k for k in body if k in allowed]})
		except Exception as e:
			return _json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)
