#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为运动健康插件 —— 隐私闸门（集中一处实现，fail-closed）。

定稿要求（本模块是唯一实现点）：
  1. 健康数据默认不送 LLM：默认配置（``allow_health_data_to_llm=false``）下，
     任何指向「外部 / LLM / 消息注入点」的出站尝试都必须被拒绝，即使被误调用；
  2. 使用者本人查看数据（命令直接回给本人、本地库直读）不受闸门限制；
  3. 只有显式打开开关后才允许外送；开关关闭 = 不允许外送。

设计约定：
  * 出站一律经过 ``PrivacyGate.check`` / ``require_outbound`` 这一个收口；
    （现状：健康数据唯一的出站路径是 ``features/llm_injection.py``，它以「闸门打开 +
    本轮 provider 在白名单」为前置条件，默认不注入；原先 main 里那个没有调用者的
    预留方法 ``guard_outbound`` 已删除，判定只留本模块一处）
  * 目的地分三类：已知「本地（本人查看）」白名单 → 放行；已知「外部」→ 看开关；
    未知目的地 → 按外部处理（fail-closed，宁拒绝不误放）；
  * 本模块不 import astrbot、不 import 第三方库，纯逻辑，可被脚本直接调用自检。

术语：destination 指「这份数据要送去哪里」的字符串标识，见下面常量。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "CONF_GROUP",
    "CONF_ALLOW_KEY",
    "DEFAULT_ALLOW_HEALTH_DATA_TO_LLM",
    "DEST_LLM",
    "DEST_LLM_REQUEST",
    "DEST_MESSAGE_INJECTION",
    "DEST_EXTERNAL",
    "DEST_USER",
    "DEST_LOCAL",
    "DEST_COMMAND_REPLY",
    "DEST_STORAGE",
    "EXTERNAL_DESTINATIONS",
    "LOCAL_DESTINATIONS",
    "MASK_KEEP",
    "mask_secret",
    "GateDecision",
    "PrivacyBlockedError",
    "PrivacyGate",
    "default_gate",
    "gate_from_config",
]

# ── 配置位置 ─────────────────────────────────────────────────────────────
CONF_GROUP = "privacy"
CONF_ALLOW_KEY = "allow_health_data_to_llm"

# 默认值必须是「不允许外送」。
DEFAULT_ALLOW_HEALTH_DATA_TO_LLM = False

# ── 目的地常量 ───────────────────────────────────────────────────────────
# 外部（需要开关打开才放行）——会离开本机 / 交给模型 / 注入到消息流。
DEST_LLM = "llm"
DEST_LLM_REQUEST = "llm_request"
DEST_MESSAGE_INJECTION = "message_injection"
DEST_EXTERNAL = "external"

# 本地（使用者本人查看，永远是本人可及，不受闸门限制）。
DEST_USER = "user"
DEST_LOCAL = "local"
DEST_COMMAND_REPLY = "command_reply"
DEST_STORAGE = "storage"

EXTERNAL_DESTINATIONS = frozenset(
    {DEST_LLM, DEST_LLM_REQUEST, DEST_MESSAGE_INJECTION, DEST_EXTERNAL}
)
LOCAL_DESTINATIONS = frozenset(
    {DEST_USER, DEST_LOCAL, DEST_COMMAND_REPLY, DEST_STORAGE}
)


# ── 脱敏（唯一实现点）────────────────────────────────────────────────────
# 只显示前后各 6 位，任何时刻不得把 token 全文写进日志 / 终端 / 报告。
# 脱敏是安全相关的基建：main.py 与自检脚本一律从这里取，不再各留一份实现。
MASK_KEEP = 6


def mask_secret(value: Any) -> str:
    """把 token 之类的敏感串脱敏成前后各 6 位，其余用 * 替代。"""
    if value is None:
        return "<无>"
    text = str(value)
    if not text:
        return "<空>"
    if len(text) <= MASK_KEEP * 2:
        return "*" * len(text)
    return f"{text[:MASK_KEEP]}{'*' * (len(text) - MASK_KEEP * 2)}{text[-MASK_KEEP:]}"


class PrivacyBlockedError(RuntimeError):
    """把健康数据送往外部 / LLM 时被闸门拒绝。不带任何 payload 内容。"""

    def __init__(self, destination: str, reason: str) -> None:
        super().__init__(f"隐私闸门拒绝了发往 {destination!r} 的出站：{reason}")
        self.destination = destination
        self.reason = reason


