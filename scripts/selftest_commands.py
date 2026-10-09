#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""查询命令层自检：临时库造数据 → stub event 直调命令 handler → 打印中文输出。

覆盖三类情形：
  * 有数据：造今天/昨天/前天的活动，今天的睡眠+心率，某天的部分心率，今天的训练会话；
  * 无数据：空库查活动/训练，显式日期查睡眠（命中无数据分支）；
  * 身份门：拥有者可正常用命令 / 非拥有者被静默拒绝 / 拿不到结构化身份字段即拒绝。

设计约定（沿用本插件其余 selftest）：
  * 只读设计之外的一切都发生在 tempfile 临时目录，不碰真实 health.db；
  * 命令 handler 层不 import astrbot，event 用最小 stub；
  * 身份门用例要驱动 main.py 的命令包装器（装饰器依赖框架），故复用
    import_check 的 astrbot 桩导入 main.py，仍不启动 AstrBot、不联网；
  * 退出码：全部 PASS → 0；任一 FAIL → 1。

用法：
    python3 scripts/selftest_commands.py
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
PKG = PLUGIN_ROOT.name
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

import import_check  # noqa: E402  （复用其 astrbot 桩，供身份门用例导入 main.py）

import_check._install_astrbot_stub()

commands = importlib.import_module(f"{PKG}.commands")
storage = importlib.import_module(f"{PKG}.storage")
main_mod = importlib.import_module(f"{PKG}.main")
HealthStore = storage.HealthStore
models = storage.models

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


class StubResult:
    """模拟宿主消息结果对象，只保留渲染文本。"""

    def __init__(self, text: str) -> None:
        self.text = text


class StubEvent:
    """最小 event 桩：handler 只用到 message_str 与 plain_result。"""

    def __init__(self, message: str) -> None:
        self.message_str = message

    def plain_result(self, text: str) -> StubResult:
        return StubResult(text)


async def run_handler(handler, store, message: str) -> str:
    """驱动一个 async generator handler，收集并拼接输出文本。"""
    event = StubEvent(message)
    chunks: list[str] = []
    async for result in handler(store, event):
        chunks.append(result.text if hasattr(result, "text") else str(result))
    return "\n".join(chunks)


def _compact(text: str) -> str:
    return text.replace("\n", " / ")


# ── 身份门用例的桩（只放行使用者本人 = 框架管理员）────────────────────────
class OwnerEvent(StubEvent):
    """拥有者事件：框架结构化字段 ``is_admin()`` 为 True。"""

    def is_admin(self) -> bool:
        return True


class GuestEvent(StubEvent):
    """非拥有者事件：``is_admin()`` 为 False，但带私聊 UMO（撞开门会写提醒目标）。"""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.unified_msg_origin = "aiocqhttp:FriendMessage:20002"

    def is_admin(self) -> bool:
        return False


class AnonEvent(StubEvent):
    """拿不到任何结构化身份字段的事件：既无 ``is_admin`` 也无 ``get_sender_id``。"""


class GateContext:
    """最小 Context 桩：只提供身份门回落路径要读的全局 admins_id 白名单。"""

    def __init__(self, admins: list[str]) -> None:
        self._admins = list(admins)

    def get_config(self) -> dict:
        return {"admins_id": list(self._admins)}


async def run_command(wrapper, event) -> str:
    """驱动 main.py 里的命令包装器（async generator），收集并拼接输出文本。"""
    chunks: list[str] = []
    async for result in wrapper(event):
        chunks.append(result.text if hasattr(result, "text") else str(result))
    return "\n".join(chunks)


