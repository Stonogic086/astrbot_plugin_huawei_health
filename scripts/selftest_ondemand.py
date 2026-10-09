#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按需刷新自检：假时钟 + 假 run_once 直接驱动 OnDemandRefresher（不联网、不启框架）。

覆盖（对应 v1 第十轮交付物 C）：
  1) 15 分钟内不重复刷新（is_due=False，run_once 不被调用）；
  2) 超过间隔刷新一次并更新上次同步时间（同一间隔内第二次仍不重复）；
  3) 刷新抛异常时仍返回旧数据（查询不受影响、不抛异常）；
  4) 刷新超时时按时返回并退回旧数据（不卡死，last_success 不被污染）。

另附带：配置「分钟 → 秒」折算（含非法值回退）。

只读边界：纯内存 + 注入的假时钟/假 run_once；不联网、不启动 AstrBot、不写任何配置、
不碰真实 health.db。退出码：全部通过 → 0；有断言失败 → 1。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from services.ondemand_refresh import (  # noqa: E402
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    OnDemandRefresher,
    interval_seconds_from_minutes,
)

_TOTAL = 0
_FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    """打印一条断言结果并累计计数。"""
    global _TOTAL, _FAILED
    _TOTAL += 1
    if not ok:
        _FAILED += 1
    status = "PASS" if ok else "FAIL"
    tail = f" —— {detail}" if detail else ""
    print(f"[{status}] {name}{tail}")


# ── 测试替身 ─────────────────────────────────────────────────────────────
class QuietLogger:
    """吞掉日志的桩 logger（避免测试输出噪音）。"""

    def _log(self, *args, **kwargs):
        return None

    info = warning = error = debug = _log


