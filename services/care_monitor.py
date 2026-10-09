#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为运动健康插件 —— 主动关怀的规则层（夜间 / 压力 / 起床 / 运动后四场景）。

本模块只回答「此刻该不该发一条主动关怀」，不含任何模型调用、不发送、不 import astrbot：
  * 夜间：深夜窗口判断（支持跨零点窗口）+ 每夜一次去重 + 「所有者近期确有私聊活动」
    + 场景冷却 + 每日上限；
  * 压力：按当日日均分定档（放松 / 正常 / 中等 / 偏高），达到配置档位才触发，每天最多一条；
  * 起床：今日睡眠记录的起床时间匹配当前日期且距现在不超过宽容度，按记录去重；
  * 运动后：本轮新发现的训练会话逐条触发，用「记录自带结束时间」与「本轮发现时间」的
    差值判断走「及时关怀」还是「滞后信息展示」分支，按 session_key 去重；
  * 命中则给出候选 finding（一条事实行 + 程序侧渲染所需的原始字段，供措辞模型与模板使用）。

口径来源：小米插件 `services/monitor_service.py`（深夜窗口 + 近期私聊活动 + 冷却/每日上限）；
压力 / 起床 / 运动后三场景按本项目定稿口径实现（不设模型闸门，频率各自独立）。

「本轮新增」的判定：夜间 / 压力 / 起床 / 运动后都落在同一个 5 分钟关怀巡检里，去重统一走
``care_event_key``（关怀层自己的「已处理」账本）——一条记录被处理过一次就不再触发；
分支判定（及时 / 滞后、宽容度）则用**记录自带时间**（``wakeup_local`` / ``end_local``）
与本轮时刻比较。故不另起常驻轮询，也不依赖易被重复同步刷新的 ``updated_at``。

时间一律本地时间。``now`` 可注入（默认 ``datetime.now``），故可用假时钟做确定性自检。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any

try:  # 常规加载路径：作为插件包子模块导入。
    from ..storage import schema
    from ..storage.models import (
        DEFAULT_TRAINING_MIN_DISTANCE_M,
        DEFAULT_TRAINING_MIN_DURATION_MIN,
        SPORT_TYPE_NAMES,
        is_valid_training,
        to_float,
        to_int,
    )
except ImportError:  # 少数自检脚本把 services 当顶层包导入（与 sync_service 同一套回退）
    from storage import schema  # type: ignore[no-redef]
    from storage.models import (  # type: ignore[no-redef]
        DEFAULT_TRAINING_MIN_DISTANCE_M,
        DEFAULT_TRAINING_MIN_DURATION_MIN,
        SPORT_TYPE_NAMES,
        is_valid_training,
        to_float,
        to_int,
    )

_LOGGER = logging.getLogger(__name__)

# 场景名（与 storage/schema.py 的 CARE_SCENARIOS 一致）。
NIGHT_SCENARIO = "night"
STRESS_SCENARIO = "stress"
WAKEUP_SCENARIO = "wakeup"
WORKOUT_SCENARIO = "workout"

# 夜间窗口默认起止。
DEFAULT_NIGHT_START = "00:30"
DEFAULT_NIGHT_END = "06:00"

# 夜间活跃窗口（分钟）：所有者最近多少分钟内有私聊活动才算「还醒着」。沿用小米默认 45。
DEFAULT_ACTIVITY_WINDOW_MINUTES = 45
# 同一场景两次发送之间的最小间隔（分钟）。沿用小米默认 120。
DEFAULT_COOLDOWN_MINUTES = 120
# 每个自然日最多发几条主动关怀（所有场景合计）。沿用小米默认 3。
DEFAULT_DAILY_LIMIT = 3

# 关怀检查间隔（分钟）：本轮为「测试期参数」，固定 5 分钟一轮（不是配置项）。
CARE_CHECK_INTERVAL_MINUTES = 5

# ── 配置读取 ─────────────────────────────────────────────────────────────
CARE_CONF_GROUP = "proactive_care"
# 训练碎片判据的阈值与同步节奏同属 sync 分组（命令展示 / LLM 摘要 / 运动后关怀共用）。
SYNC_CONF_GROUP = "sync"

# 压力档位（华为四档，按当日日均分）：放松/正常/中等/偏高。
STRESS_GRADES: tuple[str, ...] = ("relaxed", "normal", "moderate", "high")
DEFAULT_STRESS_THRESHOLD = "moderate"

