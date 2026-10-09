#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为运动健康插件 —— 对话「按需刷新」（查询命令执行前的同步闸门）。

定稿规则（v1 第十轮）：
  * 使用者跑查询命令时，若「距上次成功同步」已超过按需刷新间隔（默认 15 分钟），
    先跑一轮 SyncService.run_once() 再读库；未超过则直接读库、不刷新；
  * 间隔从 sync 分组 ``natural_query_sync_minutes`` 读（配置页可改），与自动同步的
    ``sync_interval_minutes``（默认 60 分钟）相互独立；
  * 刷新失败（网络抖动、token 失效、超时等）不得让查询命令报错或卡死：内部兜住异常、
    设超时上限，失败一律退回读旧数据；失败细节只进日志，不进使用者输出。

节流语义（最保守写法）：
    是否刷新由 ``max(上次成功时间, 上次尝试时间)`` 与当前时间之差决定。这样
  * 从未同步过 → 刷新一次；
  * 刷新失败后，在同一间隔内不会每次查询都重试（不会反复让使用者干等）；
  * 自动同步刚成功过 → 按需刷新自动跳过（两者共享「上次成功时间」）。

本模块不 import astrbot、不 import 第三方库；时间源（``now``）与「跑一轮同步」
（``run_once``）都由外部注入，故可用假时钟 + 假 run_once 直接做确定性自检
（见 scripts/selftest_ondemand.py）。

未确认：超时上限 DEFAULT_REFRESH_TIMEOUT_SECONDS=20s 为拍脑袋的经验值，未在真实
网络下计时校准；asyncio.wait_for 只能取消「等待」，已进入线程池的同步取数线程不会
被真正中断（其自然结束后自行收尾），故超时后可能有极短的残留后台动作。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

# 默认按需刷新间隔：15 分钟（与自动同步的 60 分钟分开）。
DEFAULT_REFRESH_INTERVAL_SECONDS = 15 * 60

# 单次按需刷新的超时上限（秒）：超过就让查询先返回旧数据，别让使用者干等。
DEFAULT_REFRESH_TIMEOUT_SECONDS = 20.0

# 使用者可见的「轻描淡写」提示：仅在「尝试刷新但失败」时追加一行，不含任何技术细节。
SOFT_FRESH_FAILED_HINT = "这次没能拿到最新数据，先帮你显示已缓存的记录。"

# refresh_if_due() 返回体里的 reason 取值。
REASON_FRESH = "fresh"          # 仍在间隔内，未尝试刷新
REASON_REFRESHED = "refreshed"  # 刷新成功
REASON_EMPTY = "empty"          # 同步跑了但返回未成功（token/取数/写库等）
REASON_TIMEOUT = "timeout"      # 刷新超时
REASON_ERROR = "error"          # 刷新抛异常


def interval_seconds_from_minutes(value: Any,
                                  default_minutes: int = 15) -> int:
    """把「分钟」配置值折算成秒；非法 / 小于 1 时回退到默认分钟数。

    纯函数，供 main.py 读配置与自检使用。
    """
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        minutes = int(default_minutes)
    if minutes < 1:
        minutes = int(default_minutes)
    return max(1, minutes) * 60


