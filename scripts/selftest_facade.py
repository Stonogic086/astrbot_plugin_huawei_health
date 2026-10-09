#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取数门面自检：用桩替换协议层四个取数方法（不连网），验证门面的三条核心语义。

覆盖：
  1. 单类无数据 → 门面返回空、上层（SyncService + sync_state）状态记「无数据」而不是失败；
  2. 四类同源（心率/睡眠/血氧/压力来自同一次 health_summary）与各自为空时的行为；
  3. 三类异常分流（认证 / 网络 / 解析）各一例，单类失败不影响其他类别、不中断整轮；
  4. ``no_data`` 与 ``failed`` 两条状态路径各自落到正确的状态值；
  5. 训练段合并：不伪造时间戳、dataId 去重与 ≤15 分钟间隔合并规则不被破坏。

只读边界：
  * 不联网：协议层四个方法全部由 ``StubAdapter`` 顶替（异常也是桩里注入的）；
  * 不 import astrbot、不读凭据文件、不改插件配置、不碰真实 health.db；
  * 库写在 tempfile 临时目录，跑完删除；健康数值一律不打印，只打印行数与状态。

用法：python3 scripts/selftest_facade.py
退出码：全部 PASS → 0；任一 FAIL → 1。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from adapters import (  # noqa: E402
    HuaweiApiError,
    HuaweiAuthError,
    HuaweiConnectionError,
    HuaweiHealthAuthenticationError,
    HuaweiHealthFacade,
    HuaweiHealthNetworkError,
    HuaweiHealthParseError,
)
from services import SyncService  # noqa: E402
from services.sync_service import HEALTH_CLASSES  # noqa: E402
from storage import HealthStore  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

WINDOW_DAYS = 3
TODAY = date.today()
START = TODAY - timedelta(days=WINDOW_DAYS - 1)


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def hr(title: str) -> None:
    print("\n" + title)
    print("-" * max(8, len(title) * 2))


# ════════════════════════════════════════════════════════════════════
# 桩：协议层同形取数接口（四个 async 方法 + refresh），全部不连网
# ════════════════════════════════════════════════════════════════════
class StubAdapter:
    """协议层桩：给定返回值，或按方法名注入异常（验证门面的异常翻译与上层分流）。"""

    def __init__(self, *, daily=(), health=(), sessions=(), access_token="stub-token"):
        self.tokens = SimpleNamespace(
            access_token=access_token,
            refresh_token="stub-refresh-token",
            uid="stub-uid",
            session_host="https://stub.invalid",
        )
        self.host = "https://stub.invalid"
        self.daily = list(daily)
        self.health = list(health)
        self.sessions = list(sessions)
        self.errors: dict[str, BaseException] = {}
        self.calls: dict[str, int] = {
            "refresh": 0, "daily_summary": 0, "health_summary": 0, "session_segments": 0}

    def _maybe_fail(self, name: str) -> None:
        error = self.errors.get(name)
        if error is not None:
            raise error

    async def refresh(self):
        self.calls["refresh"] += 1
        self._maybe_fail("refresh")
        return self.tokens

    async def daily_summary(self, days: int = 3):
        self.calls["daily_summary"] += 1
        self._maybe_fail("daily_summary")
        return list(self.daily)

    async def health_summary(self, days: int = 3, types=(7, 9, 11, 12)):
        self.calls["health_summary"] += 1
        self._maybe_fail("health_summary")
        return list(self.health)

    async def session_segments(self, days: int = 7):
        self.calls["session_segments"] += 1
        self._maybe_fail("session_segments")
        return list(self.sessions)


# ════════════════════════════════════════════════════════════════════
# 造样本（形状与生产一致：协议层整形行 / 原始分钟段）
# ════════════════════════════════════════════════════════════════════
def daily_row(day: date, steps: int = 3148) -> dict:
    """协议层 daily_activity 的整形行（calorie 已在协议层换算成 kcal）。"""
    return {
        "date": day.isoformat(), "sport_type": 0, "steps": steps, "distance_m": 2210,
        "kcal": 132.5, "duration_min": 51, "walk_min": 34, "active_hours": 11,
        "exercise_min": 12, "step_goal": 6000,
    }


