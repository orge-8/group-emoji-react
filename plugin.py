"""MaiBot 群聊贴表情插件。

通过 Napcat 的 set_msg_emoji_like 接口给群聊消息贴表情，由 LLM 决定用哪个表情。
两条触发路径：
  1. LLM 主动调用 @Tool("react_emoji")
  2. 普通群聊消息旁路主动贴表情（@HookHandler("chat.receive.after_process")）

踩坑点（详见 maibot-plugin-dev skill 的 runtime-gotchas，改动前请先读）：
- 本文件**不要**写 `from __future__ import annotations`：Runner 用 spec_from_file_location
  加载且不注册进 sys.modules，注解会变成字符串，pydantic 解析配置模型直接失败。
- 入站消息监听用 **hook 而不要用 EventHandler(EventType.ON_MESSAGE)**：
  部分 MaiBot 版本里 ON_MESSAGE 的派发被注释掉，注册了也永远不会触发。
  可靠的入站入口是 `chat.receive.after_process`（此时 processed_plain_text 已可用）。
- `http.client` 是阻塞调用，必须丢进 `asyncio.to_thread`，否则会卡住插件 Runner 的事件循环。
"""
import asyncio
import http.client
import json
import random
import time
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

# Napcat 支持的反应表情（ID -> 名称）。ID 必须是 Napcat 认识的，否则接口会拒绝。
AVAILABLE_REACT_EMOJIS: dict = {
    76: "点赞", 307: "喵喵", 285: "摸鱼",
    66: "爱心", 147: "棒棒糖", 424: "狂按按钮",
    49: "抱抱", 38: "木槌敲头", 277: "狗头",
    265: "辣眼睛", 390: "头秃", 63: "玫瑰",
    212: "托腮", 5: "大哭", 9: "委屈",
    350: "贴贴", 175: "卖萌", 344: "大怨种",
    187: "鬼魂", 144: "礼花", 146: "爆筋",
    311: "打call", 59: "便便", 46: "猪头",
    37: "骷髅头", 13: "呲牙", 124: "OK",
    233: "笑哭", 20: "偷笑", 293: "敲脑瓜",
}

# 已经确认"明显适合用表情回应"的消息关键词（命中则用更高的概率）
_REACTABLE_KEYWORDS: tuple = (
    "哈哈", "笑死", "好耶", "草", "可爱", "贴贴", "抱抱", "哭", "难过",
    "谢谢", "恭喜", "牛", "厉害", "救命", "离谱", "绷不住", "？", "!",
    "！", "www", "233", "orz",
)

_MAX_TRACKED_MESSAGE_IDS = 1000