# 档位序（数值越大越紧绷）与中文名；阈值比较按序，不看字符串。
STRESS_GRADE_ORDER: dict[str, int] = {
    "relaxed": 0, "normal": 1, "moderate": 2, "high": 3}
STRESS_GRADE_LABELS: dict[str, str] = {
    "relaxed": "放松", "normal": "正常", "moderate": "中等", "high": "偏高"}
# 华为四档区间上界（含端点）：放松 1–29 / 正常 30–59 / 中等 60–79 / 偏高 ≥80。
STRESS_GRADE_BOUNDS: tuple[tuple[str, float], ...] = (
    ("relaxed", 29.0), ("normal", 59.0), ("moderate", 79.0))

# 起床 / 运动宽容度默认值（分钟）。
DEFAULT_WAKEUP_TOLERANCE_MINUTES = 30
DEFAULT_WORKOUT_TOLERANCE_MINUTES = 10

# 运动后关怀的回看窗口（天）：只在本窗口内找「本轮新增」的训练会话，避免给陈年记录补发。
WORKOUT_LOOKBACK_DAYS = 1
# 起床关怀的回看窗口（天）：起床时间落在当天的睡眠记录，其归属日最多早一天。
WAKEUP_LOOKBACK_DAYS = 1

# 起床开场词：程序按当前时刻分四档（上界小时，不含）：04:00 前「晚上好」、
# 12:00 前「上午好」、18:00 前「下午好」、18:00 后「晚上好」。
GREETING_STEPS: tuple[tuple[int, str], ...] = (
    (4, "晚上好"), (12, "上午好"), (18, "下午好"), (24, "晚上好"))
DEFAULT_GREETING = GREETING_STEPS[-1][1]


@dataclass(frozen=True)
class CareSettings:
    """主动关怀的总开关与四场景开关/阈值（出厂默认全部关闭）。"""

    master_enabled: bool = False
    night_enabled: bool = False
    night_start: str = DEFAULT_NIGHT_START
    night_end: str = DEFAULT_NIGHT_END
    stress_enabled: bool = False
    stress_threshold: str = DEFAULT_STRESS_THRESHOLD
    wakeup_enabled: bool = False
    wakeup_tolerance_minutes: int = DEFAULT_WAKEUP_TOLERANCE_MINUTES
    workout_enabled: bool = False
    workout_tolerance_minutes: int = DEFAULT_WORKOUT_TOLERANCE_MINUTES
    # 训练碎片判据阈值（与命令展示 / LLM 摘要同一套，来自 sync 分组）。
    training_min_duration_min: int = DEFAULT_TRAINING_MIN_DURATION_MIN
    training_min_distance_m: int = DEFAULT_TRAINING_MIN_DISTANCE_M

    @property
    def any_enabled(self) -> bool:
        """是否有任一场景开启（总开关已在 ``care_settings_from_config`` 里折算）。"""
        return bool(
            self.night_enabled or self.stress_enabled
            or self.wakeup_enabled or self.workout_enabled
        )


def _read(config: Any, key: str, default: Any) -> Any:
    """读一个关怀配置项，兼容「分组 schema」与「扁平 key」两种布局。"""
    if not isinstance(config, dict):
        return default
    grouped = config.get(CARE_CONF_GROUP)
    if isinstance(grouped, dict) and key in grouped:
        return grouped.get(key)
    if key in config:
        return config.get(key)
    return default


def _read_sync(config: Any, key: str, default: Any) -> Any:
    """读一个 sync 分组配置项（训练碎片判据阈值与同步节奏同一分组），兼容扁平布局。"""
    if not isinstance(config, dict):
        return default
    grouped = config.get(SYNC_CONF_GROUP)
    if isinstance(grouped, dict) and key in grouped:
        return grouped.get(key)
    if key in config:
        return config.get(key)
    return default


def _flag(value: Any) -> bool:
    """严格布尔化：只认真正的 True（字符串 / 数字 / None 一律视为关）。"""
    return value is True


