"""华为运动健康插件 —— 存储层门面（SQLite，纯标准库 sqlite3）。

职责：
  * 建库建表 + schema 版本迁移（表结构见 schema.py，迁移链见 migrations.py）：
    新库一次建到最新版本；旧库原地无损升级，升级前先生成备份副本；
  * 各数据模型的幂等写入（唯一约束 + ON CONFLICT ... DO UPDATE：本次同步缺的字段保留库中
    旧值，不会把已有值写成 NULL）；
  * 同步状态（sync_state）与库元信息（meta）读写；
  * 按「模型 + 日期区间」查询。

设计约定：
  * 不 import 第三方库、不 import homeassistant、不 import astrbot（框架无关）；
  * 时间按本地时间 CST 存文本：日期 'YYYY-MM-DD'，时间 'YYYY-MM-DD HH:MM:SS'；
  * 每次操作开/关一个连接（写入量低，简单且线程安全）；
  * 表结构、字段口径与模型映射只在 storage/schema.py 一处定义，本模块不再写 DDL；
  * BodyMeasurement 已下线：不建表、不提供接口。

默认库文件（框架惯例的插件持久化位置，禁止写进插件源码目录）：
    /vol1/@appdata/astrbot/data/plugin_data/astrbot_plugin_huawei_health/health.db
"""

from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

from . import migrations, models, schema

_LOGGER = logging.getLogger(__name__)

DEFAULT_DB_PATH = (
    "/vol1/@appdata/astrbot/data/plugin_data/astrbot_plugin_huawei_health/health.db"
)
_CREATED_AT = "_created_at"

# 兼容既有导出：模型名 → (表名, 日期列)。
MODEL_TABLES = schema.MODEL_TABLES

# 同步状态表的列（写入顺序，与 sync_state DDL 对应）。
_SYNC_STATE_COLUMNS = (
    "data_type", "last_attempt_at", "last_success_at", "last_status",
    "last_window_end", "last_error", "updated_at")


def _now() -> str:
    return schema.local_now()


