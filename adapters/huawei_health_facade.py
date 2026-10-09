#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为运动健康 —— 取数门面实现（云端原始响应 → 存储层口径）。

``HuaweiHealthFacade`` 是 ``adapters.data_facade.HealthDataFacade`` 的实现：把协议层
``HuaweiHealthCloudAdapter`` 的 async 取数方法包成六个 ``iter_*(start, end)``。

三条语义（本模块的核心约定）：

  1. **单位与时区只重排一处**：一律调用 ``storage.models`` 的既有函数
     （``normalize_daily_activity`` / ``normalize_health`` / ``merge_training_segments`` /
     ``has_daily_reading``），门面里不重写换算——千分之一 kcal→kcal、0 与负值视为没测到、
     epoch 毫秒→本地时间文本都在那一处；
  2. **「云端没数据」不是失败**：某类云端没给记录、或给的记录一个有效读数都没有
     （0 / 负值 = 手环没测到）→ 返回空 list，由 ``services/sync_service`` 标注「无」；
     绝不用默认值填充、也不编造时间戳或分钟级明细；
  3. **「取数失败」只抛三种异常**：协议层四个异常按 ``adapters.errors.PROTOCOL_ERROR_MAP``
     在这里消化成 ``HuaweiHealthAuthenticationError`` / ``HuaweiHealthNetworkError`` /
     ``HuaweiHealthParseError``，协议层细节不外泄；上层据此分流（重登 / 下轮重试 / 单类降级）。

窗口换算：协议层只支持「最近 N 天（含今天）」，故 ``days`` 由 ``start`` 折算，
``end`` 只用于过滤掉窗口右界之外的行（生产接线里 ``end`` 恒为今天，过滤是空操作）。

窗口内缓存：健康四类（心率 / 睡眠 / 压力 / 血氧）来自**同一次** ``health_summary`` 响应，
故按窗口缓存该响应的归一结果，四个 ``iter_*`` 只打一次网；``close()`` 清掉缓存。

本模块不 import astrbot、不 import 第三方库；只依赖协议层那四个 async 方法，
自检时用桩替换即可（见 scripts/selftest_facade.py）。
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Iterable

from .data_facade import DATA_TYPES, HealthDataFacade
from .errors import (
    PROTOCOL_ERROR_MAP,
    HuaweiHealthError,
    HuaweiHealthParseError,
)

try:  # 宿主以内嵌包导入（astrbot_plugin_huawei_health.adapters）
    from ..storage import models
except ImportError:  # 脚本直跑：插件根目录在 sys.path 上（adapters / storage 是顶层包）
    from storage import models  # type: ignore[no-redef]

__all__ = ["HuaweiHealthFacade"]


