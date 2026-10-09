"""华为运动健康插件 —— 存储层表结构定义（schema）与版本号。

本模块只回答「库里长什么样」：schema 版本号、建表语句、字段口径、模型 → 表映射。
不含任何读写逻辑（读写见 health_store.py，版本迁移链见 migrations.py），
不 import 第三方库，也不 import astrbot / homeassistant。

════════════════════════════════════════════════════════════════════
全库统一口径
════════════════════════════════════════════════════════════════════
* 时间一律本地时间（CST）文本：日期 ``YYYY-MM-DD``，时刻 ``YYYY-MM-DD HH:MM:SS``；
* 单位：距离米（``distance_m``）、时长分钟（``*_min``）、热量千卡（``kcal``，上游
  千分之一 kcal 的换算在协议层完成，存储层原样入库）；
* 上游「手环没测到」= 0 或负值，协议层已丢弃；存储层只写有读数的行，不补空壳行；
* 每张业务表以「日期」或「训练会话 session_key」为主键，写入走
  ``INSERT ... ON CONFLICT(主键) DO UPDATE``，冲突列逐个 ``COALESCE(新值, 旧值)``：
  重复同步不产生重复行，本次没取到的字段保留库中旧值，绝不把已有值写成 NULL；
* 新增列一律可空（旧行留 NULL），新增表一律 ``CREATE TABLE IF NOT EXISTS``：
  旧库靠 migrations.py 的迁移链无损升级。

════════════════════════════════════════════════════════════════════
schema 版本
════════════════════════════════════════════════════════════════════
    0 —— 未版本化的旧库（有业务表、没有 schema_version 表）；新库也从这个起点建。
    1 —— 基线：六张业务表 + sleep_session 三个新增列 + 训练会话索引。
    2 —— 同步状态与元信息：sync_state、meta。
    3 —— 主动关怀：care_send_log（发送记录）、care_event_key（事件去重键）、
        care_scenario_state（各场景冷却状态）、care_owner_activity（所有者最近私聊活动）。
    4 —— 训练碎片标记：training_session 新增 is_fragment（0=有效训练、1=碎片），
        旧库按判据回填。碎片只标记、不删除，原始数据一律保留。
"""

from __future__ import annotations

from datetime import datetime
from typing import NamedTuple

# 当前插件代码期望的 schema 版本（迁移链跑到这里为止）。
SCHEMA_VERSION = 4

# 库属主标识：写进 meta 表，用于确认这个库是本插件的（不做强制校验，仅供参考）。
PLUGIN_ID = "astrbot_plugin_huawei_health"

# 时间文本格式（全库唯一口径）。
LOCAL_DATE_FORMAT = "%Y-%m-%d"
LOCAL_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"

# 升级前备份副本的文件名格式：<库名>.backup-YYYYMMDD-HHMMSS.db
BACKUP_STAMP_FORMAT = "%Y%m%d-%H%M%S"

# 版本表 / 元信息表名。
VERSION_TABLE = "schema_version"
META_TABLE = "meta"
SYNC_STATE_TABLE = "sync_state"

# 主动关怀（v3）表名。
CARE_SEND_LOG_TABLE = "care_send_log"
CARE_EVENT_KEY_TABLE = "care_event_key"
CARE_SCENARIO_STATE_TABLE = "care_scenario_state"
CARE_OWNER_ACTIVITY_TABLE = "care_owner_activity"


def local_now() -> str:
    """当前本地时间文本 ``YYYY-MM-DD HH:MM:SS``（全库写入时刻的唯一来源）。"""
    return datetime.now().strftime(LOCAL_TIME_FORMAT)


# ════════════════════════════════════════════════════════════════════
# 表结构（DDL）
# ════════════════════════════════════════════════════════════════════

