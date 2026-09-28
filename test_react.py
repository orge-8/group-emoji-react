"""群聊贴表情插件的本地行为回归测试（不需要 MaiBot，也不需要真实 Napcat / 适配器）。

用 FakeHost 模拟 ctx.llm / ctx.message / ctx.send / ctx.api，并把 Napcat HTTP 调用换成录制桩，
验证三条主线：
  1. 「入站消息 -> 提取字段 -> LLM 选表情 -> 发出」主链路
  2. 发送通道选择（适配器 / HTTP / auto 回落）
  3. 表情表与 QQNT emojiId 表的一致性（防释义错配回归）

用法:
    python test_react.py
退出码: 0=全部通过, 1=有失败
"""
import asyncio
import http.client
import sys
import time
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk.context import PluginContext, PluginPaths  # noqa: E402

import plugin as plugin_module  # noqa: E402
from plugin import (  # noqa: E402
    _QQNT_FACE_NAMES,
    AVAILABLE_REACT_EMOJIS,
    GroupEmojiReactPlugin,
    _is_timeout,
    create_plugin,
)

# 适配器（统一 QQ 连接器）在真机上注册的 API 名
_NAPCAT_REACT_API = "adapter.napcat.message.set_msg_emoji_like"
_NAPCAT_PROBE_API = "adapter.napcat.system.get_version_info"
_SNOWLUMA_REACT_API = "adapter.snowluma.message.set_msg_emoji_like"
_SNOWLUMA_PROBE_API = "adapter.snowluma.system.get_version_info"


class FakeHost:
    """记录插件对宿主的调用，并按能力返回假数据。"""

    def __init__(
        self,
        llm_reply: str = '{"emoji_id": "76", "reason": "赞同"}',
        llm_delay: float = 0.0,
        adapter_apis: tuple = (),
        adapter_fail_apis: tuple = (),
        adapter_raises: bool = False,
    ) -> None:
        self.llm_reply = llm_reply
        self.llm_delay = llm_delay
        self.no_recent = False  # True 时模拟真机 get_recent 查不到任何消息
        self.napcat_calls: list = []
        self.sent_text: list = []
        self.llm_calls: list = []
        self.get_recent_count = 0  # message.get_recent RPC 次数（验证单次贴表情只拉一次）
        # 适配器通道
        self.adapter_apis: tuple = tuple(adapter_apis)   # api.list 会返回这些名字
        self.adapter_fail_apis: tuple = tuple(adapter_fail_apis)  # 这些 API 返回业务失败
        self.adapter_raises: bool = adapter_raises        # True 时 api.call 一律抛异常
        self.api_calls: list = []                         # 记录 ctx.api.call 的调用
        self.api_react_calls: list = []                   # 记录经适配器发出的贴表情

    def react_calls(self) -> list:
        """只取 HTTP 贴表情调用，排除启动时的 /get_version_info 连通性检测。"""
        return [c for c in self.napcat_calls if c["path"] == "/set_msg_emoji_like"]

    async def rpc_call(self, method, plugin_id, payload, timeout_ms=None):
        if method != "cap.call":
            raise RuntimeError(f"FakeHost 不支持的 RPC: {method}")
        cap = (payload or {}).get("capability", "")
        args = (payload or {}).get("args") or {}
        if cap == "llm.generate":
            self.llm_calls.append(args)
            if self.llm_delay > 0:
                await asyncio.sleep(self.llm_delay)
            if self.llm_reply is None:
                raise RuntimeError("模拟 LLM 服务异常")
            return {"success": True, "response": self.llm_reply}
        if cap == "api.list":
            return {"success": True, "apis": list(self.adapter_apis)}
        if cap == "api.call":
            name = str(args.get("api_name", ""))
            inner = args.get("args") or {}
            self.api_calls.append({"api_name": name, **inner})
            if self.adapter_raises:
                raise RuntimeError(f"插件 API 调用失败: {name}")
            if name not in self.adapter_apis:
                raise RuntimeError(f"未找到插件 API: {name}")
            if name in self.adapter_fail_apis:
                return {
                    "success": True,
                    "result": {"status": "failed", "retcode": 1404, "message": "消息不存在"},
                }
            if name.endswith("message.set_msg_emoji_like"):
                self.api_react_calls.append({"api_name": name, **inner})
            return {"success": True, "result": {"status": "ok", "retcode": 0, "data": {}}}
        if cap == "message.get_recent":
            self.get_recent_count += 1
            if self.no_recent:
                return []
            return [
                {
                    "message_id": "555",
                    "processed_plain_text": "哈哈哈哈哈这也太好笑了",
                    "timestamp": time.time(),
                    "message_info": {"user_info": {"user_nickname": "小明", "user_id": "10001"}},
                }
            ]
        if cap == "send.text":
            self.sent_text.append(args.get("text", ""))
            return True
        return True


