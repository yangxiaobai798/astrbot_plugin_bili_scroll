"""把移植的 34 个工具注册成 AstrBot 的 LLM FunctionTool。

原项目里这些工具是给 agent 自由调用的（MCP tools），这里保持同样的自由度：
模型看到工具名和文档字符串，自己决定调哪个。
"""

import inspect
import json

from astrbot.api import logger


# 工具名 -> 中文说明（给模型看的提示，部分是原 docstring 的摘要）
TOOL_HINTS = {
	"bili_search": "搜索B站视频，返回标题/BV号/播放量/UP主",
	"bili_video_info": "取单个视频的详情（标题/UP/时长/播放/简介）",
	"bili_subtitle": "取视频字幕原文（需登录）",
	"bili_comments": "取视频热评",
	"bili_danmaku": "取视频弹幕（可抓当场梗）",
	"bili_cover": "取视频封面图",
	"bili_frames": "从视频抽画面帧（看演示类内容用，需登录）",
	"bili_recommend": "取B站首页推荐流（按账号画像，需登录）",
	"bili_hot_videos": "取热门视频",
	"bili_rank": "取排行榜",
	"bili_weekly_hot": "取每周必看",
	"bili_hot_buzzwords": "取热门梗词",
	"bili_video_zones": "取视频分区",
	"bili_user_info": "取UP主信息",
	"bili_user_videos": "取UP主的视频列表",
	"bili_crawl": "批量抓取：按关键词抓多条视频+评论+字幕",
	"bili_reply": "发一条评论（需 bili_jct）",
	"bili_send_message": "发私信",
	"bili_unread_messages": "取未读私信",
	"bili_received_replies": "取收到的评论回复",
	"bili_received_at_and_likes": "取谁@了我、谁赞了我",
	"bili_favorite_lists": "取收藏夹列表",
	"bili_favorite_content": "取收藏夹内容",
	"bili_login": "生成B站登录二维码",
	"bili_login_check": "检查登录状态",
	"bili_check_credential": "检查凭证是否有效",
	"bili_transcribe": "用本地ASR把无字幕视频转文字（慢）",
	"bili_transcribe_start": "提交转写任务，返回job_id",
	"bili_transcribe_result": "取转写结果",
}


def _sig_to_schema(fn) -> dict:
	"""从函数签名生成 JSON Schema 参数定义。"""
	props = {}
	required = []
	try:
		sig = inspect.signature(fn)
	except (TypeError, ValueError):
		return {"type": "object", "properties": {}, "required": []}

	type_map = {str: "string", int: "integer", float: "number", bool: "boolean"}
	for pname, p in sig.parameters.items():
		if pname in ("self", "cls"):
			continue
		ann = p.annotation
		py_type = "string"
		if ann in type_map:
			py_type = type_map[ann]
		elif ann is list or getattr(ann, "__origin__", None) is list:
			py_type = "array"
		prop = {"type": py_type}
		# 从 docstring 提取这个参数的说明
		doc = inspect.getdoc(fn) or ""
		for line in doc.splitlines():
			line = line.strip()
			if line.startswith(f"{pname}:"):
				prop["description"] = line[len(pname) + 1:].strip()
				break
		if p.default is not inspect.Parameter.empty:
			prop["default"] = p.default if p.default is not None else None
		else:
			required.append(pname)
		props[pname] = prop
	return {"type": "object", "properties": props, "required": required}


def build_function_tools(tools_mod, call_fn, allow_names=None):
	"""生成 AstrBot FunctionTool 列表。

	tools_mod: 已加载的 bili_tools 模块
	call_fn: async (name, **kwargs) -> str，负责实际调用
	"""
	from astrbot.api import FunctionTool  # 运行时才 import，避免顶层依赖

	out = []
	src = tools_mod.list_tools()
	for name, fn in sorted(src.items()):
		if allow_names and name not in allow_names:
			continue
		if name.startswith("duty_"):
			continue  # duty_* 是文件工具，不给模型调
		hint = TOOL_HINTS.get(name, inspect.getdoc(fn) or name)
		schema = _sig_to_schema(fn)

		def make(name_str, hint_str, schema_dict):
			# AstrBot 调用 handler 的形式是 handler(event, **kwargs)（见
			# core/provider/func_tool_manager.py:256），第一个位置参数必须是 event。
			# 写成 handler(**kwargs) 会抛
			# "handler() takes 0 positional arguments but 1 was given"，
			# 29 个工具会全部失效，而插件仍显示「已注册 29 个LLM工具」。
			async def handler(event=None, **kwargs):
				return await call_fn(name_str, **kwargs)

			handler.__name__ = name_str
			handler.__doc__ = hint_str
			tool = FunctionTool(
				name=name_str,
				description=hint_str,
				parameters=schema_dict,
				handler=handler,
			)
			return tool

		out.append(make(name, hint, schema))

	logger.info(f"[bili_scroll] 构建了 {len(out)} 个LLM工具")
	return out
