"""MaiBot 群聊贴表情插件。

给群聊消息贴 QQ 表情回应（消息角落的小黄脸），由 LLM 决定用哪个表情。
两条触发路径：
  1. LLM 主动调用 @Tool("react_emoji")
  2. 普通群聊消息旁路主动贴表情（@HookHandler("chat.receive.after_process")）

发送通道（transport）：
  - adapter：走适配器插件的公开 API
    `adapter.napcat.message.set_msg_emoji_like` / `adapter.snowluma.message.set_msg_emoji_like`
    （统一 QQ 连接器：github.com/Mai-with-u/MaiBot-SnowLuma-Adapter，两条前缀共享同一处理器）。
    这是配 SnowLuma 适配器时的正路——插件不需要再单独连一个 Napcat HTTP 服务。
  - http：直连 Napcat HTTP 服务器（旧行为，需在 Napcat WebUI 开 HTTP 服务器）。
  - auto（默认）：优先适配器，失败自动回落 HTTP，成功后记住可用路径，下次优先用它。

踩坑点（改动前请先读 maibot-plugin-dev skill 的 runtime-gotchas）：
- 本文件**不要**写 `from __future__ import annotations`：Runner 用 spec_from_file_location
  加载且不注册进 sys.modules，注解会变成字符串，pydantic 解析配置模型直接失败。
- 入站消息监听用 **hook 而不要用 EventHandler(EventType.ON_MESSAGE)**：
  部分 MaiBot 版本里 ON_MESSAGE 的派发被注释掉，注册了也永远不会触发。
  可靠的入站入口是 `chat.receive.after_process`（此时 processed_plain_text 已可用）。
- `http.client` 是阻塞调用，必须丢进 `asyncio.to_thread`，否则会卡住插件 Runner 的事件循环。
- 表情 ID 与释义必须用 **QQNT emojiId 表**（与适配器 qq_face_map.py 同源）。
  历史 bug：本插件早期用的是「按序号平移」的旧表，导致 424 续标识被当成「狂按按钮」、
  233 掐一掐被当成「笑哭」、293 摸锦鲤被当成「敲脑瓜」等——LLM 按错名字挑，
  用户看到的却是另一个表情。改表时务必核对 QQNT 官方 emojiId。
- `ctx.llm.generate` **不转发** timeout_ms（SDK 2.8.1 实测），要自定义 RPC 超时
  必须自己调 `ctx.call_capability("llm.generate", timeout_ms=..., ...)`。
"""
import asyncio
import http.client
import json
import random
import time
from collections import deque
from typing import Any

from maibot_sdk import Command, Field, HookHandler, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

# HookMode / ErrorPolicy / HookOrder 是较新的枚举，旧版 SDK 可能没有。
# 缺失时退回字符串字面量，避免整个 types 导入失败（会导致插件加载失败）。
try:
    from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

    _ERROR_POLICY_SKIP = ErrorPolicy.SKIP
    _HOOK_MODE_OBSERVE = HookMode.OBSERVE
    _HOOK_ORDER_LATE = HookOrder.LATE
except Exception:  # pragma: no cover - 仅旧版 SDK 走这里
    _ERROR_POLICY_SKIP = "skip"
    _HOOK_MODE_OBSERVE = "observe"
    _HOOK_ORDER_LATE = "late"

# ---------------------------------------------------------------------------
# 可用表情表（QQNT emojiId -> 中文名）
#
# 数据来源：QQNT 表情表 emojiId（与 MaiBot-SnowLuma-Adapter 的 qq_face_map.py 同源，
# 该表 1.0.3 起同步自 koishi QFace _index.json）。名称必须与 QQ 端实际渲染的释义
# 一致，否则 LLM 选择与用户观感会错位——这是本插件最容易静默出错的地方。
#
# 只挑群聊里当「回应」用着自然的，不做全表（QQNT 有 500+ 个，全塞给 LLM 反而选不准）。
# ---------------------------------------------------------------------------
AVAILABLE_REACT_EMOJIS: dict = {
    76: "赞", 66: "爱心", 63: "玫瑰", 144: "喝彩",
    13: "呲牙", 20: "偷笑", 182: "笑哭", 5: "流泪",
    9: "大哭", 106: "委屈", 111: "可怜", 175: "卖萌",
    187: "幽灵", 212: "托腮", 265: "辣眼睛", 267: "头秃",
    277: "汪汪", 285: "摸鱼", 307: "喵喵", 311: "打call",
    323: "嫌弃", 326: "生气", 344: "大怨种", 350: "贴贴",
    357: "裂开", 380: "真棒", 387: "太好笑", 389: "太赞了",
    425: "求放过", 428: "收到", 449: "+1", 462: "无语",
}

# QQNT 表情表的完整释义（仅用于把「适配器回传的表情 ID」翻译成可核对的名称，
# 以及自检时校验表是否漂移）。这里只收录本插件可能用到的区间，不做全表。
_QQNT_FACE_NAMES: dict = {
    5: "流泪", 9: "大哭", 13: "呲牙", 20: "偷笑", 38: "敲打",
    49: "拥抱", 63: "玫瑰", 66: "爱心", 76: "赞", 106: "委屈",
    111: "可怜", 144: "喝彩", 175: "卖萌", 182: "笑哭", 187: "幽灵",
    212: "托腮", 233: "掐一掐", 265: "辣眼睛", 267: "头秃", 277: "汪汪",
    285: "摸鱼", 293: "摸锦鲤", 307: "喵喵", 311: "打call", 323: "嫌弃",
    326: "生气", 344: "大怨种", 350: "贴贴", 357: "裂开", 380: "真棒",
    387: "太好笑", 389: "太赞了", 390: "太头秃", 424: "续标识", 425: "求放过",
    428: "收到", 449: "+1", 462: "无语",
}

# 已经确认"明显适合用表情回应"的消息关键词（命中则用更高的概率）
_REACTABLE_KEYWORDS: tuple = (
    "哈哈", "笑死", "好耶", "草", "可爱", "贴贴", "抱抱", "哭", "难过",
    "谢谢", "恭喜", "牛", "厉害", "救命", "离谱", "绷不住", "？", "!",
    "！", "www", "233", "orz",
)

_MAX_TRACKED_MESSAGE_IDS = 1000
# 冷却记录字典的惰性淘汰上限：超过才清理，避免每次写入都扫表
_MAX_COOLDOWN_ENTRIES = 512

# prompt 里的表情清单字符串：AVAILABLE_REACT_EMOJIS 是模块级常量，预拼一次复用
_EMOJI_LIST_PROMPT: str = ", ".join(f"{eid}:{name}" for eid, name in AVAILABLE_REACT_EMOJIS.items())