async def make_plugin(host: FakeHost, transport: str = "auto") -> GroupEmojiReactPlugin:
    """构造插件并走真实生命周期（Runner 一定会调 on_load）。"""
    plugin = create_plugin()
    ctx = PluginContext(
        plugin_id="org.mai-mai.group-emoji-react",
        rpc_call=host.rpc_call,
        paths=PluginPaths(data_dir=Path(_PLUGIN_DIR) / "data", runtime_dir=Path(_PLUGIN_DIR) / "runtime"),
    )
    plugin._set_context(ctx)
    plugin.set_plugin_config(plugin.get_default_config())
    plugin.config.napcat.transport = transport

    def fake_napcat(method, path, payload, timeout):
        host.napcat_calls.append({"method": method, "path": path, "payload": payload})
        return True, {"status": "ok"}, "ok"

    plugin._napcat_call_sync = fake_napcat
    await plugin.on_load()
    return plugin


GROUP_MESSAGE = {
    "session_id": "chat-1",
    "processed_plain_text": "哈哈哈哈哈这也太好笑了",
    "message_info": {
        "group_info": {"group_id": "123456"},
        "user_info": {"user_nickname": "小明", "user_id": "10001"},
    },
    "raw_message": {"message_id": "555", "self_id": "999"},
}


def _force_proactive(plugin: GroupEmojiReactPlugin) -> None:
    """把主动路径的概率/冷却调到必然触发。"""
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1


async def test_hook_reacts_to_group_message() -> bool:
    """入站群聊消息应触发一次贴表情，且表情 ID 合法。"""
    host = FakeHost()
    plugin = await make_plugin(host, transport="http")
    _force_proactive(plugin)

    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))

    calls = host.react_calls()
    if len(calls) != 1:
        print(f"[FAIL] 预期 1 次贴表情调用，实际 {len(calls)}")
        return False
    payload = calls[0]["payload"]
    if str(payload.get("message_id")) != "555":
        print(f"[FAIL] 目标消息 ID 应为 555，实际 {payload.get('message_id')}")
        return False
    if int(payload.get("emoji_id")) not in AVAILABLE_REACT_EMOJIS:
        print(f"[FAIL] 表情 ID 不在可用列表: {payload.get('emoji_id')}")
        return False
    print(
        f"[PASS] 旁路贴表情链路正常: 消息 555 贴上 "
        f"{payload['emoji_id']}:{AVAILABLE_REACT_EMOJIS[int(payload['emoji_id'])]}"
    )
    return True


async def test_hook_ignores_private_message() -> bool:
    """非群聊（拿不到 group_id）不应触发贴表情。"""
    host = FakeHost()
    plugin = await make_plugin(host, transport="http")
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    private = {
        "session_id": "chat-2",
        "processed_plain_text": "哈哈哈哈哈",
        "message_info": {"user_info": {"user_nickname": "小明"}},
        "raw_message": {"message_id": "777"},
    }
    await plugin.observe_group_message(message=private)
    await asyncio.gather(*list(plugin._tasks))
    if host.react_calls():
        print(f"[FAIL] 私聊不应贴表情，却调用了 {len(host.react_calls())} 次")
        return False
    print("[PASS] 私聊消息已正确跳过")
    return True


async def test_hook_dedups_same_message() -> bool:
    """同一条消息重复入站（hook/事件双路径）不应重复贴表情。"""
    host = FakeHost()
    plugin = await make_plugin(host, transport="http")
    _force_proactive(plugin)

    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))
    # 第二次同消息：应被去重拦下
    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))

    if len(host.react_calls()) != 1:
        print(f"[FAIL] 同一消息重复入站应只贴 1 次，实际 {len(host.react_calls())} 次")
        return False
    print("[PASS] 同一消息去重生效（hook/事件双路径不会重复贴）")
    return True


async def test_tool_rejects_non_group() -> bool:
    """Tool 在非群聊场景应返回可读错误，且不调任何通道。"""
    host = FakeHost(adapter_apis=(_NAPCAT_REACT_API, _NAPCAT_PROBE_API))
    plugin = await make_plugin(host, transport="auto")
    result = await plugin.react_emoji(target_message_id="555", chat_id="chat-1")
    if result.get("success") is not False or "群聊" not in str(result.get("content", "")):
        print(f"[FAIL] 非群聊应返回失败且提示群聊，实际 {result}")
        return False
    if host.react_calls() or host.api_react_calls:
        print("[FAIL] 非群聊不应发出任何贴表情请求")
        return False
    print("[PASS] Tool 非群聊场景正确拒绝")
    return True


async def test_tool_reacts_in_group() -> bool:
    """Tool 在群聊场景应贴表情成功并返回中文结果。"""
    host = FakeHost()
    plugin = await make_plugin(host, transport="http")
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")
    if not result.get("success"):
        print(f"[FAIL] 群聊贴表情应成功，实际 {result}")
        return False
    if not host.react_calls():
        print("[FAIL] 未调用 Napcat 贴表情")
        return False
    if "小明" not in str(result.get("content", "")):
        print(f"[FAIL] 回执应包含发送者昵称，实际 {result.get('content')}")
        return False
    print(f"[PASS] Tool 群聊贴表情正常: {result['content']}")
    return True


