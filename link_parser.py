"""从消息里认出 B站链接 / BV号。"""

import re

# BV号：BV 开头 + 10 位 base58 字符
_BV_RE = re.compile(r"BV([1-9A-HJ-NP-Za-km-z]{10})", re.IGNORECASE)
# av号
_AV_RE = re.compile(r"(?:^|[^0-9])av(\d{1,12})", re.IGNORECASE)
# 短链 b23.tv
_B23_RE = re.compile(r"https?://b23\.tv/[A-Za-z0-9]+", re.IGNORECASE)
# 完整链接
_FULL_RE = re.compile(r"https?://(?:www\.|m\.)?bilibili\.com/video/[A-Za-z0-9]+", re.IGNORECASE)


def extract_bvid(text: str) -> str:
	"""从文本中提取 BV号，取第一个。"""
	if not text:
		return ""
	m = _BV_RE.search(text)
	if m:
		return "BV" + m.group(1)
	return ""


def extract_av(text: str) -> int:
	"""提取 av号数字。"""
	if not text:
		return 0
	m = _AV_RE.search(text)
	if m:
		try:
			return int(m.group(1))
		except ValueError:
			return 0
	return 0


def extract_short_url(text: str) -> str:
	"""提取 b23.tv 短链。"""
	if not text:
		return ""
	m = _B23_RE.search(text)
	return m.group(0) if m else ""


def has_bili_link(text: str) -> bool:
	"""消息里有没有 B站视频。"""
	if not text:
		return False
	return bool(_BV_RE.search(text) or _AV_RE.search(text) or _B23_RE.search(text) or _FULL_RE.search(text))


async def resolve_short_url(url: str, session=None) -> str:
	"""把 b23.tv 短链解析成真实链接，返回其中的 BV号。"""
	if not url:
		return ""
	try:
		import aiohttp
		owns = session is None
		if owns:
			session = aiohttp.ClientSession(trust_env=True)
		try:
			async with session.get(url, allow_redirects=True,
				timeout=aiohttp.ClientTimeout(total=15)) as resp:
				final = str(resp.url)
				return extract_bvid(final)
		finally:
			if owns:
				await session.close()
	except Exception:
		return ""