def seed(store: HealthStore) -> dict:
    """往临时库造数据，返回用到的日期。

    活动行直接复刻真机样本（2026-10-07）：duration_min 恒 0 是云端没测到，
    真正有值的是 walk_min / active_hours / exercise_min；另造假 0 行与缺字段行，
    用来验证「无值显示」分支。
    """
    today = date.today()
    yesterday = today - timedelta(days=1)
    older = today - timedelta(days=2)
    store.upsert_daily_activity([
        {  # 真机样本：duration_min=0 应被忽略，walk/active/exercise 应展示
            "date": today.isoformat(), "sport_type": 0, "steps": 3172,
            "distance_m": 2448, "kcal": 121.7, "duration_min": 0,
            "walk_min": 63, "active_hours": 5, "exercise_min": 12, "step_goal": 10000,
        },
        {  # 显式 0：不能显示 0，应渲染成「无」
            "date": yesterday.isoformat(), "sport_type": 0, "steps": 1000,
            "distance_m": 900, "kcal": 30.0, "duration_min": 10,
            "walk_min": 0, "active_hours": 0, "exercise_min": 0,
        },
        {  # 缺失：同样渲染成「无」
            "date": older.isoformat(), "sport_type": 0, "steps": 500,
            "distance_m": None, "kcal": None,
        },
    ])

    # 真实路径：门面 iter_<类>() = 「协议层整形行 → storage.models 归一」，同步服务再按类
    # 走 store.upsert_rows(模型名, 行) 直写（upsert_health 已随存储层收口删除）。
    health_groups = models.normalize_health([
        {  # 今天：睡眠 + 心率五项齐全（含入睡/起床时刻与白天小睡）
            "date": today.isoformat(),
            "resting_heart_rate": 58, "heart_rate": 72,
            "average_resting_heart_rate": 60, "max_heart_rate": 135, "min_heart_rate": 49,
            "sleep_duration": 432, "sleep_score": 83, "sleep_efficiency": 92,
            "sleep_hrv": 45.5, "sleep_spo2": 97,
            "fall_asleep": f"{today.isoformat()} 03:14",
            "wakeup": f"{today.isoformat()} 09:42",
            "nap_duration": 74,
        },
        {  # 更早某天：只有日间心率，其余字段应提示「无记录」
            "date": older.isoformat(), "heart_rate": 80,
        },
    ])
    for model, name in (("heart_rate_sample", "heart_rate"),
                        ("sleep_session", "sleep"),
                        ("stress_sample", "stress"),
                        ("spo2_sample", "spo2")):
        store.upsert_rows(model, health_groups[name])

    start_ms = int(datetime(today.year, today.month, today.day, 8, 0, 0).timestamp() * 1000)
    minute = 60 * 1000
    # 真实路径：门面 iter_training() = models.merge_training_segments(原始分钟段) → upsert_rows。
    store.upsert_rows("training_session", models.merge_training_segments([
        {"dataId": "run-a1", "sportType": 4,
         "startTime": start_ms, "endTime": start_ms + minute, "deviceCode": "band-x",
         "sportBasicInfos": [{"duration": 30, "distance": 5000,
                              "calorie": 300000, "steps": 10}]},
    ]))
    return {"today": today.isoformat(), "older": older.isoformat()}


