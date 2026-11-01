"""B站数据层：搜索、视频信息、字幕、评论、弹幕。

参照 dsh-bili-scroll 的 vendor/bilibili-mcp 实现，只保留自动刷视频所需的部分。
"""

import asyncio
import json
import os
import time

from bilibili_api import search, video, Credential, homepage

import aiohttp


class BiliClient:
	"""B站接口封装，带限速与凭据管理。"""

	def __init__(self, sessdata: str = "", bili_jct: str = ""):
		self.sessdata = (sessdata or "").strip()
		self.bili_jct = (bili_jct or "").strip()
		self._last_call = 0.0
		self._min_interval = 1.2  #秒，避免触发风控

	@property
	def credential(self):
		if not self.sessdata:
			return None
		return Credential(sessdata=self.sessdata, bili_jct=self.bili_jct or None)

	@property
	def has_login(self) -> bool:
		return bool(self.sessdata)

	async def _throttle(self):
		now = time.monotonic()
		wait = self._min_interval - (now - self._last_call)
		if wait > 0:
			await asyncio.sleep(wait)
		self._last_call = time.monotonic()

	async def search_videos(self, keyword: str, num: int = 10, order: str = "pubdate") -> list:
		"""搜索视频。order: totalrank/click/pubdate/dm"""
		await self._throttle()
		order_map = {
			"totalrank": search.OrderVideo.TOTALRANK,
			"click": search.OrderVideo.CLICK,
			"pubdate": search.OrderVideo.PUBDATE,
			"dm": search.OrderVideo.DM,
		}
		res = await search.search_by_type(
			keyword=keyword,
			search_type=search.SearchObjectType.VIDEO,
			page=1,
			order_type=order_map.get(order, search.OrderVideo.PUBDATE),
		)
		out = []
		for it in (res.get("result") or [])[:num]:
			title = (it.get("title") or "").replace('<em class="keyword">', "").replace("</em>", "")
			out.append({
				"bvid": it.get("bvid", ""),
				"aid": it.get("aid", 0),
				"title": title,
				"author": it.get("author", "") or it.get("uname", ""),
				"play": it.get("play", 0),
				"review": it.get("review", 0),
				"danmaku": it.get("video_review", 0) or it.get("danmaku", 0),
				"duration": it.get("duration", ""),
				"pubdate": it.get("pubdate", 0),
				"description": (it.get("description") or "")[:300],
			})
		return out

	async def hot_videos(self, limit: int = 10) -> list:
		"""首页热门，用于关键词留空时。"""
		await self._throttle()
		out = []
		try:
			data = await homepage.get_videos()
			for item in (data or [])[:limit]:
				if isinstance(item, dict):
					continue
				try:
					# homepage返回的是 RecommendVideo 或类似对象
					bv = getattr(item, "bvid", None) or ""
					if not bv:
						continue
					out.append({"bvid": bv, "title": "", "author": "", "play": 0,
						"review": 0, "danmaku": 0, "duration": "", "pubdate": 0, "description": ""})
				except Exception:
					continue
		except Exception:
			return []
		return out

	async def video_info(self, bvid: str) -> dict:
		"""取视频详情，返回含 cid 的字典。"""
		await self._throttle()
		v = video.Video(bvid=bvid, credential=self.credential)
		info = await v.get_info()
		cid = info.get("cid") or 0
		if not cid and info.get("pages"):
			cid = (info["pages"][0] or {}).get("cid", 0)
		stat = info.get("stat") or {}
		owner = info.get("owner") or {}
		return {
			"bvid": bvid,
			"aid": info.get("aid", 0),
			"cid": cid,
			"title": info.get("title", ""),
			"author": owner.get("name", ""),
			"author_mid": owner.get("mid", 0),
			"duration": info.get("duration", 0),
			"desc": (info.get("desc") or "")[:500],
			"pubdate": info.get("pubdate", 0),
			"view": stat.get("view", 0),
			"danmaku": stat.get("danmaku", 0),
			"reply": stat.get("reply", 0),
			"like": stat.get("like", 0),
			"favorite": stat.get("favorite", 0),
			"pic": info.get("pic", ""),
		}

	async def subtitle(self, bvid: str, cid: int = 0) -> dict:
		"""取字幕原文。返回 {ok, text, lan, segments, reason}"""
		if not self.has_login:
			return {"ok": False, "text": "", "reason": "未配置 SESSDATA，无法读字幕"}
		await self._throttle()
		try:
			v = video.Video(bvid=bvid, credential=self.credential)
			if not cid:
				info = await v.get_info()
				cid = info.get("cid") or (info.get("pages") or [{}])[0].get("cid", 0)
			if not cid:
				return {"ok": False, "text": "", "reason": "无 cid"}

			data = await v.get_subtitle(cid=cid)
			subs = data.get("subtitles") or []
			if not subs:
				return {"ok": False, "text": "", "reason": "该视频没有字幕"}

			target = None
			for s in subs:
				if s.get("lan") in ("ai-zh", "zh-CN", "zh"):
					target = s
					break
			if not target:
				target = subs[0]

			url = target.get("subtitle_url") or ""
			if url.startswith("//"):
				url = "https:" + url
			if not url:
				return {"ok": False, "text": "", "reason": "字幕地址为空"}

			async with aiohttp.ClientSession(trust_env=True) as sess:
				async with sess.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
					sub_data = await resp.json(content_type=None)

			texts = [x.get("content", "") for x in (sub_data.get("body") or [])]
			full = "\n".join(t for t in texts if t)
			return {
				"ok": bool(full.strip()),
				"text": full,
				"lan": target.get("lan_doc", ""),
				"segments": len(texts),
				"reason": "" if full.strip() else "字幕为空",
			}
		except Exception as e:
			return {"ok": False, "text": "", "reason": f"{type(e).__name__}: {str(e)[:120]}"}

	async def comments(self, bvid: str, num: int = 10) -> list:
		"""取热评。"""
		await self._throttle()
		try:
			from bilibili_api import comment as cm
			v = video.Video(bvid=bvid, credential=self.credential)
			aid = (await v.get_info()).get("aid", 0)
			if not aid:
				return []
			data = await cm.get_comments(
				oid=aid,
				type_=cm.CommentResourceType.VIDEO,
				order=cm.OrderType.LIKE,  # 按点赞（热度）
				credential=self.credential,
			)
			reps = (data.get("replies") or [])[:num]
			out = []
			for r in reps:
				out.append({
					"user": (r.get("member") or {}).get("uname", ""),
					"text": ((r.get("content") or {}).get("message", ""))[:200],
					"like": r.get("like", 0),
				})
			return out
		except Exception:
			return []

	async def danmaku(self, bvid: str, cid: int = 0, num: int = 30) -> list:
		"""取弹幕文本。page_index 是分P序号（0=第一P）。"""
		await self._throttle()
		try:
			v = video.Video(bvid=bvid, credential=self.credential)
			items = await v.get_danmakus(page_index=0)
			texts = []
			for x in (items or []):
				t = getattr(x, "text", None)
				if t:
					texts.append(t)
				if len(texts) >= num:
					break
			return texts
		except Exception:
			return []

	async def reply_comment(self, bvid: str, text: str, at_uid: str = "") -> dict:
		"""发一条评论（可选@某人）。默认不启用。"""
		if not self.bili_jct:
			return {"ok": False, "reason": "未配置 bili_jct"}
		await self._throttle()
		try:
			from bilibili_api import comment as cm
			v = video.Video(bvid=bvid, credential=self.credential)
			info = await v.get_info()
			aid = info.get("aid", 0)
			final = f"@{at_uid} {text}" if at_uid else text
			r = await cm.send_comment(
				text=final,
				oid=aid,
				type_=cm.CommentResourceType.VIDEO,
				credential=self.credential,
			)
			rpid = ""
			if isinstance(r, dict):
				rpid = str(r.get("rpid_str") or r.get("rpid") or "")
			return {"ok": bool(rpid), "rpid": rpid, "text": final,
				"reason": "" if rpid else "返回里没有 rpid"}
		except Exception as e:
			return {"ok": False, "reason": f"{type(e).__name__}: {str(e)[:150]}"}