DAILY_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS daily_activity (
    date          TEXT PRIMARY KEY,          -- 本地(CST)日期 YYYY-MM-DD，按日期唯一
    sport_type    INTEGER,                   -- 原始 sportType；聚合行为 0/NULL，分项行=某一种运动
    steps         INTEGER,                   -- 当日步数（sportBasicInfo.steps）
    distance_m    INTEGER,                   -- 当日距离，米（sportBasicInfo.distance，上游即米）
    kcal          REAL,                      -- 当日活动热量，千卡（上游千分之一 kcal 已在协议层 /1000）
    duration_min  INTEGER,                   -- 当日活动时长，分钟（sportBasicInfo.duration）
    walk_min      INTEGER,                   -- 步行时长，分钟（dimenDailyActivity.walkDurations）
    active_hours  INTEGER,                   -- 活跃小时数（activeHourBasic.countActiveHour）
    exercise_min  INTEGER,                   -- 中高强度运动时长，分钟（exerciseTimeBasic.totalMidHighIntensity）
    step_goal     INTEGER,                   -- 当日步数目标（goalAchieveBasic.stepGoalValueStat）；目标值，不算「读数」
    updated_at    TEXT NOT NULL              -- 本行最近写入时刻 YYYY-MM-DD HH:MM:SS
);
"""

HEART_RATE_DDL = """
CREATE TABLE IF NOT EXISTS heart_rate_sample (
    date               TEXT PRIMARY KEY,     -- 本地日期 YYYY-MM-DD；本表是「日粒度汇总型样本」
    resting_hr         REAL,                 -- 最近静息心率（heartRateBasic.lastRestHeartRate）
    day_hr             REAL,                 -- 最近一次心率（heartRateBasic.lastHeartRate）
    average_resting_hr REAL,                 -- 当日平均静息心率（avgRestingHeartRate）
    max_hr             REAL,                 -- 当日最高心率（maxHeartRate）
    min_hr             REAL,                 -- 当日最低心率（minHeartRate）
    sample_kind        TEXT NOT NULL DEFAULT 'daily_summary',  -- 样本类型；日均汇总固定 daily_summary
    source_type        INTEGER NOT NULL DEFAULT 7,            -- 上游 health 家族编号（7=心率）
    updated_at         TEXT NOT NULL
);
"""

SLEEP_DDL = """
CREATE TABLE IF NOT EXISTS sleep_session (
    date               TEXT PRIMARY KEY,     -- 本地日期 YYYY-MM-DD（按「当晚」归属的日期）
    duration_min       REAL,                 -- 总睡眠时长，分钟（professionalSleep.allSleepTime）
    score              REAL,                 -- 睡眠评分（sleepScore）
    efficiency         REAL,                 -- 睡眠效率 %（sleepEfficiency）
    hrv                REAL,                 -- 睡眠期平均 HRV（lastAvgHrv）
    spo2               REAL,                 -- 睡眠期平均血氧 %（lastAvgSpO2）
    fall_asleep_local  TEXT,                 -- 本地入睡时刻 YYYY-MM-DD HH:MM:SS（fallAsleepTime 补秒；不推算）
    wakeup_local       TEXT,                 -- 本地起床时刻 YYYY-MM-DD HH:MM:SS（wakeupTime 补秒；不推算）
    nap_duration_min   REAL,                 -- 白天小睡时长，分钟（daySleepTime）；与夜间时长分开记
    source_type        INTEGER NOT NULL DEFAULT 9,   -- 上游 health 家族编号（9=睡眠）
    updated_at         TEXT NOT NULL
);
"""

STRESS_DDL = """
CREATE TABLE IF NOT EXISTS stress_sample (
    date          TEXT PRIMARY KEY,          -- 本地日期 YYYY-MM-DD
    average       REAL,                      -- 当日压力均值（meanScore）
    last_value    REAL,                      -- 最近一次压力值（lastScore）
    max_value     REAL,                      -- 当日最高（maxScore）
    min_value     REAL,                      -- 当日最低（minScore）
    measurements  INTEGER,                   -- 当日测量次数（measureCount）
    source_type   INTEGER NOT NULL DEFAULT 11,  -- 上游 health 家族编号（11=压力）
    updated_at    TEXT NOT NULL
);
"""

SPO2_DDL = """
CREATE TABLE IF NOT EXISTS spo2_sample (
    date          TEXT PRIMARY KEY,          -- 本地日期 YYYY-MM-DD
    spo2          REAL,                      -- 血氧饱和度 %（睡眠响应 professionalSleep.lastAvgSpO2）
    sample_kind   TEXT NOT NULL DEFAULT 'sleep_last_avg',  -- 样本类型；取自睡眠期均值
    updated_at    TEXT NOT NULL
);
"""

TRAINING_DDL = """
CREATE TABLE IF NOT EXISTS training_session (
    session_key   TEXT PRIMARY KEY,          -- "<sport_type>:<start_ms>"，同一次运动唯一
    sport_type    INTEGER NOT NULL,          -- 运动类型编号（getSportsDataByTime.sportType）
    start_ms      INTEGER NOT NULL,          -- 开始时间 epoch 毫秒（原始值，未做时区换算）
    end_ms        INTEGER NOT NULL,          -- 结束时间 epoch 毫秒
    start_local   TEXT,                      -- 开始时刻本地文本 YYYY-MM-DD HH:MM:SS
    end_local     TEXT,                      -- 结束时刻本地文本
    start_date    TEXT,                      -- 开始本地日期 YYYY-MM-DD（区间查询用）
    duration_min  INTEGER,                   -- 会话时长，分钟（各分钟段累加）
    distance_m    INTEGER,                   -- 会话距离，米（各分钟段累加）
    kcal          REAL,                      -- 会话热量，千卡（上游千分之一 kcal /1000 后累加）
    segments      INTEGER,                   -- 合并进本会话的分钟段数
    device_code   TEXT,                      -- 来源设备编号（deviceCode）
    is_fragment   INTEGER,                   -- 碎片标记（v4）：入库当时的判定快照，1=碎片、
                                             -- 0=有效；判据见 storage/models.is_valid_training
                                             -- （阈值来自配置）。展示与关怀不读本列，改按当前
                                             -- 阈值用 is_valid_training() 重算；旧行可为 NULL
    updated_at    TEXT NOT NULL
);
"""

TRAINING_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_training_session_start
    ON training_session (start_date, sport_type);
"""