async def test_llm_bad_emoji_is_rejected() -> bool:
    """LLM 返回白名单外的表情 ID 时，不能把非法 ID 发出去。"""
    host = FakeHost(llm_reply='{"emoji_id": "99999", "reason": "乱选"}')
    plugin = await make_plugin(host, transport="http")
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")
    if result.get("success"):
        print("[FAIL] 非法表情 ID 不应成功")
        return False
    if host.react_calls():
        print("[FAIL] 非法表情 ID 不应发出")
        return False
    print("[PASS] 非法表情 ID 已拦截")
    return True


async def test_rule_fallback_disabled_skips() -> bool:
    """rule_fallback=False（默认）时，LLM 超时应直接跳过，不再回退关键词规则。"""
    host = FakeHost(llm_delay=0.5)  # 500ms 才返回
    plugin = await make_plugin(host, transport="http")
    _force_proactive(plugin)
    plugin.config.proactive.llm_timeout_ms = 50  # 50ms 超时，必然触发
    # rule_fallback 默认 False，无需显式赋值

    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))

    if not host.llm_calls:
        print("[FAIL] 超时跳过路径仍应调过一次 LLM")
        return False
    if host.react_calls():
        print(f"[FAIL] rule_fallback=False 超时后不应贴表情，实际调了 {len(host.react_calls())} 次")
        return False
    print("[PASS] rule_fallback=False：LLM 超时后直接跳过本次贴表情（宁缺毋滥）")
    return True


async def test_proactive_llm_timeout_falls_back() -> bool:
    """rule_fallback=True 时，LLM 超时超过 llm_timeout_ms 应回退到规则并仍然贴上表情。"""
    host = FakeHost(llm_delay=0.5)  # 500ms 才返回
    plugin = await make_plugin(host, transport="http")
    _force_proactive(plugin)
    plugin.config.proactive.rule_fallback = True
    plugin.config.proactive.llm_timeout_ms = 50  # 50ms 超时，必然触发

    start = time.monotonic()
    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))
    elapsed = time.monotonic() - start

    if len(host.llm_calls) != 1:
        print(f"[FAIL] 超时回退路径应仍调过一次 LLM，实际 {len(host.llm_calls)} 次")
        return False
    calls = host.react_calls()
    if len(calls) != 1:
        print(f"[FAIL] 超时回退后应贴 1 次表情，实际 {len(calls)} 次")
        return False
    if elapsed >= 0.5:
        print(f"[FAIL] 应在 ~50ms 超时后回退，实际耗时 {elapsed:.2f}s（说明没生效）")
        return False
    emoji_id = int(calls[0]["payload"]["emoji_id"])
    print(f"[PASS] LLM 超时 {elapsed*1000:.0f}ms 后回退规则，表情 {emoji_id}:{AVAILABLE_REACT_EMOJIS[emoji_id]}")
    return True


async def test_proactive_llm_error_falls_back() -> bool:
    """rule_fallback=True 时，LLM 调用抛异常应回退到规则而不是放弃贴表情。"""
    host = FakeHost(llm_reply=None)  # rpc_call 里抛 RuntimeError
    plugin = await make_plugin(host, transport="http")
    _force_proactive(plugin)
    plugin.config.proactive.rule_fallback = True

    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))

    calls = host.react_calls()
    if len(calls) != 1:
        print(f"[FAIL] LLM 异常回退后应贴 1 次表情，实际 {len(calls)} 次")
        return False
    emoji_id = int(calls[0]["payload"]["emoji_id"])
    if emoji_id not in AVAILABLE_REACT_EMOJIS:
        print(f"[FAIL] 规则回退选出的表情不合法: {emoji_id}")
        return False
    print(f"[PASS] LLM 异常已回退规则贴表情: {emoji_id}:{AVAILABLE_REACT_EMOJIS[emoji_id]}")
    return True


