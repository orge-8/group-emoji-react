# 群聊贴表情（group-emoji-react）

让麦麦在**群聊**里给消息贴表情，由 LLM 从 32 个内置表情中挑一个最合适的。

表情会显示在目标消息下方（QQ 的"表情回应"），**不发送任何文字**，不会打断群聊节奏。

支持两条发送通道，默认 `auto` 自动择优：

1. **QQ 适配器通道（推荐）**：调用 [MaiBot-SnowLuma-Adapter](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter) 统一 QQ 连接器暴露的插件 API（`adapter.napcat.message.set_msg_emoji_like` / `adapter.snowluma.message.set_msg_emoji_like`）。**不需要额外开 Napcat HTTP 服务**，与 MaiBot 1.3.0 + 适配器部署方式天然契合。
2. **直连 Napcat HTTP 通道**：走 Napcat WebUI 里开的 HTTP 服务器，兼容老部署。

## 功能

- **麦麦主动贴**：群聊里有新消息时，按概率 + 冷却旁路贴表情，不拦截麦麦的正常回复流程。
- **LLM 主动贴**：麦麦自己判断"这时候该贴个表情"，调用工具贴上。
- **上下文判断（v1.4.0）**：不是"把最近 N 条原样丢给 LLM"，而是先把上下文收敛一遍再选——以目标消息为中心截窗口、裁掉已经换话题的旧消息、同一人连发的碎句子并成一行、纯图片/表情消息标注类型，并优先用入站原文补上目标正文。
- **自检命令**：一条命令确认通道连通性、当前生效配置与表情表规模。

## 前置条件

1. **QQ 连接器**（二选一）：
   - 装了 **MaiBot-SnowLuma-Adapter** 适配器插件（推荐，`transport = "auto"` 会自动发现并使用它）；
   - 或给 Napcat 开一个 **HTTP 服务器**（Napcat WebUI → 网络配置 → Host 建议 `0.0.0.0`，端口自定如 `9999`），并把 `transport` 设为 `http`。
2. **模型**：选表情的模型要有基本的情商，太弱的模型选出来会很怪。
3. 仅群聊生效，私聊不触发。

## 安装

1. 把 `group-emoji-react` 整个文件夹放到 MaiBot 的 `plugins/` 目录下。
2. 重启 MaiBot，插件目录会自动生成 `config.toml`。
3. 编辑 `config.toml`（可参考 `config.example.toml`）：
   - 走适配器：`transport` 保持 `"auto"` 即可，无需填 host/port；
   - 走直连 HTTP：`transport = "http"`，填好 Napcat 的 `host` / `port` / `token`。
4. **重启 MaiBot**（改 `capabilities` 必须重启，热重载不生效）。
5. 在群里发 `/表情测试`，回执里会说明当前走的是哪条通道。

## 命令表

| 命令 | 作用 |
|---|---|
| `/表情测试`、`/贴表情测试`、`/reacttest` | 检查通道连通性、当前生效配置与表情表规模（不回显 Token） |

## 配置表