SYNC_STATE_DDL = """
CREATE TABLE IF NOT EXISTS sync_state (
    data_type       TEXT PRIMARY KEY,        -- 数据类别：六类数据名（daily_activity/heart_rate/sleep/
                                             -- stress/spo2/training_session）+ 整轮同步汇总行 'round'
    last_attempt_at TEXT NOT NULL,           -- 该类最近一次「尝试」同步的本地时刻（成败都记）
    last_success_at TEXT,                    -- 该类最近一次「成功」同步的本地时刻；NULL=从未成功
    last_status     TEXT NOT NULL,           -- 本轮该类结果：ok / partial / failed / skipped
    last_window_end TEXT,                    -- 本轮同步覆盖窗口的上界（本地日期）；不表示该日一定有数据
    last_error      TEXT,                    -- 最近一次失败的单行原因（异常类型+原因，不含凭据与健康数值）；
                                             -- 本轮成功则写 NULL，等于清掉上次失败原因
    updated_at      TEXT NOT NULL
);
"""

META_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key        TEXT PRIMARY KEY,             -- 元信息键
    value      TEXT NOT NULL,                -- 元信息值（全部为文本）
    updated_at TEXT NOT NULL
);
"""

# ── v3：主动关怀（发送记录 / 事件去重键 / 各场景冷却状态 / 所有者最近私聊活动）──────
# 全部按 owner 隔离：owner_id = 已绑定的所有者私聊会话（unified_msg_origin）。
# 只记「谁、哪个场景、什么时候、投递到哪一步」，绝不记生成文案与任何健康数值。
CARE_SEND_LOG_DDL = """
CREATE TABLE IF NOT EXISTS care_send_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- 自增行号
    owner_id    TEXT NOT NULL,      -- 所有者私聊会话标识（UMO），按 owner 隔离
    scenario    TEXT NOT NULL,      -- 场景：night / stress / wakeup / workout
    event_key   TEXT,               -- 关联的事件去重键；与该次事件无关时留 NULL
    sent_at     TEXT NOT NULL,      -- 本地投递时刻 YYYY-MM-DD HH:MM:SS
    delivery    TEXT NOT NULL,      -- 投递状态：reserved=已占冷却待确认 / sent=已确认送达
    updated_at  TEXT NOT NULL
);
"""

CARE_SEND_LOG_INDEX_DDL = """
CREATE INDEX IF NOT EXISTS idx_care_send_log_owner_scenario
    ON care_send_log (owner_id, scenario, sent_at);
