"""群聊贴表情插件的本地行为回归测试（不需要 MaiBot，也不需要真实 Napcat）。

用 FakeHost 模拟 ctx.llm / ctx.message / ctx.send，并把 Napcat HTTP 调用替换成录制桩，
验证「入站消息 -> 提取字段 -> LLM 选表情 -> 调 Napcat」这条主链路。

用法:
    python test_react.py
退出码: 0=全部通过, 1=有失败
"""
import asyncio
import sys
import time
from pathlib import Path

_PLUGIN_DIR = str(Path(__file__).resolve().parent)
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

from maibot_sdk.context import PluginContext, PluginPaths  # noqa: E402

from plugin import AVAILABLE_REACT_EMOJIS, create_plugin  # noqa: E402


class FakeHost:
    """记录插件对宿主的调用，并按能力返回假数据。"""

    def __init__(self, llm_reply: str = '{"emoji_id": "76", "reason": "赞同"}', llm_delay: float = 0.0) -> None:
        self.llm_reply = llm_reply
        self.llm_delay = llm_delay
        self.no_recent = False  # True 时模拟真机 get_recent 查不到任何消息
        self.napcat_calls: list = []
        self.sent_text: list = []
        self.llm_calls: list = []
        self.get_recent_count = 0  # message.get_recent RPC 次数（验证单次贴表情只拉一次）

    def react_calls(self) -> list:
        """只取贴表情调用，排除启动时的 /get_version_info 连通性检测。"""
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


async def make_plugin(host: FakeHost):
    """构造插件并走真实生命周期（Runner 一定会调 on_load）。"""
    plugin = create_plugin()
    ctx = PluginContext(
        plugin_id="org.mai-mai.group-emoji-react",
        rpc_call=host.rpc_call,
        paths=PluginPaths(data_dir=Path(_PLUGIN_DIR) / "data", runtime_dir=Path(_PLUGIN_DIR) / "runtime"),
    )
    plugin._set_context(ctx)
    plugin.set_plugin_config(plugin.get_default_config())

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


async def test_hook_reacts_to_group_message() -> bool:
    """入站群聊消息应触发一次贴表情，且表情 ID 合法。"""
    host = FakeHost()
    plugin = await make_plugin(host)
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1

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
    plugin = await make_plugin(host)
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
    plugin = await make_plugin(host)
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1

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
    """Tool 在非群聊场景应返回可读错误，且不调 Napcat。"""
    host = FakeHost()
    plugin = await make_plugin(host)
    result = await plugin.react_emoji(target_message_id="555", chat_id="chat-1")
    if result.get("success") is not False or "群聊" not in str(result.get("content", "")):
        print(f"[FAIL] 非群聊应返回失败且提示群聊，实际 {result}")
        return False
    if host.react_calls():
        print("[FAIL] 非群聊不应调用 Napcat")
        return False
    print("[PASS] Tool 非群聊场景正确拒绝")
    return True


async def test_tool_reacts_in_group() -> bool:
    """Tool 在群聊场景应贴表情成功并返回中文结果。"""
    host = FakeHost()
    plugin = await make_plugin(host)
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
    """LLM 返回白名单外的表情 ID 时，不能把非法 ID 发给 Napcat。"""
    host = FakeHost(llm_reply='{"emoji_id": "99999", "reason": "乱选"}')
    plugin = await make_plugin(host)
    result = await plugin.react_emoji(target_message_id="555", group_id="123456", chat_id="chat-1")
    if result.get("success"):
        print("[FAIL] 非法表情 ID 不应成功")
        return False
    if host.react_calls():
        print("[FAIL] 非法表情 ID 不应发给 Napcat")
        return False
    print("[PASS] 非法表情 ID 已拦截，未发给 Napcat")
    return True


async def test_rule_fallback_disabled_skips() -> bool:
    """rule_fallback=False（默认）时，LLM 超时应直接跳过，不再回退关键词规则。"""
    host = FakeHost(llm_delay=0.5)  # 500ms 才返回
    plugin = await make_plugin(host)
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1
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
    plugin = await make_plugin(host)
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1
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
    plugin = await make_plugin(host)
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1
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
    plugin = await make_plugin(host)
    # 模拟真机：get_recent 返回的列表里没有目标消息 555
    host.no_recent = True
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1
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
    plugin2 = await make_plugin(host2)
    recent2 = await plugin2._get_recent_messages("chat-1", limit=20)
    prompt2 = plugin2._build_prompt("555", "小明", "哈哈哈哈", recent2[:10])
    _, _, decision_ok = await plugin2._select_emoji(prompt2, fallback_text="哈哈哈哈")
    if decision_ok != "llm":
        print(f"[FAIL] LLM 正常返回时 decision 应为 llm，实际 {decision_ok}")
        return False

    # 超时回退 -> decision=rule
    host = FakeHost(llm_delay=0.5)
    plugin = await make_plugin(host)
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
    plugin3 = await make_plugin(host3)
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
    """v1.2.1 优化：一次贴表情只应调 1 次 message.get_recent RPC。

    旧实现 _get_target_message_info（limit=20）与 _build_prompt（limit=10）各调一次，
    合并后应为 1 次。若未来有人改回两次查询，本测试应变红。
    """
    host = FakeHost()
    plugin = await make_plugin(host)
    plugin.config.proactive.chance = 1.0
    plugin.config.proactive.keyword_chance = 1.0
    plugin.config.proactive.cooldown_seconds = 0
    plugin.config.proactive.min_text_length = 1

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
    plugin = await make_plugin(host)

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
    host = FakeHost()
    plugin = await make_plugin(host)
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
    print(f"[PASS] 自检命令正常（不回显 Token）: {text[:40]}...")
    return True


async def main() -> int:
    tests = [
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
    ]
    results = []
    for test in tests:
        try:
            results.append(await test())
        except Exception as exc:
            print(f"[FAIL] {test.__name__} 抛异常: {type(exc).__name__}: {exc}")
            results.append(False)

    passed = sum(1 for r in results if r)
    print("=" * 60)
    print(f"测试结果: {passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
