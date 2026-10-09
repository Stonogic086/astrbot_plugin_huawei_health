"""华为运动健康插件 —— schema 版本迁移（前向、按序、幂等、可回退）。

本模块负责「把任意版本的库升到当前 SCHEMA_VERSION」：读版本、按序跑迁移链、落版本号、
必要时（由调用方 health_store 触发）先备份副本。不 import 第三方库，也不 import astrbot。

════════════════════════════════════════════════════════════════════
迁移链（版本号 = 该步执行完成后的 schema 版本）
════════════════════════════════════════════════════════════════════
    v1  基线：六张业务表（daily_activity / heart_rate_sample / sleep_session /
        stress_sample / spo2_sample / training_session）+ sleep_session 三个新增列
        （fall_asleep_local / wakeup_local / nap_duration_min）+ 训练会话索引；
    v2  同步状态与元信息：sync_state（每类数据最近一次尝试/成功/状态/失败原因）、
        meta（库属主、纳入版本管理时刻、最近迁移时刻、最近备份文件）。
    v3  主动关怀：care_send_log（发送记录）、care_event_key（事件去重键）、
        care_scenario_state（各场景冷却状态）、care_owner_activity（所有者最近私聊活动）；
        四张表全部按 owner 隔离（owner_id = 已绑定的所有者私聊会话 UMO）。
    v4  训练碎片标记：training_session 新增 is_fragment 列（1=碎片、0=有效训练），
        旧行按 storage/models.is_valid_training 的同一判据回填；只标记、不删除原始数据。

判定与保证
════════════════════════════════════════════════════════════════════
  * 库里没有 ``schema_version`` 表 → 视为 v0：既覆盖「旧库」也覆盖「新库」，
    新库由 initialize() 把 1..N 依次跑完，一次建到 SCHEMA_VERSION；
  * 版本号高于 SCHEMA_VERSION（插件被降级）→ 抛 ``MigrationError`` 拒绝打开，
    绝不做「静默降级」或「按当前代码猜着用」；
  * 每一步都在一个显式事务里执行（``BEGIN IMMEDIATE`` → DDL/DML → 落版本号 → commit），
    失败即 ``rollback`` 并抛 ``MigrationError``：不留半迁移状态、不静默继续；
    调用方（health_store.initialize）在真正需要升级前已生成备份副本，故原库可回退；
  * 每一步的前置条件都用 ``sqlite_master`` / ``PRAGMA table_info`` 查过再执行，
    重复运行无副作用（幂等）；
  * 迁移链按版本号升序执行（``MIGRATIONS`` 的声明顺序不影响结果），版本号必须唯一；
  * 版本号只在对应的迁移步骤**成功后**写入，所以「跑一半崩了」下次会从同一版本重跑。
"""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

from . import models, schema

_LOGGER = logging.getLogger(__name__)

# 备份副本必须是可用的 SQLite 库：先看文件头，再 quick_check。
SQLITE_HEADER = b"SQLite format 3\x00"


class StorageError(RuntimeError):
    """存储层初始化类错误（迁移 / 备份）的基类。"""


class MigrationError(StorageError):
    """schema 迁移失败或库版本不受支持；库应保持迁移前状态。"""


class BackupError(StorageError):
    """升级前备份失败；调用方必须停止初始化，不得继续迁移。"""


# ════════════════════════════════════════════════════════════════════
# 迁移步骤
# ════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Migration:
    """一步迁移：把库从 version-1 升到 version。"""

    version: int
    name: str
    apply: Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class MigrationOutcome:
    """一次迁移执行结果（供日志与自检）。"""

    from_version: int
    to_version: int
    applied: tuple[str, ...] = ()

    @property
    def migrated(self) -> bool:
        """本次是否真的跑了迁移步骤。"""
        return bool(self.applied)