class PluginSectionConfig(PluginConfigBase):
    """插件基础配置。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置版本")


class NapcatConfig(PluginConfigBase):
    """Napcat 服务连接配置。"""

    __ui_label__ = "Napcat 服务"
    __ui_icon__ = "server"
    __ui_order__ = 1

    host: str = Field(default="127.0.0.1", description="Napcat HTTP 服务地址（Docker 部署一般填容器名，如 napcat）")
    port: int = Field(default=9999, description="Napcat HTTP 服务端口")
    token: str = Field(default="", description="Napcat HTTP 服务认证 Token（没有就留空）")
    llm_task: str = Field(
        default="planner",
        description="选表情用的模型任务名或模型标识（planner / replyer / utils / tool_use，也可留空用默认模型）",
    )
    timeout_seconds: int = Field(default=10, description="Napcat 请求超时时间（秒）")


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
        self._reacted_message_ids: set = set()
        self._tasks: set = set()

    async def on_load(self) -> None:
        """插件加载时执行。"""
        self.ctx.logger.info(
            "群聊贴表情插件已加载: napcat=%s:%s",
            self.config.napcat.host,
            self.config.napcat.port,
        )
        self._spawn(self._check_napcat_connection())

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
        if scope == "self":
            self.ctx.logger.info("群聊贴表情: 配置已更新 version=%s", version)

    # ------------------------------------------------------------------
    # HookHandler: 普通群聊消息旁路主动贴表情（不拦截正常回复流程）
    # ------------------------------------------------------------------
    @HookHandler(
        "chat.receive.after_process",
        name="emoji_react_observer",
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
        description="检查贴表情插件与 Napcat 的连通性",
        pattern=r"^\s*[/／]\s*(?:表情测试|贴表情测试|reacttest)\s*$",
        aliases=["表情测试"],
    )
    async def cmd_reacttest(self, **kwargs: Any) -> tuple:
        """自检命令：报告 Napcat 连通性与当前生效配置（不回显 Token）。"""
        stream_id = ""
        for key in ("stream_id", "chat_id", "session_id", "stream"):
            if kwargs.get(key):
                stream_id = str(kwargs[key])
                break

        host = self.config.napcat.host
        port = self.config.napcat.port
        ok, _, detail = await self._napcat_call("GET", "/get_version_info", None)

        if ok:
            text = f"贴表情插件正常：已连上 Napcat {host}:{port}"
        else:
            text = (
                f"贴表情插件：连不上 Napcat {host}:{port}（{detail[:120]}）。"
                "请检查 Napcat 是否启动、WebUI 里是否配置了 HTTP 服务器，以及 config.toml 的 host/port 是否正确。"
            )

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
    # 内部实现
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
        和规则选表情的依据，不能依赖 _get_target_message_info 的查询结果——
        真机上 get_recent 可能查不到刚入站的这条消息，查不到时 content 为空。
        """
        result = await self._react_to_message(
            chat_id, group_id, message_id, source="proactive", fallback_text=text
        )
        if result.get("success"):
            self._proactive_last_react_at[chat_id] = time.time()
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
        """对指定消息贴表情：取上下文 -> LLM 选表情 -> 调 Napcat。

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

        user_name, content = await self._get_target_message_info(target_msg_id, chat_id)
        prompt = await self._build_prompt(target_msg_id, user_name, content, chat_id)

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

        ok, _, detail = await self._napcat_call(
            "POST",
            "/set_msg_emoji_like",
            {"message_id": target_msg_id, "emoji_id": emoji_id, "set": True},
        )
        if ok:
            self.ctx.logger.info(
                "贴表情成功: source=%s, decision=%s, 消息=%s, 表情=%s(%s)",
                source, decision, target_msg_id, emoji_id, emoji_name
            )
            return {
                "success": True,
                "content": f"已对 {user_name} 的消息贴了「{emoji_name}」表情",
            }
        self.ctx.logger.warning("贴表情失败: 消息=%s, 原因=%s", target_msg_id, detail)
        return {"success": False, "content": f"贴表情失败: {detail[:120]}"}

    # ---- LLM ----
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
            if timeout_ms > 0:
                raw = await asyncio.wait_for(
                    self.ctx.llm.generate(
                        prompt, model=str(self.config.napcat.llm_task or "").strip()
                    ),
                    timeout=timeout_ms / 1000.0,
                )
            else:
                raw = await self.ctx.llm.generate(
                    prompt, model=str(self.config.napcat.llm_task or "").strip()
                )
        except asyncio.TimeoutError:
            if not use_fallback:
                return "", f"LLM 调用超时（>{timeout_ms}ms）", ""
            self.ctx.logger.warning(
                "LLM 选表情超时（>%dms），回退到关键词规则", timeout_ms
            )
            emoji_id, emoji_name = self._select_emoji_by_rule(fallback_text)
            return emoji_id, emoji_name, "rule"
        except Exception as exc:
            self.ctx.logger.error("调用 LLM 选表情异常: %s", exc)
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
    _RULE_EMOJI_MAP: dict = {
        ("哈哈", "笑死", "233", "www", "绷不住"): (233, 20, 13),        # 笑哭/偷笑/呲牙
        ("好耶", "牛", "厉害", "恭喜", "666"): (124, 311, 66),          # OK/打call/爱心
        ("可爱", "贴贴", "抱抱", "么么"): (350, 175, 49),               # 贴贴/卖萌/抱抱
        ("哭", "难过", "救命", "呜呜", "orz"): (5, 9, 212),             # 大哭/委屈/托腮
        ("？", "?", "离谱", "草", "无语"): (293, 265, 187),             # 敲脑瓜/辣眼睛/鬼魂
        ("谢谢", "玫瑰"): (63, 66),                                     # 玫瑰/爱心
    }
    # 无关键词命中时的兜底候选池（群聊里比较百搭的几个）
    _RULE_DEFAULT_EMOJIS: tuple = (124, 233, 13, 66)

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

    async def _build_prompt(
        self, target_msg_id: str, user_name: str, content: str, chat_id: str
    ) -> str:
        """构造选表情的 prompt。"""
        emoji_list = ", ".join(f"{eid}:{name}" for eid, name in AVAILABLE_REACT_EMOJIS.items())
        recent = await self._get_recent_messages(chat_id, limit=10)

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
            f"可用表情（ID:名称）：\n{emoji_list}\n\n"
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

    async def _get_target_message_info(self, target_msg_id: str, chat_id: str) -> tuple:
        """从最近消息里找目标消息的发送者和内容。"""
        for msg in await self._get_recent_messages(chat_id, limit=20):
            if str(msg.get("message_id", "")) != target_msg_id:
                continue
            info = msg.get("message_info") or {}
            user = (info.get("user_info") or {}) if isinstance(info, dict) else {}
            name = str(user.get("user_nickname") or "未知用户")
            return name, str(msg.get("processed_plain_text") or "")[:120]
        return "未知用户", ""

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
                headers["Authorization"] = token
            body = json.dumps(payload) if payload is not None else None
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return False, None, f"响应不是 JSON（HTTP {resp.status}）: {raw[:200]}"
            ok = data.get("status") == "ok" or data.get("retcode") == 0
            return ok, data, str(data.get("message") or data.get("wording") or raw)[:300]
        except Exception as exc:
            return False, None, f"{type(exc).__name__}: {exc}"
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    async def _check_napcat_connection(self) -> None:
        """启动时检测 Napcat 连通性并记日志（不回显 Token）。"""
        ok, _, detail = await self._napcat_call("GET", "/get_version_info", None)
        host, port = self.config.napcat.host, self.config.napcat.port
        if ok:
            self.ctx.logger.info("Napcat 连通性检测通过: %s:%s", host, port)
        else:
            self.ctx.logger.warning(
                "Napcat 连通性检测失败: %s:%s -> %s（不影响插件加载，但贴表情会失败）",
                host, port, detail[:200],
            )

    # ---- 判断与提取 ----
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

    def _remember_message_id(self, message_id: str) -> None:
        """记录已贴过的消息，避免 hook 与事件监听重复触发。"""
        if len(self._reacted_message_ids) >= _MAX_TRACKED_MESSAGE_IDS:
            self._reacted_message_ids.clear()
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