async def test_proactive_timeout_when_target_not_in_recent() -> bool:
    """真机缺陷复现：get_recent 查不到目标消息（content 为空）时，超时回退必须仍然生效。

    这是 v1.1.0 真机日志暴露的 bug：回退依据取的是 _get_target_message_info 的查询
    结果，查不到时 content=""，导致超时包装被整体跳过，LLM 死等 21.1s。
    修复后回退依据优先用 hook 入站时拿到的消息原文。
    """
    host = FakeHost(llm_delay=0.5)  # 500ms 才返回
    plugin = await make_plugin(host, transport="http")
    # 模拟真机：get_recent 返回的列表里没有目标消息 555
    host.no_recent = True
    _force_proactive(plugin)
    plugin.config.proactive.rule_fallback = True
    plugin.config.proactive.llm_timeout_ms = 50

    start = time.monotonic()
    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))
    elapsed = time.monotonic() - start

    calls = host.react_calls()
    if len(calls) != 1:
        print(f"[FAIL] 目标消息查不到时超时回退后应贴 1 次表情，实际 {len(calls)} 次")
        return False
    if elapsed >= 0.5:
        print(f"[FAIL] 目标消息查不到时超时未生效，实际耗时 {elapsed:.2f}s（bug 复现！）")
        return False
    emoji_id = int(calls[0]["payload"]["emoji_id"])
    if emoji_id not in AVAILABLE_REACT_EMOJIS:
        print(f"[FAIL] 回退选出的表情不合法: {emoji_id}")
        return False
    print(
        f"[PASS] 目标消息不在 get_recent 里时，超时 {elapsed*1000:.0f}ms 仍生效，"
        f"回退规则贴 {emoji_id}:{AVAILABLE_REACT_EMOJIS[emoji_id]}"
    )
    return True


async def test_decision_label_reflects_fallback() -> bool:
    """v1.1.2 真机日志暴露的标签 bug：超时回退后 decision 应为 rule 而非 llm。"""
    # 正常 LLM 返回 -> decision=llm
    host2 = FakeHost()
    plugin2 = await make_plugin(host2, transport="http")
    recent2 = await plugin2._get_recent_messages("chat-1", limit=20)
    prompt2 = plugin2._build_prompt("555", "小明", "哈哈哈哈", recent2[:10])
    _, _, decision_ok = await plugin2._select_emoji(prompt2, fallback_text="哈哈哈哈")
    if decision_ok != "llm":
        print(f"[FAIL] LLM 正常返回时 decision 应为 llm，实际 {decision_ok}")
        return False

    # 超时回退 -> decision=rule
    host = FakeHost(llm_delay=0.5)
    plugin = await make_plugin(host, transport="http")
    plugin.config.proactive.llm_timeout_ms = 50
    recent = await plugin._get_recent_messages("chat-1", limit=20)
    prompt = plugin._build_prompt("555", "小明", "哈哈哈哈", recent[:10])
    eid, _, decision = await plugin._select_emoji(prompt, fallback_text="哈哈哈哈")
    if decision != "rule":
        print(f"[FAIL] 超时回退后 decision 应为 rule，实际 {decision}")
        return False
    if int(eid) not in AVAILABLE_REACT_EMOJIS:
        print(f"[FAIL] 回退表情不合法: {eid}")
        return False

    # 非法 ID 在主动路径也应回退（v1.1.2 新增行为）并标 rule
    host3 = FakeHost(llm_reply='{"emoji_id": "99999", "reason": "乱选"}')
    plugin3 = await make_plugin(host3, transport="http")
    recent3 = await plugin3._get_recent_messages("chat-1", limit=20)
    prompt3 = plugin3._build_prompt("555", "小明", "哈哈哈哈", recent3[:10])
    eid3, _, decision3 = await plugin3._select_emoji(prompt3, fallback_text="哈哈哈哈")
    if decision3 != "rule":
        print(f"[FAIL] 非法 ID 回退后 decision 应为 rule，实际 {decision3}")
        return False
    if int(eid3) not in AVAILABLE_REACT_EMOJIS:
        print(f"[FAIL] 非法 ID 回退选出的表情不合法: {eid3}")
        return False

    # 关闭回退（调用方传空 fallback_text）时，超时应直接失败返回 ("", 原因, "")
    eid4, name4, decision4 = await plugin._select_emoji(prompt, fallback_text="")
    if eid4 or decision4:
        print(f"[FAIL] 关闭回退时超时应返回失败，实际 {(eid4, decision4)}")
        return False
    if "超时" not in name4:
        print(f"[FAIL] 失败原因应说明超时，实际 {name4}")
        return False

    print("[PASS] decision 标签正确：正常=llm，超时回退=rule，非法 ID 回退=rule，关闭回退=失败")
    return True


async def test_react_uses_single_get_recent() -> bool:
    """v1.2.1 优化：一次贴表情只应调 1 次 message.get_recent RPC。"""
    host = FakeHost()
    plugin = await make_plugin(host, transport="http")
    _force_proactive(plugin)

    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))

    if not host.react_calls():
        print("[FAIL] 前置条件不满足：贴表情未成功，无法统计 get_recent 次数")
        return False
    if host.get_recent_count != 1:
        print(f"[FAIL] 一次贴表情应只调 1 次 get_recent，实际 {host.get_recent_count} 次")
        return False
    print(f"[PASS] 单次贴表情仅 1 次 get_recent RPC（合并前为 2 次）: {host.get_recent_count}")
    return True


