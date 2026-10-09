"""华为运动健康插件 —— 查询命令层的「纯展示」部分（不 import astrbot）。

本模块只做三件事，且全是纯函数 / 只读：
  * 解析命令参数（天数 / 日期）；
  * 从存储层 HealthStore 读行（只调 query / get，不自写 SQL、不动 storage/ 逻辑）；
  * 把行渲染成中文、简洁可读的文本。

设计约定：
  * 不 import astrbot、不调用任何 LLM；输出文本只发给使用者本人；
  * 不触发任何按需刷新（按需刷新属下一轮，本轮只读既有库）；
  * 无数据时返回带明确提示的文本（如「没有活动记录」）；
  * 日期用 ``storage.models.normalize_day`` 归一，避免重复实现。

注意：命令处理器与参数解析在 commands/handlers.py；本模块不含任何框架装饰器。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

try:  # 作为插件子包导入（正式运行）与脚本直跑两种方式都要能用
    from ..storage.models import (
        DEFAULT_TRAINING_MIN_DISTANCE_M,
        DEFAULT_TRAINING_MIN_DURATION_MIN,
        SPORT_TYPE_NAMES,
        is_valid_training,
        normalize_day,
    )
except ImportError:  # 脚本直跑：插件根已加入 sys.path，回退到顶层包导入
    from storage.models import (  # type: ignore[no-redef]
        DEFAULT_TRAINING_MIN_DISTANCE_M,
        DEFAULT_TRAINING_MIN_DURATION_MIN,
        SPORT_TYPE_NAMES,
        is_valid_training,
        normalize_day,
    )

__all__ = [
    # 渲染（供上层组装展示文本：命令层与 LLM 摘要共用同一口径）
    "render_activity",
    "render_sleep",
    "render_training",
    # 格式化小工具（供复用同一套「无值写成 无」的展示口径）
    "_fmt_dur",
    "_fmt_dist",
    "_fmt_kcal",
    "fmt_num",
    "_fmt_steps",
    "_fmt_minutes",
    "_fmt_hours",
    "_fmt_hr",
    "_fmt_clock",
]

# 命令名（与 handlers.py 共用，集中在此避免拼写漂移）。
COMMAND_ACTIVITY = "健康活动"
COMMAND_SLEEP = "健康睡眠"
COMMAND_TRAINING = "健康训练"

# 默认窗口。
DEFAULT_ACTIVITY_DAYS = 3
DEFAULT_TRAINING_DAYS = 7

# 最大窗口，防止把整库拖出来。
MAX_DAYS = 90

# 运动类型码 → 中文名由 storage/models.SPORT_TYPE_NAMES 单点维护（命令层与关怀层共用，
# 避免两处手抄分叉）；此处只从那里导入（见文件顶部 import），不再保留副本。


# ── 参数解析 ─────────────────────────────────────────────────────────────
def command_tail(event: Any, command: str) -> str:
    """取命令名之后的参数文本（兼容前导 '/'）。不依赖框架上下文。"""
    text = getattr(event, "message_str", "") or ""
    text = str(text).strip()
    if text.startswith("/"):
        text = text[1:].strip()
    if command and text.startswith(command):
        text = text[len(command):]
    return text.strip()


def parse_days(text: str | None, default: int) -> int:
    """把参数文本的首个 token 解析成天数；无法识别时用默认值。夹在 1~MAX_DAYS。"""
    raw = str(text or "").strip()
    if not raw:
        return default
    first = raw.split()[0]
    try:
        value = int(float(first))
    except (TypeError, ValueError):
        return default
    return max(1, min(value, MAX_DAYS))


def parse_day(text: str | None) -> str | None:
    """把参数文本的首个 token 解析成本地日期 'YYYY-MM-DD'；无法识别返回 None。"""
    raw = str(text or "").strip()
    if not raw:
        return None
    return normalize_day(raw.split()[0])


# ── 格式化小工具 ─────────────────────────────────────────────────────────
def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _positive(value: Any) -> float | None:
    """活动读数归一：0 / 负数 / 缺失一律当「无值」。

    云端对手环没测到的字段回 0，把它当读数显示会误导使用者（实测该账号
    ``duration_min`` 恒 0，而 ``walk_min`` / ``active_hours`` / ``exercise_min`` 才有值）。
    """
    number = _num(value)
    if number is None or number <= 0:
        return None
    return number


def _fmt_dur(minutes: Any) -> str:
    """分钟 → 「X 小时 Y 分」/「Z 分钟」。"""
    value = _num(minutes)
    if value is None:
        return "无"
    total = int(round(value))
    if total >= 60:
        hours, mins = divmod(total, 60)
        return f"{hours} 小时 {mins} 分" if mins else f"{hours} 小时"
    return f"{total} 分钟"


def _fmt_dist(meters: Any) -> str:
    """米 → 「X.XX 公里」或「Y 米」。"""
    value = _num(meters)
    if value is None:
        return "无"
    if value >= 1000:
        return f"{value / 1000:.2f} 公里"
    return f"{int(round(value))} 米"


def _fmt_kcal(value: Any) -> str:
    number = _num(value)
    return "无" if number is None else f"{number:.1f} 千卡"


def fmt_num(value: Any, suffix: str = "") -> str:
    """数值（缺失 → 「无」）：供命令层与 LLM 摘要共用同一套展示口径。"""
    number = _num(value)
    if number is None:
        return "无"
    return f"{number:g}{suffix}"


def _fmt_steps(value: Any) -> str:
    number = _num(value)
    return "无" if number is None else str(int(round(number)))


def _fmt_minutes(value: Any) -> str:
    """分钟数（步行 / 运动）：无值 → 「无」，否则「N 分钟」。"""
    number = _positive(value)
    return "无" if number is None else f"{int(round(number))} 分钟"


def _fmt_hours(value: Any) -> str:
    """活动小时数：无值 → 「无」，否则「N 小时」。"""
    number = _positive(value)
    return "无" if number is None else f"{number:g} 小时"


def _fmt_hr(value: Any) -> str:
    """心率读数：缺失 → 「无记录」（心率不会出现 0，故只判缺失）。"""
    number = _num(value)
    return "无记录" if number is None else f"{number:g}"


def _fmt_clock(value: Any) -> str | None:
    """本地时间文本 'YYYY-MM-DD HH:MM[:SS]' → 'HH:MM'；缺失 / 形状不符返回 None。"""
    text = "" if value is None else str(value).strip()
    if len(text) >= 16 and text[10] == " " and text[13] == ":":
        return text[11:16]
    return None


def _window(days: int, today: date | None = None) -> tuple[int, str, str]:
    """把窗口天数收口成 (days, start, end)。"""
    days = max(1, min(int(days), MAX_DAYS))
    today = today or date.today()
    start = (today - timedelta(days=days - 1)).isoformat()
    return days, start, today.isoformat()


# ── 渲染：最近 N 天活动汇总 ──────────────────────────────────────────────
def render_activity(store: Any, days: int, today: date | None = None) -> str:
    """渲染「最近 N 天活动汇总」：逐日两行 + 合计两行。

    展示口径：步数（含目标）/ 距离 / 消耗，以及步行分钟 / 活动小时 / 运动分钟。
    0 与缺失一律显示「无」，不拿 0 冒充有数据（实测 duration_min 恒 0，故不再展示它）。
    """
    days, start, end = _window(days, today)
    header = f"最近 {days} 天活动（{start} ~ {end}）"
    rows = store.query("daily_activity", start, end)
    if not rows:
        return header + "\n没有活动记录。"

    lines = [header]
    total = {
        "steps": 0.0, "distance_m": 0.0, "kcal": 0.0,
        "walk_min": 0.0, "active_hours": 0.0, "exercise_min": 0.0,
    }
    seen = {key: False for key in total}
    for row in rows:
        steps = _positive(row.get("steps"))
        dist = _positive(row.get("distance_m"))
        kcal = _positive(row.get("kcal"))
        walk = _positive(row.get("walk_min"))
        active = _positive(row.get("active_hours"))
        exercise = _positive(row.get("exercise_min"))
        goal = _positive(row.get("step_goal"))
        for key, value in (
            ("steps", steps), ("distance_m", dist), ("kcal", kcal),
            ("walk_min", walk), ("active_hours", active), ("exercise_min", exercise),
        ):
            if value is not None:
                total[key] += value
                seen[key] = True
        goal_suffix = f"（目标 {int(round(goal))}）" if goal is not None else ""
        day_label = str(row.get("date", ""))[5:]
        lines.append(
            f"- {day_label}：步数 {_fmt_steps(steps)}{goal_suffix}，"
            f"距离 {_fmt_dist(dist)}，消耗 {_fmt_kcal(kcal)}"
        )
        lines.append(
            f"  步行 {_fmt_minutes(walk)}，活动 {_fmt_hours(active)}，"
            f"运动 {_fmt_minutes(exercise)}"
        )
    lines.append(
        f"合计：步数 {_fmt_steps(total['steps']) if seen['steps'] else '无'}，"
        f"距离 {_fmt_dist(total['distance_m']) if seen['distance_m'] else '无'}，"
        f"消耗 {_fmt_kcal(total['kcal']) if seen['kcal'] else '无'}"
    )
    lines.append(
        f"      步行 {_fmt_minutes(total['walk_min']) if seen['walk_min'] else '无'}，"
        f"活动 {_fmt_hours(total['active_hours']) if seen['active_hours'] else '无'}，"
        f"运动 {_fmt_minutes(total['exercise_min']) if seen['exercise_min'] else '无'}"
    )
    return "\n".join(lines)


# ── 渲染：某天睡眠 + 心率 ────────────────────────────────────────────────
def render_sleep(store: Any, day: str) -> str:
    """渲染某天的睡眠汇总（时长/评分/效率/HRV/SpO2/入睡起床/白天小睡）与心率五项。

    入睡 / 起床 / 白天小睡都是「有时才显示」的附加行：字段缺失就整段不出现，不显示
    「--」「未知」这类空壳。
    """
    sleep = store.get("sleep_session", day)
    heart = store.get("heart_rate_sample", day)
    if not sleep and not heart:
        return f"{day} 没有睡眠或心率记录。"

    lines = [f"{day} 睡眠与心率"]

    lines.append("【睡眠】")
    if sleep:
        lines.append(f"- 时长 {_fmt_dur(sleep.get('duration_min'))}")
        lines.append(f"- 评分 {fmt_num(sleep.get('score'))}")
        lines.append(f"- 效率 {fmt_num(sleep.get('efficiency'), '%')}")
        lines.append(f"- HRV {fmt_num(sleep.get('hrv'))}")
        lines.append(f"- 血氧 {fmt_num(sleep.get('spo2'), '%')}")
        fall_asleep = _fmt_clock(sleep.get("fall_asleep_local"))
        wakeup = _fmt_clock(sleep.get("wakeup_local"))
        bounds = [text for text in (
            f"入睡 {fall_asleep}" if fall_asleep else "",
            f"起床 {wakeup}" if wakeup else "") if text]
        if bounds:
            lines.append("- " + " · ".join(bounds))
        nap = _positive(sleep.get("nap_duration_min"))
        if nap is not None:
            lines.append(f"- 白天小睡 {_fmt_minutes(nap)}")
    else:
        lines.append("- 无记录")

    lines.append("【心率】")
    if heart:
        lines.append(f"- 静息 {_fmt_hr(heart.get('resting_hr'))}")
        lines.append(f"- 日间 {_fmt_hr(heart.get('day_hr'))}")
        lines.append(f"- 平均静息 {_fmt_hr(heart.get('average_resting_hr'))}")
        lines.append(f"- 最高 {_fmt_hr(heart.get('max_hr'))}")
        lines.append(f"- 最低 {_fmt_hr(heart.get('min_hr'))}")
    else:
        lines.append("- 无记录")
    return "\n".join(lines)


# ── 渲染：最近 N 天训练会话 ──────────────────────────────────────────────
def render_training(
    store: Any,
    days: int,
    today: date | None = None,
    *,
    min_duration_min: Any = DEFAULT_TRAINING_MIN_DURATION_MIN,
    min_distance_m: Any = DEFAULT_TRAINING_MIN_DISTANCE_M,
) -> str:
    """渲染「最近 N 天训练会话列表」：时间/名称/时长/距离/卡路里/段数/设备码。

    只列「有效训练」（判据 ``storage/models.is_valid_training``，默认时长 ≥3 分钟或
    距离 ≥300 米）：手环自动产生的分钟级碎片仍留在库里，但不进列表，只在末尾用一行
    说明有几条被过滤（碎片只标记、不删除）。展示口径与活动汇总一致：时长 / 距离 /
    卡路里一律按「0 与缺失都算没测到」显示「无」，不拿 0 冒充读数。
    """
    days, start, end = _window(days, today)
    header = f"最近 {days} 天训练（{start} ~ {end}）"
    rows = store.query("training_session", start, end)
    valid = [
        row for row in rows
        if is_valid_training(
            row.get("duration_min"), row.get("distance_m"),
            min_duration_min=min_duration_min, min_distance_m=min_distance_m)
    ]
    hidden = len(rows) - len(valid)
    if not valid:
        text = header + "\n没有训练记录。"
        if hidden:
            text += f"（另有 {hidden} 条碎片记录未计入）"
        return text

    lines = [header]
    for row in valid:
        code = row.get("sport_type")
        name = SPORT_TYPE_NAMES.get(int(code) if code is not None else -1, f"运动类型{code}")
        when = row.get("start_local") or row.get("start_date") or "未知时间"
        duration = _positive(row.get("duration_min"))
        distance = _positive(row.get("distance_m"))
        kcal = _positive(row.get("kcal"))
        segments = _positive(row.get("segments"))
        segments_text = "无" if segments is None else f"{int(round(segments))} 段"
        device = row.get("device_code")
        device_text = f"，设备 {device}" if device else ""
        lines.append(
            f"- {when} {name}：{_fmt_dur(duration) if duration is not None else '无'}，"
            f"距离 {_fmt_dist(distance) if distance is not None else '无'}，"
            f"消耗 {_fmt_kcal(kcal) if kcal is not None else '无'}，"
            f"{segments_text}{device_text}"
        )
    footer = f"共 {len(valid)} 次训练"
    if hidden:
        footer += f"（另有 {hidden} 条碎片记录未计入）"
    lines.append(footer)
    return "\n".join(lines)
