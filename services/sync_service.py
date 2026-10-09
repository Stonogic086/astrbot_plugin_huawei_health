#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同步服务：把协议层拉到的云端数据喂给存储层（v1 定时同步的唯一编排点）。

职责（被调用一次 = 完成一轮同步）：
    1. 刷新一次 access token（唯一「必须先成功」的步骤）；
    2. 拉最近 N 天（默认 3，含今天）并写入六类数据（取数统一走取数门面
       ``adapters.huawei_health_facade``，门面负责单位/时区重排与异常翻译）：
         daily_activity  ← iter_daily_activity（getSportsStat 日汇总）
         heart_rate      ← iter_heart_rate    （getHealthStat 心率族）
         sleep           ← iter_sleep         （同一次响应的睡眠族）
         stress          ← iter_stress        （压力族）
         spo2            ← iter_spo2          （同一响应的睡眠期 lastAvgSpO2）
         training_session← iter_training      （getSportsDataByTime 分钟段，去重/合并由门面调存储层函数）
    3. 把本轮每类的同步状态写进存储层 sync_state（状态 / 窗口上界 / 失败原因，不含健康数值）；
    4. 返回每类写入行数 + 耗时 + 跳过原因 + 无数据类别的摘要。

设计约定：
    * 写入一律走 storage.HealthStore 已有的 upsert 接口，本模块不写任何 SQL
      （幂等由存储层的唯一约束保证）；
    * 六类中任意一类取数失败只记「跳过原因」，不中断其余类别，也不抛给上层
      （上层后台循环据此不会被打死）；失败按门面三类分流：认证类记 kind=auth
      （重登提醒 / 暂停自动同步是上层既有逻辑，见 reminder.TokenReminder 与
      sync.enable_auto_sync）、网络类 kind=network（本轮跳过、下轮再试）、
      解析类 kind=parse（只降级该类）；
    * 单类「云端没有数据」与「取数失败」是**两条不同路径**：前者取数成功但返回空
      （或取到行却一行都没入库，例如当天没有聚合行），记进 ``summary["no_data"]``，
      由 ``_record_state`` 标成「无」（不写 failed）；
    * 协议层是同步 urllib，所有取数经门面的 async 方法执行；
      存储层写入是本地 sqlite，也统一丢线程池，避免阻塞事件循环；
    * 本模块不 import astrbot，日志走标准库 logging（可由调用方注入 logger）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta
from functools import partial
from typing import Any, Callable, Iterable

try:  # 宿主以内嵌包导入（main.py 走这条）
    from ..adapters import HuaweiHealthFacade
    from ..adapters.errors import (
        HuaweiHealthAuthenticationError,
        HuaweiHealthNetworkError,
        HuaweiHealthParseError,
    )
    from ..storage import models
except ImportError:  # 脚本直跑：插件根目录在 sys.path 上（adapters 是顶层包）
    from adapters import HuaweiHealthFacade  # type: ignore[no-redef]
    from adapters.errors import (  # type: ignore[no-redef]
        HuaweiHealthAuthenticationError,
        HuaweiHealthNetworkError,
        HuaweiHealthParseError,
    )
    from storage import models  # type: ignore[no-redef]

_LOGGER = logging.getLogger(__name__)

# 一轮同步覆盖的六类数据（摘要里的键名与存储层模型名对齐）。
DATA_CLASSES: tuple[str, ...] = (
    "daily_activity",
    "heart_rate",
    "sleep",
    "stress",
    "spo2",
    "training_session",
)

# 默认窗口：最近 3 天（含今天）。
DEFAULT_SYNC_DAYS = 3

# sync_state 表里「整轮同步」汇总行的 data_type 保留值（与六类数据名不重名）。
SYNC_STATE_ROUND = "round"

# 取数失败的分类键（写进摘要的 skipped 项，供上层分流：重登 / 下轮重试 / 单类降级）。
KIND_AUTH = "auth"
KIND_NETWORK = "network"
KIND_PARSE = "parse"
KIND_OTHER = "other"

# 「该类云端没有数据」的标注：与失败原因分开记（status=skipped，绝不写 failed）。
NO_DATA_STATUS = "skipped"
NO_DATA_NOTE = "云端无数据（本类本轮无记录，非失败）"

# 取数失败的分类键 → 日志里的人话说明（key 由 _kind_of 给出）。
KIND_NOTES: dict[str, str] = {
    "auth": "认证失效，需重新登录后恢复",
    "network": "网络失败，本轮跳过、下轮重试",
    "parse": "应答不可解析，本类降级，其余类别继续",
    "other": "非门面异常，本类跳过",
}

# 健康四类：摘要键名 / 状态键名 → （门面取数方法名 iter_<名>，存储层模型名）。
HEALTH_CLASSES: tuple[tuple[str, str], ...] = (
    ("heart_rate", "heart_rate_sample"),
    ("sleep", "sleep_session"),
    ("stress", "stress_sample"),
    ("spo2", "spo2_sample"),
)