@dataclass(frozen=True)
class GateDecision:
    """一次出站判定的结果。allowed=False 时 reason 说明原因（不含 payload）。"""

    allowed: bool
    destination: str
    reason: str
    gate_open: bool
    is_external: bool


def _payload_hint(payload: Any) -> str:
    """把 payload 压成「不含内容」的一行摘要，仅用于 reason / 日志兜底。"""
    if payload is None:
        return "无 payload"
    try:
        size = len(payload)
    except TypeError:
        size = None
    kind = type(payload).__name__
    return f"{kind}（长度 {size}）" if size is not None else kind


class PrivacyGate:
    """健康数据出站闸门。默认关闭（不允许外送）。

    参数：
        allow_outbound —— True 才允许把健康数据送往外部 / LLM；默认 False。
    """

    def __init__(self, allow_outbound: bool = DEFAULT_ALLOW_HEALTH_DATA_TO_LLM) -> None:
        # 严格布尔化：只接受真正的 True 才打开，其余（None/字符串/数字）一律视为关闭。
        self._allow_outbound = allow_outbound is True

    # ── 状态 ─────────────────────────────────────────────────────────────
    @property
    def is_open(self) -> bool:
        """开关是否打开（True=允许外送）。"""
        return self._allow_outbound

    @property
    def is_closed(self) -> bool:
        """开关是否关闭（True=不允许外送，默认态）。"""
        return not self._allow_outbound

    @property
    def allow_outbound(self) -> bool:
        return self._allow_outbound

    def set_allow_outbound(self, value: bool) -> None:
        """运行期改开关（同样只认真正的 True）。"""
        self._allow_outbound = value is True

    # ── 判定 ─────────────────────────────────────────────────────────────
    @staticmethod
    def is_external(destination: str) -> bool:
        """目的地是否属于「外部」（需要开关）。本地白名单返回 False；未知按外部处理。"""
        dest = str(destination)
        if dest in LOCAL_DESTINATIONS:
            return False
        return True

    def check(self, destination: str, payload: Any = None) -> GateDecision:
        """判定一次出站是否放行。纯判定，不抛异常、不产生副作用。

        规则：
          * 本地白名单（本人查看）→ 放行，与开关无关；
          * 外部 / 未知目的地 → 开关打开才放行，否则拒绝（fail-closed）。
        """
        dest = str(destination)
        external = self.is_external(dest)
        if not external:
            return GateDecision(
                allowed=True,
                destination=dest,
                reason="本地路径（使用者本人查看），不受闸门限制",
                gate_open=self._allow_outbound,
                is_external=False,
            )
        if dest in EXTERNAL_DESTINATIONS:
            why = "外部目的地，需显式打开开关"
        else:
            why = "未知目的地，按外部处理（fail-closed）"
        if self._allow_outbound:
            return GateDecision(
                allowed=True,
                destination=dest,
                reason=f"{why}；开关已打开",
                gate_open=True,
                is_external=True,
            )
        return GateDecision(
            allowed=False,
            destination=dest,
            reason=(
                f"{why}；开关关闭（默认=不允许外送），已拒绝。"
                f" payload={_payload_hint(payload)}"
            ),
            gate_open=False,
            is_external=True,
        )

    # 闸门对外只留两个入口：check（纯判定）与 require_outbound（判定 + 拒绝时抛错）。
    # 原先的 guard_outbound 别名、require_allowed 变体没有任何调用点，已删除。
    def require_outbound(self, destination: str, payload: Any = None) -> Any:
        """放行则原样返回 payload；被拒绝则抛 PrivacyBlockedError。"""
        decision = self.check(destination, payload)
        if decision.allowed:
            return payload
        raise PrivacyBlockedError(decision.destination, decision.reason)

def default_gate() -> PrivacyGate:
    """返回默认闸门：关闭（不允许外送）。"""
    return PrivacyGate(DEFAULT_ALLOW_HEALTH_DATA_TO_LLM)


def gate_from_config(config: Any = None) -> PrivacyGate:
    """从插件配置构造闸门。

    兼容两种布局：``config["privacy"]["allow_health_data_to_llm"]``（分组）
    与扁平 ``config["allow_health_data_to_llm"]``。任何缺失 / 非 True 值 = 关闭。
    """
    value: Any = None
    if isinstance(config, dict):
        grouped = config.get(CONF_GROUP)
        if isinstance(grouped, dict) and CONF_ALLOW_KEY in grouped:
            value = grouped.get(CONF_ALLOW_KEY)
        elif CONF_ALLOW_KEY in config:
            value = config.get(CONF_ALLOW_KEY)
    return PrivacyGate(value is True)
