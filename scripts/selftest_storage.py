#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""存储层自检：临时 db 跑一遍「建表 → 写入 → 幂等 → 区间查询 → 单位换算 →
同步状态/元信息 → 旧库原地升级（备份 + 版本迁移 + 无损 + 幂等）」。

只读设计之外的一切都发生在 tempfile 临时目录里，不碰真实 health.db。
不 import astrbot（存储层本身也不依赖 astrbot）。

用法：
    python3 scripts/selftest_storage.py
退出码：全部 PASS → 0；任一 FAIL → 1。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from adapters import huawei_health_cloud as cloud  # noqa: E402
from storage import SCHEMA_VERSION, HealthStore, models, schema  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def base_ms(year: int, month: int, day: int, hour: int = 0) -> int:
    return int(datetime(year, month, day, hour, 0, 0).timestamp() * 1000)


def main() -> int:
    print("=" * 64)
    print("华为运动健康 —— 存储层自检")
    print("=" * 64)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hwhealth_store_"))
    db_path = tmp_dir / "nested" / "health.db"  # 故意多一层，验证目录自动创建
    print(f"临时库文件：{db_path}")

    store = HealthStore(db_path)

    # ── 1. 建表 ────────────────────────────────────────────────────────
    print("\n[1/10] 建库建表（目录自动创建）+ schema 版本")
    store.initialize()
    tables = set(store.tables())
    expected = {
        "daily_activity", "heart_rate_sample", "sleep_session",
        "stress_sample", "spo2_sample", "training_session",
    }
    check("目录与库文件自动创建", db_path.exists(), f"exists={db_path.exists()}")
    check("六张业务表齐全", expected <= tables, f"tables={sorted(tables)}")
    check("BodyMeasurement 未建表",
          not any("body" in name.lower() for name in tables))
    check(f"新库一次建到最新版本 v{SCHEMA_VERSION}",
          store.schema_version() == SCHEMA_VERSION,
          f"version={store.schema_version()}")
    check("同步状态表 / 元信息表 / 版本表已建",
          {"sync_state", "meta", "schema_version"} <= tables,
          f"tables={sorted(tables)}")
    with sqlite3.connect(str(db_path)) as probe:
        version_rows = probe.execute("SELECT version FROM schema_version").fetchall()
    check("版本表只有一行且等于最新版本",
          version_rows == [(SCHEMA_VERSION,)], f"rows={version_rows}")
    check("新库不生成升级备份（没有旧数据要保）",
          not list(db_path.parent.glob("*.backup-*.db")))

    # ── 2. DailyActivity 写入 + 单位换算 ───────────────────────────────
    print("\n[2/10] DailyActivity 写入 + 单位换算（calorie 千分之一 kcal → kcal）")
    # 真实形状：协议层 daily_activity() 的整形行（同步服务实际喂给存储层的入参）。
    daily_records = [
        {  # 聚合行 sportType 0：应作为该日唯一行
            "date": "2026-10-06", "sport_type": 0, "steps": 8123,
            "distance_m": 5230, "kcal": 250.5, "duration_min": 45,
            "walk_min": 30, "active_hours": 11, "exercise_min": 25,
            "step_goal": 10000,
        },
        {  # 同日分项行（sportType 5 走路）：不应覆盖聚合行
            "date": "2026-10-06", "sport_type": 5, "steps": 100,
            "distance_m": 80, "kcal": 3.0, "duration_min": 2,
        },
    ]
    written = store.upsert_daily_activity(daily_records)
    row = store.get("daily_activity", "2026-10-06")
    print(f"     写入行数={written}；行={row}")
    check("按日期唯一（同日两条→1 行）", store.count("daily_activity") == 1)
    check("聚合行优先（sportType=0 胜出）", row and row["sport_type"] == 0)
    check("steps=8123", row and row["steps"] == 8123)
    check("distance 米原样=5230", row and row["distance_m"] == 5230)
    check("kcal 原样入库 250.5（千分之一 kcal → kcal 的换算在协议层完成）",
          row and abs(row["kcal"] - 250.5) < 1e-9, f"kcal={row and row['kcal']}")
    check("duration_min=45", row and row["duration_min"] == 45)
    check("walk_min=30", row and row["walk_min"] == 30)
    check("active_hours=11", row and row["active_hours"] == 11)
    check("exercise_min=25", row and row["exercise_min"] == 25)
    check("step_goal=10000", row and row["step_goal"] == 10000)

    # ── 3. 幂等：重复写入 ──────────────────────────────────────────────
    print("\n[3/10] 幂等校验（同记录再写一遍）")
    store.upsert_daily_activity(daily_records)
    again = store.get("daily_activity", "2026-10-06")
    check("重复写不新增行", store.count("daily_activity") == 1,
          f"count={store.count('daily_activity')}")
    check("重复写数值不变", again == row)

    # ── 4. Health 写入（心率 / 睡眠 / 压力 / 血氧）─────────────────────
    print("\n[4/10] Health 写入（真实形状：协议层整形行 → 心率/睡眠/压力/血氧）")
    # 真实形状：协议层 health_series 的整形行（同步服务实际喂给存储层的入参）。
    health_payload = [{
        "date": "2026-10-06",
        "resting_heart_rate": 58, "heart_rate": 72,
        "average_resting_heart_rate": 60, "max_heart_rate": 135,
        "min_heart_rate": 49,
        "sleep_duration": 432, "sleep_score": 83, "sleep_efficiency": 92,
        "sleep_hrv": 45.5, "sleep_spo2": 97,
        "fall_asleep": "2026-10-05 23:20", "wakeup": "2026-10-06 06:32",
        "nap_duration": 74,
        "stress_average": 32, "stress_last": 28, "stress_max": 61,
        "stress_min": 12, "stress_measurements": 40,
    }, {
        # 只有 0 / 负值读数的一天（协议层 health_series 已把 <=0 丢掉，不会产出读数）：
        # 四个家族都不该写行（不补空行，查询层「无数据」分支才可达）。
        "date": "2026-10-04",
        "resting_heart_rate": 0, "heart_rate": -1,
        "sleep_score": 0, "sleep_spo2": 0,
    }]
    # 真实路径：门面 iter_<类>() = 「协议层整形行 → storage.models 归一」，同步服务再按类
    # 走 store.upsert_rows(模型名, 行) 直写（upsert_health 已随存储层收口删除）。
    groups = models.normalize_health(health_payload)
    counts = {
        "heart_rate": store.upsert_rows("heart_rate_sample", groups["heart_rate"]),
        "sleep": store.upsert_rows("sleep_session", groups["sleep"]),
        "stress": store.upsert_rows("stress_sample", groups["stress"]),
        "spo2": store.upsert_rows("spo2_sample", groups["spo2"]),
    }
    hr = store.get("heart_rate_sample", "2026-10-06")
    sleep = store.get("sleep_session", "2026-10-06")
    stress = store.get("stress_sample", "2026-10-06")
    spo2 = store.get("spo2_sample", "2026-10-06")
    print(f"     写入计数={counts}")
    print(f"     心率={hr}")
    print(f"     睡眠={sleep}")
    print(f"     压力={stress}")
    print(f"     血氧={spo2}")
    check("心率行写入（静息/日值/max/min）",
          hr and hr["resting_hr"] == 58 and hr["day_hr"] == 72
          and hr["max_hr"] == 135 and hr["min_hr"] == 49
          and hr["average_resting_hr"] == 60)
    check("心率标注为汇总型样本 sample_kind=daily_summary",
          hr and hr["sample_kind"] == "daily_summary"
          and hr["source_type"] == 7)
    check("睡眠汇总值写入（时长/评分/效率/HRV/SpO2）",
          sleep and sleep["duration_min"] == 432 and sleep["score"] == 83
          and sleep["efficiency"] == 92 and sleep["hrv"] == 45.5
          and sleep["spo2"] == 97)
    check("入睡 / 起床按本地时间补秒入库（'YYYY-MM-DD HH:MM:SS'）",
          sleep and sleep["fall_asleep_local"] == "2026-10-05 23:20:00"
          and sleep["wakeup_local"] == "2026-10-06 06:32:00",
          f"fall_asleep={sleep and sleep['fall_asleep_local']} "
          f"wakeup={sleep and sleep['wakeup_local']}")
    check("白天小睡时长入库（协议层 nap_duration → nap_duration_min=74）",
          sleep and sleep["nap_duration_min"] == 74,
          f"nap={sleep and sleep['nap_duration_min']}")
    before_row = {key: value for key, value in sleep.items() if key != "updated_at"}
    store.upsert_rows(  # 同一天同数据再同步一遍
        "sleep_session", models.normalize_health(health_payload)["sleep"])
    again_row = store.get("sleep_session", "2026-10-06")
    check("同一天同数据重复同步 → 行保持一致（updated_at 除外）",
          {key: value for key, value in again_row.items() if key != "updated_at"}
          == before_row, f"again={again_row}")
    store.upsert_rows("sleep_session", models.normalize_health(
        [{"date": "2026-10-06", "sleep_score": 85}])["sleep"])
    slim_row = store.get("sleep_session", "2026-10-06")
    check("新值缺字段时不把已有值写成 NULL（评分更新，其余保留）",
          slim_row and slim_row["score"] == 85 and slim_row["duration_min"] == 432
          and slim_row["fall_asleep_local"] == "2026-10-05 23:20:00"
          and slim_row["wakeup_local"] == "2026-10-06 06:32:00"
          and slim_row["nap_duration_min"] == 74,
          f"row={slim_row}")
    check("压力写入（均值/末值/max/min/次数）",
          stress and stress["average"] == 32 and stress["last_value"] == 28
          and stress["max_value"] == 61 and stress["min_value"] == 12
          and stress["measurements"] == 40)
    check("血氧取自睡眠 lastAvgSpO2=97",
          spo2 and spo2["spo2"] == 97
          and spo2["sample_kind"] == "sleep_last_avg")
    check("0 / 负值读数被丢弃，该日期不写任何 health 行（不补空行）",
          store.get("heart_rate_sample", "2026-10-04") is None
          and store.get("sleep_session", "2026-10-04") is None
          and store.get("stress_sample", "2026-10-04") is None
          and store.get("spo2_sample", "2026-10-04") is None,
          "2026-10-04 出现了只有空值的行")

    # ── 5. TrainingSession：dataId 去重 + ≤15 分钟合并 ─────────────────
    print("\n[5/10] TrainingSession 去重合并（dataId 去重；间隔 ≤15 分钟合并）")
    t0 = base_ms(2026, 10, 6, 8)
    minute = 60 * 1000

    def seg(data_id, sport_type, start_offset_min, dur, dist, calorie, dev):
        start = t0 + start_offset_min * minute
        return {
            "dataId": data_id, "sportType": sport_type,
            "startTime": start, "endTime": start + minute,
            "deviceCode": dev,
            "sportBasicInfos": [{"duration": dur, "distance": dist,
                                 "calorie": calorie, "steps": 10}],
        }

    training_records = [
        seg("run-a1", 4, 0, 1, 200, 15000, "band-x"),    # running 第 1 段
        seg("run-a1", 4, 0, 1, 200, 15000, "band-x"),    # 重复 dataId → 去重
        seg("run-a2", 4, 1, 1, 210, 16000, "band-x"),    # 紧邻 → 合并
        seg("run-b1", 4, 40, 1, 300, 20000, "band-x"),   # 间隔 39 分钟 → 新会话
        seg("stair-c1", 1, 5, 1, 5, 8000, "band-x"),     # 另一 sportType → 独立
    ]
    # 真实路径：门面 iter_training() = models.merge_training_segments(原始分钟段)，
    # 同步服务再走 store.upsert_rows("training_session", 会话行) 直写。
    sessions_written = store.upsert_rows(
        "training_session", models.merge_training_segments(training_records))
    sessions = store.query("training_session")
    print(f"     写入会话数={sessions_written}")
    for item in sessions:
        print(f"       {item['session_key']} type={item['sport_type']} "
              f"seg={item['segments']} dur={item['duration_min']} "
              f"dist={item['distance_m']} kcal={item['kcal']} dev={item['device_code']}")
    running = [s for s in sessions if s["sport_type"] == 4]
    stairs = [s for s in sessions if s["sport_type"] == 1]
    check("dataId 去重 + 相邻段合并 → running 两条会话", len(running) == 2,
          f"running={len(running)}")
    check("running 会话A：段数=2 / 时长=2 / 距离=410 / kcal=31.0",
          running and running[0]["segments"] == 2 and running[0]["duration_min"] == 2
          and running[0]["distance_m"] == 410 and abs(running[0]["kcal"] - 31.0) < 1e-9,
          f"A={running[0] if running else None}")
    check("running 会话B：间隔 39 分钟未合并，段数=1",
          len(running) > 1 and running[1]["segments"] == 1)
    check("sportType 分组：stairs 独立一条会话", len(stairs) == 1)
    check("device_code 入库", running and running[0]["device_code"] == "band-x")
    check("start_date 为本地日期 2026-10-06",
          running and running[0]["start_date"] == "2026-10-06")

    # ── 6. 训练会话幂等 ────────────────────────────────────────────────
    print("\n[6/10] TrainingSession 幂等（同分钟段再写一遍）")
    before = store.count("training_session")
    store.upsert_rows(
        "training_session", models.merge_training_segments(training_records))
    after = store.count("training_session")
    check("重复写不新增会话", before == after, f"{before} -> {after}")

    # ── 7. 按日期区间查询 ──────────────────────────────────────────────
    print("\n[7/10] 按日期区间查询")
    store.upsert_daily_activity([{
        "date": "2026-10-07", "sport_type": 0, "steps": 1000,
        "distance_m": 900, "kcal": 30.0, "duration_min": 10,
    }])
    store.upsert_daily_activity([{
        "date": "2026-10-05", "sport_type": 0, "steps": 100,
        "distance_m": 90, "kcal": 3.0, "duration_min": 2,
    }])
    window = store.query("daily_activity", "2026-10-05", "2026-10-06")
    print(f"     区间 2026-10-05 ~ 2026-10-06 → {[r['date'] for r in window]}")
    check("区间查询只含端点内日期",
          [r["date"] for r in window] == ["2026-10-05", "2026-10-06"])
    check("区间外日期(10-07)不在结果内",
          all(r["date"] != "2026-10-07" for r in window))
    check("全量查询=3 天", store.count("daily_activity") == 3)

    # ── 8. 日汇总护栏：无聚合行 / 读数全空 → 跳过该日 ──────────────────
    print("\n[8/10] 日汇总护栏（无聚合行不拿分项行当汇总；读数全空不写空壳行）")
    # 8a) 只有分项行（sportType 5 / 4）的一天：不能把第一条分项行当当日汇总。
    store.upsert_daily_activity([
        {"date": "2026-09-01", "sport_type": 5, "steps": 200,
         "distance_m": 150, "kcal": 8.0, "duration_min": 3},
        {"date": "2026-09-01", "sport_type": 4, "steps": 300,
         "distance_m": 500, "kcal": 30.0, "duration_min": 20},
    ])
    check("无聚合行 → 该日不写行（不拿分项行充当当日汇总）",
          store.get("daily_activity", "2026-09-01") is None,
          f"row={store.get('daily_activity', '2026-09-01')}")
    # 8b) 分项行与聚合行同时存在：只写聚合行（既有行为不回归）。
    store.upsert_daily_activity([
        {"date": "2026-09-02", "sport_type": 5, "steps": 200, "distance_m": 150},
        {"date": "2026-09-02", "sport_type": 0, "steps": 9000, "distance_m": 7000,
         "kcal": 300.0, "duration_min": 60},
    ])
    row_0902 = store.get("daily_activity", "2026-09-02")
    check("有聚合行 → 写聚合行（分项行仍被丢弃）",
          row_0902 is not None and row_0902["sport_type"] == 0
          and row_0902["steps"] == 9000,
          f"row={row_0902}")
    # 8c) 聚合行读数全空（step_goal 是目标值，不算读数）→ 跳过，不写空壳行。
    store.upsert_daily_activity([
        {"date": "2026-09-03", "sport_type": 0, "steps": 0, "distance_m": 0,
         "kcal": 0.0, "duration_min": 0, "step_goal": 10000},
    ])
    check("聚合行读数全空 → 不写空壳行（step_goal 不是读数）",
          store.get("daily_activity", "2026-09-03") is None)
    check("8a~8c 只多写入 1 行（10-05/10-06/10-07 + 09-02）",
          store.count("daily_activity") == 4, f"count={store.count('daily_activity')}")
    check("聚合行优先只有一处实现：协议层 daily_totals 已删除",
          not hasattr(cloud, "daily_totals"))

    # ── 9. 同步状态（sync_state）与元信息（meta）───────────────────────
    print("\n[9/10] 同步状态（sync_state）+ 元信息（meta）读写语义")
    states_written = store.record_sync_states([
        {"data_type": "daily_activity", "status": "ok", "window_end": "2026-10-07"},
        {"data_type": "sleep", "status": "failed", "window_end": "2026-10-07",
         "error": "HuaweiConnectionError: resultCode=-1"},
        {"data_type": "round", "status": "partial", "window_end": "2026-10-07",
         "error": "sleep: HuaweiConnectionError: resultCode=-1"},
    ])
    print(f"     写入同步状态行数={states_written}")
    for state in store.sync_state():
        print(f"       {state}")
    ok_row = store.sync_state("daily_activity")
    failed_row = store.sync_state("sleep")
    check("批量写入 3 类状态（每类一行）",
          states_written == 3 and len(store.sync_state()) == 3)
    check("成功行：status=ok、记成功时刻与窗口上界、无失败原因",
          ok_row and ok_row["last_status"] == "ok" and ok_row["last_success_at"]
          and ok_row["last_window_end"] == "2026-10-07"
          and ok_row["last_error"] is None,
          f"row={ok_row}")
    check("失败行：status=failed、记原因、从不成功则 last_success_at 为 NULL",
          failed_row and failed_row["last_status"] == "failed"
          and failed_row["last_success_at"] is None
          and "resultCode" in (failed_row["last_error"] or ""),
          f"row={failed_row}")
    store.record_sync_states([{"data_type": "sleep", "status": "ok",
                               "window_end": "2026-10-08"}])
    recovered = store.sync_state("sleep")
    check("重试成功 → 补上成功时刻并清空上次失败原因",
          recovered and recovered["last_success_at"]
          and recovered["last_error"] is None
          and recovered["last_status"] == "ok",
          f"row={recovered}")
    store.record_sync_states([{"data_type": "sleep", "status": "failed",
                               "error": "HuaweiConnectionError: timeout"}])
    degraded = store.sync_state("sleep")
    check("再次失败 → 覆盖状态与原因、保留上次成功时刻与窗口上界",
          degraded and degraded["last_status"] == "failed"
          and degraded["last_success_at"] == recovered["last_success_at"]
          and "timeout" in (degraded["last_error"] or "")
          and degraded["last_window_end"] == "2026-10-08",
          f"row={degraded}")
    check("未同步过的类 → None（按「从未同步」处理）",
          store.sync_state("spo2") is None)
    check("data_type 为主键，重复写不新增行", len(store.sync_state()) == 3)
    try:
        store.record_sync_states([{"data_type": "sleep", "status": "weird"}])
    except ValueError as error:
        check("非法 status 显式报错（ValueError），不写脏数据",
              "status" in str(error), f"{type(error).__name__}: {error}")
    else:
        check("非法 status 显式报错（ValueError），不写脏数据", False,
              "没有抛异常")
    meta = store.metadata()
    print(f"     元信息={meta}")
    check("元信息：库属主标识",
          meta.get(schema.META_KEY_PLUGIN) == schema.PLUGIN_ID,
          f"plugin={meta.get(schema.META_KEY_PLUGIN)}")
    check("元信息：首次纳入版本管理时刻与最近迁移时刻",
          bool(meta.get(schema.META_KEY_SCHEMA_CREATED_AT))
          and bool(meta.get(schema.META_KEY_SCHEMA_UPGRADED_AT)))
    check("新库无「升级前备份」记录（新库不需要备份）",
          schema.META_KEY_LAST_BACKUP_FILE not in meta)
    check("get_metadata：缺失键返回 None",
          store.get_metadata("no_such_key") is None)

    # ── 10. 旧库原地升级（备份 + 版本迁移 + 无损 + 幂等）──────────────
    print("\n[10/10] 旧库原地升级：升级前备份 + 版本迁移 + 数据无损 + 幂等")
    legacy_dir = tmp_dir / "legacy"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_path = legacy_dir / "health.db"
    conn = sqlite3.connect(str(legacy_path))
    try:
        # 旧库形状：没有 schema_version 表；sleep_session 缺三个新增列；已有数据。
        conn.executescript(
            "CREATE TABLE sleep_session ("
            " date TEXT PRIMARY KEY, duration_min REAL, score REAL, efficiency REAL,"
            " hrv REAL, spo2 REAL, source_type INTEGER NOT NULL DEFAULT 9,"
            " updated_at TEXT NOT NULL);"
            "CREATE TABLE daily_activity ("
            " date TEXT PRIMARY KEY, sport_type INTEGER, steps INTEGER,"
            " distance_m INTEGER, kcal REAL, duration_min INTEGER, walk_min INTEGER,"
            " active_hours INTEGER, exercise_min INTEGER, step_goal INTEGER,"
            " updated_at TEXT NOT NULL);")
        conn.executemany(
            "INSERT INTO sleep_session"
            " (date, duration_min, score, source_type, updated_at)"
            " VALUES (?, ?, ?, 9, ?)",
            [("2026-10-06", None, None, "2026-10-08 00:00:40"),
             ("2026-10-07", None, None, "2026-10-08 00:00:40"),
             ("2026-10-08", 388.0, 80.0, "2026-10-08 20:52:47")])
        conn.execute(
            "INSERT INTO daily_activity"
            " (date, sport_type, steps, distance_m, kcal, duration_min, updated_at)"
            " VALUES ('2026-10-08', 0, 3148, 2387, 143.8, 0, '2026-10-08 20:52:47')")
        conn.commit()
    finally:
        conn.close()
    with sqlite3.connect(str(legacy_path)) as probe:
        before_tables = [row[0] for row in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        before_columns = [row[1] for row in probe.execute(
            "PRAGMA table_info(sleep_session)")]
        before_sleep = probe.execute(
            "SELECT date, duration_min, score, efficiency, hrv, spo2, source_type,"
            " updated_at FROM sleep_session ORDER BY date").fetchall()
        before_daily = probe.execute(
            "SELECT date, steps, distance_m, kcal FROM daily_activity"
            " ORDER BY date").fetchall()
    check("升级前确实是未版本化的旧库（无 schema_version 表）",
          "schema_version" not in before_tables, f"tables={before_tables}")

    legacy = HealthStore(legacy_path)
    legacy.initialize()
    backups = sorted(legacy_dir.glob("health.backup-*.db"))
    print(f"     升级前备份副本={[p.name for p in backups]}")
    check("升级前生成备份副本（同目录、文件名带时间戳）", len(backups) == 1,
          f"backups={[p.name for p in backups]}")
    with sqlite3.connect(f"file:{backups[0]}?mode=ro", uri=True) as probe:
        backup_tables = [row[0] for row in probe.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        backup_columns = [row[1] for row in probe.execute(
            "PRAGMA table_info(sleep_session)")]
        backup_sleep = probe.execute(
            "SELECT date, duration_min, score, efficiency, hrv, spo2, source_type,"
            " updated_at FROM sleep_session ORDER BY date").fetchall()
    check("备份副本=升级前的库（同一批行、同一套列、无新表）",
          backup_sleep == before_sleep and backup_columns == before_columns
          and backup_tables == before_tables,
          f"columns={backup_columns} tables={backup_tables}")

    with sqlite3.connect(str(legacy_path)) as probe:
        legacy_columns = [row[1] for row in probe.execute(
            "PRAGMA table_info(sleep_session)")]
    legacy_rows = legacy.query("sleep_session")
    legacy_daily = legacy.query("daily_activity")
    print(f"     迁移后 sleep_session 列={legacy_columns}")
    for item in legacy_rows:
        print(f"       {item}")
    check("旧库补上三列",
          {"fall_asleep_local", "wakeup_local", "nap_duration_min"} <= set(legacy_columns),
          f"columns={legacy_columns}")
    check("旧三行原样保留（行数与既有值不变，新列全 NULL）",
          [item["date"] for item in legacy_rows]
          == ["2026-10-06", "2026-10-07", "2026-10-08"]
          and legacy_rows[2]["duration_min"] == 388.0
          and legacy_rows[2]["score"] == 80.0
          and all(item["fall_asleep_local"] is None and item["wakeup_local"] is None
                  and item["nap_duration_min"] is None for item in legacy_rows),
          f"rows={legacy_rows}")
    check("其它旧表数据原样保留（daily_activity 1 行 3148 步）",
          [(r["date"], r["steps"], r["distance_m"], r["kcal"]) for r in legacy_daily]
          == before_daily, f"rows={legacy_daily}")
    check(f"升级后版本 = v{SCHEMA_VERSION}",
          legacy.schema_version() == SCHEMA_VERSION,
          f"version={legacy.schema_version()}")
    check("升级后新增表到位（schema_version / sync_state / meta）",
          {"schema_version", "sync_state", "meta"} <= set(legacy.tables()),
          f"tables={sorted(legacy.tables())}")
    check("元信息记下本次备份副本文件名",
          legacy.get_metadata(schema.META_KEY_LAST_BACKUP_FILE) == backups[0].name,
          f"meta={legacy.metadata()}")

    legacy.initialize()  # 再跑一次：迁移必须幂等
    recheck_backups = sorted(legacy_dir.glob("health.backup-*.db"))
    with sqlite3.connect(str(legacy_path)) as probe:
        recheck_columns = [row[1] for row in probe.execute(
            "PRAGMA table_info(sleep_session)")]
        recheck_versions = [row[0] for row in probe.execute(
            "SELECT version FROM schema_version")]
    check("已是最新版本 → 不再重复备份（幂等）", len(recheck_backups) == 1,
          f"backups={[p.name for p in recheck_backups]}")
    check("迁移幂等（重复 initialize() 列不重复、行数仍为 3、版本不重复写）",
          recheck_columns == legacy_columns
          and legacy.count("sleep_session") == 3
          and recheck_versions == [SCHEMA_VERSION],
          f"columns={recheck_columns} versions={recheck_versions}")
    legacy.upsert_rows("sleep_session", models.normalize_health(
        [{"date": "2026-10-08", "sleep_score": 85}])["sleep"])
    upgraded = legacy.get("sleep_session", "2026-10-08")
    check("升级后可正常写入（新值更新、旧值保留）",
          upgraded and upgraded["score"] == 85 and upgraded["duration_min"] == 388.0,
          f"row={upgraded}")

    # ── 汇总 ───────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 64)
    print(f"结果：{passed}/{total} PASS")
    failures = [(name, detail) for name, ok, detail in RESULTS if not ok]
    for name, detail in failures:
        print(f"  FAIL: {name} {detail}")
    print("=" * 64)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
