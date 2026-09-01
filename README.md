# 群聊贴表情（group-emoji-react）

让麦麦在**群聊**里给消息贴表情。通过 Napcat 的 `set_msg_emoji_like` 接口实现，由 LLM 从 30 个内置表情中挑一个最合适的。

表情会显示在目标消息下方（QQ 的"表情回应"），**不发送任何文字**，不会打断群聊节奏。

## 功能

- **麦麦主动贴**：群聊里有新消息时，按概率 + 冷却旁路贴表情，不拦截麦麦的正常回复流程。
- **LLM 主动贴**：麦麦自己判断"这时候该贴个表情"，调用工具贴上。
- **自检命令**：一条命令确认 Napcat 连通性和当前生效配置。

## 前置条件

1. **Napcat**：本插件通过 Napcat HTTP API 贴表情，必须先在 Napcat WebUI → **网络配置** → 添加一个 **HTTP 服务器**（Host 建议 `0.0.0.0`，端口自定，如 `9999`）。
2. **模型**：`tool_use` 模型要有基本的情商，太弱的模型选表情会很怪。
3. 仅群聊生效，私聊不触发。

## 安装

1. 把 `group-emoji-react` 整个文件夹放到 MaiBot 的 `plugins/` 目录下。
2. 重启 MaiBot，插件目录会自动生成 `config.toml`。
3. 编辑 `config.toml`，填好 Napcat 的 `host` / `port` / `token`（可参考 `config.example.toml`）。
4. **重启 MaiBot**（改 `capabilities` 必须重启，热重载不生效）。
5. 在群里发 `/表情测试`，返回"已连上 Napcat"即装好。

## 命令表

| 命令 | 作用 |
|---|---|
| `/表情测试`、`/贴表情测试`、`/reacttest` | 检查 Napcat 连通性与当前生效配置（不回显 Token） |

## 配置表

| 配置段 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| `plugin` | `enabled` | `true` | 插件总开关 |
| `plugin` | `config_version` | `1.0.0` | 配置版本，升级时用于判断是否迁移 |
| `napcat` | `host` | `127.0.0.1` | Napcat 地址；Docker 部署填容器名（如 `napcat`） |
| `napcat` | `port` | `9999` | Napcat HTTP 服务端口 |
| `napcat` | `token` | 空 | Napcat 认证 Token，没设置就留空 |
| `napcat` | `llm_task` | `planner` | 选表情用的任务/模型（`planner`/`replyer`/`utils`/`tool_use`），留空用默认模型 |
| `napcat` | `timeout_seconds` | `10` | 单次 Napcat 请求超时（秒） |
| `proactive` | `enabled` | `true` | 是否开启"麦麦主动贴表情" |
| `proactive` | `chance` | `0.35` | 普通消息触发概率（0.0-1.0） |
| `proactive` | `keyword_chance` | `0.75` | 命中明显适合回应的关键词时的概率 |
| `proactive` | `cooldown_seconds` | `180` | 同一聊天流的冷却秒数，防刷屏 |
| `proactive` | `min_text_length` | `2` | 短于此长度的消息不触发 |
| `proactive` | `skip_self_messages` | `true` | 跳过麦麦自己发的消息 |
| `proactive` | `llm_enabled` | `true` | 主动贴表情是否用 LLM 选表情；`false` 走内置关键词规则（毫秒级、零 LLM 开销） |
| `proactive` | `llm_timeout_ms` | `6000` | 主动路径 LLM 选表情超时（毫秒），超时回退关键词规则；`0` 不限制 |

## 权限/能力说明

`_manifest.json` 只声明了实际用到的三个能力：

| 能力 | 用途 |
|---|---|
| `llm.generate` | 让模型挑选合适的表情 |
| `message.get_recent` | 取最近聊天记录，给模型当上下文、拿目标消息 ID |
| `send.text` | 仅用于 `/表情测试` 回执 |

无 Python 第三方依赖（只用标准库 `http.client`）。

## 故障排查表