async def test_reacted_ids_sliding_window() -> bool:
    """v1.2.1 优化：去重集合到顶后应滑动挤出最旧一条，而不是整体 clear。"""
    from plugin import _MAX_TRACKED_MESSAGE_IDS

    host = FakeHost()
    plugin = await make_plugin(host, transport="http")

    for i in range(_MAX_TRACKED_MESSAGE_IDS + 1):
        plugin._remember_message_id(f"msg-{i}")

    if "msg-0" in plugin._reacted_message_ids:
        print("[FAIL] 窗口已满后最旧的 msg-0 应被挤出")
        return False
    if f"msg-{_MAX_TRACKED_MESSAGE_IDS}" not in plugin._reacted_message_ids:
        print("[FAIL] 最新写入的 ID 应在窗口内")
        return False
    if "msg-1" not in plugin._reacted_message_ids:
        print("[FAIL] 滑动窗口只应挤掉最旧一条，msg-1 不应丢失（疑似整体 clear）")
        return False
    if len(plugin._reacted_message_ids) != _MAX_TRACKED_MESSAGE_IDS:
        print(f"[FAIL] 窗口大小应恒为 {_MAX_TRACKED_MESSAGE_IDS}，实际 {len(plugin._reacted_message_ids)}")
        return False
    print(f"[PASS] 去重滑动窗口：最旧条目被挤出，其余 {_MAX_TRACKED_MESSAGE_IDS - 1} 条保留")
    return True


async def test_command_selfcheck() -> bool:
    """自检命令应回复中文状态，且绝不回显 Token。"""
    host = FakeHost(adapter_apis=(_NAPCAT_REACT_API, _NAPCAT_PROBE_API))
    plugin = await make_plugin(host, transport="auto")
    plugin.config.napcat.token = "SUPER_SECRET_TOKEN"
    ok, text, level = await plugin.cmd_reacttest(stream_id="chat-1")
    if not ok or not text:
        print(f"[FAIL] 自检命令应返回成功与文案，实际 {(ok, text, level)}")
        return False
    if "SUPER_SECRET_TOKEN" in text:
        print("[FAIL] 自检回显泄露了 Token")
        return False
    if not host.sent_text:
        print("[FAIL] 自检命令没有发出回复")
        return False
    print(f"[PASS] 自检命令正常（不回显 Token）: {text.splitlines()[0]}...")
    return True


async def test_llm_task_and_model_are_split() -> bool:
    """llm.generate 的传参：任务名进 task_name、具体模型名进 model（MaiBot 1.2.5 语义拆分）。

    回归防护——旧写法是 generate(prompt, model=<任务名>)。1.2.5 起 model 被解释成
    「具体模型名」，任务名塞进去会变成「找不到名为 planner 的模型」，整条链路静默失效。
    详见 runtime-gotchas §47。
    """
    host = FakeHost()
    plugin = await make_plugin(host, transport="http")

    # 1) 默认配置：任务名只进 task_name，绝不进 model
    kw = plugin._llm_kwargs()
    if kw != {"task_name": "utils"}:
        print(f"[FAIL] 默认配置应只传 task_name=utils，实际 {kw}")
        return False

    # 2) 只配具体模型名 → 只出 model 键
    plugin.config.napcat.llm_task = ""
    plugin.config.napcat.llm_model = "gpt-4o-mini"
    if plugin._llm_kwargs() != {"model": "gpt-4o-mini"}:
        print(f"[FAIL] 只配模型名时不应出现 task_name，实际 {plugin._llm_kwargs()}")
        return False

    # 3) 两者都配 → 各走各的键，不串位
    plugin.config.napcat.llm_task = "replyer"
    if plugin._llm_kwargs() != {"task_name": "replyer", "model": "gpt-4o-mini"}:
        print(f"[FAIL] 两个键应同时存在且互不串位，实际 {plugin._llm_kwargs()}")
        return False

    # 4) 都留空 → 不传任何键，走 SDK 默认任务 utils
    plugin.config.napcat.llm_task = ""
    plugin.config.napcat.llm_model = ""
    if plugin._llm_kwargs() != {}:
        print(f"[FAIL] 两者留空时不应传任何键，实际 {plugin._llm_kwargs()}")
        return False

    # 5) 端到端：真实调用链里 payload 的 task_name 与配置一致，且任务名没漏进 model
    plugin.config.napcat.llm_task = "planner"
    _force_proactive(plugin)
    host.llm_calls.clear()
    await plugin.observe_group_message(message=GROUP_MESSAGE)
    await asyncio.gather(*list(plugin._tasks))
    if not host.llm_calls:
        print("[FAIL] 端到端未记录到 llm.generate 调用")
        return False
    args = host.llm_calls[-1]
    if args.get("task_name") != "planner" or args.get("model"):
        print(
            "[FAIL] 端到端传参不符（任务名不该进 model）: "
            f"task_name={args.get('task_name')!r} model={args.get('model')!r}"
        )
        return False

    print("[PASS] LLM 传参拆分：task_name / model 各走各的键，留空不传（含端到端）")
    return True


