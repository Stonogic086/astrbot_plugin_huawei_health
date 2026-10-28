#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM 工具层的数据整理与渲染（不 import astrbot，便于离线自检）。

设计目标：
  * 给 ``@filter.llm_tool`` 提供短小、可读的中文结果，避免把原始行直接丢给模型；
  * 所有函数只读 ``HealthStore``，不写库、不联网、不读配置；
  * 健康数值缺失时统一写「无」，不拿 0 冒充读数；
  * 结果末尾带数据来源与免责声明，提醒模型这是云端日汇总、不是实时诊断。

本模块与 ``features/llm_injection.py`` 的区别：
  * injection 面向「每轮自动附在上下文里」的摘要；
  * 本模块面向「模型按需调用工具」的查询结果，可按天/按指标取数，
    并额外提供睡眠趋势、心率趋势、连接状态三类工具。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any, Iterable

try:  # 正式运行：作为插件子包导入
    from ..commands.health_query import _fmt_dur, fmt_num
    from ..storage.models import normalize_day
    from .llm_injection import SUMMARY_HEADER, health_summary
except ImportError:  # 脚本直跑：插件根目录在 sys.path 上
    from commands.health_query import _fmt_dur, fmt_num  # type: ignore[no-redef]
    from storage.models import normalize_day  # type: ignore[no-redef]
    from features.llm_injection import (  # type: ignore[no-redef]
        SUMMARY_HEADER,
        health_summary,
    )

__all__ = [
    "DEFAULT_OVERVIEW_DAYS",
    "DEFAULT_TREND_DAYS",
    "MAX_DAYS",
    "FOOTER",
    "clamp_days",
    "resolve_day",
    "with_hint",
    "render_overview",
    "render_sleep_trend",
    "render_heart_trend",
    "render_status",
]

DEFAULT_OVERVIEW_DAYS = 3
DEFAULT_TREND_DAYS = 7
MAX_DAYS = 30

FOOTER = (
    "数据来源：华为运动健康云端已同步的日汇总，非实时；"
    "仅作健康趋势参考，不构成医疗诊断。"
)

# 各表在「连接状态」工具里的中文名与日期列。
_STATUS_MODELS: tuple[tuple[str, str, str], ...] = (
    ("daily_activity", "活动", "date"),
    ("heart_rate_sample", "心率", "date"),
    ("sleep_session", "睡眠", "date"),
    ("stress_sample", "压力", "date"),
    ("spo2_sample", "血氧", "date"),
    ("training_session", "训练", "start_date"),
)


def _to_int(value: Any, default: int) -> int:
    """把任意值转成正整数；失败时返回 default。"""
    if value is None or value == "":
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        match = re.search(r"\d+", str(value))
        return int(match.group()) if match else default


def clamp_days(value: Any, default: int = DEFAULT_OVERVIEW_DAYS,
               maximum: int = MAX_DAYS) -> int:
    """把天数参数夹在 1..maximum；非法值用 default。"""
    days = _to_int(value, default)
    if days < 1:
        days = default
    return max(1, min(days, maximum))


def resolve_day(value: Any, today: date | None = None) -> str:
    """把工具参数里的日期解析成 ``YYYY-MM-DD``。

    支持空串（今天）、今天/今日、昨天/昨日、ISO 日期。
    无法识别时回落到今天——工具结果里会带实际日期，模型能看出来。
    """
    base = today or date.today()
    text = "" if value is None else str(value).strip()
    lowered = text.lower()
    if lowered in ("", "today", "今天", "今日"):
        return base.isoformat()
    if lowered in ("yesterday", "昨天", "昨日"):
        return (base - timedelta(days=1)).isoformat()
    normalized = normalize_day(text)
    return normalized or base.isoformat()


