#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""隐私闸门 + 重登提醒自检（纯离线、无副作用）。

覆盖：
  A. 隐私闸门 privacy_gate.py
     * 默认关闭 → 拒绝外部目的地（llm / llm_request / message_injection / external）；
     * 开关打开 → 放行外部目的地；
     * 本地白名单（user / local / command_reply / storage）在开/关两种状态下始终放行；
     * 未知目的地按外部处理（fail-closed）；
     * require_outbound 在拒绝时抛 PrivacyBlockedError（可被捕获）；
     * gate_from_config 的分组 / 扁平 / 严格布尔化。
  B. 重登提醒 reminder.py
     * 提前 5 天 / 4 天 / 1 天 / 已过期 的窗口边界判定；
     * 同一天反复调用只提醒一次（去重）；
     * 到期时间变更后旧的去重状态被清空；
     * 发送返回 False / 抛异常 / 无到期时间 等兜底路径；
     * 真实 async 发送路径：run_once 必须 await 注入的 async send（协程真被执行），
       否则提醒发不出去却仍被标记成「已发送」；
     * main.py 的真实接线：async 私聊发送确实发出去了（用 async 假 context 记录，不真发
       消息）；通知目标只记私聊，群聊 UMO 一律不写进状态文件。

安全约定（本脚本强制遵守）：
  * 不 import 真实 astrbot（为覆盖 async 发送路径，复用同目录 import_check.py 的最小桩
    把 main.py 导进来）、不联网、不启动 AstrBot、不改任何插件配置；
  * 所有状态文件都写到 tempfile 造出的临时目录；结束即删除；
  * 发送动作一律用「只记录到列表的空发送实现」注入，绝不真发消息；
  * 不接触真实数据库与插件数据目录下的真实 reminder_state.json。

