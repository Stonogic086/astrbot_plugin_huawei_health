"""华为运动健康插件 —— 存储层数据模型与字段映射（v1，纯标准库）。

本模块把「上游云端原始记录 / 协议层整形行」归一化成可直接入库的字典，
只做纯函数转换：不碰数据库、不 import 任何第三方库、不 import homeassistant、
也不 import astrbot。语义按开发计划定稿，不得自行改动。

════════════════════════════════════════════════════════════════════════
字段映射（定稿）
════════════════════════════════════════════════════════════════════════

DailyActivity ← getSportsStat（按本地日期唯一）
    上游 sportBasicInfo.steps                     → steps          INTEGER
    上游 sportBasicInfo.distance                  → distance_m     INTEGER（上游单位=米）
    上游 sportBasicInfo.calorie                   → kcal           REAL（上游=千分之一 kcal，/1000）
    上游 sportBasicInfo.duration                  → duration_min   INTEGER（上游单位=分钟）
    上游 dimenDailyActivity.walkDurations         → walk_min       INTEGER
    上游 activeHourBasic.countActiveHour          → active_hours   INTEGER
    上游 exerciseTimeBasic.totalMidHighIntensity  → exercise_min   INTEGER
    上游 goalAchieveBasic.stepGoalValueStat       → step_goal      INTEGER

HeartRateSample ← getHealthStat type 7 heartRateBasic（日粒度，汇总型样本）
    lastRestHeartRate   → resting_hr
    lastHeartRate       → day_hr
    avgRestingHeartRate → average_resting_hr
    maxHeartRate        → max_hr
    minHeartRate        → min_hr
    sample_kind 固定 'daily_summary'（= 汇总型样本）

SleepSession ← getHealthStat type 9 professionalSleep（v1 只入汇总值，分期明细留 v2）
    allSleepTime    → duration_min
    sleepScore      → score
    sleepEfficiency → efficiency
    lastAvgHrv      → hrv
    lastAvgSpO2     → spo2
    fallAsleepTime  → fall_asleep_local（协议层已格式化为 'YYYY-MM-DD HH:MM'，本层只补秒）
    wakeupTime      → wakeup_local（同上）
    daySleepTime    → nap_duration（协议层）→ nap_duration_min（白天小睡，分钟）

StressSample ← getHealthStat type 11 stressBasic
    meanScore    → average
    lastScore    → last_value
    maxScore     → max_value
    minScore     → min_value
    measureCount → measurements

SpO2Sample ← 睡眠响应 professionalSleep.lastAvgSpO2
    lastAvgSpO2 → spo2（sample_kind 固定 'sleep_last_avg'）

TrainingSession ← getSportsDataByTime
    先按 dataId 去重，再按 sportType 分组，分钟段间隔 ≤15 分钟合并为一次会话；
    session_key = f"{sport_type}:{start_ms}"；
    字段：sport_type / duration_min / distance_m / kcal / segments / device_code
    （另存 start_ms/end_ms/start_local/end_local/start_date 便于展示与区间查询）；
    is_fragment = 「有效训练判据」的标记列（1=碎片、0=有效），判据见 is_valid_training。

「有效训练」判据（统一口径，命令展示 / LLM 摘要 / 运动后关怀三处共用）
    is_valid_training(duration_min, distance_m)：时长 ≥ min_duration_min 分钟**或**
    距离 ≥ min_distance_m 米，两个阈值都可配置（默认 3 分钟 / 300 米）。
    手环自动产生的分钟级碎片（1 分钟、几十米）两个阈值都不满足 → 判为碎片；
    碎片仍然入库，只是被标记、并从展示 / 摘要 / 关怀里过滤掉。
    实测 getSportsDataByTime 应答里没有「手动 / 自动识别」这类标记字段（只有
    appType / sportDataSource / mergedFlag / mergedFields，语义未在 APK 侧核实），
    故判据只能靠阈值，不拿未核实的字段当闸门。

BodyMeasurement 已下线：不建表、不提供接口。

════════════════════════════════════════════════════════════════════════
输入形状（与生产一致）
════════════════════════════════════════════════════════════════════════
  * health：只接受协议层 ``adapters.huawei_health_cloud.health_series`` 的整形行
    （扁平键，如 resting_heart_rate / sleep_score / stress_average …）——这正是同步服务
    实际喂进来的那一套；原先并存的「原始嵌套响应 dict」分支已删除，避免两套映射里有一套
    永远不被真实路径覆盖（离线自检也只喂真实形状）。
  * daily_activity：兼容上游原始嵌套记录（sportBasicInfo / dimenDailyActivity …）与整形行，
    靠键名区分，不会对已换算过的 kcal 二次除以 1000（换算发生在协议层）。
    「有没有读数」由 ``has_daily_reading`` 统一判定（与 health 家族同一套护栏）。
  * training：只接受 getSportsDataByTime 的原始分钟段（协议层不整形，去重/合并在本层）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

# 上游 health 家族 → (记录内的汇总块名, 目标表)
HEALTH_TYPE_HEART_RATE = 7
HEALTH_TYPE_SLEEP = 9
HEALTH_TYPE_STRESS = 11

# 训练会话合并的最大分钟间隔（定稿：≤15 分钟算同一会话）
TRAINING_GAP_MINUTES = 15

# 「有效训练」判据的默认阈值（配置项 default 与此一致）：
# 时长 ≥ 3 分钟，或距离 ≥ 300 米。手环自动产生的分钟级碎片两个都不满足。
DEFAULT_TRAINING_MIN_DURATION_MIN = 3
DEFAULT_TRAINING_MIN_DISTANCE_M = 300

# 运动类型码 → 中文展示名（唯一来源：命令层渲染与关怀层文案共用一张表，避免两处手抄分叉）。
# 取自上游 adapters/const.py 的 SPORT_TYPES（1 楼梯 / 2 爬坡 / 3 骑行 / 4 跑步 / 5 步行 /
# 9 游泳 / 10 其他 / 14 未知）；6/7/8 是睡眠分期、不是运动，故不收录（它们是分钟段的内部
# 编号）。未收录的码由消费方回退「运动类型N」，不误标。
SPORT_TYPE_NAMES: dict[int, str] = {
    1: "楼梯",
    2: "爬坡",
    3: "骑行",
    4: "跑步",
    5: "步行",
    9: "游泳",
    10: "其他",
    14: "未知",
}


def _threshold(value: Any, default: float) -> float:
    """阈值归一：非法 / 缺失一律回退默认值。"""
    number = to_float(value)
    return default if number is None else float(number)


def is_valid_training(
    duration_min: Any,
    distance_m: Any,
    *,
    min_duration_min: Any = DEFAULT_TRAINING_MIN_DURATION_MIN,
    min_distance_m: Any = DEFAULT_TRAINING_MIN_DISTANCE_M,
) -> bool:
    """一次训练会话是否算「有效训练」：时长 ≥ 阈值，或距离 ≥ 阈值。

    「或」的关系：满足任一即算有效。缺失 / 0 一律按没测到处理（按 0 参与比较）。
    判据只有这一处实现——命令展示、LLM 摘要与运动后关怀都调它，阈值来自配置。
    """
    minutes = to_float(duration_min) or 0.0
    meters = to_float(distance_m) or 0.0
    return (minutes >= _threshold(min_duration_min, DEFAULT_TRAINING_MIN_DURATION_MIN)
            or meters >= _threshold(min_distance_m, DEFAULT_TRAINING_MIN_DISTANCE_M))


def mark_training_fragments(
    rows: Iterable[dict],
    *,
    min_duration_min: Any = DEFAULT_TRAINING_MIN_DURATION_MIN,
    min_distance_m: Any = DEFAULT_TRAINING_MIN_DISTANCE_M,
) -> list[dict]:
    """给训练会话行打 is_fragment 标记（1=碎片 / 0=有效），返回新行列表。

    只加标记、不改其它字段，也绝不丢行：碎片同样入库（原始数据保留），由查询 / 关怀
    层按同一判据过滤。入参行不被改动（浅拷贝）。
    """
    marked: list[dict] = []
    for row in rows or ():
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["is_fragment"] = 0 if is_valid_training(
            item.get("duration_min"), item.get("distance_m"),
            min_duration_min=min_duration_min, min_distance_m=min_distance_m) else 1
        marked.append(item)
    return marked


# ── 基础取值 ─────────────────────────────────────────────────────────────
def to_float(value: Any) -> float | None:
    """宽松转 float；空串 / None / 非法值返回 None。"""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    """宽松转 int（先转 float 再截断）；非法值返回 None。"""
    number = to_float(value)
    return None if number is None else int(number)


def _pick(*values: Any) -> Any:
    """返回第一个非 None 的值（用于「原始键 / 整形键」二选一）。"""
    for value in values:
        if value is not None:
            return value
    return None


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def normalize_day(raw: Any) -> str | None:
    """把各种日期表示归一成本地日期字符串 'YYYY-MM-DD'；无法识别返回 None。

    接受：date/datetime 对象、int(20261007 / epoch 秒 / epoch 毫秒)、
    'YYYY-MM-DD…'、'YYYYMMDD'、epoch 数字字符串。带「-」的形状走 strptime 校验
    （'2026-1-2' 归一成 '2026-01-02'；'abc-def' 这类解析不出来的返回 None）。
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        return raw.date().isoformat()
    if isinstance(raw, date):
        return raw.isoformat()
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        number = int(raw)
        if 19000101 <= number <= 29991231:  # 20261007 这种紧凑写法
            text = f"{number:08d}"
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
        if number >= 1_000_000_000_000:  # epoch 毫秒
            number //= 1000
        if number >= 1_000_000_000:  # epoch 秒
            return datetime.fromtimestamp(number).date().isoformat()
        return None
    text = str(raw).strip()
    if not text:
        return None
    if "-" in text:
        # 带「-」的形状必须真能解析成日期：不能原样截断，否则 '2026-1-2' / 'abc-def'
        # 会被当合法日期写进库（字符串比较下还会污染区间查询）。
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date().isoformat()
        except ValueError:
            return None
    if text.isdigit():
        if len(text) == 8:
            return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
        if len(text) >= 13:
            return datetime.fromtimestamp(int(text) // 1000).date().isoformat()
        if len(text) >= 10:
            return datetime.fromtimestamp(int(text)).date().isoformat()
    return None


def _local_str(ms: Any) -> str | None:
    """epoch 毫秒 → 本地时间字符串 'YYYY-MM-DD HH:MM:SS'。"""
    number = to_float(ms)
    if not number:
        return None
    return datetime.fromtimestamp(number / 1000).strftime("%Y-%m-%d %H:%M:%S")


def _local_second(text: Any) -> str | None:
    """协议层本地时间文本 'YYYY-MM-DD HH:MM' → 'YYYY-MM-DD HH:MM:SS'（只补秒）。

    与 training_session.start_local 同格式。只做格式补秒：不推算、不从时长反推时间；
    形状不认识（既不是 16 位也不是 19 位）就返回 None，由调用方留 NULL。
    """
    if not text:
        return None
    value = str(text).strip()
    if len(value) == 16:
        return value + ":00"
    if len(value) == 19:
        return value
    return None


# ── DailyActivity（getSportsStat）────────────────────────────────────────
def normalize_daily_activity(records: Iterable[dict]) -> list[dict]:
    """把 getSportsStat 记录（或协议层 daily_activity 行）归一成 daily_activity 行。"""
    out: list[dict] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        day = normalize_day(
            _pick(record.get("recordDay"), record.get("date"), record.get("startDate"))
        )
        if not day:
            continue
        basic = _as_dict(record.get("sportBasicInfo"))
        walk = _as_dict(record.get("dimenDailyActivity"))
        goal = _as_dict(record.get("goalAchieveBasic"))
        exercise = _as_dict(record.get("exerciseTimeBasic"))
        active = _as_dict(record.get("activeHourBasic"))

        kcal = to_float(record.get("kcal"))  # 协议层整形行：已是 kcal
        if kcal is None:
            calorie_milli = _pick(basic.get("calorie"), record.get("calorie"))
            value = to_float(calorie_milli)
            kcal = None if value is None else round(value / 1000, 1)  # 千分之一 kcal → kcal

        out.append({
            "date": day,
            "sport_type": to_int(_pick(record.get("sportType"), record.get("sport_type"))),
            "steps": to_int(_pick(basic.get("steps"), record.get("steps"))),
            "distance_m": to_int(_pick(
                basic.get("distance"), record.get("distance_m"), record.get("distance"))),
            "kcal": kcal,
            "duration_min": to_int(_pick(
                basic.get("duration"), record.get("duration_min"))),
            "walk_min": to_int(_pick(
                walk.get("walkDurations"), record.get("walk_min"))),
            "active_hours": to_int(_pick(
                active.get("countActiveHour"), record.get("active_hours"))),
            "exercise_min": to_int(_pick(
                exercise.get("totalMidHighIntensity"), record.get("exercise_min"))),
            "step_goal": to_int(_pick(
                goal.get("stepGoalValueStat"), record.get("step_goal"))),
        })
    return out


# daily_activity 里属于「读数」的字段；step_goal 是目标值（不是读数），故不计入。
DAILY_READING_FIELDS: tuple[str, ...] = (
    "steps", "distance_m", "kcal", "duration_min", "walk_min", "active_hours",
    "exercise_min",
)


def has_daily_reading(row: dict) -> bool:
    """判断一行 daily_activity 是否至少有一个有效读数（0 / 负数 / 缺失都不算）。

    与 health 家族同一套护栏（见 normalize_health：只为「至少有一个有效读数」的家族写行）。
    某天若一个读数都没有，就不该在 daily_activity 里留下全空的壳行——否则查询层永远命中
    有行、渲染出「步数 无 / 距离 无 …」，「没有活动记录」分支不可达。
    """
    return any(_positive(row.get(name)) is not None for name in DAILY_READING_FIELDS)


# ── Health（getHealthStat：心率 / 睡眠 / 压力 / 血氧）──────────────────────
def _positive(value: Any) -> float | None:
    """0 / 负数是云端「手环没测到」，不算读数 → 丢弃。"""
    number = to_float(value)
    if number is None or number <= 0:
        return None
    return number


def normalize_health(raw: Any) -> dict[str, list[dict]]:
    """归一 health 数据，返回 {'heart_rate','sleep','stress','spo2'} 四类行。

    入参固定为协议层 ``health_series`` 的整形行 list（每行至少含 date，扁平键名见
    adapters.huawei_health_cloud.HEALTH_FAMILIES 里的目标名）；生产路径就是这一条，
    不再保留「原始嵌套响应 dict」的第二套映射。

    只为「至少有一个有效读数」的家族写行：某天只有心率、没有睡眠时，不再补一条全空的
    sleep 行——否则查询层永远命中有行、渲染出没有数值的空壳，「无数据」分支不可达。
    """
    result: dict[str, list[dict]] = {"heart_rate": [], "sleep": [], "stress": [], "spo2": []}
    for record in raw or []:
        if not isinstance(record, dict):
            continue
        day = normalize_day(record.get("date"))
        if not day:
            continue

        hr: dict[str, Any] = {
            "date": day,
            "sample_kind": "daily_summary",
            "source_type": HEALTH_TYPE_HEART_RATE,
        }
        hr_readings = 0
        for src, dst in (
            ("resting_heart_rate", "resting_hr"),
            ("heart_rate", "day_hr"),
            ("average_resting_heart_rate", "average_resting_hr"),
            ("max_heart_rate", "max_hr"),
            ("min_heart_rate", "min_hr"),
        ):
            got = _positive(record.get(src))
            if got is not None:
                hr[dst] = got
                hr_readings += 1
        if hr_readings:
            result["heart_rate"].append(hr)

        sleep: dict[str, Any] = {"date": day, "source_type": HEALTH_TYPE_SLEEP}
        sleep_readings = 0
        for src, dst in (
            ("sleep_duration", "duration_min"),
            ("sleep_score", "score"),
            ("sleep_efficiency", "efficiency"),
            ("sleep_hrv", "hrv"),
            ("sleep_spo2", "spo2"),
            ("nap_duration", "nap_duration_min"),
        ):
            got = _positive(record.get(src))
            if got is not None:
                sleep[dst] = got
                sleep_readings += 1
        # 入睡 / 起床只有时间、不是「读数」：单独出现不足以判定当晚有睡眠记录，故不计入
        # sleep_readings，只在有值时随行写下去。
        for src, dst in (("fall_asleep", "fall_asleep_local"),
                         ("wakeup", "wakeup_local")):
            got = _local_second(record.get(src))
            if got is not None:
                sleep[dst] = got
        if sleep_readings:
            result["sleep"].append(sleep)

        spo2 = _positive(record.get("sleep_spo2"))
        if spo2 is not None:
            result["spo2"].append(
                {"date": day, "spo2": spo2, "sample_kind": "sleep_last_avg"})

        stress: dict[str, Any] = {"date": day, "source_type": HEALTH_TYPE_STRESS}
        stress_readings = 0
        for src, dst in (
            ("stress_average", "average"),
            ("stress_last", "last_value"),
            ("stress_max", "max_value"),
            ("stress_min", "min_value"),
            ("stress_measurements", "measurements"),
        ):
            number = _positive(record.get(src))
            if number is not None:
                stress[dst] = to_int(number) if dst == "measurements" else number
                stress_readings += 1
        if stress_readings:
            result["stress"].append(stress)
    return result


# ── TrainingSession（getSportsDataByTime）────────────────────────────────
def merge_training_segments(
    records: Iterable[dict], gap_minutes: int = TRAINING_GAP_MINUTES
) -> list[dict]:
    """分钟段 → 训练会话：按 dataId 去重，按 sportType 分组，间隔 ≤gap 分钟合并。"""
    seen: set[tuple] = set()
    deduped: list[dict] = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        start = to_float(record.get("startTime"))
        end = to_float(record.get("endTime"))
        if not start or not end:
            continue
        data_id = record.get("dataId")
        if data_id is not None and data_id != "":
            dedupe_key = ("dataId", str(data_id))
        else:
            dedupe_key = (
                "span", to_int(record.get("sportType")), int(start), int(end))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        block = record.get("sportBasicInfos")
        if isinstance(block, list):
            basic = _as_dict(block[0]) if block else {}
        else:
            basic = _as_dict(block)

        deduped.append({
            "sport_type": to_int(record.get("sportType")),
            "start_ms": int(start),
            "end_ms": int(end),
            "duration_min": to_int(basic.get("duration")) or 0,
            "distance_m": to_int(basic.get("distance")) or 0,
            "calorie_milli": to_float(basic.get("calorie")) or 0.0,
            "device_code": record.get("deviceCode"),
        })

    by_type: dict[Any, list[dict]] = {}
    for seg in deduped:
        by_type.setdefault(seg["sport_type"], []).append(seg)

    gap_ms = gap_minutes * 60000
    merged: list[dict] = []
    for _, segments in by_type.items():
        segments.sort(key=lambda seg: seg["start_ms"])
        current: dict | None = None
        for seg in segments:
            if current is not None and seg["start_ms"] - current["end_ms"] <= gap_ms:
                current["end_ms"] = max(current["end_ms"], seg["end_ms"])
                current["duration_min"] += seg["duration_min"]
                current["distance_m"] += seg["distance_m"]
                current["calorie_milli"] += seg["calorie_milli"]
                current["segments"] += 1
                continue
            if current is not None:
                merged.append(current)
            current = dict(seg, segments=1)
        if current is not None:
            merged.append(current)

    out: list[dict] = []
    for session in merged:
        start_local = _local_str(session["start_ms"])
        out.append({
            "session_key": f"{session['sport_type']}:{session['start_ms']}",
            "sport_type": session["sport_type"],
            "start_ms": session["start_ms"],
            "end_ms": session["end_ms"],
            "start_local": start_local,
            "end_local": _local_str(session["end_ms"]),
            "start_date": start_local[:10] if start_local else None,
            "duration_min": session["duration_min"],
            "distance_m": session["distance_m"],
            "kcal": round(session["calorie_milli"] / 1000, 1),
            "segments": session["segments"],
            "device_code": session["device_code"],
        })
    out.sort(key=lambda item: (item["start_ms"], item["sport_type"] or 0))
    return out