class OnDemandRefresher:
    """一趟「判断是否需要刷新 → 跑一轮同步 → 记录结果」的编排。

    参数：
        run_once         —— 无参 async 可调用对象，跑一轮同步并返回摘要 dict
                            （约定：``{"ok": bool, ...}``，与 SyncService.run_once 对齐）；
        interval_seconds —— 按需刷新间隔（秒）；
        timeout_seconds  —— 单次刷新超时上限（秒）；
        now              —— 时间源，返回 epoch 秒；默认 time.time，可注入假时钟；
        logger           —— 可选 logger（标准库或注入）；缺省静默。
    """

    def __init__(
        self,
        run_once: Callable[[], Any],
        *,
        interval_seconds: Any = DEFAULT_REFRESH_INTERVAL_SECONDS,
        timeout_seconds: Any = DEFAULT_REFRESH_TIMEOUT_SECONDS,
        now: Callable[[], float] | None = None,
        logger: Any = None,
    ) -> None:
        self.run_once = run_once
        self.interval_seconds = max(1, int(_as_float(interval_seconds,
                                                     DEFAULT_REFRESH_INTERVAL_SECONDS)))
        self.timeout_seconds = max(0.1, _as_float(timeout_seconds,
                                                  DEFAULT_REFRESH_TIMEOUT_SECONDS))
        self.now = now or time.time
        self.logger = logger or _LOGGER
        # 上次成功同步 / 上次尝试刷新的 epoch 秒（0=从未）。
        self._last_success = 0.0
        self._last_attempt = 0.0

    # ── 状态读写 ─────────────────────────────────────────────────────────
    @property
    def last_success(self) -> float:
        return self._last_success

    @property
    def last_attempt(self) -> float:
        return self._last_attempt

    def mark_success(self, when: Any = None) -> None:
        """记录一次「成功同步」（自动同步与按需刷新都调本方法）。"""
        stamp = _as_float(when, self._now())
        if stamp > self._last_success:
            self._last_success = stamp

    def _now(self) -> float:
        try:
            return float(self.now())
        except Exception:  # 时间源异常也不能让命令挂掉
            return time.time()

    def _reference(self) -> float:
        """节流基准：上次成功与上次尝试中较晚的一个。"""
        return max(self._last_success, self._last_attempt)

    def is_due(self, now: Any = None) -> bool:
        """判断是否到点该刷新：从未成功/尝试过，或距基准已超过间隔。"""
        reference = self._reference()
        if reference <= 0:
            return True
        current = _as_float(now, self._now())
        return (current - reference) >= self.interval_seconds

    # ── 主流程 ───────────────────────────────────────────────────────────
    async def refresh_if_due(self) -> dict[str, Any]:
        """到点则刷新一次；任何失败/超时都兜住，绝不向上抛异常。"""
        if not self.is_due():
            return {"attempted": False, "ok": True, "reason": REASON_FRESH}

        # 先落「上次尝试」，确保并发/连续调用在同一间隔内不会重复刷新。
        self._last_attempt = self._now()

        try:
            summary = await asyncio.wait_for(
                self.run_once(), timeout=self.timeout_seconds)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            self._log("warning",
                      "[华为运动健康] 按需刷新超时（>%.1fs），先返回已缓存数据",
                      self.timeout_seconds)
            return {"attempted": True, "ok": False, "reason": REASON_TIMEOUT}
        except Exception as error:  # noqa: BLE001 - 刷新失败绝不能打断查询
            self._log("warning",
                      "[华为运动健康] 按需刷新异常（%s: %s），先返回已缓存数据",
                      type(error).__name__, error)
            return {"attempted": True, "ok": False, "reason": REASON_ERROR,
                    "error": type(error).__name__}

        if not (isinstance(summary, dict) and summary.get("ok")):
            status = summary.get("status") if isinstance(summary, dict) else None
            self._log("warning",
                      "[华为运动健康] 按需刷新未成功（status=%s），先返回已缓存数据",
                      status)
            return {"attempted": True, "ok": False, "reason": REASON_EMPTY,
                    "status": status}

        self._last_success = self._now()
        self._log("info", "[华为运动健康] 按需刷新完成（间隔 %ss）",
                  self.interval_seconds)
        return {"attempted": True, "ok": True, "reason": REASON_REFRESHED}

    # ── 日志小工具 ───────────────────────────────────────────────────────
    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self.logger, level, None)
        if not callable(method):
            return
        try:
            method(message, *args)
        except Exception:
            pass


def _as_float(value: Any, default: float) -> float:
    """把任意值安全转成 float；非法返回 default。"""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "DEFAULT_REFRESH_TIMEOUT_SECONDS",
    "SOFT_FRESH_FAILED_HINT",
    "REASON_FRESH",
    "REASON_REFRESHED",
    "REASON_EMPTY",
    "REASON_TIMEOUT",
    "REASON_ERROR",
    "interval_seconds_from_minutes",
    "OnDemandRefresher",
]
