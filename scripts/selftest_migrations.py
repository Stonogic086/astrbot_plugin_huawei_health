#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""schema 迁移专项自检（离线，全部在 tempfile 临时目录里跑）。

覆盖：
    1. 新库一次建到最新版本；
    2. 迁移幂等（重复执行无副作用）；
    3. 迁移链按版本号顺序执行（声明乱序也不影响结果）；
    4. 旧库识别与无损升级（版本号补齐、数据原样保留）；
    5. 升级前备份：文件名带时间戳 / 内容等于升级前的库 / 权限 0600 / 同秒不覆盖；
    6. 备份失败 → BackupError，且不改库、不继续迁移；
    7. 迁移中途失败 → 显式报错 + 整步回滚（不留半迁移，原库可继续用）；
    8. 库版本高于插件 → 拒绝打开（不降级、不改库、不备份）。

不联网、不 import astrbot、不碰真实 health.db。

用法：
    python3 scripts/selftest_migrations.py
退出码：全部 PASS → 0；任一 FAIL → 1。
"""

from __future__ import annotations

import re
import sqlite3
import stat
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from storage import SCHEMA_VERSION, HealthStore, migrations, schema  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

# 备份副本文件名：<库名>.backup-YYYYMMDD-HHMMSS.db（同秒追加 -N）
BACKUP_NAME_RE = re.compile(r"^health\.backup-\d{8}-\d{6}(-\d+)?\.db$")

LEGACY_SLEEP_DDL = (
    "CREATE TABLE sleep_session ("
    " date TEXT PRIMARY KEY, duration_min REAL, score REAL, efficiency REAL,"
    " hrv REAL, spo2 REAL, source_type INTEGER NOT NULL DEFAULT 9,"
    " updated_at TEXT NOT NULL);")

LEGACY_ROWS = [
    ("2026-10-06", None, None),
    ("2026-10-07", None, None),
    ("2026-10-08", 388.0, 80.0),
]


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


# ── 小工具（只用标准库 sqlite3 直接看库，不经过存储层）─────────────────
def table_names(path: Path) -> list[str]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]


def dump_tables(path: Path) -> dict[str, list[tuple]]:
    """导出所有业务表内容（用于「备份=升级前的库」比对）。"""
    data: dict[str, list[tuple]] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for name in table_names(path):
            data[name] = conn.execute(f"SELECT * FROM {name}").fetchall()
    return data


def read_version(path: Path) -> int | None:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        if "schema_version" not in table_names(path):
            return None
        row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    return None if row is None else int(row[0])


def write_version(path: Path, value: int) -> None:
    with sqlite3.connect(str(path)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (value,))
        conn.commit()


def make_legacy_db(path: Path) -> None:
    """造一个「未版本化 + 有数据」的旧库（sleep_session 缺三个新增列）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as conn:
        conn.execute(LEGACY_SLEEP_DDL)
        conn.executemany(
            "INSERT INTO sleep_session"
            " (date, duration_min, score, source_type, updated_at)"
            " VALUES (?, ?, ?, 9, '2026-10-08 20:52:47')", LEGACY_ROWS)
        conn.commit()


def apply_chain(conn_path: Path, chain) -> object:
    """用给定迁移链跑一次 apply（替换模块级 MIGRATIONS，跑完恢复）。"""
    original = migrations.MIGRATIONS
    migrations.MIGRATIONS = chain
    try:
        conn = sqlite3.connect(str(conn_path))
        try:
            return migrations.apply(conn)
        finally:
            conn.close()
    finally:
        migrations.MIGRATIONS = original