# ---------------------------------------------------------------------------
# v1.3.0 新增：发送通道
# ---------------------------------------------------------------------------
async def test_react_via_adapter_channel() -> bool:
    """transport=adapter 时应走适配器公开 API，且完全不碰 Napcat HTTP。"""
    host = FakeHost(adapter_apis=(_NAPCAT_REACT_API, _NAPCAT_PROBE_API))
    plugin = await make_plugin(host, transport="adapter")
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")

    if not result.get("success"):
        print(f"[FAIL] 适配器通道应贴表情成功，实际 {result}")
        return False
    if len(host.api_react_calls) != 1:
        print(f"[FAIL] 应经适配器发出 1 次贴表情，实际 {len(host.api_react_calls)} 次")
        return False
    call = host.api_react_calls[0]
    if call["api_name"] != _NAPCAT_REACT_API:
        print(f"[FAIL] 应调用 {_NAPCAT_REACT_API}，实际 {call['api_name']}")
        return False
    if str(call.get("message_id")) != "555" or int(call.get("emoji_id") or 0) not in AVAILABLE_REACT_EMOJIS:
        print(f"[FAIL] 适配器入参不对: {call}")
        return False
    if call.get("set") is not True:
        print(f"[FAIL] set 应为 True，实际 {call.get('set')!r}")
        return False
    if host.react_calls():
        print("[FAIL] transport=adapter 时不应再走 Napcat HTTP")
        return False
    print(f"[PASS] 适配器通道贴表情成功: {call['api_name']} emoji_id={call['emoji_id']}")
    return True


async def test_adapter_prefix_prefers_snowluma_when_napcat_absent() -> bool:
    """只有 adapter.snowluma.* 时，应自动改用 snowluma 前缀（两条前缀共享处理器）。"""
    host = FakeHost(adapter_apis=(_SNOWLUMA_REACT_API, _SNOWLUMA_PROBE_API))
    plugin = await make_plugin(host, transport="adapter")
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")

    if not result.get("success"):
        print(f"[FAIL] snowluma 前缀应可用，实际 {result}")
        return False
    if not host.api_react_calls:
        print("[FAIL] 未经适配器发出贴表情")
        return False
    if host.api_react_calls[0]["api_name"] != _SNOWLUMA_REACT_API:
        print(f"[FAIL] 应使用 snowluma 前缀，实际 {host.api_react_calls[0]['api_name']}")
        return False
    print("[PASS] 前缀自动择一：napcat 缺席时改走 adapter.snowluma.*")
    return True


async def test_auto_transport_falls_back_to_http() -> bool:
    """transport=auto 时适配器业务失败应回落 HTTP，并记住 HTTP 这条可用路径。"""
    host = FakeHost(
        adapter_apis=(_NAPCAT_REACT_API, _NAPCAT_PROBE_API),
        adapter_fail_apis=(_NAPCAT_REACT_API,),
    )
    plugin = await make_plugin(host, transport="auto")
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")

    if not result.get("success"):
        print(f"[FAIL] 适配器失败后应回落 HTTP 并成功，实际 {result}")
        return False
    if not host.react_calls():
        print("[FAIL] 未回落到 Napcat HTTP")
        return False
    if plugin._react_transport_used != "http":
        print(f"[FAIL] 应记住 HTTP 为可用路径，实际 {plugin._react_transport_used!r}")
        return False
    if plugin._transport_order()[0] != "http":
        print(f"[FAIL] 下次应优先 HTTP，实际顺序 {plugin._transport_order()}")
        return False
    print("[PASS] auto 通道回落：适配器业务失败 -> HTTP 成功，并记住可用路径")
    return True


async def test_auto_transport_falls_back_when_adapter_raises() -> bool:
    """transport=auto 时适配器 RPC 直接抛异常（未装/未授权）也应回落 HTTP。"""
    host = FakeHost(adapter_raises=True)
    plugin = await make_plugin(host, transport="auto")
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")

    if not result.get("success"):
        print(f"[FAIL] 适配器不可用时应回落 HTTP，实际 {result}")
        return False
    if not host.react_calls():
        print("[FAIL] 未回落到 Napcat HTTP")
        return False
    print("[PASS] auto 通道回落：适配器不可用 -> HTTP 成功")
    return True


async def test_adapter_only_mode_reports_clear_error() -> bool:
    """transport=adapter 且适配器不可用时应明确报错，不静默失败、也不偷偷走 HTTP。"""
    host = FakeHost()
    plugin = await make_plugin(host, transport="adapter")
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")

    if result.get("success"):
        print("[FAIL] 适配器不可用时不应报成功")
        return False
    if host.react_calls():
        print("[FAIL] transport=adapter 不应回落到 HTTP")
        return False
    content = str(result.get("content", ""))
    if "适配器" not in content and "API" not in content:
        print(f"[FAIL] 失败原因应指向适配器，实际 {content}")
        return False
    print(f"[PASS] adapter-only 模式报错清晰（不回退 HTTP）: {content[:60]}...")
    return True


