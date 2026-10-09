"""华为运动健康插件 —— 「健康数据进 LLM 上下文」的纯逻辑层。

三条能力：
  * ``provider_source`` / ``provider_allowed`` —— 按本轮实际 provider id 与白名单匹配；
  * ``health_summary`` —— 组装中文健康摘要文本（六类模型，缺失一律渲染成「无」）；
  * ``build_part`` / ``decide`` —— 统一返回「是否注入」（授权 → 名单 → 摘要非空），
    任何一步异常一律不注入。

两条硬规矩：
  1. 只有本轮实际使用的 provider 在白名单内才注入（前缀匹配：同源备用模型会一并放行）；
     降级到名单外模型的拦截由调用方在钩子内额外完成（框架的降级切换发生在钩子之后，
     见 main.on_llm_request），本模块自身不做额外特判、不读全局 fallback 配置；
  2. 注入链路任何一步失败都退化成「不带健康数据的普通回答」——本模块绝不抛异常。

隐私：本模块不写日志（健康数值绝不进日志）；摘要文本只交给调用方按临时内容注入，
调用方必须经 ``build_part`` 拿到带 mark_as_temp 的 part，否则整轮不注入。

框架依赖只用于两件事，都放在 try/except 里，拿不到就整轮不注入（fail-closed）：
  * 挂载点探测：``astrbot.api.provider.ProviderRequest`` 拿不到 → ``decide`` 直接返回不注入；
  * 封装可注入的 part：拿不到 TextPart 或 mark_as_temp 时 ``build_part`` 返回 None。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

try:  # 作为插件子包导入（正式运行）优先；脚本直跑时回退顶层包导入
    from ..commands.health_query import (
        fmt_num,
        render_activity,
        render_sleep,
        render_training,
    )
except ImportError:
    from commands.health_query import (
        fmt_num,
        render_activity,
        render_sleep,
        render_training,
    )

def mount_point_ready() -> bool:
    """框架挂载点是否可用：能 import 到 ``astrbot.api.provider.ProviderRequest`` 就算可用。

    运行期现探、不缓存模块级结果：宿主 / 自检桩的 ``sys.modules`` 可能变化，只有现探才是
    真实的挂载点状态。拿不到 = 框架不是本插件认得的形态 → 整轮不注入（fail-closed）。
    """
    try:
        from astrbot.api.provider import ProviderRequest  # noqa: F401 - 只做挂载点探测
    except Exception:  # 任何导入失败都按「无该挂载点」处理
        return False
    return True

try:
    from astrbot.core.agent.message import TextPart
except Exception:
    TextPart = None

__all__ = [
    "DEFAULT_SUMMARY_DAYS",
    "SUMMARY_HEADER",
    "InjectionDecision",
    "provider_source",
    "provider_allowed",
    "mount_point_ready",
    "health_summary",
    "build_part",
    "decide",
]

# 摘要窗口：与 sync.default_sync_days 默认值一致（3 天）。
DEFAULT_SUMMARY_DAYS = 3

SUMMARY_HEADER = "【华为运动健康 · 本地健康数据摘要（仅供本次回答参考，勿写入长期记忆）】"


# ── 能力一：provider id 与白名单匹配 ─────────────────────────────────────
def provider_source(provider_id: Any) -> str:
    """取 provider id 的来源前缀：第一个「/」之前的那一段；无「/」时就是整串。"""
    text = "" if provider_id is None else str(provider_id).strip()
    if not text:
        return ""
    return text.split("/", 1)[0]


def provider_allowed(provider_id: Any, allowlist: Any) -> bool:
    """本轮实际 provider id 是否命中白名单。

    命中条件：某条目与 provider id 完整相等，或等于其来源前缀
    （例如条目 ``siliconflow`` 命中 ``siliconflow/Qwen/Qwen3.5-4B``）。
    空 id / 空名单 / 空条目一律不命中。
    """
    target = "" if provider_id is None else str(provider_id).strip()
    if not target:
        return False
    entries = allowlist
    if entries is None:
        return False
    if isinstance(entries, str):
        entries = [entries]
    elif not isinstance(entries, (list, tuple, set, frozenset)):
        entries = [entries]
    source = provider_source(target)
    for entry in entries:
        item = "" if entry is None else str(entry).strip()
        if not item:
            continue
        if item == target or item == source:
            return True
    return False


# ── 能力二：组装健康摘要文本 ─────────────────────────────────────────────
def _render_stress(store: Any, day: str) -> str:
    row = store.get("stress_sample", day)
    if not row:
        return f"压力（{day}）：无"
    return (
        f"压力（{day}）：平均 {fmt_num(row.get('average'))}，"
        f"最高 {fmt_num(row.get('max_value'))}，"
        f"最低 {fmt_num(row.get('min_value'))}，"
        f"最近 {fmt_num(row.get('last_value'))}，"
        f"测量 {fmt_num(row.get('measurements'))} 次"
    )


def _render_spo2(store: Any, day: str) -> str:
    row = store.get("spo2_sample", day)
    if not row:
        return f"血氧（{day}）：无"
    return f"血氧（{day}）：{fmt_num(row.get('spo2'), '%')}"


def health_summary(
    store: Any,
    days: int = DEFAULT_SUMMARY_DAYS,
    today: date | None = None,
    *,
    training_min_duration_min: Any = None,
    training_min_distance_m: Any = None,
) -> str:
    """组装六类数据的摘要文本；存储不可用（store=None）时返回空串＝不注入。

    渲染复用查询命令层的 render_activity / render_sleep / render_training 与 fmt_num，
    保证「命令里看到的」与「送进上下文的」同一口径（缺失一律写「无」）。训练段同样只含
    有效训练：碎片判据的阈值由调用方从配置传入（缺省用判定层默认值）。
    """
    if store is None:
        return ""
    window = max(1, int(days))
    day = (today or date.today()).isoformat()
    training_thresholds: dict[str, Any] = {}
    if training_min_duration_min is not None:
        training_thresholds["min_duration_min"] = training_min_duration_min
    if training_min_distance_m is not None:
        training_thresholds["min_distance_m"] = training_min_distance_m
    blocks = (
        render_activity(store, window, today),
        render_sleep(store, day),
        render_training(store, window, today, **training_thresholds),
        _render_stress(store, day),
        _render_spo2(store, day),
    )
    body = "\n".join(block.strip() for block in blocks if block and block.strip())
    if not body:
        return ""
    return f"{SUMMARY_HEADER}\n{body}"


# ── 能力三：统一返回「是否注入」 ─────────────────────────────────────────
@dataclass(frozen=True)
class InjectionDecision:
    """一次注入判定：inject=False 时 text 为空；reason 说明拦在哪一步（不含健康数值）。"""

    inject: bool
    reason: str
    text: str = ""


def decide(
    store: Any,
    provider_id: Any,
    allowlist: Any,
    authorized: Any,
    days: int = DEFAULT_SUMMARY_DAYS,
    today: date | None = None,
    *,
    training_min_duration_min: Any = None,
    training_min_distance_m: Any = None,
) -> InjectionDecision:
    """统一返回是否注入，判定顺序：框架挂载点 → 授权 → provider 在名单内 → 摘要非空。

    reason 取值：no_mount_point / not_authorized / provider_not_allowed / empty_summary /
    ok / error:<异常类型>。任何一步异常都返回 inject=False，绝不抛给调用方。
    """
    try:
        if not mount_point_ready():
            return InjectionDecision(False, "no_mount_point")
        if authorized is not True:
            return InjectionDecision(False, "not_authorized")
        if not provider_allowed(provider_id, allowlist):
            return InjectionDecision(False, "provider_not_allowed")
        text = health_summary(
            store, days=days, today=today,
            training_min_duration_min=training_min_duration_min,
            training_min_distance_m=training_min_distance_m,
        )
        if not text:
            return InjectionDecision(False, "empty_summary")
        return InjectionDecision(True, "ok", text)
    except Exception as error:  # 注入链失败一律退化，绝不中断聊天
        return InjectionDecision(False, f"error:{type(error).__name__}")


def build_part(text: Any) -> Any:
    """把摘要文本封成可注入的临时 part；任一步拿不到就返回 None（fail-closed）。

    探测不到 ``mark_as_temp`` 的框架版本整轮不注入——否则这份健康数据会随该 part
    写进长期会话历史。
    """
    if TextPart is None:
        return None
    content = "" if text is None else str(text)
    if not content.strip():
        return None
    part = TextPart(text=content)
    mark = getattr(part, "mark_as_temp", None)
    if not callable(mark):
        return None
    mark()
    return part