# ── 幂等小工具 ───────────────────────────────────────────────────────
def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    """表是否存在（含视图外的普通表）。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,)).fetchone()
    return row is not None


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """表的列名集合；表不存在时返回空集合。"""
    if not table_exists(conn, table):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def add_missing_columns(conn: sqlite3.Connection, table: str,
                        columns: Iterable[tuple[str, str]]) -> list[str]:
    """给已存在的表补缺失列（``ALTER TABLE ADD COLUMN``），返回本次补上的列名。

    只补缺列：列已存在就跳过，因此可重复执行；新增列一律可空，旧行原样保留（新列 NULL）。
    """
    existing = table_columns(conn, table)
    added: list[str] = []
    for column, declared in columns:
        if column in existing:
            continue
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declared}")
        added.append(column)
    return added


def _create_tables(conn: sqlite3.Connection, ddl_statements: Iterable[str]) -> None:
    """按序执行一次一条 DDL（不用 executescript：它会先 COMMIT，破坏迁移的事务性）。"""
    for ddl in ddl_statements:
        conn.execute(ddl)


# ── 各步实现 ─────────────────────────────────────────────────────────
def _v1_baseline(conn: sqlite3.Connection) -> None:
    """v1 基线：六张业务表 + sleep_session 新增列 + 训练会话索引。"""
    _create_tables(conn, schema.BASELINE_TABLES)
    _create_tables(conn, schema.BASELINE_INDEXES)
    add_missing_columns(conn, "sleep_session", schema.SLEEP_ADDED_COLUMNS)


def _v2_sync_state(conn: sqlite3.Connection) -> None:
    """v2 同步状态与元信息：sync_state、meta。"""
    _create_tables(conn, schema.SYNC_STATE_TABLES)


def _v3_care(conn: sqlite3.Connection) -> None:
    """v3 主动关怀：发送记录 / 事件去重键 / 各场景冷却状态 / 所有者最近私聊活动。"""
    _create_tables(conn, schema.CARE_TABLES)


def _v4_training_fragment(conn: sqlite3.Connection) -> None:
    """v4 训练碎片标记：training_session 补 is_fragment 列，旧行按同一判据回填。

    判据只有一处实现（``storage/models.is_valid_training``，默认「时长 ≥ 3 分钟或
    距离 ≥ 300 米」）；碎片行只标记、不删除，原始读数一律保留。
    """
    _create_tables(conn, (schema.TRAINING_DDL,))
    add_missing_columns(conn, "training_session", schema.TRAINING_ADDED_COLUMNS)
    rows = conn.execute(
        "SELECT session_key, duration_min, distance_m FROM training_session").fetchall()
    updates = [
        (0 if models.is_valid_training(duration, distance) else 1, key)
        for key, duration, distance in rows
    ]
    if updates:
        conn.executemany(
            "UPDATE training_session SET is_fragment = ? WHERE session_key = ?",
            updates)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, "基线：六张业务表 + sleep_session 新增列 + 训练会话索引", _v1_baseline),
    Migration(2, "同步状态与元信息：sync_state / meta", _v2_sync_state),
    Migration(3, "主动关怀：发送记录 / 事件去重键 / 各场景冷却状态 / 所有者最近私聊活动",
              _v3_care),
    Migration(4, "训练碎片标记：training_session 新增 is_fragment 列并回填旧行",
              _v4_training_fragment),
)


# ════════════════════════════════════════════════════════════════════
# 版本读写
# ════════════════════════════════════════════════════════════════════
def read_version(conn: sqlite3.Connection) -> int | None:
    """读当前 schema 版本；没有版本表（或版本表为空）→ None。

    只读，不建表、不写库。返回 None 表示「未版本化的库」，由 ``current_version`` 折算成 0。
    """
    if not table_exists(conn, schema.VERSION_TABLE):
        return None
    row = conn.execute(
        f"SELECT version FROM {schema.VERSION_TABLE} LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def current_version(conn: sqlite3.Connection) -> int:
    """当前 schema 版本：旧库 / 新库（无版本表）算 0。

    版本号高于 ``schema.SCHEMA_VERSION``（插件被降级）时抛 ``MigrationError``：
    明确拒绝打开，不猜、不改。
    """
    version = read_version(conn)
    if version is None:
        return 0
    if version > schema.SCHEMA_VERSION:
        raise MigrationError(
            f"本地库 schema 版本 v{version} 高于当前插件支持的 v{schema.SCHEMA_VERSION}，"
            "已拒绝打开（不做降级迁移）；请升级插件或改用新版库的备份副本")
    return version


def needs_upgrade(conn: sqlite3.Connection) -> bool:
    """是否需要迁移（当前版本 < SCHEMA_VERSION）。降级情况会抛 MigrationError。"""
    return current_version(conn) < schema.SCHEMA_VERSION


# ════════════════════════════════════════════════════════════════════
# 迁移执行
# ════════════════════════════════════════════════════════════════════
def apply(conn: sqlite3.Connection, *, logger: logging.Logger | None = None,
          backup_path: Path | None = None) -> MigrationOutcome:
    """把库升到 ``schema.SCHEMA_VERSION``（幂等）。返回本次执行结果。

    参数：
        conn        —— 已连上的库连接（调用方负责关）；
        logger      —— 可选 logger；
        backup_path —— 本次升级前生成的备份副本路径，只用于写进 meta（可空）。

    失败时抛 ``MigrationError``（已回滚该步，库停在失败前那个版本）。
    """
    log = logger or _LOGGER
    conn.execute(schema.VERSION_DDL)
    start = current_version(conn)          # 降级会在这里抛 MigrationError
    if read_version(conn) is None:         # 新库 / 旧库：先落一条版本行（v0 = 未版本化）
        conn.execute(
            f"INSERT INTO {schema.VERSION_TABLE}(version) VALUES (?)", (start,))
        conn.commit()                      # 结束隐式事务，下面才能显式 BEGIN

    applied: list[str] = []
    version = start
    # 按版本号升序执行：MIGRATIONS 的声明顺序不影响结果（版本号必须唯一）。
    for migration in sorted(MIGRATIONS, key=lambda item: item.version):
        if migration.version <= version:
            continue
        try:
            conn.execute("BEGIN IMMEDIATE")
            migration.apply(conn)
            conn.execute(
                f"UPDATE {schema.VERSION_TABLE} SET version = ?", (migration.version,))
            conn.commit()
        except Exception as error:  # noqa: BLE001 - 任何一步失败都必须显式停下
            conn.rollback()
            raise MigrationError(
                f"schema 迁移失败：v{version} → v{migration.version}"
                f"（{migration.name}），已回滚，库仍是 v{version}："
                f"{type(error).__name__}: {error}") from error
        version = migration.version
        applied.append(f"v{migration.version} {migration.name}")
        log.info("schema 迁移完成：v%d %s", migration.version, migration.name)

    if applied:
        _touch_meta(conn, backup_path=backup_path)
        conn.commit()
    return MigrationOutcome(start, version, tuple(applied))


def _touch_meta(conn: sqlite3.Connection, *, backup_path: Path | None) -> None:
    """写库的元信息（meta 表存在时才写；只记时间与文件名，不记任何健康数值）。"""
    if not table_exists(conn, schema.META_TABLE):
        return
    now = schema.local_now()
    _set_meta(conn, schema.META_KEY_PLUGIN, schema.PLUGIN_ID)
    # 首次纳入版本管理的时刻只写一次（旧库=首次迁移时刻）。
    conn.execute(
        f"INSERT INTO {schema.META_TABLE}(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO NOTHING",
        (schema.META_KEY_SCHEMA_CREATED_AT, now, now))
    _set_meta(conn, schema.META_KEY_SCHEMA_UPGRADED_AT, now)
    if backup_path is not None:
        _set_meta(conn, schema.META_KEY_LAST_BACKUP_FILE, Path(backup_path).name)
        _set_meta(conn, schema.META_KEY_LAST_BACKUP_AT, now)


def _set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        f"INSERT INTO {schema.META_TABLE}(key, value, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
        "updated_at = excluded.updated_at",
        (key, value, schema.local_now()))


# ════════════════════════════════════════════════════════════════════
# 升级前备份
# ════════════════════════════════════════════════════════════════════
def backup_path_for(db_path: Path, *, moment: datetime | None = None) -> Path:
    """算出备份副本路径：同目录 ``<库名>.backup-YYYYMMDD-HHMMSS.db``。

    同一秒内重复调用时依次加 ``-2``、``-3``… 后缀，绝不覆盖已有备份。
    """
    stamp = (moment or datetime.now()).strftime(schema.BACKUP_STAMP_FORMAT)
    directory = Path(db_path).parent
    stem = Path(db_path).stem
    suffix = Path(db_path).suffix
    candidate = directory / f"{stem}.backup-{stamp}{suffix}"
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = directory / f"{stem}.backup-{stamp}-{counter}{suffix}"
    return candidate


def backup_database(db_path: Path, *, moment: datetime | None = None) -> Path:
    """原地升级前把库整份复制成备份副本，返回副本路径。

    用 sqlite3 的 backup API（一致性快照，不依赖文件系统复制语义），生成后校验：
    文件头是 SQLite、``PRAGMA quick_check`` 为 ok、权限收 0600。任何一步失败都删掉半成品
    并抛 ``BackupError``——调用方据此停止迁移，原库不受影响。
    """
    db_path = Path(db_path)
    if not db_path.exists():
        raise BackupError(f"备份失败：库文件不存在：{db_path}")
    target = backup_path_for(db_path, moment=moment)
    try:
        source = sqlite3.connect(str(db_path), timeout=30)
        try:
            destination = sqlite3.connect(str(target), timeout=30)
            try:
                with destination:
                    source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()
    except (OSError, sqlite3.Error) as error:
        _discard(target)
        raise BackupError(
            f"备份失败，已停止迁移（原库未改动）：{type(error).__name__}: {error}"
        ) from error
    try:
        _verify_backup(target)
    except (OSError, sqlite3.Error, ValueError) as error:
        _discard(target)
        raise BackupError(
            f"备份校验失败，已停止迁移（原库未改动）：{type(error).__name__}: {error}"
        ) from error
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


def _verify_backup(path: Path) -> None:
    """校验备份副本是可用且完整的 SQLite 库：文件头 + quick_check。"""
    if not path.exists() or path.stat().st_size == 0:
        raise ValueError(f"备份副本为空：{path}")
    with open(path, "rb") as handle:
        if handle.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise ValueError(f"备份副本不是 SQLite 库：{path}")
    probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        result = probe.execute("PRAGMA quick_check").fetchone()
    finally:
        probe.close()
    if not result or str(result[0]).lower() != "ok":
        raise ValueError(f"备份副本完整性检查未通过：{result!r}")


def _discard(path: Path) -> None:
    """删掉失败时留下的半成品备份（只删我们自己刚创建的副本）。"""
    try:
        path.unlink()
    except OSError:
        pass


__all__ = [
    "StorageError",
    "MigrationError",
    "BackupError",
    "Migration",
    "MigrationOutcome",
    "MIGRATIONS",
    "apply",
    "read_version",
    "current_version",
    "needs_upgrade",
    "table_exists",
    "table_columns",
    "add_missing_columns",
    "backup_database",
    "backup_path_for",
]