def _int_in_range(value: Any, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return int(default)
    return max(low, min(high, number))


def training_thresholds_from_config(config: Any = None) -> tuple[int, int]:
    """从 sync 分组读训练碎片判据阈值，归一成 ``(最短时长分钟, 最短距离米)``。

    口径与 ``care_settings_from_config`` 完全一致（同一对键、同一夹范围 ``_int_in_range``）：
    命令展示 / LLM 摘要 / 入库时的 is_fragment 快照 / 运动后关怀四处共用这一对已归一的值，
    避免配置写成 0 时只有关怀侧被夹到 1、其余几处仍拿 0 的口径分叉。
    """
    return (
        _int_in_range(
            _read_sync(config, "training_min_duration_min",
                       DEFAULT_TRAINING_MIN_DURATION_MIN),
            DEFAULT_TRAINING_MIN_DURATION_MIN, 1, 600),
        _int_in_range(
            _read_sync(config, "training_min_distance_m",
                       DEFAULT_TRAINING_MIN_DISTANCE_M),
            DEFAULT_TRAINING_MIN_DISTANCE_M, 1, 100000),
    )


def _clock(value: Any, default: str) -> str:
    """把 ``HH:MM`` 归一成 ``HH:MM``；非法一律回退默认值。"""
    parsed = parse_clock(value, None)
    return default if parsed is None else f"{parsed.hour:02d}:{parsed.minute:02d}"


def care_settings_from_config(config: Any = None) -> CareSettings:
    """从插件配置构造关怀设置。总开关关闭时，四个场景开关一律折算为关闭。

    出厂默认：总开关关闭、四场景全关、夜间窗口 00:30–06:00、压力档位「中等」、
    起床宽容度 30 分钟、运动宽容度 10 分钟。
    """
    master = _flag(_read(config, "enable_proactive_care", False))
    training_min_duration_min, training_min_distance_m = (
        training_thresholds_from_config(config))
    return CareSettings(
        master_enabled=master,
        night_enabled=master and _flag(_read(config, "enable_night_care", False)),
        night_start=_clock(_read(config, "night_start", DEFAULT_NIGHT_START),
                           DEFAULT_NIGHT_START),
        night_end=_clock(_read(config, "night_end", DEFAULT_NIGHT_END),
                         DEFAULT_NIGHT_END),
        stress_enabled=master and _flag(_read(config, "enable_stress_care", False)),
        stress_threshold=_stress_threshold(_read(config, "stress_threshold",
                                                 DEFAULT_STRESS_THRESHOLD)),
        wakeup_enabled=master and _flag(_read(config, "enable_wakeup_care", False)),
        wakeup_tolerance_minutes=_int_in_range(
            _read(config, "wakeup_tolerance_minutes", DEFAULT_WAKEUP_TOLERANCE_MINUTES),
            DEFAULT_WAKEUP_TOLERANCE_MINUTES, 0, 180),
        workout_enabled=master and _flag(_read(config, "enable_workout_care", False)),
        workout_tolerance_minutes=_int_in_range(
            _read(config, "workout_tolerance_minutes", DEFAULT_WORKOUT_TOLERANCE_MINUTES),
            DEFAULT_WORKOUT_TOLERANCE_MINUTES, 0, 120),
        training_min_duration_min=training_min_duration_min,
        training_min_distance_m=training_min_distance_m,
    )


def _stress_threshold(value: Any) -> str:
    """压力档位归一：接受英文名与中文名，非法回退默认「中等」。"""
    text = str(value or "").strip().lower()
    aliases = {
        "relaxed": "relaxed", "放松": "relaxed",
        "normal": "normal", "正常": "normal",
        "moderate": "moderate", "中等": "moderate",
        "high": "high", "偏高": "high",
    }
    return aliases.get(text, DEFAULT_STRESS_THRESHOLD)


# ── 压力档位 / 开场词 / 运动名（纯函数）──────────────────────────────────
def stress_grade(value: Any) -> str | None:
    """当日日均分 → 档位名；缺失 / 小于 1（云端「没测到」）返回 None。

    分档照定稿：1–29 放松 / 30–59 正常 / 60–79 中等 / ≥80 偏高。
    """
    number = to_float(value)
    if number is None or number < 1:
        return None
    for grade, upper in STRESS_GRADE_BOUNDS:
        if number <= upper:
            return grade
    return "high"


def stress_grade_rank(grade: Any) -> int:
    """档位序（未知档位返回 -1，即任何阈值都比它高 → 不触发）。"""
    return STRESS_GRADE_ORDER.get(str(grade or ""), -1)


def stress_grade_meets(grade: Any, threshold: Any) -> bool:
    """该档位是否达到（含）触发阈值：如阈值 moderate 时 moderate / high 才算达标。"""
    return stress_grade_rank(grade) >= stress_grade_rank(
        _stress_threshold(threshold))


def greeting_for(hour: Any) -> str:
    """按当前小时给起床关怀的开场词：04 前晚上好 / 12 前上午好 / 18 前下午好 / 否则晚上好。"""
    try:
        value = int(hour) % 24
    except (TypeError, ValueError):
        return DEFAULT_GREETING
    for upper, greeting in GREETING_STEPS:
        if value < upper:
            return greeting
    return DEFAULT_GREETING


def sport_label(sport_type: Any) -> str:
    """运动类型码 → 展示名；未收录的码回退「运动类型N」（与命令层同一口径，不误标）。"""
    code = to_int(sport_type)
    if code is None:
        return "运动"
    return SPORT_TYPE_NAMES.get(code, f"运动类型{code}")


def workout_detail(name: Any, duration_min: Any, distance_m: Any) -> str:
    """训练会话的一句话展示，如「跑步 45 分钟、5.23 公里」（缺项自动省略）。"""
    label = str(name or "运动")
    parts: list[str] = []
    minutes = to_int(duration_min)
    if minutes:
        parts.append(f"{minutes} 分钟")
    distance = to_int(distance_m)
    if distance:
        parts.append(f"{distance / 1000:.2f} 公里")
    return label if not parts else f"{label} " + "、".join(parts)


# ── 时间小工具（纯函数）──────────────────────────────────────────────────
def parse_clock(value: Any, fallback: time | None) -> time | None:
    """解析 ``HH:MM`` 文本为 ``time``；非法返回 ``fallback``。"""
    try:
        hour, minute = (int(part) for part in str(value).strip().split(":", 1))
        return time(hour, minute)
    except (AttributeError, TypeError, ValueError):
        return fallback


def in_window(current: time, start: time, end: time) -> bool:
    """``current`` 是否落在 [start, end) 窗口内；支持跨零点；start==end 视为空窗口。"""
    if start == end:
        return False
    if start < end:
        return start <= current < end
    return current >= start or current < end


def night_key(current: datetime, start: time, end: time) -> str:
    """把一个跨夜窗口映射到同一个「夜」，供「每夜一次」去重使用。"""
    if start > end and current.time() >= start:
        return (current.date() + timedelta(days=1)).isoformat()
    return current.date().isoformat()


def parse_local_stamp(value: Any) -> datetime | None:
    """解析全库统一口径的本地时刻文本；非法返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, schema.LOCAL_TIME_FORMAT)
    except ValueError:
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            return None


@dataclass(frozen=True)
class CareFinding:
    """一个「规则已判定值得关心」的候选（不含任何日志副作用）。

    ``fact``   —— 给措辞模型 / 模板用的一条事实行（可能含健康数值，只进 prompt 与消息，
                   绝不写日志）；
    ``data``   —— 程序侧模板渲染所需的原始字段（时段开朗词、运动时长等），同样不写日志。
    """

    scenario: str
    event_key: str
    fact: str
    data: dict = field(default_factory=dict)


class CareMonitor:
    """夜间关怀的规则判定与状态记账（存储层注入，时间源可注入）。

    参数：
        store    —— HealthStore（提供 care_* 读写）；
        owner_id —— 所有者私聊会话标识（UMO）；空串表示「取不到目标」，一律不发；
        settings —— CareSettings（总开关已折算进各场景开关）；
        now      —— 时间源，返回本地 ``datetime``；默认 ``datetime.now``，可注入假时钟。
    """

    def __init__(
        self,
        store: Any,
        owner_id: Any,
        settings: CareSettings,
        *,
        activity_window_minutes: int = DEFAULT_ACTIVITY_WINDOW_MINUTES,
        cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
        daily_limit: int = DEFAULT_DAILY_LIMIT,
        now: Any = None,
        logger: Any = None,
    ) -> None:
        self.store = store
        self.owner_id = str(owner_id or "")
        self.settings = settings
        self.activity_window_minutes = max(5, min(int(activity_window_minutes), 180))
        self.cooldown_minutes = max(1, int(cooldown_minutes))
        self.daily_limit = max(1, int(daily_limit))
        self.now = now or datetime.now
        self.logger = logger or _LOGGER
        # 本轮（一次 5 分钟巡检）已占坑的发送数，按场景计；供「每轮每场景最多 1 条」与
        # 「同轮合计不超每日上限余额」判定。begin_round() 每轮清零。
        self._round_sent: dict[str, int] = {}
        self._round_start_count = 0

    # ── 时间 ─────────────────────────────────────────────────────────────
    def _now(self) -> datetime:
        try:
            current = self.now()
        except Exception:  # 时间源异常也不能让关怀循环崩掉
            current = datetime.now()
        return current if isinstance(current, datetime) else datetime.now()

    def current_time(self) -> datetime:
        """当前本地时间（公开的时间源入口，供上层组装程序侧文案）。"""
        return self._now()

    def _bounds(self) -> tuple[time, time]:
        start = parse_clock(self.settings.night_start, time(0, 30))
        end = parse_clock(self.settings.night_end, time(6, 0))
        return start, end

    def in_night_window(self, now: datetime | None = None) -> bool:
        """当前时刻是否在深夜窗口内。"""
        current = now or self._now()
        start, end = self._bounds()
        return in_window(current.time(), start, end)

    def night_key(self, now: datetime | None = None) -> str:
        """当前所属「夜」的标识（跨零点窗口归到同一夜）。"""
        current = now or self._now()
        start, end = self._bounds()
        return night_key(current, start, end)

    # ── 判定 ─────────────────────────────────────────────────────────────
    def cooling_down(self, scenario: str = NIGHT_SCENARIO,
                     now: datetime | None = None) -> bool:
        """该场景是否还在冷却中（距上次发送不足 ``cooldown_minutes``）。"""
        if self.store is None or not self.owner_id:
            return False
        last = self.store.last_care_send_at(self.owner_id, scenario)
        parsed = parse_local_stamp(last)
        if parsed is None:
            return False
        current = now or self._now()
        elapsed = current - parsed
        return timedelta(0) <= elapsed < timedelta(minutes=self.cooldown_minutes)

    # ── 一轮巡检的记账 ───────────────────────────────────────────────────
    def begin_round(self, now: datetime | None = None) -> None:
        """开始一轮关怀巡检：清零本轮各场景发送计数，并记下本轮开始时的当日条数。

        一轮 = 四个场景共用的一次 5 分钟检查。清零后 ``reserve()`` 逐条计数，使
        「每轮每场景最多 1 条」与「同轮合计不超每日上限余额」有共同账本。
        """
        self._round_sent = {}
        self._round_start_count = self.sends_today(now)

    def sent_in_round(self, scenario: str | None = None) -> int:
        """本轮已占坑的发送数（给了 scenario 就按场景计）。"""
        if scenario is None:
            return sum(self._round_sent.values())
        return int(self._round_sent.get(str(scenario), 0))

    def sends_today(self, now: datetime | None = None) -> int:
        """今天（本地自然日）已「占坑/已发」的主动关怀条数（所有场景合计）。

        本轮 ``reserve()`` 落库的行 sent_at 就是本轮时刻，天然计入；这里再叠加本轮内存
        计数兜住「占坑写库失败」的边界：取两者较大值，既不漏计也不重复计。
        """
        if self.store is None or not self.owner_id:
            return 0
        current = now or self._now()
        midnight = current.replace(hour=0, minute=0, second=0, microsecond=0)
        since = midnight.strftime(schema.LOCAL_TIME_FORMAT)
        counted = self.store.care_send_count_since(self.owner_id, since)
        return max(counted, self._round_start_count + self.sent_in_round())

    def daily_limit_reached(self, now: datetime | None = None) -> bool:
        """今天（本地自然日）主动关怀条数是否已达上限（所有场景合计）。

        计数含本轮已 reserve / 已发的行 —— 同一轮内前几个场景占掉的坑会立刻反映到
        判定里，从而拦住「同轮连发」。
        """
        if self.store is None or not self.owner_id:
            return False
        return self.sends_today(now) >= self.daily_limit

    # ── 冷启动保护（某场景首次启用那一轮：只登记、不发送）────────────────
    def scenario_initialized(self, scenario: str) -> bool:
        """该场景是否已做过「首轮基线」（有冷却状态行即视为已初始化）。"""
        if self.store is None or not self.owner_id:
            return False
        return self.store.care_scenario_state(self.owner_id, scenario) is not None

    def mark_scenario_initialized(self, scenario: str,
                                  now: datetime | None = None) -> None:
        """标记该场景已做过首轮基线（只写一行冷却状态，不记发送、不占冷却）。"""
        if self.store is None or not self.owner_id:
            return
        stamp = (now or self._now()).strftime(schema.LOCAL_TIME_FORMAT)
        self.store.set_care_scenario_state(self.owner_id, scenario, when=stamp)

    def register_event(self, scenario: str, event_key: Any,
                       now: datetime | None = None) -> bool:
        """把一条记录登记为「已处理」（不发送）；返回本次是否为新登记。"""
        if self.store is None or not self.owner_id or not str(event_key or "").strip():
            return False
        stamp = (now or self._now()).strftime(schema.LOCAL_TIME_FORMAT)
        return self.store.mark_care_event(
            self.owner_id, scenario, str(event_key), when=stamp)

    def _first_round(self, scenario: str, keys: Any,
                     now: datetime | None = None) -> bool:
        """冷启动保护：该场景首次启用那一轮 → 只登记窗口内已有记录、不发送。

        返回 True 表示「本轮属首轮基线，调用方应放弃发送」。首轮基线过后，场景里的
        候选只会是此后新出现的记录（已有记录都已登记为已处理）。
        """
        if self.scenario_initialized(scenario):
            return False
        for key in keys or ():
            self.register_event(scenario, key, now)
        self.mark_scenario_initialized(scenario, now)
        return True

    def night_candidate(self, now: datetime | None = None) -> CareFinding | None:
        """夜间候选：窗口 + 每夜去重 + 近期私聊活动 + 场景冷却，全过才给 finding。

        窗口外、该夜已发过、所有者近期无活动、仍在冷却、没有目标、没有存储，
        任一条件不满足都返回 None（不发）。冷启动保护：该场景首次启用那一轮只登记
        当夜这一事件键、不发送（窗口外则无可登记项，仍是只初始化、不发送）。
        """
        if not self.settings.night_enabled or not self.owner_id or self.store is None:
            return None
        current = now or self._now()
        key = self.night_key(current)
        in_window = self.in_night_window(current)
        if self._first_round(
                NIGHT_SCENARIO, [key] if in_window else [], current):
            return None
        if not in_window:
            return None
        if self.store.care_event_seen(self.owner_id, NIGHT_SCENARIO, key):
            return None
        state = self.store.last_owner_activity(self.owner_id)
        if not state:
            return None
        last_seen = parse_local_stamp(state.get("last_seen_at"))
        if last_seen is None:
            return None
        age = current - last_seen
        if not (timedelta(0) <= age <= timedelta(minutes=self.activity_window_minutes)):
            return None
        if self.cooling_down(NIGHT_SCENARIO, current):
            return None
        fact = (
            f"当前本地时间 {current.strftime('%H:%M')}，"
            f"并且所有者在最近 {self.activity_window_minutes} 分钟内有私聊活动"
        )
        return CareFinding(NIGHT_SCENARIO, key, fact)

    # ── 压力关怀 ─────────────────────────────────────────────────────────
    def stress_candidate(self, now: datetime | None = None) -> CareFinding | None:
        """压力候选：当日日均分达配置档位，且今天还没发过（每天最多一条）。

        没有当日压力行、均值缺失 / 未测到、未达配置档位、今天已发过，任一不满足返回 None。
        冷启动保护：该场景首次启用那一轮，若当天已有压力行就登记当天、不发送（避免启用即发）。
        """
        if not self.settings.stress_enabled or not self.owner_id or self.store is None:
            return None
        current = now or self._now()
        day = current.date().isoformat()
        row = self.store.get("stress_sample", day)
        if self._first_round(STRESS_SCENARIO, [day] if row else [], current):
            return None
        if not row:
            return None
        grade = stress_grade(row.get("average"))
        if grade is None or not stress_grade_meets(
                grade, self.settings.stress_threshold):
            return None
        if self.store.care_event_seen(self.owner_id, STRESS_SCENARIO, day):
            return None
        average = to_float(row.get("average")) or 0.0
        label = STRESS_GRADE_LABELS.get(grade, grade)
        threshold_label = STRESS_GRADE_LABELS.get(
            _stress_threshold(self.settings.stress_threshold), "")
        fact = (
            f"今天（{day}）的当日压力日均分是 {average:.0f}，属于「{label}」档，"
            f"已达到配置的「{threshold_label}」及以上触发档位"
        )
        return CareFinding(
            STRESS_SCENARIO, day, fact,
            {"average": average, "grade": grade, "label": label, "date": day})

    # ── 起床关怀 ─────────────────────────────────────────────────────────
    def wakeup_candidates(self, now: datetime | None = None) -> list[CareFinding]:
        """起床候选：起床时间匹配当前日期 + 距现在不超宽容度 + 该记录未触发过。

        「小睡不限时段」：不区分主睡眠与白天小睡，只看记录自带的起床时间落在今天。
        冷启动保护：该场景首次启用那一轮，只登记已有候选记录、不发送。
        """
        if not self.settings.wakeup_enabled or not self.owner_id or self.store is None:
            return []
        current = now or self._now()
        day = current.date().isoformat()
        since = (current.date() - timedelta(days=WAKEUP_LOOKBACK_DAYS)).isoformat()
        tolerance_seconds = self.settings.wakeup_tolerance_minutes * 60
        findings: list[CareFinding] = []
        for row in self.store.query("sleep_session", start=since, end=day):
            wakeup = parse_local_stamp(row.get("wakeup_local"))
            if wakeup is None or wakeup.date().isoformat() != day:
                continue
            if abs((current - wakeup).total_seconds()) > tolerance_seconds:
                continue
            key = f"{row.get('date')}|{row.get('wakeup_local')}"
            if self.store.care_event_seen(self.owner_id, WAKEUP_SCENARIO, key):
                continue
            findings.append(CareFinding(
                WAKEUP_SCENARIO, key,
                f"睡眠记录显示主人今天 {wakeup.strftime('%H:%M')} 起床，"
                f"当前本地时间 {current.strftime('%H:%M')}",
                {"date": row.get("date"),
                 "wakeup_clock": wakeup.strftime("%H:%M"),
                 "clock": current.strftime("%H:%M"),
                 "greeting": greeting_for(current.hour)}))
        if self._first_round(
                WAKEUP_SCENARIO, [item.event_key for item in findings], current):
            return []
        return findings

    # ── 运动后关怀 ───────────────────────────────────────────────────────
    def workout_candidates(self, now: datetime | None = None) -> list[CareFinding]:
        """运动后候选：本轮新出现的训练会话逐条给候选（每条记录一条）。

        判据（三道，全部命中才给候选）：
          1. 碎片过滤：只认「有效训练」（``storage/models.is_valid_training``，阈值取自
             ``settings.training_min_duration_min`` / ``training_min_distance_m``）。
             手环自动产生的分钟级碎片不触发关怀（碎片仍在库、只打标记）；
          2. 去重：按 session_key 处理过的会话不再给候选；
          3. 冷启动保护：该场景首次启用那一轮只登记窗口内已有记录、不发送。

        用「记录自带的运动结束时间」与「本轮发现它的时间」的差值分档：差值不超宽容度
        → 及时（关怀建议 + 信息展示）；更远 → 滞后（只做信息展示 + 致歉，只服务
        「真新增、只是发现晚了」）。
        """
        if not self.settings.workout_enabled or not self.owner_id or self.store is None:
            return []
        current = now or self._now()
        day = current.date().isoformat()
        since = (current.date() - timedelta(days=WORKOUT_LOOKBACK_DAYS)).isoformat()
        findings: list[CareFinding] = []
        for row in self.store.query("training_session", start=since, end=day):
            key = str(row.get("session_key") or "").strip()
            if not key:
                continue
            ended = parse_local_stamp(row.get("end_local"))
            if ended is None:
                continue
            if not is_valid_training(
                    row.get("duration_min"), row.get("distance_m"),
                    min_duration_min=self.settings.training_min_duration_min,
                    min_distance_m=self.settings.training_min_distance_m):
                continue
            if self.store.care_event_seen(self.owner_id, WORKOUT_SCENARIO, key):
                continue
            lag_minutes = abs((current - ended).total_seconds()) / 60.0
            timely = lag_minutes <= self.settings.workout_tolerance_minutes
            name = sport_label(row.get("sport_type"))
            detail = workout_detail(
                name, row.get("duration_min"), row.get("distance_m"))
            fact = (
                f"{name}训练会话结束于 {ended.strftime('%H:%M')}（{detail}），"
                f"本轮发现它的时刻距结束 {lag_minutes:.0f} 分钟，"
                + ("属于刚结束、可做及时关怀" if timely else "属滞后发现，只能做信息展示并致歉")
            )
            findings.append(CareFinding(
                WORKOUT_SCENARIO, key, fact,
                {"session_key": key, "name": name,
                 "duration_min": to_int(row.get("duration_min")),
                 "distance_m": to_int(row.get("distance_m")),
                 "kcal": to_float(row.get("kcal")),
                 "detail": detail, "end_clock": ended.strftime("%H:%M"),
                 "lag_minutes": int(round(lag_minutes)), "timely": timely}))
        if self._first_round(
                WORKOUT_SCENARIO, [item.event_key for item in findings], current):
            return []
        return findings

    # ── 记账 ─────────────────────────────────────────────────────────────
    def reserve(self, finding: CareFinding, now: datetime | None = None) -> bool:
        """发送前「占坑」：记事件去重键 + 一条 reserved 发送记录 + 场景冷却状态。

        返回 True 表示本次成功占用（可以发送）；返回 False 一律表示调用方不得再发，
        三种闸门任一不过即 False：
          * 「每轮每场景最多 1 条」——该场景本轮已占用过（``_round_sent`` 计数在成功
            占用时累加，``begin_round()`` 每轮清零）；
          * 「每日额度余额」——本轮已 reserve 的条数计入当日条数后再与上限比较，超额即拦；
          * 该事件已被处理过（并发 / 重复调用）。

        发送结果无论如何都先占冷却，宁少发一条、绝不重复发。
        """
        if self.store is None or not self.owner_id:
            return False
        if self.sent_in_round(finding.scenario) >= 1:
            return False
        if self.daily_limit_reached(now):
            return False
        stamp = (now or self._now()).strftime(schema.LOCAL_TIME_FORMAT)
        if not self.store.mark_care_event(
                self.owner_id, finding.scenario, finding.event_key, when=stamp):
            return False
        self.store.record_care_send(
            self.owner_id, finding.scenario, finding.event_key,
            delivery="reserved", when=stamp)
        self.store.set_care_scenario_state(
            self.owner_id, finding.scenario,
            last_sent_at=stamp, last_event_key=finding.event_key, when=stamp)
        self._round_sent[finding.scenario] = self.sent_in_round(finding.scenario) + 1
        return True

    def confirm(self, finding: CareFinding, now: datetime | None = None) -> None:
        """发送确已送达后，把对应记录升级为 sent（失败只记、不上抛）。"""
        if self.store is None or not self.owner_id:
            return
        stamp = (now or self._now()).strftime(schema.LOCAL_TIME_FORMAT)
        try:
            self.store.confirm_care_send(
                self.owner_id, finding.scenario, finding.event_key, when=stamp)
        except Exception as error:  # noqa: BLE001 - 记账失败不该影响关怀循环
            self._log("warning", "[华为运动健康] 关怀送达确认失败（%s）",
                      type(error).__name__)

    # ── 日志 ─────────────────────────────────────────────────────────────
    def _log(self, level: str, message: str, *args: Any) -> None:
        method = getattr(self.logger, level, None)
        if not callable(method):
            return
        try:
            method(message, *args)
        except Exception:
            pass


__all__ = [
    "NIGHT_SCENARIO",
    "STRESS_SCENARIO",
    "WAKEUP_SCENARIO",
    "WORKOUT_SCENARIO",
    "DEFAULT_NIGHT_START",
    "DEFAULT_NIGHT_END",
    "DEFAULT_ACTIVITY_WINDOW_MINUTES",
    "DEFAULT_COOLDOWN_MINUTES",
    "DEFAULT_DAILY_LIMIT",
    "CARE_CHECK_INTERVAL_MINUTES",
    "CARE_CONF_GROUP",
    "STRESS_GRADES",
    "STRESS_GRADE_ORDER",
    "STRESS_GRADE_LABELS",
    "STRESS_GRADE_BOUNDS",
    "DEFAULT_STRESS_THRESHOLD",
    "DEFAULT_WAKEUP_TOLERANCE_MINUTES",
    "DEFAULT_WORKOUT_TOLERANCE_MINUTES",
    "WORKOUT_LOOKBACK_DAYS",
    "WAKEUP_LOOKBACK_DAYS",
    "GREETING_STEPS",
    "DEFAULT_GREETING",
    "CareSettings",
    "CareFinding",
    "CareMonitor",
    "care_settings_from_config",
    "training_thresholds_from_config",
    "stress_grade",
    "stress_grade_rank",
    "stress_grade_meets",
    "greeting_for",
    "sport_label",
    "workout_detail",
    "parse_clock",
    "in_window",
    "night_key",
    "parse_local_stamp",
]
