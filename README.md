# 刷B站 (astrbot_plugin_bili_scroll) v2.1

丢一个 B站 链接过来，它读完字幕告诉你这条值不值得花时间；配好 Cookie 后还能每天自动刷 B站，挑出值得看的写日报，并到 B站评论区 @ 你。

工具层移植自 [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll)（MIT），其中 29 个注册给模型自由调用。

## 功能

### 1. 发链接就分析

```
你：https://www.bilibili.com/video/BV1GJ411x7h7
Bot：看一下 BV1GJ411x7h7…
Bot：《【官方 MV】Never Gonna Give You Up - Rick Astley》
     UP：索尼音乐中国　时长：3分33秒　播放：107805868

     【可以看】
     这是 Rick Astley 1987 年的官方 MV…
     ——三分钟听首歌不亏。

     https://www.bilibili.com/video/BV1GJ411x7h7
```

支持：完整链接、`BV号`、`b23.tv` 短链、`av号`。

**无字幕的视频会自动走本地 ASR 转写**（需装 faster-whisper，见下）。

### 2. 每天自动刷

按关键词搜索 → 读字幕 → 模型判断 → 写日报 → 通知你。

判断分三档：**值得看** / **可以看** / **不值当**。只有前两档进日报。

### 3. 三种通知方式（`notify_mode`）

| 值 | 行为 |
|---|---|
| `bili_at_only` | 只在B站评论区 @ 你，B站App 会推送到手机。**不发 QQ**（默认） |
| `both` | B站 @ 你 + QQ 发一条汇总 |
| `qq_only` | 只发 QQ |

如果选 `bili_at_only` 或 `both`，还有个 `dmMode` 控制 B站 内部怎么投递：

| `dmMode` | 行为 |
|---|---|
| `off` | 只发公开评论 @ 你（默认） |
| `only` | 只发 B站私信，不公开评论 |
| `both` | 评论和私信都发 |

> ⚠️ `off`/`both` 会用**你自己的 B站账号公开发言**。`owner_comment_rate` 控制概率（默认 0.5，即一半的视频会去评论），太高容易被风控。

### 4. B站私信双向（`dmWatch`）

有人私信它，它会回一句。**只回白名单里的人**，陌生人私信不回。

- 白名单：`dm_allowed_uids`，留空则自动用 `mentionName` 对应的账号
- 轮询间隔：`dm_poll_seconds`，默认 60 秒
- 每轮只回一条，私信正文限 480 字（B站 接口上限 500，超了报 `code 21007`）
- 已回记录存 `dm_state.json`，重启不会重复回

## 安装

插件市场搜索「刷B站」，或填入仓库地址。

依赖会自动安装：

```
bilibili-api-python>=17.4.1,<18
aiohttp>=3.9.0
Pillow>=9.0
```

**本地转写（可选）**：无字幕视频要用 ASR 时，需另外装 `faster-whisper` 和模型（约 500MB）。不装的话，无字幕视频只能凭标题和简介判断。

## 首次配置

### 1. 登录 B站

**推荐：扫码登录**

聊天里发 `/bili登录`，用 B站 App 扫码。

**备选：填 SESSDATA**

浏览器登录 bilibili.com → F12 → 应用 → Cookie → 复制 `SESSDATA` 填进配置的 `bili_sessdata`。会自动写入凭证文件。

> 不登录也能用，但功能受限：读不到字幕、不能用私信、不能发评论。

### 2. 填你的 B站昵称

`mentionName` 填你自己的 B站昵称（例如 `大杨同学630`）。评论区 @ 人和私信白名单都靠它。

### 3. 设关键词

`keywords` 填你关心的内容，逗号分隔。默认 `英雄联盟,ig,theshy,美食,英雄联盟二创`。

## 全部配置项