async def test_adapter_unavailable_detected_via_api_list() -> bool:
    """api.list() 拿到结果但没有适配器 API 时应判为不可用，并给出可操作的原因。"""
    host = FakeHost(adapter_apis=("org.example.other.some_api",))
    plugin = await make_plugin(host, transport="auto")
    prefix = await plugin._ensure_adapter_prefix()
    if prefix:
        print(f"[FAIL] 不应解析出前缀，实际 {prefix!r}")
        return False
    if "MaiBot-SnowLuma-Adapter" not in plugin._adapter_last_error:
        print(f"[FAIL] 原因文本应给出可操作提示，实际 {plugin._adapter_last_error!r}")
        return False
    print("[PASS] 适配器缺席时给出可操作原因（指向统一 QQ 连接器适配器）")
    return True


# ---------------------------------------------------------------------------
# v1.3.0 新增：超时识别 / Bearer 鉴权 / 表情表一致性
# ---------------------------------------------------------------------------
def test_is_timeout_recognizes_rpc_error() -> bool:
    """超时判定必须认「RPCError + 文本带超时」，不能只认 asyncio.TimeoutError。

    真机 cap.call 超时抛的是 Runner 的 RPCError（msgpack 重建的类，本地测试里
    拿不到同一个类对象），只用 isinstance 会漏判、把它误报成「选表情异常」。
    """
    class RPCError(Exception):
        """伪装真机 Runner 的 RPCError。"""

    cases = [
        (asyncio.TimeoutError(), True),
        (TimeoutError("timed out"), True),
        (RPCError("[E_TIMEOUT] 请求 cap.call 超时 (180000ms)"), True),
        (RPCError("Request timed out."), True),
        (RPCError("connection refused"), False),
        (ValueError("bad json"), False),
    ]
    for exc, expected in cases:
        actual = _is_timeout(exc)
        if actual != expected:
            print(f"[FAIL] _is_timeout({type(exc).__name__}: {exc}) 应为 {expected}，实际 {actual}")
            return False
    print("[PASS] _is_timeout 三取一判据正确（含真机 RPCError 超时）")
    return True


def test_http_token_uses_bearer_scheme() -> bool:
    """HTTP 通道的 Token 必须以 Authorization: Bearer 发出。

    旧实现直接写原始 token，Napcat 配了 token 时一律 401 —— 贴表情会全量失败。
    """
    host = FakeHost()
    plugin = GroupEmojiReactPlugin()
    plugin.config_model  # 触达配置模型，确认类可用
    plugin.set_plugin_config(plugin.get_default_config())
    plugin.config.napcat.token = "abc123"
    plugin.config.napcat.host = "127.0.0.1"
    plugin.config.napcat.port = 9999

    captured: dict = {}

    class RecordingConn:
        def __init__(self, host, port, timeout=None):
            captured["host"] = host
            captured["port"] = port

        def request(self, method, path, body=None, headers=None):
            captured["headers"] = headers or {}
            captured["path"] = path

        def getresponse(self):
            class Resp:
                status = 200

                def read(self):
                    return b'{"status": "ok", "retcode": 0}'

            return Resp()

        def close(self):
            pass

    original = http.client.HTTPConnection
    http.client.HTTPConnection = RecordingConn
    try:
        ok, _, _ = plugin._napcat_call_sync("POST", "/set_msg_emoji_like", {"message_id": "1"}, 5)
    finally:
        http.client.HTTPConnection = original

    auth = captured.get("headers", {}).get("Authorization", "")
    if not ok:
        print("[FAIL] 录制桩应返回成功")
        return False
    if auth != "Bearer abc123":
        print(f"[FAIL] Authorization 应为 'Bearer abc123'，实际 {auth!r}")
        return False

    # 已经带了 Bearer 前缀时不要重复叠加
    plugin.config.napcat.token = "Bearer xyz"
    http.client.HTTPConnection = RecordingConn
    try:
        plugin._napcat_call_sync("GET", "/get_version_info", None, 5)
    finally:
        http.client.HTTPConnection = original
    auth2 = captured.get("headers", {}).get("Authorization", "")
    if auth2 != "Bearer xyz":
        print(f"[FAIL] 已带 Bearer 前缀时不应重复叠加，实际 {auth2!r}")
        return False

    print("[PASS] HTTP Token 以 Bearer 方案发送（且不重复叠加前缀）")
    return True