"""

CARE_EVENT_KEY_DDL = """
CREATE TABLE IF NOT EXISTS care_event_key (
    owner_id   TEXT NOT NULL,       -- 所有者私聊会话标识（UMO）
    scenario   TEXT NOT NULL,       -- 场景
    event_key  TEXT NOT NULL,       -- 事件去重键（如夜间场景的「夜」标识）
    created_at TEXT NOT NULL,       -- 首次记录时刻 YYYY-MM-DD HH:MM:SS
    PRIMARY KEY (owner_id, scenario, event_key)
);
"""

CARE_SCENARIO_STATE_DDL = """
CREATE TABLE IF NOT EXISTS care_scenario_state (
    owner_id       TEXT NOT NULL,   -- 所有者私聊会话标识（UMO）
    scenario       TEXT NOT NULL,   -- 场景
    last_sent_at   TEXT,            -- 该场景最近一次「占冷却」时刻；NULL=从未发送
    last_event_key TEXT,            -- 最近一次发送关联的事件键；NULL=无
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (owner_id, scenario)
);
"""

CARE_OWNER_ACTIVITY_DDL = """
CREATE TABLE IF NOT EXISTS care_owner_activity (
    owner_id     TEXT PRIMARY KEY,  -- 所有者私聊会话标识（UMO）
    session      TEXT NOT NULL,     -- 最近一次私聊的会话标识（与 owner_id 同值，留作对照）
    last_seen_at TEXT NOT NULL,     -- 最近一次私聊活动时刻 YYYY-MM-DD HH:MM:SS
    updated_at   TEXT NOT NULL
);
"""

# 版本表：单行，记当前 schema 版本。
VERSION_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL                 -- 当前 schema 版本；见本模块顶部「schema 版本」
);
"""

# v1 基线要建的业务表（顺序=依赖顺序，彼此无外键，仅为可读性）。
BASELINE_TABLES: tuple[str, ...] = (
    DAILY_ACTIVITY_DDL,
    HEART_RATE_DDL,
    SLEEP_DDL,
    STRESS_DDL,
    SPO2_DDL,
    TRAINING_DDL,
)

BASELINE_INDEXES: tuple[str, ...] = (TRAINING_INDEX_DDL,)

# v2 新增的表。
SYNC_STATE_TABLES: tuple[str, ...] = (SYNC_STATE_DDL, META_DDL)

# 主动关怀（v3）新增的表与索引。
CARE_TABLES: tuple[str, ...] = (
    CARE_SEND_LOG_DDL,
    CARE_SEND_LOG_INDEX_DDL,
    CARE_EVENT_KEY_DDL,
    CARE_SCENARIO_STATE_DDL,
    CARE_OWNER_ACTIVITY_DDL,
)

# 关怀场景名（与 care_send_log.scenario / care_event_key.scenario 取值一致）。
CARE_SCENARIOS: tuple[str, ...] = ("night", "stress", "wakeup", "workout")

# 关怀投递状态：reserved=已占冷却、待确认送达；sent=已确认送达。
CARE_DELIVERY_VALUES: tuple[str, ...] = ("reserved", "sent")

# 关怀表名（自检用来确认该建的都建了）。
CARE_TABLE_NAMES: tuple[str, ...] = (
    CARE_SEND_LOG_TABLE, CARE_EVENT_KEY_TABLE,
    CARE_SCENARIO_STATE_TABLE, CARE_OWNER_ACTIVITY_TABLE,
)

# sleep_session 后来新增的列（列名, 声明类型）。CREATE TABLE IF NOT EXISTS 不给已存在的表补列，
# 旧库必须靠迁移链 ALTER TABLE 补上；新增列一律可空，旧行原样保留（新列为 NULL）。
SLEEP_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("fall_asleep_local", "TEXT"),
    ("wakeup_local", "TEXT"),
    ("nap_duration_min", "REAL"),
)

# training_session 后来新增的列（v4 碎片标记）；同上一律可空，旧行靠迁移回填。
TRAINING_ADDED_COLUMNS: tuple[tuple[str, str], ...] = (
    ("is_fragment", "INTEGER"),
)

# meta 表的键（口径见每行注释；值全部为文本）。
META_KEY_PLUGIN = "plugin"                      # 库属主标识（PLUGIN_ID）
META_KEY_SCHEMA_CREATED_AT = "schema_created_at"  # 该库首次纳入版本管理的时刻（旧库=首次迁移时刻）
META_KEY_SCHEMA_UPGRADED_AT = "schema_upgraded_at"  # 最近一次迁移完成的时刻
META_KEY_LAST_BACKUP_FILE = "last_backup_file"    # 最近一次升级前备份的副本文件名
META_KEY_LAST_BACKUP_AT = "last_backup_at"        # 上述备份的生成时刻