def with_hint(text: str, hint: str) -> str:
    """把「按需刷新失败」的轻提示附在工具结果后面。"""
    body = str(text or "").rstrip()
    tip = str(hint or "").strip()
    return f"{body}\n\n{tip}" if tip else body


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_hr(value: Any) -> str:
    number = _num(value)
    return "无" if number is None else f"{number:g}"


def _fmt_pct(value: Any) -> str:
    number = _num(value)
    return "无" if number is None else f"{number:g}%"


def _fmt_float(value: Any) -> str:
    number = _num(value)
    return "无" if number is None else f"{number:g}"


def _mean(values: Iterable[Any]) -> float | None:
    numbers = [number for number in (_num(value) for value in values)
               if number is not None]
    return sum(numbers) / len(numbers) if numbers else None


def _window(days: int, today: date | None = None) -> tuple[int, date, date]:
    count = clamp_days(days, DEFAULT_TREND_DAYS)
    end = today or date.today()
    return count, end - timedelta(days=count - 1), end


def render_overview(
    store: Any,
    days: int = DEFAULT_OVERVIEW_DAYS,
    today: date | None = None,
    *,
    training_min_duration_min: Any = None,
    training_min_distance_m: Any = None,
) -> str:
    """最近 N 天总览：活动/训练按窗口，睡眠/压力/血氧取当天。"""
    count, start, end = _window(days, today)
    if store is None:
        return "健康数据存储暂不可用。"
    thresholds: dict[str, Any] = {}
    if training_min_duration_min is not None:
        thresholds["training_min_duration_min"] = training_min_duration_min
    if training_min_distance_m is not None:
        thresholds["training_min_distance_m"] = training_min_distance_m
    text = health_summary(
        store, count, end, **thresholds,
    )
    if not text:
        return f"{start} ~ {end} 没有可用的健康记录。\n{FOOTER}"
    # health_summary 的抬头是给「上下文注入」用的；工具结果换成更直白的抬头。
    text = text.replace(
        SUMMARY_HEADER,
        f"【健康概览 · 最近 {count} 天（{start} ~ {end}）】\n"
        f"（活动/训练为窗口汇总；睡眠/压力/血氧为 {end} 当天）",
        1,
    )
    return f"{text.rstrip()}\n\n{FOOTER}"


def render_sleep_trend(store: Any, days: int = DEFAULT_TREND_DAYS,
                       today: date | None = None) -> str:
    """最近 N 天睡眠趋势：逐日时长/评分/效率/HRV/血氧 + 平均值。"""
    count, start, end = _window(days, today)
    header = f"最近 {count} 天睡眠（{start} ~ {end}）"
    if store is None:
        return "健康数据存储暂不可用。"
    rows = store.query("sleep_session", start.isoformat(), end.isoformat())
    if not rows:
        return f"{header}\n没有睡眠记录。\n{FOOTER}"

    lines = [header]
    for row in rows:
        parts = [f"时长 {_fmt_dur(row.get('duration_min'))}",
                 f"评分 {fmt_num(row.get('score'))}",
                 f"效率 {_fmt_pct(row.get('efficiency'))}"]
        if _num(row.get("hrv")) is not None:
            parts.append(f"HRV {_fmt_float(row.get('hrv'))}")
        if _num(row.get("spo2")) is not None:
            parts.append(f"血氧 {_fmt_pct(row.get('spo2'))}")
        lines.append(f"- {row.get('date')}：" + "，".join(parts))

    avg_duration = _mean(row.get("duration_min") for row in rows)
    avg_score = _mean(row.get("score") for row in rows)
    avg_efficiency = _mean(row.get("efficiency") for row in rows)
    avg_hrv = _mean(row.get("hrv") for row in rows)
    avg_spo2 = _mean(row.get("spo2") for row in rows)
    averages = [f"时长 {_fmt_dur(avg_duration) if avg_duration is not None else '无'}",
                f"评分 {_fmt_float(avg_score)}",
                f"效率 {_fmt_pct(avg_efficiency)}"]
    if avg_hrv is not None:
        averages.append(f"HRV {_fmt_float(avg_hrv)}")
    if avg_spo2 is not None:
        averages.append(f"血氧 {_fmt_pct(avg_spo2)}")
    lines.append("平均：" + "，".join(averages))
    lines.append(FOOTER)
    return "\n".join(lines)