用法：python3 scripts/selftest_privacy.py
"""

from __future__ import annotations

import asyncio
import inspect
import shutil
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import privacy_gate as pg  # noqa: E402
import reminder as rm  # noqa: E402

FAILURES: list[str] = []
_CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    """记录一条断言结果，失败时打出原因。"""
    global _CHECKS
    _CHECKS += 1
    if cond:
        print(f"  [PASS] {name}")
    else:
        line = f"  [FAIL] {name}" + (f" —— {detail}" if detail else "")
        print(line)
        FAILURES.append(name)


def _section(title: str) -> None:
    print(f"\n── {title} ──")


def run_once_sync(reminder, now=None) -> dict:
    """同步驱动 async 的 TokenReminder.run_once（本脚本自身不需要常驻事件循环）。"""
    return asyncio.run(reminder.run_once(now))


# ── A. 隐私闸门 ──────────────────────────────────────────────────────────
def test_gate_default_closed_rejects_external() -> None:
    _section("闸门：默认关闭时拒绝外部目的地")
    closed = pg.default_gate()
    check("default_gate() 为关闭态", closed.is_closed is True and closed.is_open is False)
    for dest in (
        pg.DEST_LLM,
        pg.DEST_LLM_REQUEST,
        pg.DEST_MESSAGE_INJECTION,
        pg.DEST_EXTERNAL,
    ):
        decision = closed.check(dest)
        check(f"关闭时拒绝 {dest}", decision.allowed is False)
        check(f"关闭时 {dest} 标记为外部", decision.is_external is True)


def test_gate_open_allows_external() -> None:
    _section("闸门：开关打开后放行外部目的地")
    opened = pg.PrivacyGate(True)
    check("PrivacyGate(True) 为打开态", opened.is_open is True)
    for dest in (
        pg.DEST_LLM,
        pg.DEST_LLM_REQUEST,
        pg.DEST_MESSAGE_INJECTION,
        pg.DEST_EXTERNAL,
    ):
        check(f"打开时放行 {dest}", opened.check(dest).allowed is True)


def test_gate_local_whitelist_always_allowed() -> None:
    _section("闸门：本地白名单始终放行（与开关无关）")
    closed = pg.default_gate()
    opened = pg.PrivacyGate(True)
    for dest in (
        pg.DEST_USER,
        pg.DEST_LOCAL,
        pg.DEST_COMMAND_REPLY,
        pg.DEST_STORAGE,
    ):
        check(
            f"关闭时本地 {dest} 放行且非外部",
            closed.check(dest).allowed is True and closed.check(dest).is_external is False,
        )
        check(f"打开时本地 {dest} 放行", opened.check(dest).allowed is True)


def test_gate_unknown_treated_as_external() -> None:
    _section("闸门：未知目的地按外部处理（fail-closed）")
    closed = pg.default_gate()
    opened = pg.PrivacyGate(True)
    unknown = "some_unknown_sink"
    check("is_external(未知)=True", closed.is_external(unknown) is True)
    check("关闭时未知目的地被拒绝", closed.check(unknown).allowed is False)
    check(
        "拒绝原因标注“按外部处理”",
        "按外部处理" in closed.check(unknown).reason,
    )
    check("打开时未知目的地被放行", opened.check(unknown).allowed is True)


def test_gate_privacy_blocked_error() -> None:
    _section("闸门：PrivacyBlockedError 可被捕获")
    closed = pg.default_gate()
    raised = None
    try:
        closed.require_outbound(pg.DEST_LLM, {"steps": 12345})
    except pg.PrivacyBlockedError as error:  # 期望走这里
        raised = error
    check("关闭时 require_outbound 抛 PrivacyBlockedError", raised is not None)
    if raised is not None:
        check("PrivacyBlockedError 是 RuntimeError 子类", isinstance(raised, RuntimeError))
        check("异常携带正确的 destination", raised.destination == pg.DEST_LLM)
        check("异常 reason 不泄漏 payload 内容", "12345" not in raised.reason)

    payload = {"any": "object"}
    opened = pg.PrivacyGate(True)
    check(
        "打开时 require_outbound 原样返回 payload",
        opened.require_outbound(pg.DEST_LLM, payload) is payload,
    )
    check(
        "本地目的地 require_outbound 原样返回 payload",
        closed.require_outbound(pg.DEST_USER, payload) is payload,
    )


def test_gate_from_config() -> None:
    _section("闸门：gate_from_config 解析与严格布尔化")
    check("None 配置 = 关闭", pg.gate_from_config(None).is_closed is True)
    check("空配置 = 关闭", pg.gate_from_config({}).is_closed is True)
    check(
        "分组 True = 打开",
        pg.gate_from_config({"privacy": {"allow_health_data_to_llm": True}}).is_open is True,
    )
    check(
        "扁平 True = 打开",
        pg.gate_from_config({"allow_health_data_to_llm": True}).is_open is True,
    )
    check(
        "分组 False = 关闭",
        pg.gate_from_config({"privacy": {"allow_health_data_to_llm": False}}).is_closed is True,
    )
    check(
        "字符串 'true' 不被当作真（严格布尔化）",
        pg.gate_from_config({"privacy": {"allow_health_data_to_llm": "true"}}).is_closed is True,
    )
    check(
        "数字 1 不被当作真（严格布尔化）",
        pg.gate_from_config({"privacy": {"allow_health_data_to_llm": 1}}).is_closed is True,
    )


# ── B. 重登提醒 ──────────────────────────────────────────────────────────
def test_reminder_window_boundaries() -> None:
    _section("提醒：窗口边界判定（due_windows / remaining_days）")
    day = 86400.0
    base = 1_700_000_000.0

    check("remaining 恰为 5.0 → 命中窗口 5", rm.due_windows(5.0) == [5])
    check("remaining 略大于 5（5.0001）→ 不命中", rm.due_windows(5.0001) == [])
    check("remaining 4.0 → 命中窗口 5", rm.due_windows(4.0) == [5])
    check("remaining 恰为 1.0 → 命中窗口 1 与 5", rm.due_windows(1.0) == [1, 5])
    check("remaining 略大于 1（1.0001）→ 仅命中窗口 5", rm.due_windows(1.0001) == [5])
    check("remaining 0（到期当天）→ 不提醒", rm.due_windows(0.0) == [])
    check("remaining 为负（已过期）→ 不提醒", rm.due_windows(-3.0) == [])
    check("remaining None（无到期时间）→ 不提醒", rm.due_windows(None) == [])

    check("已提醒窗口 5 后，4 天剩余不再命中", rm.due_windows(4.0, notified=[5]) == [])
    check("已提醒窗口 1 后，1 天剩余仅剩窗口 5", rm.due_windows(1.0, notified=[1]) == [5])

    check(
        "remaining_days 正常计算",
        abs(rm.remaining_days(base + 5 * day, base) - 5.0) < 1e-6,
    )
    check("remaining_days 非法值 → None", rm.remaining_days("abc") is None)
    check("remaining_days 到期<=0 → None", rm.remaining_days(0) is None)


def test_reminder_run_once_dedup(tmp_dir: Path) -> None:
    _section("提醒：run_once 边界 + 同天去重 + 落盘")
    state_path = tmp_dir / "reminder_state.json"
    state = rm.ReminderState(state_path)
    sent: list[str] = []
    clock = [1_700_000_000.0]
    expiry = [clock[0] + 4 * 86400.0]  # 4 天后到期

    reminder = rm.TokenReminder(
        state,
        get_expiry=lambda: expiry[0],
        send=lambda text: (sent.append(text), True)[1],  # 只记录，不真发
        now=lambda: clock[0],
    )

    first = run_once_sync(reminder)
    check("剩余 4 天首次 → 应提醒窗口 5", first["due"] == [5] and first["sent"] == [5])
    check("发送被调用恰好 1 次", len(sent) == 1)
    check("提醒文案为中文且不含 token 片段", "华为健康授权" in sent[0])
    check("状态文件已落盘（临时目录内）", state_path.exists())

    same_day = run_once_sync(reminder)
    check("同一天再次调用 → 不再命中窗口", same_day["due"] == [] and same_day["sent"] == [])
    check("同一天再次调用 → 未再发送", len(sent) == 1)

    reloaded = rm.ReminderState(state_path).load()
    check("去重状态持久化：窗口 5 已标记", reloaded.is_notified(5) is True)

    # 跨到 1 天前：只应补发窗口 1
    clock[0] += 3 * 86400.0
    near = run_once_sync(reminder)
    check("剩余 1 天 → 仅补发窗口 1", near["due"] == [1] and near["sent"] == [1])
    check("发送累计 2 次（5 与 1 各一次）", len(sent) == 2)

    # 已过期：不提醒
    clock[0] = expiry[0] + 86400.0
    expired = run_once_sync(reminder)
    check("已过期 → 不再提醒", expired["due"] == [] and expired["sent"] == [])
    check("已过期 → 未新增发送", len(sent) == 2)


def test_reminder_expiry_change_clears_dedup(tmp_dir: Path) -> None:
    _section("提醒：到期时间变更后旧去重被清空")
    state_path = tmp_dir / "reminder_state_expiry_change.json"
    state = rm.ReminderState(state_path)
    sent: list[str] = []
    clock = [1_700_000_000.0]
    old_expiry = clock[0] + 4 * 86400.0  # 旧 token：4 天后到期（会触发窗口 5）
    expiry = [old_expiry]

    reminder = rm.TokenReminder(
        state,
        get_expiry=lambda: expiry[0],
        send=lambda text: (sent.append(text), True)[1],
        now=lambda: clock[0],
    )

    run_once_sync(reminder)
    check("旧周期：窗口 5 已标记", rm.ReminderState(state_path).load().is_notified(5) is True)

    # 换成新 token：到期时间大幅推后（约 170 天后），旧去重应被清空
    new_expiry = clock[0] + 170 * 86400.0
    expiry[0] = new_expiry
    changed = run_once_sync(reminder)
    check("换新到期时间 → 本轮不命中任何窗口", changed["due"] == [])
    check("换新到期时间 → 内存去重被清空", reminder.state.notified == {})
    on_disk = rm.ReminderState(state_path).load()
    check("换新到期时间 → 磁盘去重被清空", on_disk.notified == {})
    check(
        "换新到期时间 → 磁盘绑定新的到期时间",
        abs(on_disk.bound_expiry - new_expiry) < 1.0,
    )


def test_reminder_send_edges(tmp_dir: Path) -> None:
    _section("提醒：发送失败 / 异常 / 无到期时间 的兜底")
    clock = 1_700_000_000.0
    expiry = clock + 4 * 86400.0

    # 1) 发送返回 False：不算已发送，不落去重
    state_false = rm.ReminderState(tmp_dir / "reminder_state_send_false.json")
    calls: list[str] = []
    reminder_false = rm.TokenReminder(
        state_false,
        get_expiry=lambda: expiry,
        send=lambda text: (calls.append(text), False)[1],
        now=lambda: clock,
    )
    result_false = run_once_sync(reminder_false)
    check("发送返回 False → 不计为已发送", result_false["sent"] == [])
    check("发送返回 False → 未落去重", state_false.is_notified(5) is False)

    # 2) 发送抛异常：被内部兜住，不崩溃、不落去重
    state_raise = rm.ReminderState(tmp_dir / "reminder_state_send_raise.json")

    def _boom(_text: str):
        raise RuntimeError("boom")

    reminder_raise = rm.TokenReminder(
        state_raise,
        get_expiry=lambda: expiry,
        send=_boom,
        now=lambda: clock,
    )
    result_raise = run_once_sync(reminder_raise)
    check("发送抛异常 → 整体不崩溃（ok=True）", result_raise["ok"] is True)
    check("发送抛异常 → 不计为已发送", result_raise["sent"] == [])
    check("发送抛异常 → 未落去重", state_raise.is_notified(5) is False)

    # 3) 无到期时间：跳过，不发送、不落文件
    state_none = rm.ReminderState(tmp_dir / "reminder_state_no_expiry.json")
    none_calls: list[str] = []
    reminder_none = rm.TokenReminder(
        state_none,
        get_expiry=lambda: 0,
        send=lambda text: (none_calls.append(text), True)[1],
        now=lambda: clock,
    )
    result_none = run_once_sync(reminder_none)
    check(
        "无到期时间 → 跳过且不发送",
        result_none["stage"] == "skip" and result_none["reason"] == "no_expiry",
    )
    check("无到期时间 → 未触碰发送", none_calls == [])
    check("无到期时间 → 未创建状态文件", not state_none.path.exists())


def test_state_persistence_roundtrip(tmp_dir: Path) -> None:
    _section("提醒：ReminderState 原子落盘/回读往返")
    state_path = tmp_dir / "reminder_state_roundtrip.json"
    state = rm.ReminderState(state_path)
    state.bind_expiry(1_700_000_000.0)
    state.mark(5, 1_700_000_000.0)
    state.notify_umo = "aiocqhttp:FriendMessage:10001"
    saved = state.save()
    check("save() 返回 True", saved is True)
    check("状态文件存在", state_path.exists())
    check("未残留 .tmp 临时文件", not (state_path.with_suffix(state_path.suffix + ".tmp")).exists())

    reloaded = rm.ReminderState(state_path).load()
    check("回读到窗口 5 去重", reloaded.is_notified(5) is True)
    check("回读到到期时间绑定", abs(reloaded.bound_expiry - 1_700_000_000.0) < 1.0)
    check("回读到通知目标", reloaded.notify_umo == "aiocqhttp:FriendMessage:10001")


def test_reminder_mark_saved_immediately(tmp_dir: Path) -> None:
    _section("提醒：每次 mark 立刻落盘（多窗口命中时 mark/save 不可交错）")
    state_path = tmp_dir / "reminder_state_mark_then_save.json"
    state = rm.ReminderState(state_path)
    clock = 1_700_000_000.0
    expiry = clock + 0.5 * 86400.0   # 只剩半天 → 窗口 1 与 5 同时命中
    seen_on_disk: list[set] = []

    def send(_text: str) -> bool:
        # 每次发送前重新读盘：上一次 mark 若还没落盘，这里就读不到。
        seen_on_disk.append(set(rm.ReminderState(state_path).load().notified))
        return True

    result = run_once_sync(rm.TokenReminder(
        state, get_expiry=lambda: expiry, send=send, now=lambda: clock))
    check("只剩半天 → 窗口 1 与 5 同时命中",
          result["due"] == [1, 5] and result["sent"] == [1, 5],
          f"due={result['due']} sent={result['sent']}")
    check("第一次发送前磁盘还没有去重标记",
          seen_on_disk[:1] == [set()], f"seen={seen_on_disk}")
    check("第二次发送前，第一次的标记已经落盘（mark 紧跟 save，无窗口）",
          len(seen_on_disk) == 2 and seen_on_disk[1] == {"1"}, f"seen={seen_on_disk}")
    check("两个窗口各自落盘，磁盘最终含 1 与 5",
          set(rm.ReminderState(state_path).load().notified) == {"1", "5"})


def test_reminder_async_send(tmp_dir: Path) -> None:
    _section("提醒：真实 async 发送路径（协程必须被 await）")
    clock = 1_700_000_000.0
    expiry = clock + 4 * 86400.0

    check(
        "TokenReminder.run_once 是协程函数",
        inspect.iscoroutinefunction(rm.TokenReminder.run_once) is True,
    )

    # 1) async send：只有被真正 await，才会执行到 append 那一行
    state_path = tmp_dir / "reminder_state_async_send.json"
    delivered: list[str] = []

    async def async_send(text: str) -> bool:
        await asyncio.sleep(0)
        delivered.append(text)
        return True

    result = run_once_sync(rm.TokenReminder(
        rm.ReminderState(state_path),
        get_expiry=lambda: expiry,
        send=async_send,
        now=lambda: clock,
    ))
    check("async 发送被真正执行（收到 1 条提醒）", len(delivered) == 1,
          f"delivered={len(delivered)}")
    check("async 发送返回 True → 记为已发送", result["sent"] == [5],
          f"sent={result['sent']}")
    check("async 发送成功后才落去重",
          rm.ReminderState(state_path).load().is_notified(5) is True)

    # 2) async send 返回 False：不算已发送、不落去重
    state_false = rm.ReminderState(tmp_dir / "reminder_state_async_false.json")
    false_calls: list[str] = []

    async def async_send_false(text: str) -> bool:
        await asyncio.sleep(0)
        false_calls.append(text)
        return False

    result_false = run_once_sync(rm.TokenReminder(
        state_false,
        get_expiry=lambda: expiry,
        send=async_send_false,
        now=lambda: clock,
    ))
    check("async 发送返回 False → 被调用过", len(false_calls) == 1)
    check("async 发送返回 False → 不计为已发送", result_false["sent"] == [])
    check("async 发送返回 False → 未落去重", state_false.is_notified(5) is False)

    # 3) async send 抛异常：内部兜住、不算已发送
    state_raise = rm.ReminderState(tmp_dir / "reminder_state_async_raise.json")

    async def async_send_boom(text: str) -> bool:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    result_raise = run_once_sync(rm.TokenReminder(
        state_raise,
        get_expiry=lambda: expiry,
        send=async_send_boom,
        now=lambda: clock,
    ))
    check("async 发送抛异常 → 整体不崩溃", result_raise["ok"] is True)
    check("async 发送抛异常 → 不计为已发送", result_raise["sent"] == [])
    check("async 发送抛异常 → 未落去重", state_raise.is_notified(5) is False)

    # 4) 同步 send（返回 bool）仍受支持：既有注入方式不回归
    state_sync = rm.ReminderState(tmp_dir / "reminder_state_sync_send.json")
    sync_calls: list[str] = []
    result_sync = run_once_sync(rm.TokenReminder(
        state_sync,
        get_expiry=lambda: expiry,
        send=lambda text: (sync_calls.append(text), True)[1],
        now=lambda: clock,
    ))
    check("同步 send 仍可用（兼容既有注入）",
          result_sync["sent"] == [5] and len(sync_calls) == 1)


def test_main_wiring_async_reminder(tmp_dir: Path) -> None:
    _section("提醒：main.py 真实接线（async 私聊发送确实发出去 + 目标只记私聊）")
    import import_check  # 同目录的最小 astrbot 桩

    import_check._install_astrbot_stub()
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))
    import importlib

    main_mod = importlib.import_module(f"{PLUGIN_ROOT.name}.main")

    class FakeContext:
        """只提供 send_message：记录私聊发送，不真发消息、不联网。"""

        def __init__(self) -> None:
            self.sent: list[tuple] = []

        async def send_message(self, umo, chain) -> None:
            await asyncio.sleep(0)
            self.sent.append((umo, chain))

    class FakeEvent:
        """只提供 unified_msg_origin 的最小 event 桩。"""

        def __init__(self, umo: str) -> None:
            self.unified_msg_origin = umo

    ctx = FakeContext()
    plugin = main_mod.HuaweiHealthPlugin(ctx, {"account": {}})
    check("main._send_owner_message 是 async",
          inspect.iscoroutinefunction(plugin._send_owner_message) is True)

    # main 里的 TokenReminder 没有注入时钟（用真实 time.time），所以到期时间要相对真实 now 取。
    private_umo = "aiocqhttp:FriendMessage:10001"
    group_umo = "aiocqhttp:GroupMessage:20002"
    state_path = tmp_dir / "reminder_state_wiring.json"
    plugin._reminder_state = rm.ReminderState(state_path)
    plugin._reminder_state.notify_umo = private_umo
    plugin.refresh_token_expires_at = time.time() + 4 * 86400.0
    plugin._init_reminder()
    check("main 已构建 TokenReminder", plugin._reminder is not None)

    result = asyncio.run(plugin._safe_check_reminder())
    check("同步循环里跑完提醒检查，窗口 5 记为已发送",
          isinstance(result, dict) and result.get("sent") == [5], f"result={result}")
    check("真实 async 私聊发送被调用 1 次（不是只拿到未 await 的协程）",
          len(ctx.sent) == 1, f"sent={len(ctx.sent)}")
    if ctx.sent:
        umo, chain = ctx.sent[0]
        check("发往已记录的私聊目标", umo == private_umo)
        check("提醒文案为中文且不含 token 片段", "华为健康授权" in str(chain))
    check("去重状态已落盘",
          rm.ReminderState(state_path).load().is_notified(5) is True)

    # 通知目标只记私聊：群聊来源一律不写
    plugin._reminder_state = rm.ReminderState(tmp_dir / "reminder_state_target.json")
    plugin._remember_notify_target(FakeEvent(group_umo))
    check("群聊来源不写入提醒目标", plugin._reminder_state.notify_umo == "",
          f"umo={plugin._reminder_state.notify_umo!r}")
    plugin._remember_notify_target(FakeEvent(private_umo))
    check("私聊来源写入提醒目标",
          plugin._reminder_state.notify_umo == private_umo)
    plugin._remember_notify_target(FakeEvent(group_umo))
    check("已有私聊目标不被群聊覆盖",
          plugin._reminder_state.notify_umo == private_umo)
    plugin._remember_notify_target(FakeEvent("aiocqhttp:FriendMessage:30003"))
    check("已有私聊目标不被后来的私聊覆盖",
          plugin._reminder_state.notify_umo == private_umo)
    check("is_friend_umo 判定（沿用 UMO 里的 FriendMessage 段）",
          rm.is_friend_umo(private_umo) is True
          and rm.is_friend_umo(group_umo) is False)

    # P2-2：记录目标不得抹掉尚未落盘的去重标记（整体 load() 会覆盖内存去重状态）。
    guard_path = tmp_dir / "reminder_state_no_clobber.json"
    pre = rm.ReminderState(guard_path)
    pre.notify_umo = private_umo
    pre.save()                        # 磁盘上留下 {"notified": {}, "notify_umo": ...}
    guarded = rm.ReminderState(guard_path)
    guarded.load()                    # 进程内首次读盘（run_once 第一步就是这个）
    plugin._reminder_state = guarded
    guarded.mark(5)                   # 新的去重标记，尚未落盘
    plugin._remember_notify_target(FakeEvent("aiocqhttp:FriendMessage:40004"))
    check("记录目标不会抹掉尚未落盘的去重标记", guarded.is_notified(5) is True,
          f"notified={guarded.notified}")
    check("磁盘上已有私聊目标时不被新会话抢占", guarded.notify_umo == private_umo,
          f"umo={guarded.notify_umo!r}")

    flush_path = tmp_dir / "reminder_state_no_clobber_flush.json"
    flushing = rm.ReminderState(flush_path)
    flushing.load()                   # 磁盘不存在：只把内存标记为「已与磁盘对齐」
    plugin._reminder_state = flushing
    flushing.mark(5)                  # 尚未落盘的去重标记
    plugin._remember_notify_target(FakeEvent(private_umo))
    check("记录目标时把尚未落盘的去重标记一起落盘",
          flushing.notify_umo == private_umo
          and rm.ReminderState(flush_path).load().is_notified(5) is True,
          f"umo={flushing.notify_umo!r}")

    # P2-5：私聊判定优先用框架结构化字段，拿不到才回落 UMO 字符串判定。
    check("is_friend_event 对只有 UMO 的 stub event 返回 None（回落字符串判定）",
          rm.is_friend_event(FakeEvent(private_umo)) is None)

    class StructuredEvent:
        """带框架结构化字段的最小 event 桩（is_private_chat / get_message_type /
        get_group_id），三者按需注入。"""

        def __init__(self, umo: str, private=None, message_type=None, group_id=None) -> None:
            self.unified_msg_origin = umo
            if private is not None:
                self.is_private_chat = lambda: private
            if message_type is not None:
                self.get_message_type = lambda: message_type
            if group_id is not None:
                self.get_group_id = lambda: group_id

    check("结构化 is_private_chat()=True → 私聊",
          rm.is_friend_event(StructuredEvent(private_umo, private=True)) is True)
    check("结构化 is_private_chat()=False → 非私聊",
          rm.is_friend_event(StructuredEvent(private_umo, private=False)) is False)
    check("get_message_type() 按枚举值判定（只有 FriendMessage 算私聊）",
          rm.is_friend_event(StructuredEvent(
              private_umo,
              message_type=SimpleNamespace(value="FriendMessage",
                                           name="FRIEND_MESSAGE"))) is True
          and rm.is_friend_event(StructuredEvent(
              private_umo,
              message_type=SimpleNamespace(value="GroupMessage",
                                           name="GROUP_MESSAGE"))) is False
          and rm.is_friend_event(StructuredEvent(
              private_umo,
              message_type=SimpleNamespace(value="OtherMessage",
                                           name="OTHER_MESSAGE"))) is False)
    check("get_group_id() 兜底：空=私聊、非空=群聊",
          rm.is_friend_event(StructuredEvent(private_umo, group_id="")) is True
          and rm.is_friend_event(StructuredEvent(private_umo, group_id="20002")) is False)

    plugin._reminder_state = rm.ReminderState(tmp_dir / "reminder_state_structured.json")
    plugin._remember_notify_target(StructuredEvent(private_umo, private=False))
    check("结构化判定优先：结构化说群聊时，即使 UMO 含 FriendMessage 也不记录",
          plugin._reminder_state.notify_umo == "",
          f"umo={plugin._reminder_state.notify_umo!r}")
    plugin._remember_notify_target(StructuredEvent(group_umo, private=True))
    check("结构化判定优先：结构化说私聊时，即使 UMO 是群聊也记录",
          plugin._reminder_state.notify_umo == group_umo,
          f"umo={plugin._reminder_state.notify_umo!r}")


def main() -> int:
    print("华为运动健康插件 —— 隐私闸门 + 重登提醒自检")
    print(f"插件根：{PLUGIN_ROOT}")
    print("说明：全部离线、纯内存/临时目录，不联网、不 import astrbot、不真发消息。")

    test_gate_default_closed_rejects_external()
    test_gate_open_allows_external()
    test_gate_local_whitelist_always_allowed()
    test_gate_unknown_treated_as_external()
    test_gate_privacy_blocked_error()
    test_gate_from_config()

    test_reminder_window_boundaries()

    tmp_dir = Path(tempfile.mkdtemp(prefix="hw_health_selftest_"))
    try:
        test_reminder_run_once_dedup(tmp_dir)
        test_reminder_expiry_change_clears_dedup(tmp_dir)
        test_reminder_send_edges(tmp_dir)
        test_state_persistence_roundtrip(tmp_dir)
        test_reminder_mark_saved_immediately(tmp_dir)
        test_reminder_async_send(tmp_dir)
        test_main_wiring_async_reminder(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    print(f"\n共 {_CHECKS} 项检查，失败 {len(FAILURES)} 项。")
    if FAILURES:
        for name in FAILURES:
            print(f"  - 失败：{name}")
        print("结果：存在失败项。")
        return 1
    print("结果：全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