def main() -> int:
    print("=" * 64)
    print("华为运动健康 —— schema 迁移自检")
    print("=" * 64)
    tmp_dir = Path(tempfile.mkdtemp(prefix="hwhealth_migrate_"))
    print(f"临时目录：{tmp_dir}")
    print(f"插件要求的 schema 版本：v{SCHEMA_VERSION}")

    # ── 1. 新库一次建到最新版本 ────────────────────────────────────────
    print("\n[1/8] 新库一次建到最新版本")
    fresh = tmp_dir / "fresh" / "health.db"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(fresh))
    try:
        outcome = migrations.apply(conn)
    finally:
        conn.close()
    fresh_tables = table_names(fresh)
    print(f"     迁移结果：v{outcome.from_version} → v{outcome.to_version}；{list(outcome.applied)}")
    check("从 v0 直接建到最新版本",
          outcome.from_version == 0 and outcome.to_version == SCHEMA_VERSION
          and read_version(fresh) == SCHEMA_VERSION,
          f"version={read_version(fresh)}")
    check("迁移链每一步都执行了（顺序=版本号升序）",
          [item.split()[0] for item in outcome.applied]
          == [f"v{m.version}" for m in sorted(migrations.MIGRATIONS, key=lambda x: x.version)],
          f"applied={list(outcome.applied)}")
    check("六张业务表 + 同步状态 + 元信息 + 版本表齐全",
          {"daily_activity", "heart_rate_sample", "sleep_session", "stress_sample",
           "spo2_sample", "training_session", "sync_state", "meta",
           "schema_version"} <= set(fresh_tables),
          f"tables={fresh_tables}")
    check("新库不生成备份（没有旧数据要保）",
          not list(fresh.parent.glob("*.backup-*.db")))

    # ── 2. 幂等 ────────────────────────────────────────────────────────
    print("\n[2/8] 迁移幂等（重复执行无副作用）")
    before_dump = dump_tables(fresh)
    conn = sqlite3.connect(str(fresh))
    try:
        second = migrations.apply(conn)
    finally:
        conn.close()
    check("已达最新版本 → 本次不执行任何步骤",
          second.applied == () and second.from_version == SCHEMA_VERSION
          and second.to_version == SCHEMA_VERSION,
          f"applied={list(second.applied)}")
    check("表结构与内容不变",
          table_names(fresh) == fresh_tables and dump_tables(fresh) == before_dump)
    with sqlite3.connect(str(fresh)) as probe:
        version_rows = probe.execute("SELECT version FROM schema_version").fetchall()
    check("版本表仍只有一行", version_rows == [(SCHEMA_VERSION,)], f"rows={version_rows}")

    # ── 3. 顺序无关：声明乱序也按版本号升序执行 ───────────────────────
    print("\n[3/8] 迁移链按版本号升序执行（声明乱序不影响结果）")
    shuffled_db = tmp_dir / "shuffled" / "health.db"
    shuffled_db.parent.mkdir(parents=True, exist_ok=True)
    by_version = {migration.version: migration for migration in migrations.MIGRATIONS}
    shuffled = tuple(by_version[v] for v in sorted(by_version, reverse=True))
    outcome = apply_chain(shuffled_db, shuffled)
    check("逆序声明仍建到最新版本",
          outcome.to_version == SCHEMA_VERSION
          and read_version(shuffled_db) == SCHEMA_VERSION,
          f"version={read_version(shuffled_db)}")
    check("逆序声明也建全了基线与 v2 的表",
          set(table_names(shuffled_db)) == set(fresh_tables),
          f"tables={table_names(shuffled_db)}")

    # ── 4. 旧库识别与无损升级 ──────────────────────────────────────────
    print("\n[4/8] 旧库（未版本化）识别与无损升级")
    legacy = tmp_dir / "legacy" / "health.db"
    make_legacy_db(legacy)
    before_rows = dump_tables(legacy)
    with sqlite3.connect(str(legacy)) as conn:
        before_columns = [row[1] for row in conn.execute("PRAGMA table_info(sleep_session)")]
    check("旧库被识别为未版本化（v0）",
          read_version(legacy) is None, f"tables={table_names(legacy)}")
    conn = sqlite3.connect(str(legacy))
    try:
        check("needs_upgrade(旧库) = True", migrations.needs_upgrade(conn) is True)
        check("current_version(旧库) = 0", migrations.current_version(conn) == 0)
    finally:
        conn.close()
    store = HealthStore(legacy)
    store.initialize()
    after_rows = store.query("sleep_session")
    with sqlite3.connect(str(legacy)) as conn:
        after_columns = [row[1] for row in conn.execute("PRAGMA table_info(sleep_session)")]
    print(f"     升级后列={after_columns}")
    check("旧库补上 sleep_session 三个新增列",
          {"fall_asleep_local", "wakeup_local", "nap_duration_min"} <= set(after_columns)
          and before_columns == [c for c in after_columns
                                 if c not in ("fall_asleep_local", "wakeup_local",
                                              "nap_duration_min")],
          f"before={before_columns} after={after_columns}")
    check("旧数据无损（行数与既有值不变）",
          [(r["date"], r["duration_min"], r["score"]) for r in after_rows]
          == LEGACY_ROWS, f"rows={after_rows}")
    check("旧库里的行数没有增多（不产生重复行）",
          len(after_rows) == len(before_rows["sleep_session"]))
    check(f"升级后版本 = v{SCHEMA_VERSION}，且不再标记为需要升级",
          store.schema_version() == SCHEMA_VERSION)
    with sqlite3.connect(str(legacy)) as conn:
        check("needs_upgrade(升级后) = False", migrations.needs_upgrade(conn) is False)

    # ── 5. 升级前备份 ──────────────────────────────────────────────────
    print("\n[5/8] 升级前备份：文件名 / 内容 / 权限 / 同秒不覆盖")
    backup_dir = tmp_dir / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    source = backup_dir / "health.db"
    make_legacy_db(source)
    source_dump = dump_tables(source)
    moment = datetime(2026, 10, 8, 21, 30, 15)
    first = migrations.backup_database(source, moment=moment)
    second = migrations.backup_database(source, moment=moment)
    print(f"     备份副本：{first.name} / {second.name}")
    check("文件名同目录、格式 <库名>.backup-YYYYMMDD-HHMMSS.db",
          first.parent == source.parent and BACKUP_NAME_RE.match(first.name) is not None
          and first.name == "health.backup-20261008-213015.db",
          f"name={first.name}")
    check("同一秒的第二次备份不覆盖首个（补 -2 后缀）",
          second != first and second.exists() and "-2" in second.name,
          f"第二份={second.name}")
    check("备份内容 = 源库（表结构与行完全一致）",
          dump_tables(first) == source_dump,
          f"tables={table_names(first)}")
    check("备份副本是合法 SQLite 库（quick_check=ok）",
          sqlite3.connect(f"file:{first}?mode=ro", uri=True)
          .execute("PRAGMA quick_check").fetchone()[0] == "ok")
    check("备份副本权限收 0600",
          stat.S_IMODE(first.stat().st_mode) == 0o600,
          oct(stat.S_IMODE(first.stat().st_mode)))
    check("备份不改动源库（源库版本仍是未版本化）",
          read_version(source) is None and dump_tables(source) == source_dump)

    # ── 6. 备份失败必须报错并停下 ──────────────────────────────────────
    print("\n[6/8] 备份失败 → BackupError，且不改库、不继续迁移")
    broken_dir = tmp_dir / "broken"
    broken_dir.mkdir(parents=True, exist_ok=True)
    broken = broken_dir / "health.db"
    make_legacy_db(broken)
    broken_dump = dump_tables(broken)
    original_backup = migrations.backup_database

    def failing_backup(db_path, *, moment=None):
        raise migrations.BackupError("模拟备份失败：磁盘不可写")

    migrations.backup_database = failing_backup
    try:
        try:
            HealthStore(broken).initialize()
        except migrations.BackupError as error:
            raised = error
        else:
            raised = None
    finally:
        migrations.backup_database = original_backup
    check("备份失败时 initialize() 抛 BackupError（不静默继续）",
          isinstance(raised, migrations.BackupError), f"{type(raised).__name__}: {raised}")
    check("备份失败时库未被改动（无版本表、数据原样）",
          read_version(broken) is None and dump_tables(broken) == broken_dump
          and not list(broken_dir.glob("*.backup-*.db")))
    check("备份不存在的库文件也显式报错",
          _expect_backup_error(tmp_dir / "no_such" / "health.db"))
    store = HealthStore(broken)  # 修好备份后仍能正常升级（库没被写坏）
    store.initialize()
    check("恢复备份能力后可正常升级到最新版本",
          store.schema_version() == SCHEMA_VERSION
          and [r["date"] for r in store.query("sleep_session")]
          == [row[0] for row in LEGACY_ROWS])

    # ── 7. 迁移中途失败：显式报错 + 整步回滚 ──────────────────────────
    print("\n[7/8] 迁移中途失败 → 显式报错 + 整步回滚（不留半迁移）")
    failing = tmp_dir / "failing" / "health.db"
    make_legacy_db(failing)
    failing_dump = dump_tables(failing)

    def _boom(conn: sqlite3.Connection) -> None:
        # 先建表（DDL）再抛异常：验证这一步被整体回滚，不留半迁移产物。
        conn.execute("CREATE TABLE should_rollback (x INTEGER)")
        conn.execute("ALTER TABLE sleep_session ADD COLUMN should_rollback_col TEXT")
        raise RuntimeError("模拟迁移中途异常")

    try:
        apply_chain(failing, (migrations.Migration(1, "失败步：模拟中途异常", _boom),))
    except migrations.MigrationError as error:
        failure = error
    else:
        failure = None
    print(f"     报错：{failure}")
    check("迁移失败抛 MigrationError（显式报错，不静默）",
          isinstance(failure, migrations.MigrationError)
          and "已回滚" in str(failure) and "v1" in str(failure),
          f"{type(failure).__name__}: {failure}")
    check("失败步的 DDL 被回滚（不留半迁移产物）",
          "should_rollback" not in table_names(failing))
    with sqlite3.connect(str(failing)) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(sleep_session)")]
    check("失败步的 ALTER 也被回滚（列没补上）",
          "should_rollback_col" not in columns, f"columns={columns}")
    check("版本号停在失败前（旧库仍视为 v0）", read_version(failing) in (None, 0),
          f"version={read_version(failing)}")
    # 版本表本身是迁移的引导表（先建它才能记版本），失败后只多这一张空表 + 一行 v0；
    # 业务表的内容必须原样不动。
    surviving = {name: rows for name, rows in dump_tables(failing).items()
                 if name != schema.VERSION_TABLE}
    check("原库业务数据未被破坏（只多了一张空的版本表）",
          surviving == failing_dump and read_version(failing) == 0,
          f"tables={table_names(failing)}")
    store = HealthStore(failing)
    store.initialize()
    check("换回正确迁移链后仍可升级成功",
          store.schema_version() == SCHEMA_VERSION and store.count("sleep_session") == 3)

    # ── 8. 版本高于插件：拒绝打开 ──────────────────────────────────────
    print("\n[8/8] 库版本高于插件 → 拒绝打开（不降级、不改库、不备份）")
    newer_dir = tmp_dir / "newer"
    newer_dir.mkdir(parents=True, exist_ok=True)
    newer = newer_dir / "health.db"
    make_legacy_db(newer)
    write_version(newer, SCHEMA_VERSION + 1)
    newer_dump = dump_tables(newer)
    with sqlite3.connect(f"file:{newer}?mode=ro", uri=True) as conn:
        try:
            migrations.current_version(conn)
        except migrations.MigrationError as error:
            version_error = error
        else:
            version_error = None
    check("current_version 对更高版本抛 MigrationError",
          isinstance(version_error, migrations.MigrationError)
          and str(SCHEMA_VERSION + 1) in str(version_error),
          f"{type(version_error).__name__}: {version_error}")
    try:
        HealthStore(newer).initialize()
    except migrations.MigrationError as error:
        init_error = error
    else:
        init_error = None
    check("initialize() 同样拒绝（不静默降级打开）",
          isinstance(init_error, migrations.MigrationError),
          f"{type(init_error).__name__}: {init_error}")
    check("拒绝打开时库未被改动、也没生成备份",
          dump_tables(newer) == newer_dump
          and read_version(newer) == SCHEMA_VERSION + 1
          and not list(newer_dir.glob("*.backup-*.db")))

    # ── 汇总 ───────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 64)
    print(f"结果：{passed}/{total} PASS")
    for name, detail in [(n, d) for n, ok, d in RESULTS if not ok]:
        print(f"  FAIL: {name} {detail}")
    print("=" * 64)
    return 0 if passed == total else 1


def _expect_backup_error(path: Path) -> bool:
    """备份不存在的库文件必须抛 BackupError。"""
    try:
        migrations.backup_database(path)
    except migrations.BackupError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