| 现象 | 原因 | 处理 |
|---|---|---|
| `/表情测试` 提示连不上 Napcat | Napcat 没开 HTTP 服务，或 host/port 填错 | Napcat WebUI → 网络配置 → 添加 HTTP 服务器；Docker 部署 host 填容器名 |
| 插件加载失败，日志说能力未授权 | `capabilities` 名写错或没重启 | 用点分精确名（本插件已配好），**改完必须重启 MaiBot** |
| 一直不贴表情 | 主动贴概率未命中 / 在冷却中 / 消息太短 | 调 `chance`、`cooldown_seconds`、`min_text_length`；确认是群聊 |
| 提示"LLM 返回了不可用的表情 ID" | 模型乱选 ID | 属正常保护：非法 ID 不会发给 Napcat。换更强的 `llm_task` 模型 |
| 配置改了没生效 | WebUI 改配置只写 `config.toml`，不推送给运行中的插件 | **完整重启 MaiBot** |
| 麦麦给自己贴表情 | 自身消息识别失败 | 保持 `skip_self_messages = true`；仍发生则看日志里的 user_id/bot_id |
| 贴表情慢（十几秒才贴上） | 主动路径 LLM 选表情太慢（真机见过 19.4s，`planner` 配了大模型） | 三选一：① `proactive.llm_enabled = false` 纯规则；② 调小 `proactive.llm_timeout_ms`（超时自动回退规则）；③ `napcat.llm_task` 换快模型 |
| 日志里出现 `decision=rule` | LLM 超时/异常/被关闭后走了关键词规则 | 属预期回退行为，表情仍会贴上；想全走 LLM 就加大 `llm_timeout_ms` 或关掉回退 |
| 设了 `llm_timeout_ms` 但日志里 LLM 仍耗时十几秒且 `decision=llm` | v1.1.0 的 bug：回退依据取自"最近消息里查目标消息"，真机上查不到时依据为空，超时包装被跳过 | **v1.1.1 已修**：回退依据改为 hook 入站时的消息原文，升级后即可生效 |
| 麦麦对同一意图既贴表情又发表情包图 | Planner 混淆了本插件的"贴表情回应"与 MaiBot 内置的"发表情包图片"工具 | v1.1.0 已在工具描述里明确二者区别；仍出现则属模型行为，可在提示词里引导 |

## 实现要点（改代码前必读）

- **入站监听用 hook，不要用 `EventHandler(EventType.ON_MESSAGE)`**：部分 MaiBot 版本里 `ON_MESSAGE` 的派发被注释掉，注册了也永远不触发。本插件用 `chat.receive.after_process`（`HookMode.OBSERVE` + `ErrorPolicy.SKIP`），只读不改写消息，插件出错也不会影响聊天。
- **`plugin.py` 不要写 `from __future__ import annotations`**：Runner 用 `spec_from_file_location` 加载且不注册进 `sys.modules`，注解会变成字符串，pydantic 解析配置模型会直接失败。
- **`http.client` 是阻塞的**：所有 Napcat 请求都包在 `asyncio.to_thread` 里，避免卡住插件 Runner 的事件循环。
- **去重 + 冷却**：以消息 ID 去重（hook 与事件监听可能对同一条消息各触发一次），按聊天流冷却，`on_unload` 会取消所有未完成的后台任务。
- 内部状态在 `__init__` 初始化而非 `on_load`，保证任何组件先于生命周期被触发时也不会崩。
- **主动路径低延迟设计**：LLM 选表情带 `llm_timeout_ms` 超时（默认 6s），超时/异常/关闭时回退到内置关键词规则（关键词 → 候选表情池随机），保证表情"跟得上"聊天节奏；工具路径（LLM 主动调用）不回退，保持 LLM 决策纯度。

## 本地测试

```bash
cd MaiBot插件开发
.venv/Scripts/python.exe check_plugin.py plugins/group-emoji-react   # 结构自检
.venv/Scripts/python.exe smoke_test.py  plugins/group-emoji-react   # 生命周期冒烟
.venv/Scripts/python.exe plugins/group-emoji-react/test_react.py    # 行为回归（10 项，含 FakeHost 打桩）
```

`test_react.py` 不需要 MaiBot 也不需要真实 Napcat：用 FakeHost 模拟 LLM/消息/发送，并把 HTTP 调用换成录制桩，验证"入站消息 → 提取字段 → 选表情 → 调 Napcat"主链路、私聊跳过、重复消息去重、非法表情拦截、LLM 关闭/超时/异常三种回退场景、自检命令不泄露 Token。

## 许可证

MIT（与 `_manifest.json` 的 `license` 一致）

参考实现：[DavidBlackCN/maibot-message-react-plugin](https://github.com/DavidBlackCN/maibot-message-react-plugin)