def health_row(day: date, **values) -> dict:
    """协议层 health_series 的整形行（扁平键名）。"""
    return {"date": day.isoformat(), **values}


def segment(start_ms: int, end_ms: int, data_id: str | None, *, sport_type: int = 1,
            duration_min: int = 10, distance_m: int = 1500,
            calorie_milli: int = 80000) -> dict:
    """getSportsDataByTime 的原始分钟段（协议层不整形，去重/合并在存储层）。"""
    return {
        "dataId": data_id,
        "sportType": sport_type,
        "startTime": start_ms,
        "endTime": end_ms,
        "sportBasicInfos": [{"duration": duration_min, "distance": distance_m,
                             "calorie": calorie_milli}],
        "deviceCode": "stub-band",
    }


def local_str(stamp_ms: int) -> str:
    return datetime.fromtimestamp(stamp_ms / 1000).strftime("%Y-%m-%d %H:%M:%S")


def ms_at(day: date, hour: int, minute: int = 0) -> int:
    return int(datetime(day.year, day.month, day.day, hour, minute).timestamp() * 1000)


def new_store(tmp_dir: Path, name: str) -> HealthStore:
    store = HealthStore(tmp_dir / name / "health.db")
    store.initialize()
    return store


async def run_round(adapter: StubAdapter, store: HealthStore) -> dict:
    """跑一轮完整同步（门面 + 存储层），返回 summary。"""
    return await SyncService(adapter, store, days=WINDOW_DAYS).run_once()


def state_of(store: HealthStore, data_type: str) -> dict:
    return store.sync_state(data_type) or {}


# ════════════════════════════════════════════════════════════════════
# 用例 1：单类无数据 → 返回空 + 状态记「无数据」而不是失败
# ════════════════════════════════════════════════════════════════════
async def case_single_class_no_data(tmp_dir: Path) -> None:
    hr("[1/5] 单类无数据 → 返回空、上层记「无数据」而不是失败")

    # 1a 门面层：当天只有睡眠读数（心率/压力/血氧一个有效读数都没有）→ 那三类返回空。
    adapter = StubAdapter(health=[health_row(TODAY, sleep_score=81.0, sleep_duration=402.0)])
    facade = HuaweiHealthFacade(adapter)
    await facade.connect()
    heart = await facade.iter_heart_rate(START, TODAY)
    sleep = await facade.iter_sleep(START, TODAY)
    stress = await facade.iter_stress(START, TODAY)
    spo2 = await facade.iter_spo2(START, TODAY)
    await facade.close()
    check("门面无读数类返回空 list（心率/压力/血氧），不抛异常",
          heart == [] and stress == [] and spo2 == [],
          f"heart={len(heart)} stress={len(stress)} spo2={len(spo2)}")
    check("同一响应里有读数的类照常返回（睡眠 1 行）", len(sleep) == 1,
          f"sleep={len(sleep)}")

    # 1b 日汇总：云端给了记录但一个读数都没有（全 0）→ 视同无数据，不写全空壳行。
    zero = StubAdapter(daily=[{"date": TODAY.isoformat(), "sport_type": 0, "steps": 0,
                               "distance_m": 0, "kcal": 0.0, "duration_min": 0}])
    facade = HuaweiHealthFacade(zero)
    rows = await facade.iter_daily_activity(START, TODAY)
    check("日汇总全 0（手环没测到）→ 返回空、不写空壳行", rows == [], f"rows={len(rows)}")

    # 1c 端到端：心率/压力/血氧/训练无数据 → summary 记 no_data，sync_state 记 skipped。
    store = new_store(tmp_dir, "case1")
    adapter = StubAdapter(daily=[daily_row(TODAY)],
                          health=[health_row(TODAY, sleep_score=81.0)])
    summary = await run_round(adapter, store)
    check("整轮不因「无数据」判失败（ok=True / status=ok）",
          summary["ok"] is True and summary["status"] == "ok",
          f"ok={summary['ok']} status={summary['status']}")
    check("无数据类别进 summary['no_data']，不进 skipped",
          set(summary["no_data"]) == {"heart_rate", "stress", "spo2", "training_session"}
          and summary["skipped"] == [],
          f"no_data={sorted(summary['no_data'])} skipped={summary['skipped']}")
    heart_state = state_of(store, "heart_rate")
    sleep_state = state_of(store, "sleep")
    check("sync_state：无数据类记 skipped（不是 failed）",
          heart_state.get("last_status") == "skipped"
          and "无数据" in str(heart_state.get("last_error")),
          f"status={heart_state.get('last_status')}")
    check("sync_state：有读数的类记 ok",
          sleep_state.get("last_status") == "ok", f"status={sleep_state.get('last_status')}")
    check("sync_state：整轮汇总行为 ok（未因无数据转 failed）",
          state_of(store, "round").get("last_status") == "ok",
          f"status={state_of(store, 'round').get('last_status')}")