class HealthStore:
    """华为运动健康 SQLite 存储门面。只需一个 db 路径即可工作。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else Path(DEFAULT_DB_PATH)

    # ── 生命周期 ─────────────────────────────────────────────────────────
    def initialize(self) -> Path:
        """建目录 + （旧库先备份）+ schema 版本迁移 + 收 0600，返回库文件路径。

        迁移/备份失败一律抛 ``migrations.MigrationError`` / ``migrations.BackupError``：
        显式报错并停下，库保持迁移前状态（每步迁移在事务里，失败即回滚；升级前已生成备份副本）。
        """
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path: Path | None = None
        if self._has_existing_data():
            with self._connect() as conn:
                start = migrations.current_version(conn)  # 降级会在这里显式报错
            if start < schema.SCHEMA_VERSION:
                backup_path = migrations.backup_database(self.db_path)
                # 只记副本路径，不记任何健康数值。
                _LOGGER.info("schema 升级前已备份库：%s", backup_path)
        with self._connect() as conn:
            outcome = migrations.apply(
                conn, logger=_LOGGER, backup_path=backup_path)
        if outcome.migrated:
            _LOGGER.info(
                "schema 已迁移：v%d → v%d（%s）",
                outcome.from_version, outcome.to_version,
                "；".join(outcome.applied))
        # 库文件是未加密的健康数据缓存，建库后显式收权限（失败不影响功能）。
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        return self.db_path

    def _has_existing_data(self) -> bool:
        """库文件已存在且非空才算「旧库」（空文件视同新库，不需要备份）。"""
        try:
            return self.db_path.exists() and self.db_path.stat().st_size > 0
        except OSError:
            return False

    def schema_version(self) -> int:
        """读当前 schema 版本（迁移链跑到的版本）。"""
        with self._connect() as conn:
            return migrations.current_version(conn)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── 写入（幂等）──────────────────────────────────────────────────────
    def upsert_daily_activity(self, records: Iterable[dict]) -> int:
        """写日汇总；同一天多条时聚合行（sportType 0/NULL）优先。返回写入行数。

        降级策略（与协议层「聚合行才是当日汇总」的定稿一致，避免静默写入偏小的错误数据）：
          * 该日有聚合行 → 只写聚合行，分项行（某一种运动的记录）一律丢弃；
          * 该日**没有**聚合行（sportType 非 0/NULL）→ **跳过该日**：分项行只是单种运动
            的记录，拿第一条分项行当当日汇总会让步数 / 距离 / 卡路里整体偏小；
          * 选中行一个有效读数都没有（全 0 / 缺失）→ 同样跳过，不写全空的壳行
            （models.has_daily_reading，与 health 家族同一套护栏）。
        跳过的日期各记一条 warning 日志（不静默），便于与云端返回对照排查。
        """
        rows = models.normalize_daily_activity(records)
        chosen: dict[str, dict] = {}
        for row in rows:
            existing = chosen.get(row["date"])
            if existing is None or (
                row.get("sport_type") in (0, None)
                and existing.get("sport_type") not in (0, None)
            ):
                chosen[row["date"]] = row
        selected: list[dict] = []
        no_aggregate: list[str] = []
        no_reading: list[str] = []
        for date, row in sorted(chosen.items()):
            if row.get("sport_type") not in (0, None):
                no_aggregate.append(date)
            elif not models.has_daily_reading(row):
                no_reading.append(date)
            else:
                selected.append(row)
        if no_aggregate:
            _LOGGER.warning(
                "日汇总跳过 %d 天（云端未给聚合行，不用分项行充当当日汇总）：%s",
                len(no_aggregate), ", ".join(no_aggregate[:10]))
        if no_reading:
            _LOGGER.warning(
                "日汇总跳过 %d 天（聚合行读数全空）：%s",
                len(no_reading), ", ".join(no_reading[:10]))
        return self._write_rows("daily_activity", selected)

    def upsert_rows(self, model: str, rows: Iterable[dict]) -> int:
        """写入「已归一化」的行（取数门面已按 storage/models 口径整形），返回写入行数。

        取数门面（``adapters.huawei_health_facade``）返回的正是归一后的行，本方法就是那一条
        直写通道：本层不再做任何归一，少一道归一 = 不会二次整形丢数据；主键冲突时逐列
        COALESCE 保留旧值的幂等语义与 ``upsert_daily_activity`` 完全一致。
        """
        if model not in schema.MODEL_SPECS:
            raise KeyError(f"未知模型：{model}")
        return self._write_rows(model, rows)

    def _write_rows(self, model: str, rows: Iterable[dict]) -> int:
        """按主键 upsert：冲突时逐列 COALESCE(excluded, 旧值)。

        同一 date 重复同步结果一致；本次行里缺的字段（None）保留库中旧值，绝不把已有值
        覆盖成 NULL。列顺序取自 storage/schema.py 的 MODEL_SPECS。
        """
        spec = schema.MODEL_SPECS[model]
        payload = []
        for row in rows:
            payload.append([
                row.get(col, _now() if col == "updated_at" else None)
                for col in spec.columns])
        if not payload:
            return 0
        placeholders = ", ".join("?" for _ in spec.columns)
        names = ", ".join(spec.columns)
        updates = ", ".join(
            f"{col} = COALESCE(excluded.{col}, {spec.table}.{col})"
            for col in spec.columns if col != spec.key_column)
        statement = (
            f"INSERT INTO {spec.table} ({names}) VALUES ({placeholders}) "
            f"ON CONFLICT({spec.key_column}) DO UPDATE SET {updates}")
        with self._connect() as conn:
            conn.executemany(statement, payload)
        return len(payload)

    # ── 同步状态（sync_state）────────────────────────────────────────────
    def record_sync_states(self, records: Iterable[dict]) -> int:
        """写同步状态（每个 data_type 一行，主键=data_type）。返回写入行数。

        入参 record 口径：
            data_type   必填，数据类别（六类数据名，或整轮同步的汇总键）；
            status      必填，取值 ok / partial / failed / skipped；
            window_end  可选，本轮同步覆盖窗口的上界（本地日期 'YYYY-MM-DD'）；
            error       可选，失败原因单行文本（异常类型 + 原因；不得含凭据与健康数值）；
            when        可选，本次时刻文本，缺省当前时间。

        列更新语义：
            last_attempt_at  每次调用都刷新；
            last_success_at  仅 status='ok' 时刷新，否则保留旧值（不会把「上次成功」抹掉）；
            last_status      每次覆盖；
            last_window_end  给了就覆盖，没给保留旧值；
            last_error       每次覆盖（成功了传 None，等于清掉上次失败原因）。

        缺失 data_type / status，或 status 不在约定取值内的行直接抛 ValueError（显式报错，
        不写可疑数据）。
        """
        payload = []
        for record in records or []:
            data_type = str(record.get("data_type") or "").strip()
            status = str(record.get("status") or "").strip()
            if not data_type:
                raise ValueError(f"同步状态行缺 data_type：{record!r}")
            if status not in schema.SYNC_STATUS_VALUES:
                raise ValueError(
                    f"同步状态行 status 非法（{status!r}），"
                    f"允许值：{'/'.join(schema.SYNC_STATUS_VALUES)}")
            when = str(record.get("when") or _now())
            window_end = record.get("window_end")
            error = record.get("error")
            payload.append([
                data_type,
                when,
                when if status == "ok" else None,
                status,
                None if window_end is None else str(window_end),
                None if error is None else str(error),
                when,
            ])
        if not payload:
            return 0
        placeholders = ", ".join("?" for _ in _SYNC_STATE_COLUMNS)
        names = ", ".join(_SYNC_STATE_COLUMNS)
        statement = (
            f"INSERT INTO {schema.SYNC_STATE_TABLE} ({names}) VALUES ({placeholders}) "
            "ON CONFLICT(data_type) DO UPDATE SET "
            "last_attempt_at = excluded.last_attempt_at, "
            "last_success_at = COALESCE(excluded.last_success_at, "
            f"{schema.SYNC_STATE_TABLE}.last_success_at), "
            "last_status = excluded.last_status, "
            "last_window_end = COALESCE(excluded.last_window_end, "
            f"{schema.SYNC_STATE_TABLE}.last_window_end), "
            "last_error = excluded.last_error, "
            "updated_at = excluded.updated_at")
        with self._connect() as conn:
            conn.executemany(statement, payload)
        return len(payload)

    def sync_state(self, data_type: str | None = None) -> Any:
        """读同步状态：给了 data_type 返回单行 dict（没有该行 → None），否则返回全部行。

        不存在的 data_type 同样返回 None（不抛错）：调用方按「从未同步过」处理。
        """
        with self._connect() as conn:
            if data_type is not None:
                row = conn.execute(
                    f"SELECT * FROM {schema.SYNC_STATE_TABLE} WHERE data_type = ?",
                    (str(data_type),)).fetchone()
                return dict(row) if row else None
            return [
                dict(row) for row in conn.execute(
                    f"SELECT * FROM {schema.SYNC_STATE_TABLE} ORDER BY data_type")
            ]

    # ── 主动关怀（care_*）────────────────────────────────────────────────
    def touch_owner_activity(self, owner_id: Any, session: Any = None,
                             when: Any = None) -> None:
        """记一次所有者私聊活动（夜间场景「近期确有私聊活动」的唯一证据）。

        owner_id 为空则直接忽略（不写可疑行）。同一 owner 只保留最近一次时刻。
        """
        owner = str(owner_id or "").strip()
        if not owner:
            return
        now = str(when or _now())
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO care_owner_activity"
                " (owner_id, session, last_seen_at, updated_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(owner_id) DO UPDATE SET"
                " session = excluded.session,"
                " last_seen_at = excluded.last_seen_at,"
                " updated_at = excluded.updated_at",
                (owner, str(session or owner), now, now))

    def last_owner_activity(self, owner_id: Any) -> dict | None:
        """读某 owner 最近一次私聊活动；没有则 None。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT owner_id, session, last_seen_at FROM care_owner_activity"
                " WHERE owner_id = ?", (str(owner_id or ""),)).fetchone()
        return dict(row) if row else None

    def care_event_seen(self, owner_id: Any, scenario: str,
                        event_key: str) -> bool:
        """该 (owner, 场景, 事件键) 是否已处理过（去重判定）。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM care_event_key"
                " WHERE owner_id = ? AND scenario = ? AND event_key = ?",
                (str(owner_id or ""), str(scenario), str(event_key))).fetchone()
        return row is not None

    def mark_care_event(self, owner_id: Any, scenario: str, event_key: str,
                        when: Any = None) -> bool:
        """记录事件去重键；返回 True=本次新插入（尚未处理过），False=已存在。

        并发/重复调用只会插入一次（主键冲突被忽略），是「同一事件只处理一次」的落点。
        """
        self._require_scenario(scenario)
        owner = str(owner_id or "").strip()
        if not owner or not str(event_key or "").strip():
            raise ValueError("事件去重键行缺 owner_id 或 event_key")
        with self._connect() as conn:
            cursor = conn.execute(
                "INSERT OR IGNORE INTO care_event_key"
                " (owner_id, scenario, event_key, created_at) VALUES (?, ?, ?, ?)",
                (owner, str(scenario), str(event_key), str(when or _now())))
            return cursor.rowcount > 0

    def last_care_send_at(self, owner_id: Any,
                          scenario: str | None = None) -> str | None:
        """最近一次关怀「占冷却」时刻（给了 scenario 就按场景过滤）；没有则 None。"""
        sql = (f"SELECT MAX(sent_at) FROM {schema.CARE_SEND_LOG_TABLE}"
               " WHERE owner_id = ?")
        params: list[Any] = [str(owner_id or "")]
        if scenario is not None:
            sql += " AND scenario = ?"
            params.append(str(scenario))
        with self._connect() as conn:
            row = conn.execute(sql, params).fetchone()
        return None if row is None else row[0]

    def care_send_count_since(self, owner_id: Any, since: str,
                              scenario: str | None = None) -> int:
        """自 ``since``（含）以来该 owner 的关怀发送行数（按场景可选过滤）。"""
        sql = (f"SELECT COUNT(*) FROM {schema.CARE_SEND_LOG_TABLE}"
               " WHERE owner_id = ? AND sent_at >= ?")
        params: list[Any] = [str(owner_id or ""), str(since)]
        if scenario is not None:
            sql += " AND scenario = ?"
            params.append(str(scenario))
        with self._connect() as conn:
            return int(conn.execute(sql, params).fetchone()[0])

    def record_care_send(self, owner_id: Any, scenario: str, event_key: Any = None,
                         delivery: str = "reserved", when: Any = None) -> int:
        """写一条发送记录（默认 reserved=已占冷却、待确认送达）。返回自增行号。"""
        self._require_scenario(scenario)
        self._require_delivery(delivery)
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("发送记录缺 owner_id")
        now = str(when or _now())
        key = None if event_key is None else str(event_key)
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT INTO {schema.CARE_SEND_LOG_TABLE}"
                " (owner_id, scenario, event_key, sent_at, delivery, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (owner, str(scenario), key, now, str(delivery), now))
            return int(cursor.lastrowid or 0)

    def confirm_care_send(self, owner_id: Any, scenario: str, event_key: Any = None,
                          when: Any = None) -> int:
        """把一条已占冷却的记录升级为「已确认送达」。返回被更新的行数。"""
        self._require_scenario(scenario)
        sql = (f"UPDATE {schema.CARE_SEND_LOG_TABLE} SET delivery = 'sent',"
               " updated_at = ? WHERE owner_id = ? AND scenario = ?"
               " AND delivery = 'reserved'")
        params: list[Any] = [str(when or _now()), str(owner_id or ""), str(scenario)]
        if event_key is not None:
            sql += " AND event_key = ?"
            params.append(str(event_key))
        with self._connect() as conn:
            return int(conn.execute(sql, params).rowcount)

    def care_scenario_state(self, owner_id: Any, scenario: str) -> dict | None:
        """读某 owner 某场景的冷却状态；没有则 None。"""
        self._require_scenario(scenario)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {schema.CARE_SCENARIO_STATE_TABLE}"
                " WHERE owner_id = ? AND scenario = ?",
                (str(owner_id or ""), str(scenario))).fetchone()
        return dict(row) if row else None

    def set_care_scenario_state(self, owner_id: Any, scenario: str,
                                last_sent_at: Any = None,
                                last_event_key: Any = None,
                                when: Any = None) -> None:
        """写某 owner 某场景的冷却状态（ON CONFLICT 覆盖给定列，未给的列保留旧值）。"""
        self._require_scenario(scenario)
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("冷却状态行缺 owner_id")
        now = str(when or _now())
        stamp = None if last_sent_at is None else str(last_sent_at)
        key = None if last_event_key is None else str(last_event_key)
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO {schema.CARE_SCENARIO_STATE_TABLE}"
                " (owner_id, scenario, last_sent_at, last_event_key, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(owner_id, scenario) DO UPDATE SET"
                " last_sent_at = COALESCE(excluded.last_sent_at,"
                f" {schema.CARE_SCENARIO_STATE_TABLE}.last_sent_at),"
                " last_event_key = COALESCE(excluded.last_event_key,"
                f" {schema.CARE_SCENARIO_STATE_TABLE}.last_event_key),"
                " updated_at = excluded.updated_at",
                (owner, str(scenario), stamp, key, now))

    @staticmethod
    def _require_scenario(scenario: Any) -> None:
        value = str(scenario or "")
        if value not in schema.CARE_SCENARIOS:
            raise ValueError(
                f"关怀场景非法（{scenario!r}），允许值：{'/'.join(schema.CARE_SCENARIOS)}")

    @staticmethod
    def _require_delivery(delivery: Any) -> None:
        value = str(delivery or "")
        if value not in schema.CARE_DELIVERY_VALUES:
            raise ValueError(
                f"关怀投递状态非法（{delivery!r}），允许值："
                f"{'/'.join(schema.CARE_DELIVERY_VALUES)}")

    # ── 元信息（meta）──────────────────────────────────────────────────
    def metadata(self) -> dict[str, str]:
        """读库元信息（键按字母序）。键的口径见 storage/schema.py 的 META_KEY_*。"""
        with self._connect() as conn:
            return {
                row["key"]: row["value"] for row in conn.execute(
                    f"SELECT key, value FROM {schema.META_TABLE} ORDER BY key")
            }

    def get_metadata(self, key: str) -> str | None:
        """读单个元信息；键不存在返回 None。"""
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT value FROM {schema.META_TABLE} WHERE key = ?",
                (str(key),)).fetchone()
        return None if row is None else row["value"]

    # ── 查询 ─────────────────────────────────────────────────────────────
    def query(self, model: str, start: str | None = None,
              end: str | None = None) -> list[dict]:
        """按模型查行；给了 start/end 就按日期区间（含端点）过滤。"""
        if model not in schema.MODEL_SPECS:
            raise KeyError(f"未知模型：{model}")
        spec = schema.MODEL_SPECS[model]
        clauses: list[str] = []
        params: list[Any] = []
        if start:
            clauses.append(f"{spec.date_column} >= ?")
            params.append(start)
        if end:
            clauses.append(f"{spec.date_column} <= ?")
            params.append(end)
        sql = f"SELECT * FROM {spec.table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += f" ORDER BY {spec.date_column}"
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(sql, params).fetchall()]

    def get(self, model: str, key: str) -> dict | None:
        """按主键取单行（日期或 session_key）。"""
        if model not in schema.MODEL_SPECS:
            raise KeyError(f"未知模型：{model}")
        spec = schema.MODEL_SPECS[model]
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {spec.table} WHERE {spec.key_column} = ?",
                (key,)).fetchone()
        return dict(row) if row else None

    def count(self, model: str) -> int:
        """统计某模型行数。"""
        if model not in schema.MODEL_SPECS:
            raise KeyError(f"未知模型：{model}")
        with self._connect() as conn:
            return int(conn.execute(
                f"SELECT COUNT(*) FROM {schema.MODEL_SPECS[model].table}").fetchone()[0])

    def tables(self) -> list[str]:
        """列出当前库内的用户表（供自检确认 BodyMeasurement 未建表）。"""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        return [row[0] for row in rows]
