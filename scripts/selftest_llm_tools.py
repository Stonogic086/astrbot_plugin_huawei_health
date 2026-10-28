#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 工具层自检：临时库造数据 → stub event 直调 ``@filter.llm_tool`` 方法。

覆盖：
  * 七个工具都能返回中文结果，且包含预期的关键字段；
  * 所有者 + 私聊 + 隐私闸门 + provider 白名单四道门；
  * 非所有者 / 群聊 / 闸门关闭 / provider 不在名单 → 一律不返回健康数值；
  * ``privacy.inject_health_context=false`` 时旧注入钩子不追加任何 part；
  * 打开注入开关后旧钩子仍可工作（兼容旧模式）。

设计约定与其余 selftest 一致：临时目录、最小 astrbot 桩、不联网、不启动框架。
用法：
    python3 scripts/selftest_llm_tools.py
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

import import_check  # noqa: E402

import_check._install_astrbot_stub()

storage = importlib.import_module(f"{PKG}.storage")
main_mod = importlib.import_module(f"{PKG}.main")
health_tools = importlib.import_module(f"{PKG}.features.health_tools")
HealthStore = storage.HealthStore
models = storage.models

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def _compact(text: str) -> str:
    return str(text).replace("\n", " / ")


class Ctx:
    """最小 Context 桩：只提供身份门 / 备用模型名单要读的全局配置。"""

    def get_config(self) -> dict:
        return {"admins_id": ["10001"]}


class ToolEvent:
    """工具调用事件桩：覆盖身份门、私聊判定与 selected_provider。"""

    def __init__(
        self,
        *,
        admin: bool = True,
        private: bool = True,
        provider: str = "test/provider",
        message: str = "",
    ) -> None:
        self._admin = admin
        self._private = private
        self._provider = provider
        self.message_str = message
        self.unified_msg_origin = "aiocqhttp:FriendMessage:10001"

    def is_admin(self) -> bool:
        return self._admin

    def is_private_chat(self) -> bool:
        return self._private

    def get_extra(self, key: str):
        return self._provider if key == "selected_provider" else None

    def plain_result(self, text: str) -> str:
        return text


class Request:
    """ProviderRequest 桩：只保留注入链要看的 extra_user_content_parts。"""

    def __init__(self) -> None:
        self.extra_user_content_parts: list = []


def make_config(
    *,
    allow: bool = True,
    inject: bool = False,
    allowlist: tuple[str, ...] = ("test",),
) -> dict:
    return {
        "account": {},
        "sync": {"enable_auto_sync": False},
        "storage": {},
        "privacy": {
            "allow_health_data_to_llm": allow,
            "inject_health_context": inject,
            "llm_provider_allowlist": list(allowlist),
        },
    }


def make_plugin(config: dict, store: HealthStore):
    plugin = main_mod.HuaweiHealthPlugin(Ctx(), config)
    plugin.store = store
    plugin._ondemand = None  # 自检不触发网络刷新
    return plugin