| 配置段 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `plugin` | `enabled` | `true` | 插件总开关 |
| `plugin` | `config_version` | `1.0.0` | 配置版本，升级时用于判断是否迁移 |
| `napcat` | `transport` | `auto` | 发送通道：`auto` 优先适配器、失败自动回落 HTTP；`adapter` 只用适配器；`http` 只用直连 HTTP |
| `napcat` | `host` | `127.0.0.1` | Napcat 地址（仅 http/auto 通道用）；Docker 部署填容器名（如 `napcat`） |
| `napcat` | `port` | `9999` | Napcat HTTP 服务端口（仅 http/auto 通道用） |
| `napcat` | `token` | 空 | Napcat 认证 Token，以 `Authorization: Bearer <token>` 发送；没设置就留空 |
| `napcat` | `llm_task` | `utils` | 选表情用的任务名（如 `utils`/`planner`/`replyer`/`tool_use`），留空用默认模型 |
| `napcat` | `timeout_seconds` | `10` | 单次 Napcat HTTP 请求超时（秒） |
| `proactive` | `enabled` | `true` | 是否开启"麦麦主动贴表情" |
| `proactive` | `chance` | `0.35` | 普通消息触发概率（0.0-1.0） |
| `proactive` | `keyword_chance` | `0.75` | 命中明显适合回应的关键词时的概率 |
| `proactive` | `cooldown_seconds` | `180` | 同一聊天流的冷却秒数，防刷屏 |
| `proactive` | `min_text_length` | `2` | 短于此长度的消息不触发 |
| `proactive` | `skip_self_messages` | `true` | 跳过麦麦自己发的消息 |
| `proactive` | `rule_fallback` | `false` | LLM 选表情失败（超时/异常/返回非法）时是否回退内置关键词规则；`false` 直接跳过本次贴表情 |
| `proactive` | `llm_timeout_ms` | `6000` | 主动路径 LLM 选表情超时（毫秒），超时按 `rule_fallback` 决定回退或跳过；`0` 不限制 |
| `context` | `before_count` | `6` | 目标消息「之前」纳入上下文的条数（语气、话题、梗都在前文） |
| `context` | `after_count` | `2` | 目标消息「之后」纳入上下文的条数 |
| `context` | `max_age_seconds` | `300` | 上下文与目标消息的时间差上限（秒），超出视为另一个话题；`0` 不限 |
| `context` | `max_text_length` | `60` | 上下文单条消息的最大文本长度 |
| `context` | `merge_same_sender` | `true` | 同一人连发的多条是否并成一行 |
| `context` | `skip_when_no_content` | `true` | 目标内容 + 上下文都拿不到时是否直接跳过（不浪费一次 LLM） |
| `context` | `allow_skip` | `false` | 是否允许 LLM 判断"这条不值得贴"而跳过；`false` 沿用旧行为——无论如何都挑一个 |

## 权限/能力说明

`_manifest.json` 声明了实际用到的五个能力：

| 能力 | 用途 |
|---|---|
| `api.call` | 经适配器插件调用 `adapter.napcat.*` / `adapter.snowluma.*` 贴表情 |
| `api.list` | 探测当前可见的插件 API，判断适配器通道是否就绪 |
| `llm.generate` | 让模型挑选合适的表情 |
| `message.get_recent` | 取最近聊天记录，给模型当上下文、拿目标消息 ID |
| `send.text` | 仅用于 `/表情测试` 回执 |

无 Python 第三方依赖（只用标准库 `http.client`）。

## 故障排查表

