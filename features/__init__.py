"""华为运动健康插件 —— 面向能力的特性模块（纯逻辑，不依赖运行期框架上下文）。

``llm_injection``：把本地健康摘要注入 LLM 上下文（多重前置门 + fail-closed）。
``proactive_care``：主动关怀（夜间 / 压力 / 起床 / 运动后四场景）的发送前模型闸门
（仅夜间）、按人格的措辞生成与程序侧兜底模板。
本包不 import 框架私有接口，可被脚本直接导入自检。
"""

from .llm_injection import (
    DEFAULT_SUMMARY_DAYS,
    SUMMARY_HEADER,
    InjectionDecision,
    build_part,
    decide,
    health_summary,
    provider_allowed,
    provider_source,
)
from .proactive_care import (
    DEFAULT_PROACTIVE_CONTEXT_PROMPT,
    DEFAULT_PROACTIVE_DECISION_PROMPT,
    PROACTIVE_DECISION_SYSTEM_PROMPT,
    ProactiveCare,
    build_decision_prompt,
    clean_reply,
    fallback_night_text,
    fallback_stress_text,
    fallback_wakeup_text,
    fallback_workout_text,
    parse_decision,
    wakeup_instruction,
)

__all__ = [
    "DEFAULT_SUMMARY_DAYS",
    "SUMMARY_HEADER",
    "InjectionDecision",
    "provider_source",
    "provider_allowed",
    "health_summary",
    "build_part",
    "decide",
    "DEFAULT_PROACTIVE_DECISION_PROMPT",
    "DEFAULT_PROACTIVE_CONTEXT_PROMPT",
    "PROACTIVE_DECISION_SYSTEM_PROMPT",
    "ProactiveCare",
    "parse_decision",
    "clean_reply",
    "build_decision_prompt",
    "fallback_night_text",
    "fallback_stress_text",
    "fallback_wakeup_text",
    "fallback_workout_text",
    "wakeup_instruction",
]
