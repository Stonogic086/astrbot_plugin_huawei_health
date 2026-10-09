#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""华为运动健康插件 —— refresh token 到期重登提醒（纯逻辑，时间源可注入）。

定稿规则：
  * refresh token 约 180 天有效；在到期前「提前 5 天」「提前 1 天」各提醒一次；
  * 同一窗口只提醒一次（去重），重启后不得重复轰炸；
  * 提醒内容是中文、口语、不含任何 token 片段；
  * 提醒失败不得影响同步主循环（send 抛异常由内部兜住）。

去重与目标的落盘选择：写插件数据目录下的 ``reminder_state.json``（原子写）。
理由：
  * 与 storage/ 的健康数据解耦，不新增表、不改现有 schema（红线要求不动 storage/ 既有逻辑）；
  * 本模块自包含、可用临时路径注入，便于脚本自检；
  * 纯 JSON、可读可手工清理，重启即恢复去重状态，天然满足「重启不重复轰炸」。

本模块不 import astrbot、不 import 第三方库。发送动作是外部注入的可调用对象，签名是
``send(text) -> bool | Awaitable[bool]``：插件里注入的是 async 方法，必须在事件循环里真正
await（否则协程从不执行，提醒发不出去却会被记成「已提醒」）；同步返回 bool 的实现也照样支持。
自检里用空实现替换，故不会真发消息。

线程约定：``run_once`` 是 async，与 ``_remember_notify_target`` 一样只在事件循环线程里被调用
（见 main.py 的 _safe_check_reminder 接线），ReminderState 因此不需要额外加锁。