def test_emoji_table_matches_qqnt() -> bool:
    """表情 ID 与释义必须与 QQNT emojiId 表一致，否则 LLM 选的和用户看到的是两个表情。

    历史 bug：旧表按序号平移，把 424 续标识当成「狂按按钮」、233 掐一掐当成「笑哭」、
    293 摸锦鲤当成「敲脑瓜」、390 太头秃当成「头秃」、277 汪汪当成「狗头」。
    """
    drifted = []
    for emoji_id, name in AVAILABLE_REACT_EMOJIS.items():
        expected = _QQNT_FACE_NAMES.get(emoji_id)
        if expected is None:
            drifted.append(f"{emoji_id}:{name}（不在 QQNT 核对表内）")
        elif expected != name:
            drifted.append(f"{emoji_id}: 表里写 {name}，QQNT 是 {expected}")
    if drifted:
        print(f"[FAIL] 表情表与 QQNT 释义漂移: {drifted}")
        return False

    invalid = []
    for keywords, candidates in GroupEmojiReactPlugin._RULE_EMOJI_MAP.items():
        for cid in candidates:
            if cid not in AVAILABLE_REACT_EMOJIS:
                invalid.append(f"{keywords} -> {cid}")
    for cid in GroupEmojiReactPlugin._RULE_DEFAULT_EMOJIS:
        if cid not in AVAILABLE_REACT_EMOJIS:
            invalid.append(f"默认池 -> {cid}")
    if invalid:
        print(f"[FAIL] 关键词规则引用了白名单外的表情 ID: {invalid}")
        return False

    print(f"[PASS] 表情表与 QQNT emojiId 一致（{len(AVAILABLE_REACT_EMOJIS)} 个），规则池 ID 全在白名单内")
    return True


async def test_llm_rpc_timeout_is_wider_than_business_timeout() -> bool:
    """外层 RPC 超时必须比内层业务超时宽，否则外层会先断、把「模型慢」报成「代码错」。"""
    host = FakeHost()
    plugin = await make_plugin(host, transport="http")
    plugin.config.proactive.llm_timeout_ms = 5000
    captured: dict = {}

    async def fake_call_capability(capability, timeout_ms=None, **kwargs):
        captured["capability"] = capability
        captured["timeout_ms"] = timeout_ms
        captured["kwargs"] = kwargs
        return {"success": True, "response": '{"emoji_id": "76"}'}

    plugin.ctx.call_capability = fake_call_capability
    await plugin._llm_generate("测试")
    if captured.get("capability") != "llm.generate":
        print(f"[FAIL] 应调用 llm.generate，实际 {captured.get('capability')}")
        return False
    if captured.get("timeout_ms") != 7000:
        print(f"[FAIL] 外层超时应为 5000+2000=7000ms，实际 {captured.get('timeout_ms')}")
        return False
    if captured["kwargs"].get("task_name") != "utils":
        print(f"[FAIL] 默认任务名应为 utils，实际 {captured['kwargs'].get('task_name')!r}")
        return False
    if "prompt" not in captured["kwargs"]:
        print("[FAIL] 载荷应含 prompt")
        return False

    # llm_timeout_ms=0（不限制）时也要给一个有限上界，别被 cap.call 的 30s 默认值悄悄截断
    plugin.config.proactive.llm_timeout_ms = 0
    await plugin._llm_generate("测试")
    if not captured.get("timeout_ms") or captured["timeout_ms"] <= 30000:
        print(f"[FAIL] 不限制时也应给宽松上界（>30000ms），实际 {captured.get('timeout_ms')}")
        return False

    print("[PASS] 双层超时：外层恒比内层宽 2s；不限制时给宽松上界而非落到 30s 默认")
    return True


async def main() -> int:
    async_tests = [
        test_hook_reacts_to_group_message,
        test_hook_ignores_private_message,
        test_hook_dedups_same_message,
        test_tool_rejects_non_group,
        test_tool_reacts_in_group,
        test_llm_bad_emoji_is_rejected,
        test_rule_fallback_disabled_skips,
        test_proactive_llm_timeout_falls_back,
        test_proactive_llm_error_falls_back,
        test_proactive_timeout_when_target_not_in_recent,
        test_decision_label_reflects_fallback,
        test_react_uses_single_get_recent,
        test_reacted_ids_sliding_window,
        test_command_selfcheck,
        test_llm_task_and_model_are_split,
        test_react_via_adapter_channel,
        test_adapter_prefix_prefers_snowluma_when_napcat_absent,
        test_auto_transport_falls_back_to_http,
        test_auto_transport_falls_back_when_adapter_raises,
        test_adapter_only_mode_reports_clear_error,
        test_adapter_unavailable_detected_via_api_list,
        test_llm_rpc_timeout_is_wider_than_business_timeout,
    ]
    sync_tests = [
        test_is_timeout_recognizes_rpc_error,
        test_http_token_uses_bearer_scheme,
        test_emoji_table_matches_qqnt,
    ]

    results = []
    for test in async_tests:
        try:
            results.append(await test())
        except Exception as exc:
            print(f"[FAIL] {test.__name__} 抛异常: {type(exc).__name__}: {exc}")
            results.append(False)
    for test in sync_tests:
        try:
            results.append(test())
        except Exception as exc:
            print(f"[FAIL] {test.__name__} 抛异常: {type(exc).__name__}: {exc}")
            results.append(False)

    passed = sum(1 for r in results if r)
    print("=" * 60)
    print(f"测试结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