# ════════════════════════════════════════════════════════════════════
# 用例 2：四类同源（同一次 health_summary）+ 各自为空
# ════════════════════════════════════════════════════════════════════
async def case_shared_health_source(tmp_dir: Path) -> None:
    hr("[2/5] 四类同源（同一次 health_summary）与各自为空")

    # 2a 一次响应出四类：四个 iter_* 只打一次网。
    adapter = StubAdapter(health=[health_row(
        TODAY, resting_heart_rate=58.0, heart_rate=72.0, sleep_score=81.0,
        sleep_duration=402.0, sleep_spo2=97.0, stress_average=31.0, stress_last=28.0)])
    facade = HuaweiHealthFacade(adapter)
    await facade.connect()
    counts = {
        "heart_rate": len(await facade.iter_heart_rate(START, TODAY)),
        "sleep": len(await facade.iter_sleep(START, TODAY)),
        "spo2": len(await facade.iter_spo2(START, TODAY)),
        "stress": len(await facade.iter_stress(START, TODAY)),
    }
    check("四类全部取到行", all(value == 1 for value in counts.values()), f"{counts}")
    check("四类只打一次网（health_summary 调用次数 == 1）",
          adapter.calls["health_summary"] == 1,
          f"calls={adapter.calls['health_summary']}")
    check("取健康四类不额外拉日汇总/训练段",
          adapter.calls["daily_summary"] == 0 and adapter.calls["session_segments"] == 0,
          f"daily={adapter.calls['daily_summary']} sessions={adapter.calls['session_segments']}")
    await facade.close()
    await facade.iter_heart_rate(START, TODAY)
    check("close() 清窗口缓存 → 再取数重新打一次网（同一天两轮不复用上一轮响应）",
          adapter.calls["health_summary"] == 2,
          f"calls={adapter.calls['health_summary']}")

    # 2b 同源响应整体为空 → 四类都返回空、不抛异常；上层四类都记「无数据」。
    store = new_store(tmp_dir, "case2")
    adapter = StubAdapter(daily=[daily_row(TODAY)], health=[], sessions=[])
    facade = HuaweiHealthFacade(adapter)
    await facade.connect()
    empty = [len(await getattr(facade, f"iter_{name}")(START, TODAY))
             for name, _ in HEALTH_CLASSES]
    await facade.close()
    check("health_summary 空数组 → 四类均返回空、不抛异常",
          empty == [0, 0, 0, 0], f"{empty}")
    adapter = StubAdapter(daily=[daily_row(TODAY)], health=[], sessions=[])
    summary = await run_round(adapter, store)
    health_names = {name for name, _ in HEALTH_CLASSES}
    check("整轮不减分：四类无数据只记 no_data，status 仍为 ok",
          health_names <= set(summary["no_data"]) and summary["status"] == "ok",
          f"no_data={sorted(summary['no_data'])} status={summary['status']}")
    statuses = {name: state_of(store, name).get("last_status") for name, _ in HEALTH_CLASSES}
    check("sync_state：四类各自记 skipped + 「无数据」标注",
          all(value == "skipped" for value in statuses.values())
          and all("无数据" in str(state_of(store, name).get("last_error"))
                  for name, _ in HEALTH_CLASSES),
          f"{statuses}")

    # 2c 同源响应只有压力有读数 → 其余三类各自为空、压力照常落库。
    adapter = StubAdapter(daily=[daily_row(TODAY)],
                          health=[health_row(TODAY, stress_average=31.0, stress_last=28.0)],
                          sessions=[])
    facade = HuaweiHealthFacade(adapter)
    await facade.connect()
    per_class = {name: len(await getattr(facade, f"iter_{name}")(START, TODAY))
                 for name, _ in HEALTH_CLASSES}
    await facade.close()
    check("同源响应按类各自判空（只有压力有读数）",
          per_class == {"heart_rate": 0, "sleep": 0, "stress": 1, "spo2": 0}, f"{per_class}")