| 配置项 | 说明 | 默认 |
|---|---|---|
| `keywords` | 搜索关键词，逗号分隔 | 英雄联盟,ig,theshy,美食,英雄联盟二创 |
| `perDay` | 每天最多挑几条 | 10 |
| `cron_hour` | 每天几点自动刷 | 10 |
| `searchSort` | 搜索排序：`pubdate`/`click`/`totalrank`/`dm` | pubdate |
| `excludeWords` | 排除词，标题含这些词的直接丢掉 | 空 |
| `reportLevel` | 日报详细度：`brief`/`normal`/`full` | full |
| `personaPrompt` | 判断视频时的额外准则 | 空 |
| `transcribe` | 无字幕时走本地 ASR | true |
| `notify_mode` | 通知方式（见上） | bili_at_only |
| `mentionName` | 你的B站昵称（@ 和私信用） | 空 |
| `owner_bili_uid` | 你的B站UID（留空自动推断） | 空 |
| `dmMode` | B站投递方式（见上） | off |
| `autoComment` | 是否允许发评论 | true |
| `owner_comment_rate` | 评论 @ 你的概率 0~1 | 0.5 |
| `commentPerRound` | 一轮最多评论几个视频 | 1 |
| `commentLength` | 评论长度：`short`/`medium`/`long` | medium |
| `dmWatch` | 私信双向回复 | true |
| `dm_allowed_uids` | 私信白名单 UID | 空 |
| `dm_poll_seconds` | 私信轮询间隔（秒） | 60 |
| `dmPersona` | 私信专用人设 | 空 |
| `auto_on_link` | 收到链接自动分析 | true |
| `group_need_at` | 群里需要 @ 才响应 | true |
| `push_target` | QQ 推送目标（umo 格式） | 空 |
| `push_group` | 「值得看」时额外喊一声的群号 | 空 |
| `llm_provider_id` | 指定模型，留空用当前模型 | 空 |
| `prompt_cache` | 让提示词前缀稳定，便于缓存 | true |
| `cache_probe` | 日志里显示缓存命中量 | true |

## 命令

| 命令 | 说明 |
|---|---|
| `/看看 BV号` | 分析指定视频 |
| `/刷b站` | 立刻跑一轮 |
| `/bili状态` | 查看配置和登录状态 |
| `/bili登录` | 扫码登录 |
| `/bili登录状态` | 检查登录 |
| `/bili工具` | 列出全部工具 |
| `/bili私信` | 查私信监听状态和未读 |

## 产物位置

```
data/plugin_data/astrbot_plugin_bili_scroll/
├── out/2026-11-01.md        # 每日日报
├── seen.jsonl               # 判重表（刷过的不重复刷）
├── dm_state.json            # 私信已回记录
└── bili_credential.json     # 登录凭证（不会打包进插件）
```

## 与原项目的差异

**已移植**：34 个工具的全部函数体逻辑。

**适配层**：

- 原项目用 MCP（FastMCP）暴露工具，本插件用 `_mcp_shim.py` 替代，函数逻辑不变
- 原项目靠 agent 读 `duty.md` 章程自主执行；本插件固化成确定性流程（可控、可预算）
- 新增 AstrBot 特性：链接自动识别、群里 @ 才响应、命令入口、按发送者分人设

**取舍**：

- 「回复别人评论（楼中楼）」这条链没做，原版才有
- `bili_frames` 返回图片数据，纯文本模型看不图，会退化成「已抽帧但未做视觉判断」

## 已知限制

- B站 接口有频率限制，刷太猛会返回 429。`perDay` 建议不超过 15
- 评论区 @ 太频繁可能被风控，`owner_comment_rate` 建议 0.2~0.4
- 每次转写返回的字数可能被截断（上游 ASR 行为），长视频的转写不完整

## 许可

MIT。

工具部分移植自 [AGI_Ananas/bilibili-mcp](https://github.com/AGI_Ananas/bilibili-mcp)（MIT），经 [dsh-bili-scroll](https://github.com/c1overzz/dsh-bili-scroll) vendor。