def render_heart_trend(store: Any, days: int = DEFAULT_TREND_DAYS,
                       today: date | None = None) -> str:
    """最近 N 天心率日值趋势：静息/平均静息/日间/最高/最低。"""
    count, start, end = _window(days, today)
    header = f"最近 {count} 天心率（{start} ~ {end}）"
    if store is None:
        return "健康数据存储暂不可用。"
    rows = store.query("heart_rate_sample", start.isoformat(), end.isoformat())
    if not rows:
        return f"{header}\n没有心率记录。\n{FOOTER}"

    lines = [header]
    for row in rows:
        parts = []
        for key, label in (
            ("resting_hr", "静息"),
            ("average_resting_hr", "平均静息"),
            ("day_hr", "日间"),
            ("max_hr", "最高"),
            ("min_hr", "最低"),
        ):
            if _num(row.get(key)) is not None:
                parts.append(f"{label} {_fmt_hr(row.get(key))}")
        if parts:
            lines.append(f"- {row.get('date')}：" + "，".join(parts))

    if len(lines) == 1:
        lines.append("没有有效心率读数。")

    avg_resting = _mean(row.get("resting_hr") for row in rows)
    avg_average_resting = _mean(row.get("average_resting_hr") for row in rows)
    if avg_resting is not None or avg_average_resting is not None:
        parts = []
        if avg_resting is not None:
            parts.append(f"静息均值 {_fmt_hr(avg_resting)}")
        if avg_average_resting is not None:
            parts.append(f"平均静息均值 {_fmt_hr(avg_average_resting)}")
        lines.append("汇总：" + "，".join(parts))
    lines.append(FOOTER)
    return "\n".join(lines)


def render_status(
    store: Any,
    *,
    refresh_token_present: bool,
    refresh_expires_at: Any = 0,
    sync_enabled: bool = False,
    sync_interval_minutes: Any = 60,
) -> str:
    """连接与数据覆盖状态：不返回任何健康数值，只告诉模型「有没有数据、新不新」。"""
    lines = ["【华为运动健康 · 连接状态】"]
    lines.append(f"- 云端凭证：{'已配置' if refresh_token_present else '未配置'}")
    expiry = _num(refresh_expires_at)
    if expiry and expiry > 0:
        try:
            when = datetime.fromtimestamp(expiry).strftime("%Y-%m-%d")
            lines.append(f"- refresh token 到期：{when}")
        except (OverflowError, OSError, ValueError):
            pass
    lines.append(
        f"- 自动同步：{'开启' if sync_enabled else '关闭'}"
        f"（间隔 {clamp_days(sync_interval_minutes, 60, 1440)} 分钟）"
    )
    if store is None:
        lines.append("- 本地存储：不可用")
        return "\n".join(lines) + f"\n{FOOTER}"

    round_state = store.sync_state("round")
    if round_state:
        lines.append(
            f"- 最近同步尝试：{round_state.get('last_attempt_at') or '未知'}"
            f"（{round_state.get('last_status') or '未知'}）"
        )
        if round_state.get("last_success_at"):
            lines.append(f"- 上次同步成功：{round_state.get('last_success_at')}")
    for model, label, date_key in _STATUS_MODELS:
        rows = store.query(model)
        if rows:
            first = rows[0].get(date_key) or "未知"
            last = rows[-1].get(date_key) or "未知"
            lines.append(f"- {label}：{len(rows)} 条，{first} ~ {last}")
        else:
            lines.append(f"- {label}：无数据")
    lines.append(FOOTER)
    return "\n".join(lines)