def seed(store: HealthStore) -> dict:
    """造今天/昨天/前天数据：活动、睡眠、心率、训练各一组。"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    older = today - timedelta(days=2)
    store.upsert_daily_activity([
        {
            "date": today.isoformat(), "sport_type": 0, "steps": 3172,
            "distance_m": 2448, "kcal": 121.7, "duration_min": 0,
            "walk_min": 63, "active_hours": 5, "exercise_min": 12,
            "step_goal": 10000,
        },
        {
            "date": yesterday.isoformat(), "sport_type": 0, "steps": 1000,
            "distance_m": 900, "kcal": 30.0, "duration_min": 10,
            "walk_min": 0, "active_hours": 0, "exercise_min": 0,
        },
        {
            "date": older.isoformat(), "sport_type": 0, "steps": 500,
            "distance_m": None, "kcal": None,
        },
    ])
    groups = models.normalize_health([
        {
            "date": today.isoformat(),
            "resting_heart_rate": 58, "heart_rate": 72,
            "average_resting_heart_rate": 60,
            "max_heart_rate": 135, "min_heart_rate": 49,
            "sleep_duration": 432, "sleep_score": 83, "sleep_efficiency": 92,
            "sleep_hrv": 45.5, "sleep_spo2": 97,
            "fall_asleep": f"{today.isoformat()} 03:14",
            "wakeup": f"{today.isoformat()} 09:42",
            "nap_duration": 74,
        },
        {
            "date": yesterday.isoformat(),
            "resting_heart_rate": 61, "heart_rate": 76,
            "average_resting_heart_rate": 63,
            "max_heart_rate": 140, "min_heart_rate": 52,
            "sleep_duration": 390, "sleep_score": 76, "sleep_efficiency": 89,
            "sleep_hrv": 41.0, "sleep_spo2": 96,
        },
        {
            "date": older.isoformat(), "heart_rate": 80,
        },
    ])
    for model, name in (
        ("heart_rate_sample", "heart_rate"),
        ("sleep_session", "sleep"),
        ("stress_sample", "stress"),
        ("spo2_sample", "spo2"),
    ):
        store.upsert_rows(model, groups[name])

    start_ms = int(datetime(today.year, today.month, today.day, 8, 0, 0).timestamp() * 1000)
    minute = 60 * 1000
    store.upsert_rows("training_session", models.merge_training_segments([
        {
            "dataId": "run-a1", "sportType": 4,
            "startTime": start_ms, "endTime": start_ms + minute,
            "deviceCode": "band-x",
            "sportBasicInfos": [{
                "duration": 30, "distance": 5000,
                "calorie": 300000, "steps": 10,
            }],
        },
    ]))
    return {"today": today.isoformat(), "yesterday": yesterday.isoformat()}


async def main() -> int:
    print("=" * 64)
    print("华为运动健康 —— LLM 工具层自检")
    print("=" * 64)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hwhealth_tools_"))
    db_path = tmp_dir / "tools.db"
    store = HealthStore(db_path)
    store.initialize()
    seed(store)
    plugin = make_plugin(make_config(), store)

    owner = ToolEvent()
    group = ToolEvent(private=False)
    guest = ToolEvent(admin=False)
    other_provider = ToolEvent(provider="other/model")

    # ── 1. 七个工具的正常路径 ────────────────────────────────────────────
    print("\n[1/3] 七个工具的正常路径")
    overview = await plugin.tool_health_overview(owner, 3)
    print(f"  overview：{_compact(overview)}")
    check("overview 含概览抬头与窗口",
          "健康概览" in overview and "最近 3 天" in overview)
    check("overview 含活动/睡眠/训练数据",
          "3172" in overview and "7 小时 12 分" in overview and "跑步" in overview)
    check("overview 带来源与免责声明", "不构成医疗诊断" in overview)

    sleep = await plugin.tool_health_sleep(owner, "")
    print(f"  sleep：{_compact(sleep)}")
    check("sleep 默认今天且含睡眠/心率",
          "睡眠与心率" in sleep and "评分 83" in sleep and "静息 58" in sleep)

    sleep_trend = await plugin.tool_health_sleep_trend(owner, 7)
    print(f"  sleep_trend：{_compact(sleep_trend)}")
    check("sleep_trend 含逐日与平均值",
          "最近 7 天睡眠" in sleep_trend
          and "评分 83" in sleep_trend and "平均：" in sleep_trend)

    heart = await plugin.tool_health_heart(owner, 7)
    print(f"  heart：{_compact(heart)}")
    check("heart 含静息/日间/汇总",
          "最近 7 天心率" in heart and "静息 58" in heart
          and "日间 72" in heart and "汇总：" in heart)

    activity = await plugin.tool_health_activity(owner, 3)
    print(f"  activity：{_compact(activity)}")
    check("activity 含步数与合计", "最近 3 天活动" in activity and "合计" in activity)

    training = await plugin.tool_health_training(owner, 7)
    print(f"  training：{_compact(training)}")
    check("training 含训练会话", "最近 7 天训练" in training and "跑步" in training)

    status = await plugin.tool_health_status(owner)
    print(f"  status：{_compact(status)}")
    check("status 含凭证/同步/数据覆盖且不含具体数值",
          "连接状态" in status and "云端凭证" in status
          and "睡眠：" in status and "3172" not in status)

    # 空库与不存在的日期：给出明确无数据提示，不抛错
    empty = HealthStore(tmp_dir / "empty.db")
    empty.initialize()
    empty_plugin = make_plugin(make_config(), empty)
    empty_overview = await empty_plugin.tool_health_overview(owner, 3)
    print(f"  空库 overview：{_compact(empty_overview)}")
    check("空库 overview 给出明确无数据提示",
          "没有活动记录" in empty_overview
          and "没有睡眠或心率记录" in empty_overview)

    missing_day = await plugin.tool_health_sleep(owner, "2000-01-01")
    print(f"  不存在日期 sleep：{_compact(missing_day)}")
    check("不存在的日期给出明确无数据提示",
          "2000-01-01 没有睡眠或心率记录" in missing_day)

    # ── 2. 四道门：非所有者 / 群聊 / 闸门关闭 / provider 不在名单 ─────────
    print("\n[2/3] 四道门")
    denied_guest = await plugin.tool_health_overview(guest, 3)
    print(f"  非所有者：{denied_guest}")
    check("非所有者被拒且不泄露数据",
          "仅限管理员" in denied_guest and "3172" not in denied_guest)

    denied_group = await plugin.tool_health_sleep(group, "")
    print(f"  群聊：{denied_group}")
    check("群聊被拒且不泄露数据",
          "仅限私聊" in denied_group and "评分 83" not in denied_group)

    closed = make_plugin(make_config(allow=False), store)
    denied_closed = await closed.tool_health_overview(owner, 3)
    print(f"  闸门关闭：{denied_closed}")
    check("隐私闸门关闭时被拒",
          "未启用" in denied_closed and "3172" not in denied_closed)

    denied_provider = await plugin.tool_health_overview(other_provider, 3)
    print(f"  provider 不在名单：{denied_provider}")
    check("provider 不在名单时被拒",
          "不在健康数据信任名单" in denied_provider and "3172" not in denied_provider)

    # ── 3. 注入开关：默认关闭，打开后旧模式仍可用 ─────────────────────────
    print("\n[3/3] 注入开关")
    req = Request()
    await plugin.on_llm_request(owner, req)
    print(f"  默认 inject=false：parts={len(req.extra_user_content_parts)}")
    check("默认不注入上下文", len(req.extra_user_content_parts) == 0)

    inject_plugin = make_plugin(make_config(inject=True), store)
    inject_req = Request()
    await inject_plugin.on_llm_request(owner, inject_req)
    injected = getattr(inject_req.extra_user_content_parts[0], "text", "")
    print(f"  打开 inject=true：parts={len(inject_req.extra_user_content_parts)}")
    check("打开后旧注入模式仍可工作",
          len(inject_req.extra_user_content_parts) == 1
          and "华为运动健康" in injected)

    # 纯函数边界：天数夹取 / 日期解析
    check("clamp_days 夹在 1..30",
          health_tools.clamp_days(0) == 3
          and health_tools.clamp_days(999) == 30
          and health_tools.clamp_days("7") == 7)
    check("resolve_day 支持今天/昨天/ISO",
          health_tools.resolve_day("今天") == date.today().isoformat()
          and health_tools.resolve_day("昨天")
          == (date.today() - timedelta(days=1)).isoformat()
          and health_tools.resolve_day("2026-01-02") == "2026-01-02")

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