# ════════════════════════════════════════════════════════════════════
# 用例 3：三类异常分流（认证 / 网络 / 解析）
# ════════════════════════════════════════════════════════════════════
PROTOCOL_ERRORS: tuple[tuple[BaseException, type, str], ...] = (
    (HuaweiAuthError("stub://session: no accessToken in the login answer"),
     HuaweiHealthAuthenticationError, "auth"),
    (HuaweiConnectionError("stub://data: DNS lookup failed"),
     HuaweiHealthNetworkError, "network"),
    (HuaweiApiError(0, "stub://data/getHealthStat", "answer is not JSON"),
     HuaweiHealthParseError, "parse"),
)


async def case_error_buckets(tmp_dir: Path) -> None:
    hr("[3/5] 三类异常分流：认证 / 网络 / 解析")

    # 3a 门面把协议层四个异常翻译成门面三类（不把协议层细节漏给上层）。
    for protocol_error, expected, kind in PROTOCOL_ERRORS:
        adapter = StubAdapter()
        adapter.errors["daily_summary"] = protocol_error
        facade = HuaweiHealthFacade(adapter)
        await facade.connect()
        caught: BaseException | None = None
        try:
            await facade.iter_daily_activity(START, TODAY)
        except Exception as error:  # noqa: BLE001 - 自检就是要看异常
            caught = error
        await facade.close()
        check(f"协议层 {type(protocol_error).__name__} → 门面 {expected.__name__}（kind={kind}）",
              isinstance(caught, expected)
              and not isinstance(caught, (HuaweiApiError, HuaweiConnectionError,
                                          HuaweiAuthError)),
              f"实际 {type(caught).__name__}")

    # 3b 单类失败不影响其他类别、不中断整轮：日汇总认证失败。
    store = new_store(tmp_dir, "case3")
    adapter = StubAdapter(daily=[daily_row(TODAY)],
                          health=[health_row(TODAY, resting_heart_rate=58.0)],
                          sessions=[segment(ms_at(TODAY, 8), ms_at(TODAY, 8, 10), "a1")])
    adapter.errors["daily_summary"] = HuaweiAuthError("stub://data: refresh token rejected")
    summary = await run_round(adapter, store)
    kinds = {item["stage"]: item.get("kind") for item in summary["skipped"]}
    check("日汇总认证失败 → kind=auth，且只有该类被跳过",
          kinds == {"daily_activity": "auth"}, f"skipped={kinds}")
    check("认证失败不打断整轮：其余类别照常取数落库",
          summary["written"]["heart_rate"] == 1
          and summary["written"]["training_session"] == 1,
          f"written={summary['written']}")
    check("整轮判为 partial（部分完成，不是失败）",
          summary["ok"] is True and summary["status"] == "partial",
          f"ok={summary['ok']} status={summary['status']}")

    # 3c 训练段网络失败 → kind=network。
    adapter = StubAdapter(daily=[daily_row(TODAY)],
                          health=[health_row(TODAY, resting_heart_rate=58.0)])
    adapter.errors["session_segments"] = HuaweiConnectionError("stub://data: timeout")
    summary = await run_round(adapter, store)
    kinds = {item["stage"]: item.get("kind") for item in summary["skipped"]}
    check("训练段网络失败 → kind=network，日汇总/健康照常",
          kinds == {"training_session": "network"}
          and summary["written"]["daily_activity"] == 1
          and summary["written"]["heart_rate"] == 1,
          f"skipped={kinds} written={summary['written']}")

    # 3d 健康响应解析失败 → 四类各自记 kind=parse，其余两类照常。
    adapter = StubAdapter(daily=[daily_row(TODAY)],
                          sessions=[segment(ms_at(TODAY, 8), ms_at(TODAY, 8, 10), "a1")])
    adapter.errors["health_summary"] = HuaweiApiError(
        0, "stub://data/getHealthStat", "answer is not an object")
    summary = await run_round(adapter, store)
    parsed = {item["stage"] for item in summary["skipped"] if item.get("kind") == "parse"}
    check("健康响应解析失败 → 心率/睡眠/压力/血氧四类各自记 kind=parse",
          parsed == {"heart_rate", "sleep", "stress", "spo2"}, f"parse={sorted(parsed)}")
    check("解析失败只降级该类：日汇总与训练段照常取数落库",
          summary["written"]["daily_activity"] == 1
          and summary["written"]["training_session"] == 1,
          f"written={summary['written']}")