class HuaweiHealthFacade(HealthDataFacade):
    """把华为云协议层包成「六类数据 → 存储层口径」的取数门面。

    参数：
        adapter —— ``HuaweiHealthCloudAdapter`` 或自检用的同形桩（只要提供
                   ``refresh`` / ``daily_summary`` / ``health_summary`` / ``session_segments``
                   四个 async 方法即可，不连真网也能跑）。
    """

    def __init__(self, adapter: Any) -> None:
        self.adapter = adapter
        self._health_cache: dict[tuple[str, str], dict[str, list[dict]]] = {}

    # ── 生命周期 ─────────────────────────────────────────────────────────
    async def connect(self) -> bool:
        """刷新一次 access token；返回是否拿到可用 access token。"""
        await self._call(self.adapter.refresh)
        tokens = getattr(self.adapter, "tokens", None)
        return bool(getattr(tokens, "access_token", None))

    async def close(self) -> None:
        """清掉窗口缓存（协议层无持久连接，故只是清缓存）。"""
        self._health_cache.clear()

    def get_available_data_types(self) -> list[str]:
        """返回门面声明支持的六类数据（不探测云端）。"""
        return list(DATA_TYPES)

    # ── 六类取数：返回 storage/models 口径的行 ───────────────────────────
    async def iter_daily_activity(self, start: date, end: date) -> list[dict]:
        """[start, end] 内的日汇总（协议层 getSportsStat 的整形行 → 存储口径）。"""
        raw = await self._call(self.adapter.daily_summary, self._days(start))
        rows = _within(models.normalize_daily_activity(raw), start, end)
        # 云端给了记录、但一个有效读数都没有（全 0 / 缺失）→ 视同「无数据」，不写空壳行。
        if not any(models.has_daily_reading(row) for row in rows):
            return []
        return rows

    async def iter_heart_rate(self, start: date, end: date) -> list[dict]:
        """心率日值（heartRate 族）；v1 只有日粒度汇总型样本，不拆分钟。"""
        return list((await self._health_groups(start, end))["heart_rate"])

    async def iter_sleep(self, start: date, end: date) -> list[dict]:
        """睡眠汇总（professionalSleep 族，只取汇总值，分期明细留 v2）。"""
        return list((await self._health_groups(start, end))["sleep"])

    async def iter_spo2(self, start: date, end: date) -> list[dict]:
        """血氧（取自睡眠响应的 lastAvgSpO2，不是独立采样）。"""
        return list((await self._health_groups(start, end))["spo2"])

    async def iter_stress(self, start: date, end: date) -> list[dict]:
        """压力日值（stress 族）。"""
        return list((await self._health_groups(start, end))["stress"])

    async def iter_training(self, start: date, end: date) -> list[dict]:
        """训练会话：原始分钟段 → dataId 去重 / ≤15 分钟合并都由 storage.models 完成。"""
        raw = await self._call(self.adapter.session_segments, self._days(start))
        sessions = models.merge_training_segments(raw)
        low, high = start.isoformat(), end.isoformat()
        return [
            row for row in sessions
            if row.get("start_date") and low <= str(row["start_date"]) <= high
        ]

    # ── 内部 ─────────────────────────────────────────────────────────────
    async def _health_groups(self, start: date, end: date) -> dict[str, list[dict]]:
        """一次 health_summary 出四类（心率 / 睡眠 / 压力 / 血氧），按窗口缓存。"""
        key = (start.isoformat(), end.isoformat())
        cached = self._health_cache.get(key)
        if cached is None:
            raw = await self._call(self.adapter.health_summary, self._days(start))
            groups = models.normalize_health(raw)
            cached = {
                name: _within(rows, start, end) for name, rows in groups.items()
            }
            self._health_cache[key] = cached
        return cached

    @staticmethod
    def _days(start: date) -> int:
        """按 start 折算协议层的「最近 N 天（含今天）」。"""
        return max(1, (date.today() - start).days + 1)

    async def _call(self, method: Callable[..., Any], *args: Any) -> Any:
        """调协议层方法，并把协议层异常翻译成门面三类（协议层细节不外泄）。"""
        try:
            return await method(*args)
        except HuaweiHealthError:
            raise  # 已经是门面异常（自检注入的桩可能直接抛这类），原样上抛
        except Exception as error:  # noqa: BLE001 - 协议层四个异常在这里消化掉
            raise _translate(error) from error


def _translate(error: BaseException) -> HuaweiHealthError:
    """协议层异常 → 门面三类（映射表见 ``adapters.errors.PROTOCOL_ERROR_MAP``）。"""
    for protocol_error, facade_error, _note in PROTOCOL_ERROR_MAP:
        if isinstance(error, protocol_error):
            return facade_error(str(error))
    # 协议层没预见的异常（响应形状 / 类型不符）按「云端应答不可用」降级这一类，不重试。
    return HuaweiHealthParseError(f"{type(error).__name__}: {error}")


def _within(rows: Iterable[dict] | None, start: date, end: date) -> list[dict]:
    """只留本地日期落在 [start, end] 内的行（日期无法识别 / 越界的行直接丢）。"""
    low, high = start.isoformat(), end.isoformat()
    kept: list[dict] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        day = models.normalize_day(row.get("date"))
        if day is not None and low <= day <= high:
            kept.append(row)
    return kept