# ════════════════════════════════════════════════════════════════════
# 模型 → 表映射（写入 / 查询共用）
# ════════════════════════════════════════════════════════════════════
class ModelSpec(NamedTuple):
    """一个可写入模型对应的表信息。

    table       —— 表名；
    key_column  —— 主键列（写入时 ON CONFLICT 的冲突目标，也是 get() 的定位列）；
    date_column —— 日期列（按日期区间查询用）；
    columns     —— 写入列顺序，与 health_store._write_rows 的取值一一对应。
    """

    table: str
    key_column: str
    date_column: str
    columns: tuple[str, ...]


MODEL_SPECS: dict[str, ModelSpec] = {
    "daily_activity": ModelSpec(
        "daily_activity", "date", "date",
        ("date", "sport_type", "steps", "distance_m", "kcal", "duration_min",
         "walk_min", "active_hours", "exercise_min", "step_goal", "updated_at"),
    ),
    "heart_rate_sample": ModelSpec(
        "heart_rate_sample", "date", "date",
        ("date", "resting_hr", "day_hr", "average_resting_hr", "max_hr", "min_hr",
         "sample_kind", "source_type", "updated_at"),
    ),
    "sleep_session": ModelSpec(
        "sleep_session", "date", "date",
        ("date", "duration_min", "score", "efficiency", "hrv", "spo2",
         "fall_asleep_local", "wakeup_local", "nap_duration_min",
         "source_type", "updated_at"),
    ),
    "stress_sample": ModelSpec(
        "stress_sample", "date", "date",
        ("date", "average", "last_value", "max_value", "min_value", "measurements",
         "source_type", "updated_at"),
    ),
    "spo2_sample": ModelSpec(
        "spo2_sample", "date", "date",
        ("date", "spo2", "sample_kind", "updated_at"),
    ),
    "training_session": ModelSpec(
        "training_session", "session_key", "start_date",
        ("session_key", "sport_type", "start_ms", "end_ms", "start_local", "end_local",
         "start_date", "duration_min", "distance_m", "kcal", "segments", "device_code",
         "is_fragment", "updated_at"),
    ),
}

# 兼容既有导出：模型名 → (表名, 日期列)。
MODEL_TABLES: dict[str, tuple[str, str]] = {
    name: (spec.table, spec.date_column) for name, spec in MODEL_SPECS.items()
}

# 六张业务表（自检用来确认「该建的表都建了」，不含版本 / 元信息 / 同步状态表）。
BUSINESS_TABLES: tuple[str, ...] = tuple(
    dict.fromkeys(spec.table for spec in MODEL_SPECS.values())
)

# 同步状态表里「整轮同步」汇总行的 data_type 保留值，以及阶段失败原因的写法约定。
SYNC_STATUS_VALUES: tuple[str, ...] = ("ok", "partial", "failed", "skipped")

__all__ = [
    "SCHEMA_VERSION",
    "PLUGIN_ID",
    "LOCAL_DATE_FORMAT",
    "LOCAL_TIME_FORMAT",
    "BACKUP_STAMP_FORMAT",
    "VERSION_TABLE",
    "META_TABLE",
    "SYNC_STATE_TABLE",
    "CARE_SEND_LOG_TABLE",
    "CARE_EVENT_KEY_TABLE",
    "CARE_SCENARIO_STATE_TABLE",
    "CARE_OWNER_ACTIVITY_TABLE",
    "local_now",
    "MODEL_SPECS",
    "ModelSpec",
    "MODEL_TABLES",
    "BUSINESS_TABLES",
    "BASELINE_TABLES",
    "BASELINE_INDEXES",
    "SYNC_STATE_TABLES",
    "CARE_TABLES",
    "CARE_TABLE_NAMES",
    "CARE_SCENARIOS",
    "CARE_DELIVERY_VALUES",
    "SLEEP_ADDED_COLUMNS",
    "TRAINING_ADDED_COLUMNS",
    "META_KEY_PLUGIN",
    "META_KEY_SCHEMA_CREATED_AT",
    "META_KEY_SCHEMA_UPGRADED_AT",
    "META_KEY_LAST_BACKUP_FILE",
    "META_KEY_LAST_BACKUP_AT",
]