| 现象 | 原因 | 处理 |
|---|---|---|
| 自检提示"api.list() 无结果，且探针均失败" | 没装 QQ 适配器插件，或调试期 `capabilities` 没生效 | 装 [MaiBot-SnowLuma-Adapter](https://github.com/Mai-with-u/MaiBot-SnowLuma-Adapter)；改完 `capabilities` **必须完整重启 MaiBot** |
| 自检提示"已列出 N 个插件 API，但没有适配器的 set_msg_emoji_like" | 装的是别的适配器，或适配器版本过旧没暴露该 API | 确认为统一 QQ 连接器适配器（SnowLuma / NapCat），升级到含 `message.set_msg_emoji_like` 的版本 |
| `/表情测试` 提示连不上 Napcat | 走 http 通道但 Napcat 没开 HTTP 服务，或 host/port 填错 | 二选一：改用 `transport = "auto"` 走适配器；或确认 Napcat HTTP 服务器已开、Docker 部署 host 填容器名 |
| 贴表情返回 401 / 鉴权失败 | Napcat 设了 Token 但插件没填或填错 | 在 `napcat.token` 填入 Token（插件会自动加 `Bearer ` 前缀，不要手写） |
| 插件加载失败，日志说能力未授权 | `capabilities` 名写错或没重启 | 用点分精确名（本插件已配好），**改完必须重启 MaiBot** |
| 一直不贴表情 | 主动贴概率未命中 / 在冷却中 / 消息太短 | 调 `chance`、`cooldown_seconds`、`min_text_length`；确认是群聊 |
| 提示"LLM 返回了不可用的表情 ID" | 模型乱选 ID | 属正常保护：非法 ID 不会发出去。换更强的 `llm_task` 模型 |
| 配置改了没生效 | WebUI 改配置只写 `config.toml`，不推送给运行中的插件 | **完整重启 MaiBot**（通道探测结果也会在配置更新时重置） |
| 麦麦给自己贴表情 | 自身消息识别失败 | 保持 `skip_self_messages = true`；仍发生则看日志里的 user_id/bot_id |
| 贴表情慢（十几秒才贴上） | 主动路径 LLM 选表情太慢（真机见过 19.4s） | 三选一：① 调小 `proactive.llm_timeout_ms`；② `rule_fallback = true` 让超时后走规则兜底；③ `napcat.llm_task` 换快模型 |
| 日志里出现 `decision=rule` | `rule_fallback = true` 且 LLM 超时/异常/返回非法，走了关键词规则 | 属预期回退行为，表情仍会贴上；想全走 LLM 就改回 `rule_fallback = false` |
| 日志出现"选表情失败: LLM 调用超时"且未贴表情 | `rule_fallback = false`（默认）且 LLM 超时，直接跳过本次 | 属预期行为（宁缺毋滥）；想要兜底就设 `rule_fallback = true` |
| 设了 `llm_timeout_ms` 但日志里 LLM 仍耗时十几秒且 `decision=llm` | v1.1.0 的 bug：回退依据取自"最近消息里查目标消息"，真机上查不到时依据为空 | **v1.1.1 已修**：回退依据改为 hook 入站时的消息原文，升级后即可生效 |
| 麦麦对同一意图既贴表情又发表情包图 | Planner 混淆了本插件的"贴表情回应"与 MaiBot 内置的"发表情包图片"工具 | v1.1.0 已在工具描述里明确二者区别；仍出现则属模型行为，可在提示词里引导 |

## 实现要点（改代码前必读）

### 发送通道（v1.3.0 新增，本次重写核心）

- **为什么要有两条通道**：插件原先只支持直连 Napcat HTTP，而 MaiBot 1.3.0 的推荐部署是装**统一 QQ 连接器适配器**——适配器自己连 QQ，插件之间通过 Host 转发互调。此时插件再去连一个并不存在的 HTTP 端口，就必然失败。
- **`transport` 三态**：`auto`（默认，适配器优先、失败回落 HTTP、并记住上次成功的通道下次先用）/ `adapter`（只用适配器）/ `http`（只用直连）。
- **前缀自动择一**：`_ADAPTER_PREFIXES = ("adapter.napcat", "adapter.snowluma")`。两者**共享同一个处理器**，用哪个前缀都行。插件先调一次只读的 `api.list()` 拿全部可见 API 名做权威判断；list 拿不到东西时才退化为用无害的 `system.get_version_info` 逐前缀试探。结论会缓存，不必每次贴表情都探。
- **`api.list()` 有结果但里面没有适配器 API** 时，结论是"权威不可用"，直接给出指向适配器的可操作原因，**不再回退 HTTP**——避免把"装错适配器"误诊成"网络问题"。
- **`version="1"` 是适配器注册版本**：个别宿主对 `version` 参数处理不同，先带 `version="1"` 调，失败再退回不带 version 重试一次。

### 上下文判断（v1.4.0 新增）

旧实现是把 `get_recent` 的结果取前 10 条原样塞进 prompt。看着"有上下文"，其实有三处会静默选错：

- **窗口取错了**：`ctx.message.get_recent` 的**返回顺序没有约定**（可能旧→新也可能新→旧）。固定取前 N 条在真机上很容易取到"几分钟前另一个话题"的消息，而且**不报错**，表现只是"选得不太对"，最难排查。现在改成**以目标消息为中心**截窗口（前 `before_count` 条 / 后 `after_count` 条），先按时间戳归一成旧→新（任一条缺时间戳就保持宿主原顺序、不瞎猜）。
- **目标正文是空的**：真机上 `get_recent` **常常查不到刚入站的那条消息**（还没落库），旧实现只用查询结果，于是 prompt 里"内容:"是空的，LLM 只能瞎猜。现在**优先用入站 hook 拿到的原文**兜底。
- **非文本消息被当成"什么都没说"**：`processed_plain_text` 对图片/表情只留一个占位标记（如 `[图片]`），纯图片消息正文就是空的。现在会标注类型（`图片` / `表情+文字` / …），LLM 才知道该怎么反应。

另外两处收敛：**按时间差裁掉已换话题的旧消息**（`max_age_seconds`），**同一人连发的碎句子并成一行**（QQ 里很常见，否则 6 条上下文里 4 条是同一个人拆着发的，真正的话题信息被挤掉）。

可选的"不值得就别贴"：`context.allow_skip = true` 时 LLM 可以返回 `{"skip": true}`，插件就不贴（默认 `false`，沿用"无论如何都挑一个"的旧行为）。注意 skip 判据要走 `_truthy()` 显式枚举真值——只判 `bool(value)` 会把字符串 `"false"` 当成真，让开了开关的插件几乎不再贴任何表情。

### 表情 ID 表（v1.3.0 重建）

- **原表是错的**：旧表基于一套偏移过的编号，与 QQ 客户端实际渲染的 `emojiId` 对不上——LLM 选"赞"、QQ 却渲染成别的表情。这与适配器 v1.0.3 修的"ID 错配"是同一类问题。
- **现表按 QQNT `emojiId` 重建**（koishi QFace `_index.json` 口径，32 项），例如 `76:"赞"`、`66:"爱心"`、`63:"玫瑰"`、`182:"笑哭"`、`144:"喝彩"`、`13:"呲牙"`、`20:"偷笑"`、`5:"流泪"`、`9:"大哭"`、`106:"委屈"`、`111:"可怜"`、`175:"卖萌"`、`187:"幽灵"`、`212:"托腮"`、`265:"辣眼睛"`、`267:"头秃"`、`277:"汪汪"`、`285:"摸鱼"`、`307:"喵喵"`、`311:"打call"`、`323:"嫌弃"`、`326:"生气"`、`344:"大怨种"`、`350:"贴贴"`、`357:"裂开"`、`380:"真棒"`、`387:"太好笑"`、`389:"太赞了"`、`425:"求放过"`、`428:"收到"`、`449:"+1"`、`462:"无语"`。
- 表内另有 `_QQNT_FACE_NAMES` 校验字典与回归用例 `test_emoji_table_matches_qqnt`，防止以后手抖再改歪。

### LLM 双层超时（v1.3.0 修正）

- **坑**：SDK 的 `ctx.llm.generate` **不转发** `timeout_ms`。只靠它，超时控制形同虚设。
- **正解**：改用 `ctx.call_capability("llm.generate", timeout_ms=..., ...)` 显式传外层 RPC 超时，再叠一层 `asyncio.wait_for` 做内层业务超时。
- **外层必须比内层宽**（`_LLM_RPC_TIMEOUT_SLACK_MS = 2000`）：外层 RPC 超时是"整条模型回退链"的总预算，若它先于内层触发，抛出的是 Runner 的 `RPCError`，会被误报成"选表情异常"，把排查方向带偏。`llm_timeout_ms = 0`（不限制）时给一个宽松上界（`_LLM_RPC_TIMEOUT_NO_LIMIT_MS = 180000`），而不是落到 SDK 默认的 30s。
- **`_is_timeout()` 三取一**：`isinstance` / 类名含 `timeout` / 文本命中 `timeout`｜`timed out`｜`超时`。真机超时抛的 `RPCError` 是 msgpack 重建的类，与本地测试里的**不是同一个类对象**，且 `from None` 掐掉了 `__cause__`，只用 `isinstance` 必然漏判。注意 `"Request timed out."` **不含** `"timeout"` 子串，所以必须同时匹配 `"timed out"`。

### 其它既有约束

- **入站监听用 hook，不要用 `EventHandler(EventType.ON_MESSAGE)`**：部分 MaiBot 版本里 `ON_MESSAGE` 的派发被注释掉，注册了也永远不触发。本插件用 `chat.receive.after_process`（`HookMode.OBSERVE` + `ErrorPolicy.SKIP`），只读不改写消息，插件出错也不会影响聊天。
- **`plugin.py` 不要写 `from __future__ import annotations`**：Runner 用 `spec_from_file_location` 加载且不注册进 `sys.modules`，注解会变成字符串，pydantic 解析配置模型会直接失败。
- **`http.client` 是阻塞的**：所有 Napcat 请求都包在 `asyncio.to_thread` 里，避免卡住插件 Runner 的事件循环。
- **能力调用写成字面量 `self.ctx.api.list()` / `self.ctx.api.call(...)`**：静态门禁靠正则扫描 `self.ctx.<dotted>(` 核对 manifest 能力声明；若经 `getattr` 间接调用会被判成"声明了但未用到"。兼容性守卫仍用 `getattr(..., None)` 做，不影响老 SDK 降级。
- **去重 + 冷却**：以消息 ID 去重（hook 与事件监听可能对同一条消息各触发一次），按聊天流冷却，`on_unload` 会取消所有未完成的后台任务。
- 内部状态在 `__init__` 初始化而非 `on_load`，保证任何组件先于生命周期被触发时也不会崩。
- **单次 `get_recent` 复用（v1.2.1）**：一次贴表情只拉一次最近消息（limit=20），目标消息提取与 prompt 上下文（前 10 条）共用同一份结果，不再连发两次 RPC。
- **去重滑动窗口（v1.2.1）**：已贴消息 ID 用 `deque(maxlen=1000)` + `set` 维护，满了挤掉最旧一条，不再到顶整体 clear 导致去重瞬间全失效。
- **冷却字典惰性淘汰（v1.2.1）**：`_proactive_last_react_at` 超过 512 条时先删过期条目、仍超则按时间保留最新 512 条，避免随群数无界增长。
- **LLM 是唯一选表情路径**：主动路径调 LLM 带 `llm_timeout_ms` 超时（默认 6s）。`rule_fallback = true` 时超时/异常/非法回退到内置关键词规则（关键词 → 候选表情池随机），`false`（默认）时超时/失败直接跳过本次贴表情（宁缺毋滥）；工具路径（LLM 主动调用）永不回退，保持 LLM 决策纯度。
- **v1.2.0 配置变更**：`llm_enabled` 已移除（LLM 成为唯一路径）；旧 config.toml 里残留的 `llm_enabled` 字段会被自动忽略，可删可留。

## 本地测试

```bash
cd C:/Users/38160/Desktop/tools/maibot-devkit
.venv/Scripts/python.exe check_plugin.py --plugin C:/Users/38160/Desktop/group-emoji-react   # 结构自检
.venv/Scripts/python.exe run_gates.py   --plugin C:/Users/38160/Desktop/group-emoji-react   # 完整门禁
.venv/Scripts/python.exe C:/Users/38160/Desktop/group-emoji-react/test_react.py             # 行为回归（25 项）
```

`test_react.py` 不需要 MaiBot、也不需要真实 Napcat：用 FakeHost 模拟 `ctx.llm` / `ctx.message` / `ctx.send` / `ctx.api`，并把 HTTP 调用换成录制桩。覆盖主链路（入站消息 → 提取字段 → 选表情 → 贴表情）、私聊跳过、重复消息去重、非法表情拦截、LLM 超时/异常回退场景、自检不泄露 Token，以及 v1.3.0 新增的：适配器通道贴表情、前缀自动择一、auto 回落（业务失败 / 适配器异常两条路径）、adapter-only 报错清晰度、`api.list()` 判定、`_is_timeout` 对真机 `RPCError` 的识别、HTTP Token Bearer 方案、表情表与 QQNT 一致性、双层超时宽度，以及 v1.4.0 新增的：上下文窗口以目标为中心（正序/逆序输入都对）、超时旧话题裁剪、同一人连发合并、消息类型标注、目标正文回退入站原文、信息不足时跳过、`allow_skip` 生效与 `"false"` 不被误判、已贴表情进上下文、配置对象缺 `context` 节时的兜底。

## 许可证

MIT（与 `_manifest.json` 的 `license` 一致）

参考实现：[DavidBlackCN/maibot-message-react-plugin](https://github.com/DavidBlackCN/maibot-message-react-plugin)