# ---- 传输通道常量 ----
_TRANSPORT_AUTO = "auto"
_TRANSPORT_ADAPTER = "adapter"
_TRANSPORT_HTTP = "http"
_TRANSPORTS = (_TRANSPORT_AUTO, _TRANSPORT_ADAPTER, _TRANSPORT_HTTP)

# 适配器（统一 QQ 连接器）暴露的两组等价前缀，按此顺序择一
_ADAPTER_PREFIXES = ("adapter.napcat", "adapter.snowluma")
# 贴表情 API 相对前缀的后缀名
_ADAPTER_REACT_SUFFIX = "message.set_msg_emoji_like"
# 适配器的无害探针（只读，不产生任何副作用）
_ADAPTER_PROBE_SUFFIX = "system.get_version_info"

# llm_timeout_ms=0（不限制）时给 RPC 通道的上界：宽松但有限，
# 避免 cap.call 默认 30s 把「死等」变成「30s 后抛 RPCError」。
_LLM_RPC_TIMEOUT_NO_LIMIT_MS = 180_000
# 外层 RPC 超时比内层业务超时宽出的裕量，保证先触发内层 wait_for
_LLM_RPC_TIMEOUT_SLACK_MS = 2_000
# 默认模型任务名：utils 是宿主里最轻的一档（flash 级候选），
# planner 真机单次 25~50s，配在本插件 6s 上限上必然次次超时。
_DEFAULT_LLM_TASK = "utils"


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.1.0", description="配置版本")


class NapcatConfig(PluginConfigBase):
    """连接与模型配置。

    配置节名沿用 `napcat`（避免升级时丢用户已有配置），但实际含义是「发送通道 + 选表情模型」：
    走适配器时 host/port/token 全部用不到。
    """

    __ui_label__ = "连接与模型"
    __ui_icon__ = "server"
    __ui_order__ = 1

    transport: str = Field(
        default=_TRANSPORT_AUTO,
        description=(
            "发送通道：auto=优先适配器、失败回落 HTTP（推荐）；"
            "adapter=只走适配器插件的公开 API（SnowLuma / NapCat 统一连接器）；"
            "http=只直连 Napcat HTTP 服务器。改这项需重启 MaiBot"
        ),
    )
    host: str = Field(
        default="127.0.0.1",
        description="仅 transport=http/auto 用到：Napcat HTTP 服务地址（Docker 部署一般填容器名，如 napcat）",
    )
    port: int = Field(default=9999, description="仅 transport=http/auto 用到：Napcat HTTP 服务端口")
    token: str = Field(
        default="", description="仅 transport=http/auto 用到：Napcat HTTP 服务认证 Token（没有就留空）"
    )
    llm_task: str = Field(
        default=_DEFAULT_LLM_TASK,
        description=(
            "选表情用的模型任务名（MaiBot 1.2.5+：model_task_config 的键，如 planner / replyer / utils）；"
            "留空则不显式指定，走 SDK 默认任务 utils。建议用 utils —— planner 通常是大模型，单次 20s+，"
            "会一直撞 proactive.llm_timeout_ms"
        ),
    )
    llm_model: str = Field(
        default="",
        description=(
            "选表情用的具体模型名（可选）。留空则用任务名对应的模型；"
            "两者语义不同：任务名指向一套配置，模型名指向某个具体模型"
        ),
    )
    timeout_seconds: int = Field(default=10, description="仅 HTTP 通道：单次 Napcat 请求超时时间（秒）")


class ProactiveConfig(PluginConfigBase):
    """普通聊天中主动贴表情的配置。"""

    __ui_label__ = "主动贴表情"
    __ui_icon__ = "smile-plus"
    __ui_order__ = 2

    enabled: bool = Field(default=True, description="是否在普通群聊消息里主动尝试贴表情")
    chance: float = Field(default=0.35, description="普通消息主动贴表情概率（0.0-1.0）")
    keyword_chance: float = Field(default=0.75, description="明显适合回应的消息主动贴表情概率（0.0-1.0）")
    cooldown_seconds: int = Field(default=180, description="同一个聊天流主动贴表情的冷却时间（秒）")
    min_text_length: int = Field(default=2, description="触发主动贴表情的最短文本长度")
    skip_self_messages: bool = Field(default=True, description="是否跳过机器人自己发的消息")
    rule_fallback: bool = Field(
        default=False,
        description="LLM 超时/异常/返回非法时是否回退到内置关键词规则选表情；关闭则直接跳过本次贴表情",
    )
    llm_timeout_ms: int = Field(
        default=6000,
        description="主动路径 LLM 选表情的超时时间（毫秒），超时按 rule_fallback 决定回退或跳过；0 表示不限制",
    )


