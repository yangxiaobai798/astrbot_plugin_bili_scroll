"""用 AstrBot 插件方式承载原项目的 B站工具集。

原项目 vendor/bilibili-mcp/mcp_server.py 基于 FastMCP，这里用一个极简的
注册器替身取代 MCP 依赖，33个工具函数原样保留（逻辑一行不改）。
"""

import json


class ImageContent:
	"""替身：原 MCP 的图片内容块。"""

	def __init__(self, type="image", data="", mimeType="image/jpeg"):
		self.type = type
		self.data = data
		self.mimeType = mimeType

	def __repr__(self):
		return f"ImageContent({self.mimeType}, {len(self.data)}b64)"


class TextContent:
	"""替身：原 MCP 的文本内容块。"""

	def __init__(self, type="text", text=""):
		self.type = type
		self.text = text

	def __repr__(self):
		return f"TextContent({self.text[:40]})"


class ToolRegistry:
	"""收集所有 @tool() 装饰的函数。"""

	def __init__(self):
		self.tools = {}

	def tool(self):
		def deco(fn):
			self.tools[fn.__name__] = fn
			return fn
		return deco


class FakeMCP:
	"""替身 FastMCP，只保留 .tool() 注册能力。"""

	def __init__(self, name="bilibili-mcp"):
		self.name = name
		self.registry = ToolRegistry()

	def tool(self):
		return self.registry.tool()

	def run(self, *a, **kw):
		raise RuntimeError("这是 AstrBot 插件，不该调用 mcp.run()")