def _err(error: BaseException) -> str:
    """把异常压成单行原因文本（协议层异常只含 url/resultCode，不含 token）。"""
    return f"{type(error).__name__}: {error}"


def _kind_of(error: BaseException) -> str:
    """按门面三类给异常分流（刷新与六类取数共用同一套分类，summary 里的 kind 才有一致口径）。

    门面异常以外的异常一律 KIND_OTHER：不猜类别、不把它当认证失败。
    """
    if isinstance(error, HuaweiHealthAuthenticationError):
        return KIND_AUTH
    if isinstance(error, HuaweiHealthNetworkError):
        return KIND_NETWORK
    if isinstance(error, HuaweiHealthParseError):
        return KIND_PARSE
    return KIND_OTHER


class SyncService:
    """编排一轮「云端 → 存储层」同步。

    参数：
        adapter —— HuaweiHealthCloudAdapter（协议层取数 + refresh；门面在其上再包一层）；
        store   —— storage.HealthStore（幂等写入）；
        days    —— 日期窗口（最近 N 天，含今天），默认 3；
        logger  —— 可选，注入 astrbot logger；缺省用标准库 logger。
    """

    def __init__(
        self,
        adapter: Any,
        store: Any,
        *,
        days: Any = DEFAULT_SYNC_DAYS,
        training_min_duration_min: Any = models.DEFAULT_TRAINING_MIN_DURATION_MIN,
        training_min_distance_m: Any = models.DEFAULT_TRAINING_MIN_DISTANCE_M,
        logger: Any = None,
    ) -> None:
        self.adapter = adapter
        self.store = store
        # 取数门面：单位/时区重排 + 协议层异常 → 门面三类（六类数据都从这里取）。
        self.facade = HuaweiHealthFacade(adapter)
        try:
            self.days = max(1, int(days))
        except (TypeError, ValueError):
            self.days = DEFAULT_SYNC_DAYS
        # 「有效训练」判据阈值：写库时给训练会话打碎片标记，与查询 / 关怀共用同一判据。
        self.training_min_duration_min = training_min_duration_min
        self.training_min_distance_m = training_min_distance_m
        self.logger = logger or _LOGGER

    # ── 主流程 ───────────────────────────────────────────────────────────
    async def run_once(self) -> dict[str, Any]:
        """执行一轮完整同步，返回结构化摘要（不抛异常，失败记进摘要）。"""
        started = time.monotonic()
        today = date.today()
        start = today - timedelta(days=self.days - 1)
        summary: dict[str, Any] = {
            "ok": False,
            "status": "failed",
            "days": self.days,
            "window_start": start.isoformat(),
            "window_end": today.isoformat(),
            "written": {name: 0 for name in DATA_CLASSES},
            "skipped": [],
            "no_data": [],
            "elapsed_sec": 0.0,
        }
        # 每轮开始先清掉门面的窗口缓存：同一天连跑两轮时不会复用上一轮的 health 响应。
        await self.facade.close()

        # 1. 刷新 access token —— 唯一「必须先成功」的步骤，失败即整轮无数据。
        #    分类走与六类取数同一套 _kind_of：认证失效记 kind=auth（重登 / 暂停自动同步
        #    由上层既有逻辑处理），不再一律塞 KIND_OTHER。
        try:
            await self.adapter.refresh()
        except Exception as error:  # noqa: BLE001 - 必须兜住，不能打死后台循环
            kind = _kind_of(error)
            summary["skipped"].append(
                {"stage": "refresh", "kind": kind, "reason": _err(error)})
            summary["elapsed_sec"] = round(time.monotonic() - started, 2)
            self.logger.warning(
                "同步：刷新 token 失败（%s），本轮无数据：%s",
                KIND_NOTES.get(kind, KIND_NOTES[KIND_OTHER]), _err(error))
            await self._record_state(summary)
            return summary

        # 2. 日汇总（getSportsStat → daily_activity 表）
        await self._pull(
            "daily_activity",
            partial(self.facade.iter_daily_activity, start, today),
            self.store.upsert_daily_activity,
            summary,
        )
        # 3. 心率 / 睡眠 / 压力 / 血氧（门面里同一次 health 响应拆成四类）
        await self._pull_health(start, today, summary)
        # 4. 训练会话（门面按 dataId 去重、按 sportType 合并成会话 → training_session 表）
        await self._pull(
            "training_session",
            partial(self.facade.iter_training, start, today),
            self._write_training,
            summary,
        )

        summary["ok"] = True
        summary["status"] = "partial" if summary["skipped"] else "ok"
        summary["elapsed_sec"] = round(time.monotonic() - started, 2)
        if summary["no_data"]:
            self.logger.info(
                "同步：本轮云端无数据的类别（标注「无」）：%s",
                "，".join(summary["no_data"]),
            )
        await self._record_state(summary)
        return summary

    async def _record_state(self, summary: dict[str, Any]) -> None:
        """把本轮同步状态写进存储层 sync_state（best-effort，失败只记警告）。

        只写状态：data_type / status / 窗口上界 / 失败原因；不含任何健康数值，
        失败原因取自门面异常（只含 url / resultCode，不含 token）。

        单类状态两条路径显式分开：
          * 有失败原因（取数或写库失败）→ ``failed`` + 原因；
          * ``summary["no_data"]`` 里的类别（云端没数据）→ ``skipped`` + 「无」标注；
          * 其余 → ``ok``。
        """
        reasons = {
            str(item.get("stage")): item.get("reason")
            for item in summary.get("skipped", [])
        }
        no_data = {str(name) for name in summary.get("no_data", [])}
        window_end = summary.get("window_end")
        records: list[dict[str, Any]] = [{
            "data_type": SYNC_STATE_ROUND,
            "status": str(summary.get("status") or "failed"),
            "window_end": window_end,
            # 整轮原因最多留三条，避免库里堆全量文本。
            "error": "；".join(
                f"{item.get('stage')}[{item.get('kind')}]: {item.get('reason')}"
                for item in summary.get("skipped", [])[:3]
            ) or None,
        }]
        for name in DATA_CLASSES:
            reason = reasons.get(name) or reasons.get(f"{name}:write")
            if reason:
                status, error = "failed", reason
            elif name in no_data:
                status, error = NO_DATA_STATUS, NO_DATA_NOTE
            else:
                status, error = "ok", None
            records.append({
                "data_type": name,
                "status": status,
                "window_end": window_end,
                "error": error,
            })
        try:
            await asyncio.to_thread(self.store.record_sync_states, records)
        except Exception as error:  # noqa: BLE001 - 状态记录失败不影响本轮结论
            self.logger.warning("同步：写同步状态失败（%s）", _err(error))

    # ── 单类取数 + 写入 ──────────────────────────────────────────────────
    def _write_training(self, rows: Iterable[dict]) -> int:
        """训练会话写库：先按统一判据打碎片标记（is_fragment），再直写存储层。

        碎片只标记、不删除，行数与读数一并保留；标记口径与命令展示 / LLM 摘要 /
        运动后关怀完全一致（storage/models.is_valid_training，阈值来自配置）。
        """
        marked = models.mark_training_fragments(
            rows,
            min_duration_min=self.training_min_duration_min,
            min_distance_m=self.training_min_distance_m,
        )
        return self.store.upsert_rows("training_session", marked)

    @staticmethod
    def _skip(summary: dict[str, Any], stage: str, kind: str,
              error: BaseException) -> None:
        """记一条跳过原因（含分类键，供上层分流）。"""
        summary["skipped"].append(
            {"stage": stage, "kind": kind, "reason": _err(error)})

    async def _pull(
        self,
        name: str,
        fetch: Callable[[], Any],
        write: Callable[[Any], int],
        summary: dict[str, Any],
    ) -> None:
        """取一类数据并写入；失败只记跳过原因，不影响其他类别。

        三条路径互不混淆：
          * 抛门面三类异常（或别的异常）→ 记 ``summary["skipped"]``（含 kind，供上层分流）；
          * 取数成功但返回空 → 记 ``summary["no_data"]``（上层标注「无」）；
          * 取到行但一行都没入库（日汇总会丢掉「当天没有聚合行」的日期）→ 同样记
            ``no_data``：不能记成 ok，否则这一天会静默消失。
        """
        try:
            rows = await fetch()
        except Exception as error:  # noqa: BLE001 - 单类失败不能打断整轮
            kind = _kind_of(error)
            self._skip(summary, name, kind, error)
            self.logger.warning(
                "同步：%s 取数失败（%s），%s",
                name, _err(error), KIND_NOTES.get(kind, KIND_NOTES[KIND_OTHER]))
            return
        if not rows:
            summary["no_data"].append(name)
            self.logger.info("同步：%s 云端无数据（标注「无」）", name)
            return
        try:
            written = await asyncio.to_thread(write, rows)
        except Exception as error:  # noqa: BLE001 - 单类写库失败不致命
            self._skip(summary, f"{name}:write", KIND_OTHER, error)
            self.logger.warning("同步：%s 写库失败（%s）", name, _err(error))
            return
        if int(written or 0) <= 0:
            summary["no_data"].append(name)
            self.logger.warning(
                "同步：%s 取到 %d 行但一行都没入库（按「无数据」标注，不记成功）",
                name, len(rows))
            return
        summary["written"][name] = int(written or 0)

    async def _pull_health(self, start: date, end: date,
                           summary: dict[str, Any]) -> None:
        """心率 / 睡眠 / 压力 / 血氧：四类各自取数、各自落库。

        四类共用门面里同一次 health 响应；某类云端没给读数时只有该类空（标注「无」），
        其余类别照常取数与落库。门面返回的是已归一化的行，走 ``store.upsert_rows``
        直写（存储层不再做第二次归一）。
        """
        for name, model in HEALTH_CLASSES:
            await self._pull(
                name,
                partial(getattr(self.facade, f"iter_{name}"), start, end),
                partial(self.store.upsert_rows, model),
                summary,
            )