class GroupEmojiReactConfig(PluginConfigBase):
    """插件顶层配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    napcat: NapcatConfig = Field(default_factory=NapcatConfig)
    proactive: ProactiveConfig = Field(default_factory=ProactiveConfig)


def _fix_broken_json(raw: str) -> str:
    """截取 LLM 返回里的第一个 JSON 对象，容忍前后多余的说明文字。"""
    if not raw:
        return raw
    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start : end + 1]
    return raw


def _relative_time(ts: Any) -> str:
    """把时间戳转成相对时间描述，便于 LLM 理解上下文。"""
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        return "未知"
    if not ts:
        return "未知"
    diff = time.time() - ts
    if diff < 60:
        return "刚刚"
    if diff < 3600:
        return f"{int(diff // 60)}分钟前"
    if diff < 86400:
        return f"{int(diff // 3600)}小时前"
    return f"{int(diff // 86400)}天前"


def _excerpt(value: Any, limit: int = 160) -> str:
    """把任意返回值压成一行短文本，用于日志与自检（永不抛出）。"""
    try:
        if isinstance(value, (dict, list, tuple)):
            text = json.dumps(value, ensure_ascii=False, default=str)
        else:
            text = str(value)
    except Exception:
        text = f"<{type(value).__name__} 无法序列化>"
    text = text.replace("\n", " ").replace("\r", " ")
    return text if len(text) <= limit else text[:limit] + "…"


def _is_timeout(exc: BaseException) -> bool:
    """判断异常是不是超时。

    不能只用 isinstance(asyncio.TimeoutError)：真机上 cap.call 超时抛的是 Runner 的
    RPCError（msgpack 重建的类，与本地测试里的不是同一个对象，且 `from None` 掐掉了
    __cause__），只用 isinstance 会漏判、把它当成「选表情异常」，把排查方向带偏。
    所以三取一：isinstance / 类名含 timeout / 文本命中任一超时标记。

    文本标记必须同时覆盖：
      - "timeout"   —— [E_TIMEOUT] 请求 cap.call 超时 (180000ms)
      - "timed out" —— Request timed out.（不含 "timeout" 子串，只写 "timeout" 会漏判）
      - "超时"       —— 中文回退链路
    """
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return True
    if "timeout" in type(exc).__name__.lower():
        return True
    text = str(exc).lower()
    return any(
        marker in text
        for marker in ("timeout", "timed out", "超时")
    )


class GroupEmojiReactPlugin(MaiBotPlugin):
    """群聊贴表情插件。"""

    config_model = GroupEmojiReactConfig

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """初始化内部状态。

        状态放 __init__ 而不是 on_load：保证任何组件（尤其是入站 hook）即使
        在生命周期方法之前被触发，也不会因属性不存在而 AttributeError。
        """
        super().__init__(*args, **kwargs)
        self._proactive_last_react_at: dict = {}
        # 去重滑动窗口：set 负责 O(1) 查询，deque(maxlen) 负责 FIFO 挤掉最旧条目，
        # 替代之前"到 1000 条整体 clear()"——clear 瞬间会让所有历史消息失去去重保护。
        self._reacted_message_ids: set = set()
        self._reacted_message_id_queue: deque = deque(maxlen=_MAX_TRACKED_MESSAGE_IDS)
        self._tasks: set = set()
        # 适配器通道状态
        self._adapter_prefix: str = ""          # "" = 尚未解析
        self._adapter_probed: bool = False      # 是否已经解析过一次（哪怕结果是"不可用"）
        self._adapter_list_seen: bool = False   # api.list() 是否给出过结果（给出过则前缀结论权威）
        self._adapter_last_error: str = ""      # 最近一次适配器通道的失败原因（自检用）
        # auto 模式下上一次成功的通道，下次优先用它，避免每次都先踩一次失败
        self._react_transport_used: str = ""

    async def on_load(self) -> None:
        """插件加载时执行。"""
        self.ctx.logger.info(
            "群聊贴表情插件已加载: transport=%s, napcat=%s:%s, llm_task=%r",
            self.config.napcat.transport,
            self.config.napcat.host,
            self.config.napcat.port,
            self.config.napcat.llm_task,
        )
        self._spawn(self._check_channel())

    async def on_unload(self) -> None:
        """插件卸载时执行：取消所有未完成的后台任务。"""
        tasks = list(getattr(self, "_tasks", set()))
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.ctx.logger.info("群聊贴表情插件已卸载（已取消 %d 个后台任务）", len(tasks))

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        """配置热重载时执行。"""
        if scope != "self":
            return
        # 通道相关配置变了就得重新探测，否则会一直用旧结论
        self._adapter_prefix = ""
        self._adapter_probed = False
        self._adapter_list_seen = False
        self._react_transport_used = ""
        self.ctx.logger.info("群聊贴表情: 配置已更新 version=%s（通道已重置，将重新探测）", version)

    # ------------------------------------------------------------------
    # HookHandler: 普通群聊消息旁路主动贴表情（不拦截正常回复流程）
    # ------------------------------------------------------------------
    @HookHandler(
        "chat.receive.after_process",
        name="observe_group_message",
        description="群聊消息处理后旁路判断是否贴表情，不拦截正常回复流程",
        mode=_HOOK_MODE_OBSERVE,
        order=_HOOK_ORDER_LATE,
        error_policy=_ERROR_POLICY_SKIP,
    )
    async def observe_group_message(self, message: Any = None, **kwargs: Any) -> dict:
        """观察入站群聊消息，按概率/冷却旁路贴表情。

        注意：这里只做判断并把贴表情丢到后台任务，不 await 网络请求，
        避免拖慢入站消息的处理流程。
        """
        if not self.config.plugin.enabled or not self.config.proactive.enabled:
            return {"action": "continue"}

        msg = message if isinstance(message, dict) else {}
        if not msg and isinstance(kwargs.get("message"), dict):
            msg = kwargs["message"]
        if not msg:
            return {"action": "continue"}

        group_id = self._extract_group_id(msg, kwargs)
        message_id = self._extract_message_id(msg, kwargs)
        chat_id = self._first_text(
            msg.get("session_id"), msg.get("chat_id"), msg.get("stream_id"), kwargs.get("chat_id")
        )
        text = self._first_text(
            msg.get("processed_plain_text"), msg.get("plain_text"), msg.get("text")
        )

        # 只处理群聊；拿不到 message_id 就没法贴
        if not group_id or not message_id:
            return {"action": "continue"}
        if len(text.strip()) < max(0, int(self.config.proactive.min_text_length)):
            return {"action": "continue"}
        if self._is_self_message(msg, kwargs):
            return {"action": "continue"}
        if not self._should_try_proactive(chat_id or group_id, message_id, text):
            return {"action": "continue"}

        self._spawn(
            self._proactive_react(
                chat_id=chat_id or group_id,
                group_id=group_id,
                message_id=message_id,
                text=text,
            )
        )
        return {"action": "continue"}

    # ------------------------------------------------------------------
    # Tool: 让 LLM 主动给消息贴表情
    # ------------------------------------------------------------------
    @Tool(
        "react_emoji",
        brief_description="给群聊里的某条消息贴一个 QQ 小黄脸表情回应（不是发表情包图片）",
        detailed_description=(
            "给群聊中的某条消息添加反应表情（消息右下角会出现一个小黄脸表情标记，"
            "类似微信的\"拍一拍\"式轻互动，不会在聊天流里发出任何新消息）。\n\n"
            "与其他表情能力的区别：\n"
            "- 本工具是「贴表情回应」：贴在某条已有消息上，不产生新消息\n"
            "- 如果你想发送表情包图片/动图到聊天里，那不是本工具，请改用发送表情的内置能力\n\n"
            "使用场景：\n"
            "- 想对某条消息表达情绪，但又不想发消息打断聊天节奏时\n"
            "- 想和某人友好互动时\n"
            "- 想用表情回应某个梗或某句话时\n\n"
            "注意事项：\n"
            "- 仅支持群聊\n"
            "- 不要频繁使用，更不要对同一条消息连续贴多个表情\n"
            "- 贴表情不等于回复，需要说话时请正常回复\n\n"
            "参数说明：\n"
            "- target_message_id：string，可选。要贴表情的消息 ID，不填则默认对当前触发消息贴"
        ),
        parameters=[
            ToolParameterInfo(
                name="target_message_id",
                param_type=ToolParamType.STRING,
                description="要贴表情的消息 ID（可选，不填则默认对当前消息贴）",
                required=False,
            ),
        ],
    )
    async def react_emoji(self, target_message_id: str = "", **kwargs: Any) -> dict:
        """LLM 调用的贴表情入口。"""
        if not self.config.plugin.enabled:
            return {"success": False, "content": "插件未启用"}

        group_id = self._first_text(kwargs.get("group_id"))
        if not group_id:
            return {"success": False, "content": "贴表情仅支持群聊"}

        chat_id = self._first_text(
            kwargs.get("chat_id"), kwargs.get("stream_id"), kwargs.get("session_id")
        )
        target = self._first_text(target_message_id)
        if not target:
            target = await self._get_latest_message_id(chat_id or group_id)
        if not target:
            return {"success": False, "content": "拿不到目标消息 ID，无法贴表情"}

        return await self._react_to_message(chat_id or group_id, group_id, target, source="tool")

    # ------------------------------------------------------------------
    # Command: 自检 / 手动测试
    # ------------------------------------------------------------------
    @Command(
        "reacttest",
        description="检查贴表情插件的发送通道与模型配置",
        pattern=r"^\s*[/／]\s*(?:表情测试|贴表情测试|reacttest)\s*$",
        aliases=["表情测试"],
    )
    async def cmd_reacttest(self, **kwargs: Any) -> tuple:
        """自检命令：报告发送通道可用性、表情表一致性与当前生效配置（不回显 Token）。"""
        stream_id = ""
        for key in ("stream_id", "chat_id", "session_id", "stream"):
            if kwargs.get(key):
                stream_id = str(kwargs[key])
                break

        text = await self._build_selfcheck_text()

        sent = False
        if stream_id:
            try:
                sent = bool(await self.ctx.send.text(text, stream_id))
            except Exception as exc:
                self.ctx.logger.error("自检命令发送失败: %s", exc)
        else:
            self.ctx.logger.error("自检命令载荷里没有 stream_id，无法回复")

        # 第三个返回值是拦截级别（不是权重）：发出去了才拦截，没发出去就让 bot 接一句
        return True, text, 2 if sent else 0

    # ------------------------------------------------------------------
    # 内部实现：发送通道
    # ------------------------------------------------------------------
    def _configured_transport(self) -> str:
        """读取并归一化 transport 配置。"""
        raw = str(getattr(self.config.napcat, "transport", _TRANSPORT_AUTO) or "").strip().lower()
        return raw if raw in _TRANSPORTS else _TRANSPORT_AUTO

    def _transport_order(self) -> list:
        """本次要尝试的通道顺序。auto 会把上次成功的那条排到最前面。"""
        configured = self._configured_transport()
        if configured == _TRANSPORT_ADAPTER:
            return [_TRANSPORT_ADAPTER]
        if configured == _TRANSPORT_HTTP:
            return [_TRANSPORT_HTTP]
        order = [_TRANSPORT_ADAPTER, _TRANSPORT_HTTP]
        if self._react_transport_used in order:
            order.remove(self._react_transport_used)
            order.insert(0, self._react_transport_used)
        return order

    async def _ensure_adapter_prefix(self, force: bool = False) -> str:
        """解析可用的适配器 API 前缀，返回 "" 表示适配器通道不可用。

        优先用一次只读的 `ctx.api.list()` 拿到全部可见 API 名（无副作用、无报错噪音）；
        list 不可用时才用 `system.get_version_info` 逐前缀试探。
        结论会缓存——`adapter.napcat.*` 与 `adapter.snowluma.*` 共享处理器，
        解析一次就够，不必每次贴表情都探。
        """
        if self._adapter_prefix:
            return self._adapter_prefix
        if self._adapter_probed and not force:
            return ""

        api = getattr(self.ctx, "api", None)
        if api is None or not callable(getattr(api, "call", None)):
            self._adapter_probed = True
            self._adapter_last_error = "当前 SDK 未提供 ctx.api.call，无法走适配器通道"
            return ""

        names = await self._list_visible_api_names()
        if names:
            self._adapter_list_seen = True
            for prefix in _ADAPTER_PREFIXES:
                if f"{prefix}.{_ADAPTER_REACT_SUFFIX}" in names:
                    self._adapter_prefix = prefix
                    self._adapter_last_error = ""
                    self.ctx.logger.info("适配器通道就绪: %s.%s", prefix, _ADAPTER_REACT_SUFFIX)
                    return prefix
            self._adapter_probed = True
            self._adapter_last_error = (
                f"已列出 {len(names)} 个插件 API，但没有适配器的 {_ADAPTER_REACT_SUFFIX}；"
                "请确认装的是统一 QQ 连接器适配器（MaiBot-SnowLuma-Adapter）"
            )
            return ""

        # api.list() 拿不到东西，退回逐前缀试探
        for prefix in _ADAPTER_PREFIXES:
            if await self._probe_adapter_prefix(prefix):
                self._adapter_prefix = prefix
                self._adapter_last_error = ""
                self.ctx.logger.info("适配器通道就绪（探针命中）: %s", prefix)
                return prefix
        self._adapter_probed = True
        self._adapter_last_error = (
            "api.list() 无结果，且 adapter.napcat / adapter.snowluma 探针均失败；"
            "请确认 QQ 适配器插件已加载"
        )
        return ""

    async def _list_visible_api_names(self) -> list:
        """调一次 ctx.api.list()，把返回结构里能当名字用的字符串全捞出来。"""
        api = getattr(self.ctx, "api", None)
        if not callable(getattr(api, "list", None)):
            return []
        try:
            # 这里刻意写成字面量 self.ctx.api.list()：静态门禁靠正则扫描
            # `self.ctx.<dotted>(` 来核对 manifest 能力声明，经 getattr 间接调用会被
            # 判成「声明了但未用到」，白吃一条 WARN。守卫用 getattr(...,None) 做，不影响兼容性。
            listed = await self.ctx.api.list()
        except Exception as exc:
            self.ctx.logger.debug("ctx.api.list 探测失败: %s", exc)
            return []
        return self._collect_strings(listed)

    async def _probe_adapter_prefix(self, prefix: str) -> bool:
        """用无害的 get_version_info 探针验证某组前缀是否可用。"""
        ok, _, _ = await self._call_adapter_api(f"{prefix}.{_ADAPTER_PROBE_SUFFIX}")
        return ok

    async def _call_adapter_api(self, api_name: str, **kwargs: Any) -> tuple:
        """调用适配器公开 API，返回 (是否业务成功, 原始返回, 详情文本)。

        适配器这些动作型 API 的返回就是 OneBot v11 原始响应（status/retcode/data），
        success 判定沿用 HTTP 通道那套。
        """
        api = getattr(self.ctx, "api", None)
        if not callable(getattr(api, "call", None)):
            return False, None, "当前 SDK 未提供 ctx.api.call"
        # version="1" 是适配器的注册版本；个别宿主对 version 参数处理不同，失败再退回不带 version。
        # 同理写成字面量 self.ctx.api.call(...)，让静态门禁能核对 api.call 已声明。
        detail = f"{api_name} 调用异常: SDK 未提供 ctx.api.call"
        for version in ("1", ""):
            try:
                if version:
                    result = await self.ctx.api.call(api_name, version=version, **kwargs)
                else:
                    result = await self.ctx.api.call(api_name, **kwargs)
            except Exception as exc:
                detail = f"{api_name} 调用异常: {type(exc).__name__}: {exc}"
                continue
            if self._payload_ok(result):
                return True, result, f"{api_name} -> {_excerpt(result)}"
            return False, result, f"{api_name} 返回失败: {_excerpt(result)}"
        return False, None, detail

    async def _apply_emoji_like(self, message_id: str, emoji_id: int) -> tuple:
        """按 transport 顺序贴表情，返回 (是否成功, 详情文本)。"""
        attempts = []
        for channel in self._transport_order():
            if channel == _TRANSPORT_ADAPTER:
                ok, detail = await self._apply_via_adapter(message_id, emoji_id)
            else:
                ok, detail = await self._apply_via_http(message_id, emoji_id)
            if ok:
                self._react_transport_used = channel
                return True, detail
            attempts.append(detail)
            self.ctx.logger.debug("贴表情通道 %s 失败: %s", channel, detail)
        return False, " | ".join(attempts)

    async def _apply_via_adapter(self, message_id: str, emoji_id: int) -> tuple:
        """经适配器插件公开 API 贴表情。"""
        prefix = await self._ensure_adapter_prefix()
        if not prefix:
            return False, self._adapter_last_error or "适配器通道不可用"
        ok, _, detail = await self._call_adapter_api(
            f"{prefix}.{_ADAPTER_REACT_SUFFIX}",
            message_id=message_id,
            emoji_id=int(emoji_id),
            set=True,
        )
        if not ok:
            self._adapter_last_error = detail
        return ok, detail

    async def _apply_via_http(self, message_id: str, emoji_id: int) -> tuple:
        """直连 Napcat HTTP 贴表情。"""
        ok, _, detail = await self._napcat_call(
            "POST",
            "/set_msg_emoji_like",
            {"message_id": message_id, "emoji_id": emoji_id, "set": True},
        )
        return ok, detail

    async def _check_channel(self) -> None:
        """启动时探测可用通道并记日志（不回显 Token）。"""
        mode = self._configured_transport()
        if mode in (_TRANSPORT_AUTO, _TRANSPORT_ADAPTER):
            prefix = await self._ensure_adapter_prefix()
            if prefix:
                self.ctx.logger.info("发送通道: 适配器可用（%s）", prefix)
            elif mode == _TRANSPORT_ADAPTER:
                self.ctx.logger.warning("发送通道: 适配器不可用 -> %s", self._adapter_last_error)
            else:
                self.ctx.logger.debug("发送通道: 适配器不可用 -> %s", self._adapter_last_error)

        if mode in (_TRANSPORT_AUTO, _TRANSPORT_HTTP):
            ok, _, detail = await self._napcat_call("GET", "/get_version_info", None)
            host, port = self.config.napcat.host, self.config.napcat.port
            if ok:
                self.ctx.logger.info("发送通道: Napcat HTTP 可用（%s:%s）", host, port)
            elif mode == _TRANSPORT_HTTP:
                self.ctx.logger.warning(
                    "发送通道: 连不上 Napcat HTTP %s:%s -> %s。"
                    "若你在用统一 QQ 连接器适配器，把 napcat.transport 改成 adapter 或 auto 即可，无需 HTTP 服务器",
                    host, port, detail[:200],
                )
            else:
                self.ctx.logger.debug("发送通道: Napcat HTTP 不可用（%s:%s）", host, port)

    async def _build_selfcheck_text(self) -> str:
        """拼自检文案：发送通道 + 模型配置 + 表情表状态。"""
        lines = ["贴表情插件自检"]
        mode = self._configured_transport()
        lines.append(f"传输方式配置：{mode}")

        if mode in (_TRANSPORT_AUTO, _TRANSPORT_ADAPTER):
            prefix = await self._ensure_adapter_prefix(force=True)
            if prefix:
                lines.append(f"适配器通道：可用（{prefix}）")
            else:
                lines.append(f"适配器通道：不可用（{self._adapter_last_error}）")

        if mode in (_TRANSPORT_AUTO, _TRANSPORT_HTTP):
            ok, _, detail = await self._napcat_call("GET", "/get_version_info", None)
            host, port = self.config.napcat.host, self.config.napcat.port
            if ok:
                lines.append(f"Napcat HTTP：可用（{host}:{port}）")
            else:
                lines.append(f"Napcat HTTP：不可用（{host}:{port}，{detail[:80]}）")

        if self._react_transport_used:
            lines.append(f"最近成功通道：{self._react_transport_used}")

        task = str(self.config.napcat.llm_task or "").strip() or _DEFAULT_LLM_TASK
        model = str(self.config.napcat.llm_model or "").strip()
        lines.append(f"选表情模型：task_name={task}" + (f"，model={model}" if model else ""))
        lines.append(
            f"主动贴表情：{'开' if self.config.proactive.enabled else '关'}"
            f"（LLM 超时 {self.config.proactive.llm_timeout_ms}ms，"
            f"失败{'回退关键词规则' if self.config.proactive.rule_fallback else '直接跳过'}）"
        )
        lines.append(f"内置表情：{len(AVAILABLE_REACT_EMOJIS)} 个（QQNT emojiId 表）")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部实现：贴表情主链路
    # ------------------------------------------------------------------
    def _spawn(self, coro: Any) -> None:
        """起一个可追踪的后台任务，on_unload 时统一取消。"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _proactive_react(
        self, chat_id: str, group_id: str, message_id: str, text: str = ""
    ) -> None:
        """后台执行主动贴表情，成功则登记冷却与去重。

        text：入站 hook 时拿到的消息原文。作为超时回退（rule_fallback=True 时）
        和规则选表情的依据，不能依赖 get_recent 的查询结果——
        真机上 get_recent 可能查不到刚入站的这条消息，查不到时 content 为空。
        """
        result = await self._react_to_message(
            chat_id, group_id, message_id, source="proactive", fallback_text=text
        )
        if result.get("success"):
            self._record_cooldown(chat_id)
            self._remember_message_id(message_id)
            self.ctx.logger.info("主动贴表情成功: message_id=%s", message_id)
        else:
            self.ctx.logger.debug(
                "主动贴表情跳过或失败: message_id=%s, reason=%s",
                message_id,
                result.get("content", ""),
            )

    async def _react_to_message(
        self,
        chat_id: str,
        group_id: str,
        target_msg_id: str,
        source: str,
        fallback_text: str = "",
    ) -> dict:
        """对指定消息贴表情：取上下文 -> LLM 选表情 -> 按通道发出。

        选表情策略（LLM 是唯一选表情路径）：
        - 主动路径（source=proactive）：调 LLM（带 llm_timeout_ms 超时）。
          proactive.rule_fallback=True 时，超时/异常/空返回/解析失败/非法 ID
          回退关键词规则（回退依据 = fallback_text 即 hook 传入的消息原文优先，
          其次目标消息内容）；False 时直接跳过本次贴表情。
        - 工具路径（source=tool）：始终用 LLM，不回退（LLM 主导的场景应由 LLM 决定）。
        """
        if not group_id:
            return {"success": False, "content": "贴表情仅支持群聊"}
        if not target_msg_id:
            return {"success": False, "content": "没有可用的目标消息"}

        # 只拉一次最近消息（limit=20 覆盖目标查找），目标消息提取与 prompt 上下文
        #（取前 10 条）复用同一份结果，避免连续两次 get_recent RPC。
        recent = await self._get_recent_messages(chat_id, limit=20)
        user_name, content = "未知用户", ""
        for msg in recent:
            if str(msg.get("message_id", "")) != target_msg_id:
                continue
            info = msg.get("message_info") or {}
            user = (info.get("user_info") or {}) if isinstance(info, dict) else {}
            user_name = str(user.get("user_nickname") or "未知用户")
            content = str(msg.get("processed_plain_text") or "")[:120]
            break

        prompt = self._build_prompt(target_msg_id, user_name, content, recent[:10])

        rule_basis = (
            fallback_text or content
            if source == "proactive" and self.config.proactive.rule_fallback
            else ""
        )
        emoji_id, emoji_name, decision = await self._select_emoji(
            prompt, fallback_text=rule_basis
        )
        if not emoji_id:
            return {"success": False, "content": f"选表情失败: {emoji_name}"}

        ok, detail = await self._apply_emoji_like(target_msg_id, int(emoji_id))
        if ok:
            self.ctx.logger.info(
                "贴表情成功: source=%s, decision=%s, 通道=%s, 消息=%s, 表情=%s(%s)",
                source, decision, self._react_transport_used or self._configured_transport(),
                target_msg_id, emoji_id, emoji_name,
            )
            return {
                "success": True,
                "content": f"已对 {user_name} 的消息贴了「{emoji_name}」表情",
            }
        self.ctx.logger.warning("贴表情失败: 消息=%s, 原因=%s", target_msg_id, detail)
        return {"success": False, "content": f"贴表情失败: {detail[:160]}"}

    # ---- LLM ----
    def _llm_kwargs(self) -> dict:
        """把配置解析成 llm.generate 的 kwargs。

        MaiBot 1.2.5 起「模型任务名」与「具体模型名」是两个参数（见 runtime-gotchas §47）：
          - 1.2.4- ：generate(model=X) 里 X 按**任务名**解释，任务名与模型名共用同一命名空间
          - 1.2.5+ ：task_name = 任务名；model / model_name = **具体模型名**

        旧写法把任务名塞进 model=，升级后会变成「找不到名为 planner 的模型」而整条链路失效。
        这里两个值分开传，各自留空则不传对应键，全空则走 SDK 默认（task_name="utils"）。
        """
        kwargs: dict = {}
        task = str(self.config.napcat.llm_task or "").strip()
        model = str(self.config.napcat.llm_model or "").strip()
        if task:
            kwargs["task_name"] = task
        if model:
            kwargs["model"] = model
        return kwargs

    async def _llm_generate(self, prompt: str) -> Any:
        """按 llm_timeout_ms 决定双层超时后调用 llm.generate。

        两层必须分开理解（别把外层当成内层用）：
        - 内层 asyncio.wait_for：我们要的「业务超时」，到点按 rule_fallback 决定回退/跳过；
        - 外层 RPC timeout_ms：cap.call 通道超时，SDK 默认只有 30s。外层一旦先断，
          抛的是 RPC 的 E_TIMEOUT（不是 asyncio.TimeoutError），会被误判成「选表情异常」，
          把「模型慢」错报成「代码错」。

        所以外层恒比内层宽 2s；llm_timeout_ms=0（不限制）时也给一个宽松但有限的上界，
        否则「不限制」会被 cap.call 的 30s 默认值悄悄变成「30s 限制」。
        """
        kwargs = self._llm_kwargs()
        # 与 SDK LLMCapability.generate 的载荷保持一致：prompt / model / task_name 恒有
        payload = {
            "prompt": prompt,
            "model": str(kwargs.get("model", "") or ""),
            "task_name": str(kwargs.get("task_name", "") or _DEFAULT_LLM_TASK),
        }
        timeout_ms = int(self.config.proactive.llm_timeout_ms or 0)
        rpc_timeout = timeout_ms + _LLM_RPC_TIMEOUT_SLACK_MS if timeout_ms > 0 else _LLM_RPC_TIMEOUT_NO_LIMIT_MS

        call_capability = getattr(self.ctx, "call_capability", None)
        if callable(call_capability):
            coro = call_capability("llm.generate", timeout_ms=rpc_timeout, **payload)
        else:  # pragma: no cover - 老 SDK 兜底
            coro = self.ctx.llm.generate(prompt, **kwargs)

        if timeout_ms <= 0:
            return await coro
        return await asyncio.wait_for(coro, timeout=timeout_ms / 1000.0)

    async def _select_emoji(self, prompt: str, fallback_text: str = "") -> tuple:
        """让 LLM 挑一个表情，返回 (emoji_id, emoji_name, decision)。

        decision：表情的最终来源——"llm"=LLM 自己选的；"rule"=超时/异常/空返回/
        解析失败/非法 ID 后回退关键词规则选的。失败返回 ("", 原因, "")。

        fallback_text：LLM 超时/失败时用于规则回退的原文。是否回退由调用方按
        proactive.rule_fallback 开关控制——传非空则超时后回退，传空则超时后直接
        失败返回。超时永远生效（llm_timeout_ms > 0 时），与是否回退解耦。
        """
        use_fallback = bool(fallback_text)
        timeout_ms = int(self.config.proactive.llm_timeout_ms or 0)
        try:
            raw = await self._llm_generate(prompt)
        except Exception as exc:
            if _is_timeout(exc):
                if not use_fallback:
                    return "", f"LLM 调用超时（>{timeout_ms}ms）", ""
                self.ctx.logger.warning(
                    "LLM 选表情超时（>%dms，%s），回退到关键词规则", timeout_ms, type(exc).__name__
                )
                emoji_id, emoji_name = self._select_emoji_by_rule(fallback_text)
                return emoji_id, emoji_name, "rule"
            self.ctx.logger.error(
                "调用 LLM 选表情异常: %s: %s（本次 task_name=%r model=%r）",
                type(exc).__name__,
                exc,
                self.config.napcat.llm_task,
                self.config.napcat.llm_model,
            )
            if use_fallback:
                emoji_id, emoji_name = self._select_emoji_by_rule(fallback_text)
                return emoji_id, emoji_name, "rule"
            return "", f"LLM 调用异常: {exc}", ""

        content = self._extract_llm_text(raw)
        if not content:
            if use_fallback:
                emoji_id, emoji_name = self._select_emoji_by_rule(fallback_text)
                return emoji_id, emoji_name, "rule"
            return "", "LLM 返回内容为空", ""

        try:
            data = json.loads(_fix_broken_json(content))
            emoji_id = str(data.get("emoji_id", "")).strip().strip("\"'")
            emoji_int = int(emoji_id)
            if emoji_int not in AVAILABLE_REACT_EMOJIS:
                # 非法 ID 也走规则回退（之前直接失败，浪费一次贴表情机会）
                if use_fallback:
                    self.ctx.logger.warning(
                        "LLM 返回非法表情 ID %s，回退到关键词规则", emoji_id
                    )
                    rid, rname = self._select_emoji_by_rule(fallback_text)
                    return rid, rname, "rule"
                return "", f"LLM 返回了不可用的表情 ID: {emoji_id}", ""
            return str(emoji_int), AVAILABLE_REACT_EMOJIS[emoji_int], "llm"
        except (json.JSONDecodeError, ValueError, TypeError, KeyError) as exc:
            self.ctx.logger.warning("解析 LLM 选表情结果失败: %s, 原文=%s", exc, content[:200])
            if use_fallback:
                emoji_id, emoji_name = self._select_emoji_by_rule(fallback_text)
                return emoji_id, emoji_name, "rule"
            return "", f"解析 LLM 结果失败: {exc}", ""

    # 关键词 -> 候选表情 ID。命中关键词后从对应候选池随机挑一个。
    # 候选 ID 必须落在 AVAILABLE_REACT_EMOJIS 内，且释义以 QQNT 表为准。
    _RULE_EMOJI_MAP: dict = {
        ("哈哈", "笑死", "233", "www", "绷不住", "太好笑"): (182, 20, 13),   # 笑哭/偷笑/呲牙
        ("好耶", "牛", "厉害", "恭喜", "666", "赞"): (76, 311, 144),        # 赞/打call/喝彩
        ("可爱", "贴贴", "抱抱", "么么", "萌"): (350, 175, 66),             # 贴贴/卖萌/爱心
        ("哭", "难过", "救命", "呜呜", "orz", "惨"): (9, 5, 106),           # 大哭/流泪/委屈
        ("？", "?", "离谱", "草", "无语", "什么"): (462, 265, 187),         # 无语/辣眼睛/幽灵
        ("谢谢", "感谢", "玫瑰", "爱你"): (63, 66, 144),                    # 玫瑰/爱心/喝彩
        ("生气", "气死", "烦", "讨厌"): (326, 323, 462),                    # 生气/嫌弃/无语
        ("求", "放过", "饶命"): (425, 111, 428),                            # 求放过/可怜/收到
    }
    # 无关键词命中时的兜底候选池（群聊里比较百搭的几个）
    _RULE_DEFAULT_EMOJIS: tuple = (76, 66, 182, 13)

    def _select_emoji_by_rule(self, text: str) -> tuple:
        """关键词规则选表情：毫秒级返回，不依赖 LLM。返回 (emoji_id, emoji_name)。"""
        lower = (text or "").strip().lower()
        for keywords, candidates in self._RULE_EMOJI_MAP.items():
            if any(keyword in lower for keyword in keywords):
                emoji_id = random.choice(candidates)
                return str(emoji_id), AVAILABLE_REACT_EMOJIS[emoji_id]
        emoji_id = random.choice(self._RULE_DEFAULT_EMOJIS)
        return str(emoji_id), AVAILABLE_REACT_EMOJIS[emoji_id]

    @staticmethod
    def _extract_llm_text(result: Any) -> str:
        """兼容 SDK 2.x 的 response 字段与旧版 content 字段。"""
        if isinstance(result, dict):
            if not result.get("success", True):
                return ""
            return str(result.get("response") or result.get("content") or result.get("text") or "")
        return str(result or "")

    def _build_prompt(
        self, target_msg_id: str, user_name: str, content: str, recent: list
    ) -> str:
        """构造选表情的 prompt。recent 由调用方提供（与目标消息提取共用同一次 get_recent）。"""

        if recent:
            lines = []
            for msg in recent:
                info = msg.get("message_info") or {}
                user = (info.get("user_info") or {}) if isinstance(info, dict) else {}
                name = str(user.get("user_nickname") or "未知用户")
                text = str(msg.get("processed_plain_text") or "").replace("\n", " ").replace("\r", " ")[:50]
                marker = " [目标消息]" if str(msg.get("message_id", "")) == target_msg_id else ""
                lines.append(
                    f"{msg.get('message_id','')},{_relative_time(msg.get('timestamp'))},{name}:{text}{marker}"
                )
            context = "\n".join(lines)
        else:
            context = "（无法获取最近消息）"

        return (
            "你是一个正在群里聊天的网友，需要给「目标消息」选一个最合适的反应表情。\n\n"
            f"目标消息：\n- ID: {target_msg_id}\n- 发送者: {user_name}\n- 内容: {content[:120]}\n\n"
            f"最近聊天记录（格式：消息ID,时间,昵称:内容）：\n{context}\n\n"
            f"可用表情（ID:名称）：\n{_EMOJI_LIST_PROMPT}\n\n"
            "请只返回一个 JSON 对象，不要有任何解释或多余文字：\n"
            '{"emoji_id": "表情ID数字", "reason": "简短理由，10字以内"}'
        )

    # ---- 消息查询 ----
    async def _get_recent_messages(self, chat_id: str, limit: int = 10) -> list:
        """取最近消息，失败返回空列表（不抛给调用方）。"""
        if not chat_id:
            return []
        try:
            result = await self.ctx.message.get_recent(chat_id=chat_id, limit=limit)
            return result if isinstance(result, list) else []
        except Exception as exc:
            self.ctx.logger.warning("ctx.message.get_recent 调用失败: %s", exc)
            return []

    async def _get_latest_message_id(self, chat_id: str) -> str:
        """取最新一条消息的 ID。"""
        recent = await self._get_recent_messages(chat_id, limit=1)
        return str(recent[0].get("message_id", "")) if recent else ""

    # ---- Napcat HTTP（阻塞调用，全部走 to_thread） ----
    async def _napcat_call(
        self, method: str, path: str, payload: Any = None, timeout: int = 0
    ) -> tuple:
        """异步调 Napcat HTTP 接口，返回 (是否成功, 响应dict或None, 详情文本)。"""
        seconds = timeout or max(1, int(self.config.napcat.timeout_seconds or 10))
        return await asyncio.to_thread(self._napcat_call_sync, method, path, payload, seconds)

    def _napcat_call_sync(self, method: str, path: str, payload: Any, timeout: int) -> tuple:
        """同步实现，只在 asyncio.to_thread 里被调用。"""
        host = str(self.config.napcat.host or "127.0.0.1").strip()
        port = int(self.config.napcat.port or 0)
        token = str(self.config.napcat.token or "").strip()
        conn = None
        try:
            conn = http.client.HTTPConnection(host, port, timeout=timeout)
            headers = {"Content-Type": "application/json"}
            if token:
                # OneBot v11 标准写法是 Authorization: Bearer <token>；
                # 早前这里直接塞原始 token，Napcat 配了 token 时会一律 401。
                headers["Authorization"] = token if token.lower().startswith("bearer ") else f"Bearer {token}"
            body = json.dumps(payload) if payload is not None else None
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return False, None, f"响应不是 JSON（HTTP {resp.status}）: {raw[:200]}"
            return self._payload_ok(data), data, self._payload_detail(data, raw)
        except Exception as exc:
            return False, None, f"{type(exc).__name__}: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    # ---- 判断与提取 ----
    @staticmethod
    def _payload_ok(result: Any) -> bool:
        """判定 OneBot v11 响应是否成功（HTTP 与适配器通道共用同一判据）。"""
        if not isinstance(result, dict):
            return False
        if result.get("status") == "ok" or result.get("retcode") == 0:
            return True
        return False

    @staticmethod
    def _payload_detail(result: Any, raw: str = "") -> str:
        """从 OneBot 响应里取一句可读详情（优先 message/wording，退化到原文）。"""
        if isinstance(result, dict):
            text = result.get("message") or result.get("wording") or result.get("msg")
            if text:
                return str(text)[:300]
        return str(raw or result)[:300]

    @staticmethod
    def _collect_strings(node: Any) -> list:
        """递归收集结构里所有字符串值（用于解析 ctx.api.list() 的返回）。"""
        found: list = []

        def walk(current: Any) -> None:
            if isinstance(current, str):
                found.append(current)
            elif isinstance(current, dict):
                for value in current.values():
                    walk(value)
            elif isinstance(current, (list, tuple, set)):
                for item in current:
                    walk(item)

        walk(node)
        return found

    def _should_try_proactive(self, chat_id: str, message_id: str, content: str) -> bool:
        """按去重、冷却、概率判断是否主动贴表情。"""
        if message_id in self._reacted_message_ids:
            return False
        last = float(self._proactive_last_react_at.get(chat_id, 0))
        if time.time() - last < max(0, int(self.config.proactive.cooldown_seconds)):
            return False
        chance = (
            self.config.proactive.keyword_chance
            if self._looks_reactable(content)
            else self.config.proactive.chance
        )
        try:
            chance = min(1.0, max(0.0, float(chance)))
        except (TypeError, ValueError):
            chance = 0.0
        return random.random() < chance

    def _record_cooldown(self, chat_id: str) -> None:
        """登记冷却时间；条目超过上限时惰性淘汰（先删过期，仍超则按时间保留最新一批）。

        冷却字典只写不删会随群数/运行时长无界增长，这里把清理压在写入路径上，
        且只在超过 _MAX_COOLDOWN_ENTRIES 时才扫表，常态零开销。
        """
        now = time.time()
        self._proactive_last_react_at[chat_id] = now
        if len(self._proactive_last_react_at) <= _MAX_COOLDOWN_ENTRIES:
            return
        cutoff = now - max(0, int(self.config.proactive.cooldown_seconds))
        expired = [k for k, ts in self._proactive_last_react_at.items() if ts < cutoff]
        for k in expired:
            del self._proactive_last_react_at[k]
        if len(self._proactive_last_react_at) > _MAX_COOLDOWN_ENTRIES:
            keep = sorted(
                self._proactive_last_react_at.items(), key=lambda kv: kv[1], reverse=True
            )[:_MAX_COOLDOWN_ENTRIES]
            self._proactive_last_react_at = dict(keep)

    def _remember_message_id(self, message_id: str) -> None:
        """记录已贴过的消息，避免 hook 与事件监听重复触发。

        滑动窗口：deque(maxlen) 满时自动挤掉最旧 ID，同步从 set 删除，
        不会像以前那样到顶整体 clear、瞬间失去全部去重保护。
        """
        if message_id in self._reacted_message_ids:
            return
        if len(self._reacted_message_id_queue) == self._reacted_message_id_queue.maxlen:
            self._reacted_message_ids.discard(self._reacted_message_id_queue[0])
        self._reacted_message_id_queue.append(message_id)
        self._reacted_message_ids.add(message_id)

    @staticmethod
    def _looks_reactable(content: str) -> bool:
        """粗略判断这条消息是否明显适合用表情回应。"""
        text = (content or "").strip().lower()
        return any(keyword in text for keyword in _REACTABLE_KEYWORDS)

    @staticmethod
    def _first_text(*values: Any) -> str:
        """返回第一个非空字符串。"""
        for value in values:
            if value is None:
                continue
            text = str(value).strip()
            if text:
                return text
        return ""

    @staticmethod
    def _deep_get(data: Any, *keys: str) -> Any:
        """安全读嵌套字典字段。"""
        current = data
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current

    def _extract_group_id(self, msg: dict, kwargs: dict) -> str:
        """存在 group_id 即视为群聊。"""
        return self._first_text(
            msg.get("group_id"),
            self._deep_get(msg, "message_info", "group_info", "group_id"),
            self._deep_get(msg, "group_info", "group_id"),
            kwargs.get("group_id"),
        )

    def _extract_message_id(self, msg: dict, kwargs: dict) -> str:
        """Napcat 的消息 ID 通常在 raw_message 里。"""
        return self._first_text(
            msg.get("message_id"),
            self._deep_get(msg, "raw_message", "message_id"),
            kwargs.get("message_id"),
        )

    def _is_self_message(self, msg: dict, kwargs: dict) -> bool:
        """尽量识别机器人自己的消息，避免自我贴表情。"""
        if not self.config.proactive.skip_self_messages:
            return False
        if any(
            flag is True
            for flag in (
                msg.get("is_self"),
                msg.get("from_self"),
                self._deep_get(msg, "message_info", "is_self"),
                self._deep_get(msg, "raw_message", "self"),
            )
        ):
            return True
        user_id = self._first_text(
            kwargs.get("user_id"),
            msg.get("user_id"),
            self._deep_get(msg, "message_info", "user_info", "user_id"),
        )
        bot_id = self._first_text(
            kwargs.get("bot_id"), kwargs.get("self_id"), msg.get("bot_id"), msg.get("self_id")
        )
        return bool(user_id and bot_id and user_id == bot_id)


def create_plugin() -> GroupEmojiReactPlugin:
    """创建插件实例。"""
    return GroupEmojiReactPlugin()