# ════════════════════════════════════════════════════════════════════
# 用例 4：no_data 与 failed 两条状态路径各落到正确状态值
# ════════════════════════════════════════════════════════════════════
async def case_two_state_paths(tmp_dir: Path) -> None:
    hr("[4/5] no_data 与 failed 两条路径各落正确的状态值")

    store = new_store(tmp_dir, "case4")
    adapter = StubAdapter(
        daily=[daily_row(TODAY)],
        health=[health_row(TODAY, sleep_score=81.0)],  # 心率无数据、睡眠有数据
    )
    adapter.errors["session_segments"] = HuaweiConnectionError("stub://data: timeout")
    summary = await run_round(adapter, store)

    skipped_stages = {item["stage"] for item in summary["skipped"]}
    check("两条路径互不混淆（no_data 与 skipped 无交集）",
          not (set(summary["no_data"]) & skipped_stages)
          and "heart_rate" in summary["no_data"]
          and skipped_stages == {"training_session"},
          f"no_data={sorted(summary['no_data'])} skipped={sorted(skipped_stages)}")

    heart_state = state_of(store, "heart_rate")
    train_state = state_of(store, "training_session")
    daily_state = state_of(store, "daily_activity")
    sleep_state = state_of(store, "sleep")
    check("无数据路径 → last_status=skipped + 「无数据」原因",
          heart_state.get("last_status") == "skipped"
          and "无数据" in str(heart_state.get("last_error")),
          f"status={heart_state.get('last_status')}")
    check("失败路径 → last_status=failed + 原因（异常类型 + 该阶段）",
          train_state.get("last_status") == "failed"
          and "HuaweiHealthNetworkError" in str(train_state.get("last_error")),
          f"status={train_state.get('last_status')}")
    check("有数据的类 → last_status=ok 且无失败原因",
          daily_state.get("last_status") == "ok" and sleep_state.get("last_status") == "ok"
          and not daily_state.get("last_error") and not sleep_state.get("last_error"),
          f"daily={daily_state.get('last_status')} sleep={sleep_state.get('last_status')}")
    round_state = state_of(store, "round")
    check("整轮汇总行记 partial，且原因只含失败的类别（不含健康数值）",
          round_state.get("last_status") == "partial"
          and "training_session" in str(round_state.get("last_error"))
          and "steps" not in str(round_state.get("last_error")),
          f"status={round_state.get('last_status')}")