class FakeClock:
    """可推进的假时钟：__call__ 返回当前 epoch 秒。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += float(seconds)


class FakeRunOnce:
    """假的「跑一轮同步」：可计数、可抛异常、可拖时间。"""

    def __init__(self, result=None, error=None, delay=0.0) -> None:
        self.calls = 0
        self.result = result if result is not None else {"ok": True, "status": "ok"}
        self.error = error
        self.delay = float(delay)
        self.on_call = None

    async def __call__(self):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.on_call is not None:
            self.on_call()
        if self.error is not None:
            raise self.error
        return self.result


class FakeStore:
    """假的存储层：只提供 query，返回既有旧数据。"""

    def __init__(self, rows) -> None:
        self.rows = list(rows)

    def query(self, model, start=None, end=None):
        return list(self.rows)


async def run_query(refresher: OnDemandRefresher, store: FakeStore):
    """模拟查询命令流程：先按需刷新判断，再读库。返回 (旧数据, 刷新结果)。"""
    result = await refresher.refresh_if_due()
    return store.query("daily_activity"), result


# ── 各用例 ───────────────────────────────────────────────────────────────
async def case_interval_config() -> None:
    print("── 用例 0：配置「分钟 → 秒」折算 ──")
    check("默认常量为 900 秒", DEFAULT_REFRESH_INTERVAL_SECONDS == 900,
          f"DEFAULT={DEFAULT_REFRESH_INTERVAL_SECONDS}")
    check("15 分钟 → 900 秒", interval_seconds_from_minutes(15) == 900)
    check("60 分钟 → 3600 秒", interval_seconds_from_minutes("60") == 3600)
    check("非法值 → 回退默认 900 秒", interval_seconds_from_minutes("x") == 900)
    check("0 / 负数 → 回退默认 900 秒",
          interval_seconds_from_minutes(0) == 900
          and interval_seconds_from_minutes(-5) == 900)


async def case_within_interval() -> None:
    print("\n── 用例 1：15 分钟内不重复刷新 ──")
    clock = FakeClock()
    runner = FakeRunOnce()
    refresher = OnDemandRefresher(
        runner, interval_seconds=15 * 60, now=clock, logger=QuietLogger())
    refresher.mark_success()          # 基准：刚同步成功
    clock.advance(14 * 60)            # 推进到 14 分钟

    due = refresher.is_due()
    store = FakeStore([{"date": "2024-05-01", "steps": 8000}])
    data, res = await run_query(refresher, store)

    check("14 分钟时 is_due=False", due is False)
    check("14 分钟内 run_once 未被调用", runner.calls == 0, f"calls={runner.calls}")
    check("14 分钟内 refresh_if_due.attempted=False",
          res["attempted"] is False and res["reason"] == "fresh", f"res={res}")
    check("14 分钟内查询直接读到旧数据", len(data) == 1)


async def case_due_refresh_once() -> None:
    print("\n── 用例 2：超过间隔刷新一次并更新上次同步时间 ──")
    clock = FakeClock()
    runner = FakeRunOnce()
    refresher = OnDemandRefresher(
        runner, interval_seconds=15 * 60, now=clock, logger=QuietLogger())
    refresher.mark_success()
    baseline = refresher.last_success
    clock.advance(16 * 60)            # 推进到 16 分钟

    check("16 分钟时 is_due=True", refresher.is_due() is True)
    store = FakeStore([{"date": "2024-05-02", "steps": 9000}])
    data, res = await run_query(refresher, store)

    check("到点触发且只刷新一次",
          runner.calls == 1 and res["attempted"] is True and res["ok"] is True,
          f"calls={runner.calls} res={res}")
    check("刷新成功后 last_success 更新到当前时刻",
          abs(refresher.last_success - clock.t) < 1e-6
          and refresher.last_success > baseline,
          f"last_success={refresher.last_success} now={clock.t}")

    _data2, res2 = await run_query(refresher, FakeStore([{"date": "2024-05-03"}]))
    check("刷新后同一间隔内第二次不再刷新",
          runner.calls == 1 and res2["attempted"] is False, f"calls={runner.calls}")


async def case_error_returns_old() -> None:
    print("\n── 用例 3：刷新抛异常时仍返回旧数据 ──")
    clock = FakeClock()
    runner = FakeRunOnce(error=RuntimeError("network jitter"))
    refresher = OnDemandRefresher(
        runner, interval_seconds=15 * 60, now=clock, logger=QuietLogger())
    clock.advance(1)                  # 从未同步过 → 到点
    old_rows = [{"date": "2024-04-30", "steps": 4321}]
    store = FakeStore(old_rows)

    data, res = await run_query(refresher, store)

    check("刷新抛异常时查询不报错、仍返回旧数据",
          data == old_rows and len(data) == 1, f"data={data}")
    check("异常被兜住：attempted=True ok=False reason=error",
          res["attempted"] is True and res["ok"] is False
          and res["reason"] == "error", f"res={res}")
    check("异常时不更新 last_success", refresher.last_success == 0.0,
          f"last_success={refresher.last_success}")

    _data2, res2 = await run_query(refresher, store)
    check("失败后同一间隔内不反复重试",
          runner.calls == 1 and res2["attempted"] is False, f"calls={runner.calls}")


async def case_timeout() -> None:
    print("\n── 用例 4：刷新超时按时返回旧数据 ──")
    clock = FakeClock()
    runner = FakeRunOnce(delay=1.0)   # 故意比超时上限慢
    refresher = OnDemandRefresher(
        runner, interval_seconds=15 * 60, timeout_seconds=0.05,
        now=clock, logger=QuietLogger())
    old_rows = [{"date": "2024-04-29", "steps": 111}]
    store = FakeStore(old_rows)

    started = time.monotonic()
    data, res = await run_query(refresher, store)
    elapsed = time.monotonic() - started

    check("超时按时返回（<0.5s，未等满 1s）", elapsed < 0.5,
          f"elapsed={elapsed:.3f}s")
    check("超时返回 attempted=True ok=False reason=timeout",
          res["attempted"] is True and res["ok"] is False
          and res["reason"] == "timeout", f"res={res}")
    check("超时时仍返回旧数据", data == old_rows, f"data={data}")
    check("超时不更新 last_success", refresher.last_success == 0.0,
          f"last_success={refresher.last_success}")


async def main_async() -> int:
    print("=" * 68)
    print("华为运动健康 —— 按需刷新自检（假时钟 + 假 run_once）")
    print("=" * 68)
    await case_interval_config()
    await case_within_interval()
    await case_due_refresh_once()
    await case_error_returns_old()
    await case_timeout()
    print("-" * 68)
    if _FAILED:
        print(f"结果：{_TOTAL - _FAILED}/{_TOTAL} 通过，{_FAILED} 条失败。")
        return 1
    print(f"结果：全部通过（{_TOTAL}/{_TOTAL}）。")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