async def main() -> int:
    print("=" * 64)
    print("华为运动健康 —— 查询命令层自检")
    print("=" * 64)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hwhealth_cmd_"))
    filled_path = tmp_dir / "filled.db"
    empty_path = tmp_dir / "empty.db"
    print(f"有数据库：{filled_path}")
    print(f"空数据库：{empty_path}")

    filled = HealthStore(filled_path)
    filled.initialize()
    days = seed(filled)
    empty = HealthStore(empty_path)
    empty.initialize()

    # ── 1. 健康活动（有数据）───────────────────────────────────────────
    print("\n[1/7] 健康活动（有数据）")
    out = await run_handler(commands.handle_activity, filled, "健康活动 3")
    print("  输出：")
    for line in out.splitlines():
        print(f"    {line}")
    check("含表头『最近 3 天活动』", "最近 3 天活动" in out)
    check("展示步数与步数目标", "步数 3172（目标 10000）" in out)
    check("展示距离与消耗",
          "距离 2.45 公里" in out and "消耗 121.7 千卡" in out)
    check("新展示口径：步行分钟 / 活动小时 / 运动分钟",
          "步行 63 分钟" in out and "活动 5 小时" in out and "运动 12 分钟" in out)
    check("合计行存在", "合计" in out)
    check("无值字段显示「无」（0 与缺省都不冒充数据）",
          "步行 无" in out and "距离 无" in out)
    check("不显示 0 冒充有数据", "0 分钟" not in out and "0 小时" not in out)

    # ── 2. 健康活动（默认天数 / 无数据）────────────────────────────────
    print("\n[2/7] 健康活动（默认天数 + 无数据）")
    default_out = await run_handler(commands.handle_activity, filled, "健康活动")
    print(f"  默认输出（有数据）：{_compact(default_out)}")
    check("无参数时默认 3 天", "最近 3 天活动" in default_out)

    empty_out = await run_handler(commands.handle_activity, empty, "健康活动 5")
    print(f"  空库输出：{_compact(empty_out)}")
    check("空库给出明确无数据提示", "没有活动记录" in empty_out)

    # ── 3. 健康睡眠（今天 / 有数据）────────────────────────────────────
    print("\n[3/7] 健康睡眠（今天，有数据）")
    sleep_out = await run_handler(commands.handle_sleep, filled, "健康睡眠")
    print("  输出：")
    for line in sleep_out.splitlines():
        print(f"    {line}")
    check("含睡眠时长（7 小时 12 分）", "7 小时 12 分" in sleep_out)
    check("含评分/效率/HRV/血氧",
          "评分 83" in sleep_out and "效率 92%" in sleep_out
          and "HRV 45.5" in sleep_out and "血氧 97%" in sleep_out)
    check("睡眠时间一行：入睡 03:14 · 起床 09:42",
          "入睡 03:14 · 起床 09:42" in sleep_out, f"out={_compact(sleep_out)}")
    check("白天小睡一行：白天小睡 74 分钟",
          "白天小睡 74 分钟" in sleep_out, f"out={_compact(sleep_out)}")
    check("心率五项齐全（静息/日间/平均静息/最高/最低）",
          "静息 58" in sleep_out and "日间 72" in sleep_out
          and "平均静息 60" in sleep_out and "最高 135" in sleep_out
          and "最低 49" in sleep_out)

    # ── 4. 健康睡眠（显式日期 / 部分心率 / 无数据）─────────────────────
    print("\n[4/7] 健康睡眠（显式日期 + 部分心率 + 无数据）")
    miss_out = await run_handler(commands.handle_sleep, filled, "健康睡眠 2000-01-01")
    print(f"  未命中日期输出：{_compact(miss_out)}")
    check("未命中日期给出明确提示",
          "没有睡眠或心率记录" in miss_out and "2000-01-01" in miss_out)

    partial_out = await run_handler(commands.handle_sleep, filled, f"健康睡眠 {days['older']}")
    print("  部分心率输出：")
    for line in partial_out.splitlines():
        print(f"    {line}")
    check("缺值的心率字段提示「无记录」", "静息 无记录" in partial_out)
    check("有值的心率字段照常显示", "日间 80" in partial_out)
    check("入睡/起床/小睡字段缺失时整段不显示（不出现「--」「未知」空壳）",
          "入睡" not in partial_out and "起床" not in partial_out
          and "白天小睡" not in partial_out
          and "入睡" not in miss_out and "白天小睡" not in miss_out,
          f"partial={_compact(partial_out)} miss={_compact(miss_out)}")

    # ── 5. 健康训练（有数据）───────────────────────────────────────────
    print("\n[5/7] 健康训练（有数据）")
    train_out = await run_handler(commands.handle_training, filled, "健康训练")
    print("  输出：")
    for line in train_out.splitlines():
        print(f"    {line}")
    check("含表头『最近 7 天训练』", "最近 7 天训练" in train_out)
    check("列出会话（跑步 / 距离 / 共 1 次）",
          "跑步" in train_out and "5.00 公里" in train_out and "共 1 次训练" in train_out)
    check("补齐段数与设备码",
          "1 段" in train_out and "设备 band-x" in train_out)

    # ── 6. 健康训练（空库）+ store 不可用 ──────────────────────────────
    print("\n[6/7] 健康训练（空库）+ 存储不可用")
    empty_train = await run_handler(commands.handle_training, empty, "健康训练")
    print(f"  空库输出：{_compact(empty_train)}")
    check("空库给出明确无数据提示", "没有训练记录" in empty_train)

    down_out = await run_handler(commands.handle_activity, None, "健康活动")
    print(f"  store=None 输出：{_compact(down_out)}")
    check("存储不可用给出明确提示", "存储层未初始化" in down_out)

    # ── 7. 身份门：拥有者放行 / 非拥有者静默拒绝 / 拿不到结构化字段即拒绝 ──
    print("\n[7/7] 身份门（只放行使用者本人 = 框架管理员）")
    plugin_cls = getattr(main_mod, "HuaweiHealthPlugin")
    plugin = plugin_cls(GateContext(["10001"]), {})
    plugin.store = filled          # 身份门只决定「读不读库」；库内容沿用第 1~6 段的真实数据
    # 提醒状态落到本轮临时目录：不读插件数据目录里的既有文件，负向对照才干净。
    plugin._reminder_state = main_mod.ReminderState(
        tmp_dir / main_mod.REMINDER_STATE_FILENAME)

    owner_out = {
        "活动": await run_command(plugin.cmd_health_activity, OwnerEvent("健康活动 3")),
        "睡眠": await run_command(plugin.cmd_health_sleep, OwnerEvent("健康睡眠")),
        "训练": await run_command(plugin.cmd_health_training, OwnerEvent("健康训练")),
    }
    for name, text in owner_out.items():
        print(f"  拥有者 · 健康{name}：{_compact(text)}")
    check("拥有者（is_admin()=True）三个命令都能正常用（返回真实数据）",
          "最近 3 天活动" in owner_out["活动"]
          and "步数 3172（目标 10000）" in owner_out["活动"]
          and "7 小时 12 分" in owner_out["睡眠"]
          and "共 1 次训练" in owner_out["训练"],
          f"活动={_compact(owner_out['活动'])} 睡眠={_compact(owner_out['睡眠'])}")

    guest_out = {
        "活动": await run_command(plugin.cmd_health_activity, GuestEvent("健康活动 3")),
        "睡眠": await run_command(plugin.cmd_health_sleep, GuestEvent("健康睡眠")),
        "训练": await run_command(plugin.cmd_health_training, GuestEvent("健康训练")),
    }
    state = plugin._reminder_state
    print(f"  非拥有者输出（应为空）：{guest_out}")
    check("非拥有者（is_admin()=False）被拒：不给任何提示文案、也不记提醒目标",
          all(text == "" for text in guest_out.values())
          and state is not None and state.notify_umo == "",
          f"输出={guest_out} notify_umo={state and state.notify_umo!r}")

    anon_out = {
        "活动": await run_command(plugin.cmd_health_activity, AnonEvent("健康活动 3")),
        "睡眠": await run_command(plugin.cmd_health_sleep, AnonEvent("健康睡眠")),
        "训练": await run_command(plugin.cmd_health_training, AnonEvent("健康训练")),
    }
    print(f"  拿不到结构化身份字段的输出（应为空）：{anon_out}")
    check("事件拿不到结构化身份字段（无 is_admin / 无 get_sender_id）→ 拒绝（fail-closed）",
          all(text == "" for text in anon_out.values()),
          f"输出={anon_out}")

    # ── 汇总 ───────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 64)
    print(f"结果：{passed}/{total} PASS")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} {detail}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