# ════════════════════════════════════════════════════════════════════
# 用例 5：训练段合并（不伪造时间戳、间隔合并规则不被破坏）
# ════════════════════════════════════════════════════════════════════
async def case_training_merge(tmp_dir: Path) -> None:
    hr("[5/5] 训练段合并：时间戳来自数据、≤15 分钟合并规则不变")

    first_start, first_end = ms_at(TODAY, 8, 0), ms_at(TODAY, 8, 10)      # 10 分钟段
    second_start, second_end = ms_at(TODAY, 8, 20), ms_at(TODAY, 8, 35)    # 间隔 10 分钟 → 合并
    third_start, third_end = ms_at(TODAY, 9, 10), ms_at(TODAY, 9, 20)      # 间隔 35 分钟 → 新会话
    other_start, other_end = ms_at(TODAY, 10, 0), ms_at(TODAY, 10, 10)     # 另一种运动 → 另一组
    old_start, old_end = ms_at(START - timedelta(days=1), 8), ms_at(START - timedelta(days=1), 8, 10)

    segments = [
        segment(first_start, first_end, "a1", duration_min=10, distance_m=1500,
                calorie_milli=80000),
        segment(first_start, first_end, "a1", duration_min=10, distance_m=1500,
                calorie_milli=80000),  # 同 dataId 重复投递 → 必须先去掉
        segment(second_start, second_end, "a2", duration_min=15, distance_m=2400,
                calorie_milli=120000),
        segment(third_start, third_end, "a3", duration_min=10, distance_m=1600,
                calorie_milli=90000),
        segment(other_start, other_end, "b1", sport_type=2, duration_min=10,
                distance_m=1000, calorie_milli=60000),
        segment(old_start, old_end, "old1"),                    # 窗口之外 → 丢
        {"dataId": "broken", "sportType": 1, "startTime": first_start,
         "sportBasicInfos": [{"duration": 5}]},                 # 缺 endTime → 丢，不补时间戳
    ]
    adapter = StubAdapter(sessions=segments)
    facade = HuaweiHealthFacade(adapter)
    await facade.connect()
    sessions = await facade.iter_training(START, TODAY)
    await facade.close()

    by_key = {row["session_key"]: row for row in sessions}
    check("窗口外 / 缺时间戳的段被丢弃（不补、不编时间戳、不编会话）",
          len(sessions) == 3 and f"1:{old_start}" not in by_key
          and all(row["start_ms"] and row["end_ms"] for row in sessions),
          f"sessions={len(sessions)} keys={sorted(by_key)}")
    check("同 dataId 重复段去重 + 间隔 ≤15 分钟合并为一次会话",
          by_key.get(f"1:{first_start}", {}).get("segments") == 2,
          f"segments={by_key.get(f'1:{first_start}', {}).get('segments')}")
    merged = by_key.get(f"1:{first_start}", {})
    check("合并后累加读数（时长 10+15、距离 1500+2400、kcal 80+120）",
          merged.get("duration_min") == 25 and merged.get("distance_m") == 3900
          and merged.get("kcal") == 200.0,
          f"duration={merged.get('duration_min')} distance={merged.get('distance_m')} "
          f"kcal={merged.get('kcal')}")
    check("会话起止时刻取自数据本身（结束时刻不是「开始 + 累计时长」推的）",
          merged.get("start_local") == local_str(first_start)
          and merged.get("end_local") == local_str(second_end)
          and merged.get("end_ms") == second_end,
          f"start={merged.get('start_local')} end={merged.get('end_local')}")
    check("间隔 35 分钟切分成新会话（合并阈值仍为 15 分钟）",
          f"1:{third_start}" in by_key
          and by_key[f"1:{third_start}"].get("segments") == 1,
          f"keys={sorted(by_key)}")
    check("不同 sportType 不互相合并", f"2:{other_start}" in by_key,
          f"keys={sorted(by_key)}")
    check("会话日期与本地时刻自洽（start_date == 本地时刻前 10 位）",
          all(row["start_date"] == row["start_local"][:10] for row in sessions),
          f"dates={sorted({row['start_date'] for row in sessions})}")


async def main_async() -> int:
    print("=" * 68)
    print("华为运动健康 —— 取数门面自检（桩替换网络层，不连网）")
    print("=" * 68)
    print(f"窗口：最近 {WINDOW_DAYS} 天（含今天）；python：{sys.version.split()[0]}")
    print("桩：StubAdapter 顶替 refresh / daily_summary / health_summary / session_segments")

    tmp_dir = Path(tempfile.mkdtemp(prefix="hwhealth_facade_"))
    print(f"临时库目录：{tmp_dir}（跑完删除，不碰真实 health.db）")
    try:
        await case_single_class_no_data(tmp_dir)
        await case_shared_health_source(tmp_dir)
        await case_error_buckets(tmp_dir)
        await case_two_state_paths(tmp_dir)
        await case_training_merge(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 68)
    print(f"结果：{passed}/{total} PASS")
    for name, detail in [(n, d) for n, ok, d in RESULTS if not ok]:
        print(f"  FAIL: {name} {detail}")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