状态读盘约定：内存状态是权威副本，``load()`` 只在「尚未与磁盘对齐」时经 ``load_if_needed()``
调用一次（此后读盘 / 写盘都算已对齐），不再整体读盘，避免磁盘内容覆盖内存里尚未落盘的改动。
"""

from __future__ import annotations

import inspect
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "REMINDER_WINDOWS",
    "REMINDER_STATE_FILENAME",
    "STATE_KEY_NOTIFIED",
    "STATE_KEY_EXPIRY",
    "STATE_KEY_NOTIFY_UMO",
    "remaining_days",
    "due_windows",
    "build_message",
    "format_expiry",
    "is_friend_event",
    "is_friend_umo",
    "ReminderState",
    "TokenReminder",
]

# 提前提醒窗口（天）。顺序从宽到窄；同一次判定里可能同时命中（首次启用且已临近）。
REMINDER_WINDOWS: tuple[int, ...] = (5, 1)

REMINDER_STATE_FILENAME = "reminder_state.json"

STATE_KEY_NOTIFIED = "notified"
STATE_KEY_EXPIRY = "refresh_expires_at"
STATE_KEY_NOTIFY_UMO = "notify_umo"

# 私聊的消息类型段取值（宿主 core/platform/message_type.py：MessageType.FRIEND_MESSAGE）。
FRIEND_MESSAGE_TYPE = "FriendMessage"


# ── 纯函数：时间与窗口 ───────────────────────────────────────────────────
def remaining_days(refresh_expires_at: Any, now: Any = None) -> float | None:
    """返回距 refresh token 到期的剩余天数（可为负=已过期）。

    到期时间缺失 / 非法 / <=0 时返回 None（表示「不知道到期时间」，不提醒）。
    """
    try:
        expiry = float(refresh_expires_at)
    except (TypeError, ValueError):
        return None
    if expiry <= 0:
        return None
    current = time.time() if now is None else float(now)
    return (expiry - current) / 86400.0


def due_windows(remaining: float | None, notified: Iterable[int] = ()) -> list[int]:
    """判定当前应触发的提醒窗口（返回命中且尚未提醒过的窗口，升序=从窄到宽）。

    规则：
      * remaining 为 None 或 <=0（已过期）→ 不提醒，返回 []；
      * 命中条件：remaining <= 窗口天数 且该窗口未提醒过；
      * 已提醒过的窗口（在 notified 里）不再命中 → 「同一窗口只提醒一次」。
    """
    if remaining is None or remaining <= 0:
        return []
    done = {int(x) for x in notified}
    hits = [w for w in REMINDER_WINDOWS if remaining <= w and w not in done]
    # 从窄到宽返回（先 1 天再 5 天对使用者更有意义：越紧急先说）。
    return sorted(hits)


def format_expiry(expiry: Any) -> str:
    """把到期 epoch 秒格式化成日期字符串（本地时区）；非法返回空串。"""
    try:
        value = float(expiry)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    try:
        return datetime.fromtimestamp(value).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return ""


def build_message(window: int, refresh_expires_at: Any) -> str:
    """生成一条中文、口语、不含任何 token 片段的提醒文案。"""
    date_text = format_expiry(refresh_expires_at)
    when = f"（预计 {date_text} 到期）" if date_text else ""
    if int(window) <= 1:
        head = "你的华为健康授权明天就要到期啦"
    else:
        head = f"你的华为健康授权还有大约 {int(window)} 天就到期啦"
    return (
        f"{head}{when}。到期之后插件就拉不到新的健康数据了，"
        "抽空重新授权一下就行，步骤和第一次一模一样（去插件的配置页走一遍授权即可，不用重新填别的）。"
    )


def is_friend_umo(umo: Any) -> bool:
    """判断 ``unified_msg_origin`` 是否来自「私聊」（字符串判定，结构化判定的兜底）。

    AstrBot 的 UMO 形如 ``<平台>:<消息类型>:<会话 id>``（宿主 unified_msg_origin =
    ``str(MessageSession)``，见 core/platform/astr_message_event.py），私聊段的取值就是
    ``MessageType.FRIEND_MESSAGE = "FriendMessage"``（core/platform/message_type.py，
    本机 astrbot 4.x 源码已核）。重登提醒定稿要求只走私聊，所以记录提醒目标前必须先过
    这一关：群聊（或任何非私聊）来源一律丢弃，避免提醒被发进群。
    优先用结构化字段（见 ``is_friend_event``），本函数只在拿不到结构化字段时兜底。
    """
    return FRIEND_MESSAGE_TYPE in str(umo or "")


def is_friend_event(event: Any) -> bool | None:
    """用框架的结构化字段判断这个事件是否来自「私聊」；拿不到结构化字段返回 None。

    判定顺序（均为 AstrMessageEvent 的公开方法，本机 astrbot 4.x 源码已核）：
      1. ``event.is_private_chat()`` —— 框架自带实现（等价于 get_message_type() ==
         MessageType.FRIEND_MESSAGE）；
      2. ``event.get_message_type()`` —— 取枚举值（FriendMessage / GroupMessage /
         OtherMessage）比较，只有 FriendMessage 才算私聊；
      3. ``event.get_group_id()``（或 ``message_obj.group_id``）—— 非空=群聊，空=私聊。
    三条都拿不到（例如自检里的最小 stub event）→ 返回 None，由调用方退回
    ``is_friend_umo`` 的 UMO 字符串判定。
    """
    private = getattr(event, "is_private_chat", None)
    if callable(private):
        try:
            result = private()
        except Exception:  # 结构化字段炸了也不能让记录提醒目标这一步失败
            result = None
        if isinstance(result, bool):
            return result
    message_type = getattr(event, "get_message_type", None)
    if callable(message_type):
        try:
            got = message_type()
        except Exception:
            got = None
        value = getattr(got, "value", None)
        if value is None and isinstance(got, str):
            value = got
        if isinstance(value, str) and value:
            return value == FRIEND_MESSAGE_TYPE
    group_id = getattr(event, "get_group_id", None)
    if callable(group_id):
        try:
            got = group_id()
        except Exception:
            got = None
        if got is not None:
            return not str(got).strip()
    message_obj = getattr(event, "message_obj", None)
    got = getattr(message_obj, "group_id", None) if message_obj is not None else None
    if got is not None:
        return not str(got).strip()
    return None


# ── 去重状态（JSON，原子写）─────────────────────────────────────────────
class ReminderState:
    """提醒去重 / 通知目标的小状态文件。path=None 时退化为纯内存态。"""

    def __init__(self, path: Any = None) -> None:
        self.path = Path(path) if path else None
        self._data: dict[str, Any] = {
            STATE_KEY_NOTIFIED: {},
            STATE_KEY_EXPIRY: 0.0,
            STATE_KEY_NOTIFY_UMO: "",
        }
        # 内存是否已与磁盘对齐（读过盘或写过盘）。见 load_if_needed()。
        self._synced: bool = False

    # ── 读写 ─────────────────────────────────────────────────────────────
    def load(self) -> "ReminderState":
        """从磁盘读状态（缺失 / 损坏时保持默认，不抛异常）。

        注意：本方法会用磁盘内容**整体替换**内存里的去重标记，所以不要在内存已有尚未
        落盘的改动时调用它——那种情况下用 ``load_if_needed()``（最多只读一次）。
        """
        self._synced = True
        if self.path is None or not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return self
        if isinstance(raw, dict):
            notified = raw.get(STATE_KEY_NOTIFIED)
            if isinstance(notified, dict):
                self._data[STATE_KEY_NOTIFIED] = {
                    str(k): v for k, v in notified.items()
                }
            try:
                self._data[STATE_KEY_EXPIRY] = float(raw.get(STATE_KEY_EXPIRY) or 0.0)
            except (TypeError, ValueError):
                self._data[STATE_KEY_EXPIRY] = 0.0
            umo = raw.get(STATE_KEY_NOTIFY_UMO)
            self._data[STATE_KEY_NOTIFY_UMO] = str(umo) if umo else ""
        return self

    def load_if_needed(self) -> "ReminderState":
        """只在「内存尚未与磁盘对齐」时读一次盘（首次读盘 / 首次写盘之前）。

        内存状态是权威副本：一旦读过盘或写过盘，之后就不再整体读盘，避免磁盘内容覆盖
        内存里还没落盘的改动（``run_once`` 里 mark() 之后、save() 之前就是这种窗口）。
        """
        if not self._synced:
            self.load()
        return self

    def save(self) -> bool:
        """原子写状态文件。写失败只返回 False，不上抛。

        无论写成功与否都记「已与磁盘对齐」：写失败时内存仍是最新的权威副本，之后再读盘
        只会把内存里的新状态换成磁盘上的旧内容（可能重复提醒）。
        """
        self._synced = True
        if self.path is None:
            return False
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2, sort_keys=True),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
            self._restrict_permissions()
            return True
        except Exception:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False

    def _restrict_permissions(self) -> None:
        """状态文件含私聊会话标识，落盘后显式收成 0600（失败不影响功能）。"""
        if self.path is None:
            return
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ── 去重标记 ─────────────────────────────────────────────────────────
    @property
    def notified(self) -> dict[str, float]:
        data = self._data.get(STATE_KEY_NOTIFIED)
        return data if isinstance(data, dict) else {}

    def is_notified(self, window: int) -> bool:
        return str(int(window)) in self.notified

    def mark(self, window: int, when: Any = None) -> None:
        self.notified[str(int(window))] = float(time.time() if when is None else when)

    def clear_notified(self) -> None:
        self._data[STATE_KEY_NOTIFIED] = {}

    # ── 到期时间绑定（换新 token → 到期时间变 → 清空旧去重）─────────────────
    @property
    def bound_expiry(self) -> float:
        try:
            return float(self._data.get(STATE_KEY_EXPIRY) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def bind_expiry(self, expires_at: Any) -> bool:
        """把当前到期时间绑定进状态；若与上次不同则清空去重标记。返回是否发生了变化。"""
        try:
            value = float(expires_at)
        except (TypeError, ValueError):
            return False
        if value <= 0:
            return False
        previous = self.bound_expiry
        # 允许极小误差（同一 token 反复落盘不应被判为“换了”）。
        if previous and abs(previous - value) < 1.0:
            return False
        self._data[STATE_KEY_EXPIRY] = value
        self.clear_notified()
        return True

    # ── 通知目标 ─────────────────────────────────────────────────────────
    @property
    def notify_umo(self) -> str:
        return str(self._data.get(STATE_KEY_NOTIFY_UMO) or "")

    @notify_umo.setter
    def notify_umo(self, value: str) -> None:
        self._data[STATE_KEY_NOTIFY_UMO] = str(value or "")


# ── 编排：一轮提醒检查 ───────────────────────────────────────────────────
class TokenReminder:
    """一趟「读到期时间 → 判窗口 → 发送 → 记去重」的编排。

    参数：
        state       —— ReminderState（去重/目标落盘）；
        get_expiry  —— 返回 refresh token 到期 epoch 秒的可调用对象（可为 None/0）；
        send        —— ``send(text) -> bool | Awaitable[bool]``；返回 False 视为「未送达」
                       （不记去重、下轮再试），抛异常同样按未送达处理；
        now         —— 时间源，默认 time.time；可注入以做确定性自检；
        logger      —— 可选 logger（标准库或注入），缺省静默。
    """

    def __init__(
        self,
        state: ReminderState,
        get_expiry: Callable[[], Any],
        send: Callable[[str], Any],
        now: Callable[[], float] | None = None,
        logger: Any = None,
    ) -> None:
        self.state = state
        self.get_expiry = get_expiry
        self.send = send
        self.now = now or time.time
        self.logger = logger

    def _log(self, level: str, message: str, *args: Any) -> None:
        if self.logger is None:
            return
        method = getattr(self.logger, level, None)
        if callable(method):
            try:
                method(message, *args)
            except Exception:
                pass

    async def run_once(self, now: Any = None) -> dict[str, Any]:
        """检查一次并按需提醒（async：真实发送是 async，必须 await）。

        绝不抛异常（提醒失败只记日志）。
        """
        try:
            expiry = self.get_expiry()
        except Exception as error:
            self._log("warning", "[华为运动健康] 读取到期时间失败：%s", type(error).__name__)
            return {"ok": False, "stage": "expiry", "reason": type(error).__name__}

        try:
            # 只读一次盘：内存里可能已有尚未落盘的去重标记，整体读盘会把它们抹掉。
            self.state.load_if_needed()
        except Exception:
            pass

        if not expiry or float(expiry) <= 0:
            return {"ok": True, "stage": "skip", "reason": "no_expiry", "sent": []}

        current = self.now() if now is None else float(now)
        rem = remaining_days(expiry, current)

        # 到期时间变了（换了新 token）→ 清空旧窗口去重，避免新周期不提醒。
        self.state.bind_expiry(expiry)

        pending = due_windows(rem, self.state.notified)
        sent: list[int] = []
        for window in pending:
            text = build_message(window, expiry)
            try:
                outcome = self.send(text)
                # 注入的 send 可能是 async（插件里就是）：协程必须先 await 再判成败，否则
                # 它永远不执行——提醒发不出去，却会被标记成「已提醒」并落盘、该周期不再重试。
                if inspect.isawaitable(outcome):
                    outcome = await outcome
                ok = outcome
            except Exception as error:
                self._log(
                    "warning",
                    "[华为运动健康] 重登提醒发送失败（窗口 %s 天，%s），已忽略",
                    window,
                    type(error).__name__,
                )
                continue
            if ok is False:
                self._log(
                    "warning",
                    "[华为运动健康] 重登提醒未能送达（窗口 %s 天），下次再试",
                    window,
                )
                continue
            self.state.mark(window, current)
            # mark 与落盘收成不可交错：多窗口命中时下一步还要 await 发送，期间若有别的
            # 调用点（如记录通知目标）整体读盘，就会把这条还没落盘的标记抹掉（重复提醒）。
            try:
                self.state.save()
            except Exception:
                pass
            sent.append(int(window))
            self._log("info", "[华为运动健康] 已发送重登提醒（窗口 %s 天）", window)

        try:
            self.state.save()
        except Exception:
            pass

        return {
            "ok": True,
            "stage": "done",
            "remaining_days": rem,
            "due": [int(w) for w in pending],
            "sent": sent,
            "expires_at": float(expiry),
        }
