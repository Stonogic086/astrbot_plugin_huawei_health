"""华为运动健康插件 —— 查询命令 handler（不 import astrbot，可脱离框架自检）。

每个 handler 都是 async generator，只读 event 的 ``message_str`` 与 ``plain_result``，
因此用 stub event 即可直接驱动（见 scripts/selftest_commands.py）。

职责边界（严格遵守本轮范围）：
  * 只做「读」：不触发按需刷新、不写库、不调用任何 LLM；
  * store 为 None（存储层未初始化）时返回明确提示，不抛异常。

本模块不 import astrbot：装饰器（@filter.command）由 main.py 在外层包装。
"""

from __future__ import annotations

from datetime import date
from typing import Any, AsyncIterator

from .health_query import (
    COMMAND_ACTIVITY,
    COMMAND_SLEEP,
    COMMAND_TRAINING,
    DEFAULT_ACTIVITY_DAYS,
    DEFAULT_TRAINING_DAYS,
    command_tail,
    parse_day,
    parse_days,
    render_activity,
    render_sleep,
    render_training,
)

__all__ = [
    "COMMAND_ACTIVITY",
    "COMMAND_SLEEP",
    "COMMAND_TRAINING",
    "STORE_UNAVAILABLE",
    "handle_activity",
    "handle_sleep",
    "handle_training",
    "plain_result",
]

STORE_UNAVAILABLE = "健康数据暂不可用（存储层未初始化）。"


def plain_result(event: Any, text: str) -> Any:
    """把文本封成宿主消息结果；无 ``plain_result``（如纯 stub）时退回字符串。

    插件内唯一实现点：main.py 的命令包装器直接 import 本函数，不再各写一份。
    """
    make = getattr(event, "plain_result", None)
    if callable(make):
        try:
            return make(text)
        except Exception:
            return text
    return text


async def handle_activity(store: Any, event: Any) -> AsyncIterator[Any]:
    """「健康活动 [天数]」：最近 N 天活动汇总，默认 3 天。"""
    if store is None:
        yield plain_result(event, STORE_UNAVAILABLE)
        return
    days = parse_days(command_tail(event, COMMAND_ACTIVITY), DEFAULT_ACTIVITY_DAYS)
    yield plain_result(event, render_activity(store, days))


async def handle_sleep(store: Any, event: Any) -> AsyncIterator[Any]:
    """「健康睡眠 [YYYY-MM-DD]」：某天睡眠汇总 + 心率日值，默认今天。"""
    if store is None:
        yield plain_result(event, STORE_UNAVAILABLE)
        return
    day = parse_day(command_tail(event, COMMAND_SLEEP)) or date.today().isoformat()
    yield plain_result(event, render_sleep(store, day))


async def handle_training(
    store: Any,
    event: Any,
    *,
    min_duration_min: Any = None,
    min_distance_m: Any = None,
) -> AsyncIterator[Any]:
    """「健康训练 [天数]」：最近 N 天训练会话列表，默认 7 天。

    只列有效训练（碎片被过滤掉）；阈值由调用方从配置传入，缺省用判定层默认值。
    """
    if store is None:
        yield plain_result(event, STORE_UNAVAILABLE)
        return
    days = parse_days(command_tail(event, COMMAND_TRAINING), DEFAULT_TRAINING_DAYS)
    thresholds: dict[str, Any] = {}
    if min_duration_min is not None:
        thresholds["min_duration_min"] = min_duration_min
    if min_distance_m is not None:
        thresholds["min_distance_m"] = min_distance_m
    yield plain_result(event, render_training(store, days, **thresholds))
