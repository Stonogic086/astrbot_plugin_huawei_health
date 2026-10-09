#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""主动关怀（夜间 / 压力 / 起床 / 运动后四场景）+ schema v4 自检（离线、纯桩）。

覆盖：
  A. 迁移 v4
     * 新库一次建到 v4，四张 care_* 表 + 索引齐全、training_session 带 is_fragment 列；
     * 迁移幂等（重复执行无副作用、行数不变）；
     * 旧库（v3 有数据）原地无损升级到 v4，升级前生成备份副本，既有行原样保留，
       旧训练行按同一判据回填 is_fragment（碎片只标记、不删除）。
  B. 总开关：关闭时任何场景都不发（含「只开场景开关、总开关仍关」）。
  C. 冷启动基线 / 夜间窗口 / 每夜一次 / 两道发送闸：
     * 冷启动：某场景首次启用那一轮只登记、不发（夜间窗口内登记当夜；
       窗口外的首轮不消耗当夜，之后进窗口仍能发）；
     * 窗口外不发；
     * 窗口内 + 所有者近期有私聊活动 → 有候选；
     * 所有者近期无活动 → 无候选；
     * 同夜只发一次（第一次发完，同夜再判无候选）；
     * 每日上限：同一轮内第三个 reserve 被「每日额度余额」拦住；
     * 每轮每场景最多 1 条（同轮同场景第二次 reserve 被拦）。
  D. 发送前闸门（仅夜间）：拿不准（非 JSON / 布尔假 / 模型抛错）不发；true 才发；
     provider 不在白名单 / 未授权不发。
  E. 措辞退化：措辞模型失败时用程序侧信息展示文本发送，不报错；投递失败静默不发且占冷却。
  F. main.py 真实接线：记录私聊活动（只记私聊主人）→ 冷启动首轮只登记 →
    关怀循环取不到发送目标时不发且不报错。
  G. 压力关怀：档位边界（1–29 放松 / 30–59 正常 / 60–79 中等 / ≥80 偏高）、阈值可调、
     无数据 / 未达档位不触发、冷启动只登记、每天最多一条、白名单外与未授权退化为模板、
     数值不进日志、投递失败静默。
  H. 起床关怀：开场词四个时段分档、无数据 / 时间不匹配 / 日期不匹配不触发、小睡不限时段、
     冷启动只登记、同一条记录只触发一次、白名单外退化模板、措辞失败与投递失败静默。
  I. 运动后关怀：无数据 / 回看窗口外不触发、冷启动只登记、碎片不触发、
     时差分档（及时 vs 滞后）、每轮每场景最多 1 条、按 session_key 去重、
     白名单外退化为滞后模板、投递失败静默。
  J. main.py 接线：一轮巡检把四个场景都跑到（冷启动只登记、新事件才发）；
     单场景异常不影响其余场景。
  K. 碎片过滤端到端：6 条碎片 + 2 条有效训练，命令渲染 / LLM 摘要 / 运动后关怀
     三处同口径，只认有效训练（碎片只标记、不删除）。

安全约定：全部离线；数据都写在 tempfile 临时目录；发送用「只记录到列表」的空实现；
不碰真实 health.db、不碰插件数据目录、不改插件配置、不写任何日志到磁盘。

用法：python3 scripts/selftest_care.py
退出码：全部 PASS → 0；任一 FAIL → 1。
"""

from __future__ import annotations

import asyncio
import importlib
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPTS_DIR.parent
PKG = PLUGIN_ROOT.name

import_check = importlib.import_module("import_check")
import_check._install_astrbot_stub()
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

storage = importlib.import_module(f"{PKG}.storage")
schema = importlib.import_module(f"{PKG}.storage.schema")
migrations = importlib.import_module(f"{PKG}.storage.migrations")
care = importlib.import_module(f"{PKG}.services.care_monitor")
proactive = importlib.import_module(f"{PKG}.features.proactive_care")
reminder = importlib.import_module(f"{PKG}.reminder")
main_mod = importlib.import_module(f"{PKG}.main")
health_query = importlib.import_module(f"{PKG}.commands.health_query")
llm_injection = importlib.import_module(f"{PKG}.features.llm_injection")

HealthStore = storage.HealthStore
models = storage.models

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def _section(title: str) -> None:
    print(f"\n── {title} ──")


# ── 桩：框架上下文 ───────────────────────────────────────────────────────
class _Response:
    def __init__(self, text: str) -> None:
        self.completion_text = text


class _Conversation:
    def __init__(self, history: str) -> None:
        self.history = history


class _ConversationManager:
    def __init__(self, history: str) -> None:
        self._history = history

    async def get_curr_conversation_id(self, session):
        return "conv-1"

    async def get_conversation(self, session, conversation_id):
        return _Conversation(self._history)


class _PersonaManager:
    async def get_default_persona_v3(self, umo=None):
        return {"prompt": "你是测试人格，语气自然。"}


HISTORY = (
    '[{"role":"user","content":"今天加班到现在，眼睛快睁不开了"},'
    '{"role":"assistant","content":"那快休息呀"},'
    '{"role":"user","content":"还差一点点"},'
    '{"role":"user","content":"弄完了"}]'
)


class FakeContext:
    """最小框架上下文桩：llm_generate / 会话历史 / 人格 / provider / 发送。"""

    def __init__(self, *, decision='{"send_care":true}',
                 wording="夜深了，早点休息。",
                 provider="siliconflow/Qwen/Qwen3.5-4B",
                 raise_decision=False, raise_wording=False) -> None:
        self.conversation_manager = _ConversationManager(HISTORY)
        self.persona_manager = _PersonaManager()
        self._decision = decision
        self._wording = wording
        self._provider = provider
        self._raise_decision = raise_decision
        self._raise_wording = raise_wording
        self.decision_calls = 0
        self.wording_calls = 0
        self.prompts: list[str] = []
        self.sent: list[tuple] = []

    async def get_current_chat_provider_id(self, umo):
        return self._provider

    async def llm_generate(self, *, chat_provider_id, prompt, system_prompt):
        self.prompts.append(prompt)
        if "发送闸门" in system_prompt:
            self.decision_calls += 1
            if self._raise_decision:
                raise RuntimeError("decision boom")
            return _Response(self._decision)
        self.wording_calls += 1
        if self._raise_wording:
            raise RuntimeError("wording boom")
        return _Response(self._wording)

    async def send_message(self, umo, chain):
        self.sent.append((umo, chain))


# ── 桩：event ────────────────────────────────────────────────────────────
class FakeEvent:
    """最小 event 桩：is_admin=True（主人）+ 私聊 UMO。"""

    def __init__(self, umo: str, *, admin: bool = True) -> None:
        self.unified_msg_origin = umo
        self._admin = admin

    def is_admin(self):
        return self._admin


def make_store(tmp_dir: Path, name: str) -> HealthStore:
    store = HealthStore(tmp_dir / name)
    store.initialize()
    return store


def prime(monitor: Any, *scenarios: str) -> None:
    """把指定场景标记为「已过首轮基线」，让后续判定走稳态。

    等价于生产里的「该场景已经跑过至少一轮巡检」：冷启动那一轮只登记、不发送，
    之后才是稳态判定（该发就发）。只写场景状态行，不记发送、不占冷却。
    """
    for scenario in scenarios:
        monitor.mark_scenario_initialized(scenario)


def enabled_settings(**overrides) -> care.CareSettings:
    base = dict(
        master_enabled=True, night_enabled=True,
        night_start="00:30", night_end="06:00")
    base.update(overrides)
    return care.CareSettings(**base)


OWNER = "aiocqhttp:FriendMessage:10001"


def only_settings(scenario: str, **overrides) -> care.CareSettings:
    """只开总开关 + 指定场景（其余场景保持出厂默认关）。"""
    base: dict = {"master_enabled": True, f"{scenario}_enabled": True}
    base.update(overrides)
    return care.CareSettings(**base)


class CollectLogger:
    """收集日志行的假 logger（只用于断言「数值不进日志」，不写盘）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _log(self, level, message, *args):
        try:
            self.lines.append(message % args if args else str(message))
        except Exception:
            self.lines.append(str(message))

    info = warning = error = debug = _log


def today_stamp(delta_minutes: int = 0) -> str:
    """当前本地时刻（可偏移若干分钟）的库口径文本。"""
    return (datetime.now() + timedelta(minutes=delta_minutes)).strftime(
        "%Y-%m-%d %H:%M:%S")


# ══════════════════════════════════════════════════════════════════════════
# A. 迁移 v4
# ══════════════════════════════════════════════════════════════════════════
def test_migration_v4(tmp_dir: Path) -> None:
    _section("A1. 新库一次建到 v4，care_* 表齐全 + training_session 带 is_fragment 列")
    fresh = tmp_dir / "fresh.db"
    fresh_conn = sqlite3.connect(str(fresh))
    try:
        outcome = migrations.apply(fresh_conn)
    finally:
        fresh_conn.close()
    check("schema 版本常量 = 4", schema.SCHEMA_VERSION == 4,
          f"SCHEMA_VERSION={schema.SCHEMA_VERSION}")
    check("新库直接建到 v4",
          outcome.from_version == 0 and outcome.to_version == 4,
          f"{outcome.from_version}->{outcome.to_version}")
    names = set(HealthStore(fresh).tables())
    check("四张 care_* 表齐全",
          set(schema.CARE_TABLE_NAMES) <= names, f"tables={sorted(names)}")
    check("training_session 已带 v4 碎片标记列 is_fragment",
          "is_fragment" in _columns(fresh, "training_session"),
          f"columns={_columns(fresh, 'training_session')}")

    _section("A2. 迁移幂等（重复执行无副作用）")
    before = _dump(fresh)
    fresh_conn = sqlite3.connect(str(fresh))
    try:
        second = migrations.apply(fresh_conn)
    finally:
        fresh_conn.close()
    check("已达 v4 → 本次不执行任何步骤",
          second.applied == () and second.to_version == 4,
          f"applied={list(second.applied)}")
    check("表内容与版本不变",
          _dump(fresh) == before and _version(fresh) == 4)

    _section("A3. 旧库（v3 有数据 + 旧训练行）无损升级到 v4 + 升级前备份 + 回填 is_fragment")
    v3db = tmp_dir / "legacy_v3.db"
    store = HealthStore(v3db)
    original = migrations.MIGRATIONS
    migrations.MIGRATIONS = tuple(m for m in original if m.version <= 3)
    try:
        store.initialize()      # 建到 v3
    finally:
        migrations.MIGRATIONS = original
    check("旧库先被建到 v3", store.schema_version() == 3,
          f"version={store.schema_version()}")
    store.upsert_rows("sleep_session", [
        {"date": "2026-10-08", "duration_min": 388.0, "score": 80.0,
         "source_type": 9, "updated_at": "2026-10-08 20:52:47"}])
    # v3 的 training_session 没有 is_fragment 列，旧训练行只能用裸 SQL 造（模拟升级前的库）。
    with sqlite3.connect(str(v3db)) as conn:
        conn.executemany(
            "INSERT INTO training_session (session_key, sport_type, start_ms, end_ms,"
            " start_local, end_local, start_date, duration_min, distance_m, kcal,"
            " segments, device_code, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [("4:1000", 4, 1, 2, "2026-10-08 18:00:00", "2026-10-08 18:30:00",
              "2026-10-08", 30, 5000, 200.0, 1, "band-x", "2026-10-08 18:40:00"),
             ("4:2000", 4, 3, 4, "2026-10-08 19:00:00", "2026-10-08 19:01:00",
              "2026-10-08", 1, 30, 5.0, 1, "band-x", "2026-10-08 19:05:00")])
        conn.commit()
    check("升级前旧训练行的 is_fragment 未标记（全为 NULL）",
          _flags(v3db) == [None, None], f"flags={_flags(v3db)}")
    check("升级前库已是 v3（care_* 表已存在，v4 只补碎片标记）",
          set(schema.CARE_TABLE_NAMES) <= set(store.tables()))
    upgraded = HealthStore(v3db)
    upgraded.initialize()       # 原地升到 v4
    check("旧库升级到 v4", upgraded.schema_version() == 4,
          f"version={upgraded.schema_version()}")
    check("新增 care_* 表齐全",
          set(schema.CARE_TABLE_NAMES) <= set(upgraded.tables()))
    row = upgraded.get("sleep_session", "2026-10-08")
    check("既有业务数据无损（时长/评分原样）",
          row is not None and row["duration_min"] == 388.0 and row["score"] == 80.0,
          f"row={row}")
    valid = upgraded.get("training_session", "4:1000")
    fragment = upgraded.get("training_session", "4:2000")
    check("旧训练行按同一判据回填 is_fragment（有效=0 / 碎片=1），且不丢行",
          valid is not None and valid["is_fragment"] == 0
          and fragment is not None and fragment["is_fragment"] == 1
          and upgraded.count("training_session") == 2,
          f"valid={valid and valid['is_fragment']} fragment={fragment and fragment['is_fragment']}")
    backups = list(v3db.parent.glob("legacy_v3.backup-*.db"))
    check("升级前生成了备份副本", len(backups) == 1, f"backups={[b.name for b in backups]}")


def _columns(path: Path, table: str) -> list[str]:
    """读某表的列名（只读打开临时库；用于断言 v4 的 is_fragment 列）。"""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def _flags(path: Path) -> list[Any]:
    """读 training_session 的全部 is_fragment 值（NULL = 尚未标记）。"""
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return [row[0] for row in conn.execute(
            "SELECT is_fragment FROM training_session ORDER BY session_key")]


def _dump(path: Path) -> dict:
    data: dict = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for (name,) in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"):
            data[name] = conn.execute(f"SELECT * FROM {name}").fetchall()
    return data


def _version(path: Path) -> int:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        return int(conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()[0])


# ══════════════════════════════════════════════════════════════════════════
# B. 总开关
# ══════════════════════════════════════════════════════════════════════════
def test_master_switch(tmp_dir: Path) -> None:
    _section("B. 总开关关闭时任何场景都不发")
    off = care.care_settings_from_config({})
    check("默认配置：总开关关闭", off.master_enabled is False)
    check("默认配置：四场景全关",
          not off.night_enabled and not off.stress_enabled
          and not off.wakeup_enabled and not off.workout_enabled)

    grouped = care.care_settings_from_config({"proactive_care": {
        "enable_proactive_care": False, "enable_night_care": True,
        "enable_stress_care": True, "enable_wakeup_care": True,
        "enable_workout_care": True}})
    check("总开关关：即使场景开关为真也一律折算为关",
          grouped.master_enabled is False
          and not grouped.night_enabled and not grouped.stress_enabled
          and not grouped.wakeup_enabled and not grouped.workout_enabled)
    check("总开关关：any_enabled = False", grouped.any_enabled is False)

    on = care.care_settings_from_config({"proactive_care": {
        "enable_proactive_care": True, "enable_night_care": True}})
    check("总开关开 + 夜间开：night_enabled = True", on.night_enabled is True)
    check("总开关开：其余三场景仍默认关",
          not on.stress_enabled and not on.wakeup_enabled and not on.workout_enabled)

    flat = care.care_settings_from_config(
        {"enable_proactive_care": True, "enable_night_care": True})
    check("扁平 key 布局同样生效", flat.night_enabled is True)

    store = make_store(tmp_dir, "master.db")
    now = datetime(2026, 10, 9, 1, 30, 0)
    store.touch_owner_activity(OWNER, when=now.strftime("%Y-%m-%d %H:%M:%S"))
    monitor = care.CareMonitor(store, OWNER, grouped, now=lambda: now)
    check("总开关关：夜间候选为 None", monitor.night_candidate() is None)


# ══════════════════════════════════════════════════════════════════════════
# C. 夜间窗口 / 每夜一次
# ══════════════════════════════════════════════════════════════════════════
def test_night_window_and_dedupe(tmp_dir: Path) -> None:
    _section("C1. 纯函数：窗口与夜键")
    check("跨零点窗口 23:00–06:00 含 01:00",
          care.in_window(datetime(2026, 10, 9, 1, 0).time(),
                         care.parse_clock("23:00", None),
                         care.parse_clock("06:00", None)) is True)
    check("跨零点窗口 23:00–06:00 不含 12:00",
          care.in_window(datetime(2026, 10, 9, 12, 0).time(),
                         care.parse_clock("23:00", None),
                         care.parse_clock("06:00", None)) is False)
    check("同一天窗口 00:30–06:00 含 03:00",
          care.in_window(datetime(2026, 10, 9, 3, 0).time(),
                         care.parse_clock("00:30", None),
                         care.parse_clock("06:00", None)) is True)
    check("start==end 视为空窗口",
          care.in_window(datetime(2026, 10, 9, 0, 30).time(),
                         care.parse_clock("00:30", None),
                         care.parse_clock("00:30", None)) is False)
    check("跨零点夜键：23:40 与次日 01:00 归到同一夜",
          care.night_key(datetime(2026, 10, 8, 23, 40),
                         care.parse_clock("23:00", None),
                         care.parse_clock("06:00", None))
          == care.night_key(datetime(2026, 10, 9, 1, 0),
                            care.parse_clock("23:00", None),
                            care.parse_clock("06:00", None)))

    _section("C2. 冷启动首轮只登记 / 窗口外不发 / 近期无活动不发 / 每夜只发一次")
    store = make_store(tmp_dir, "night.db")
    settings = enabled_settings()
    clock = [datetime(2026, 10, 9, 1, 30, 0)]
    store.touch_owner_activity(OWNER, when=clock[0].strftime("%Y-%m-%d %H:%M:%S"))
    monitor = care.CareMonitor(store, OWNER, settings, now=lambda: clock[0])

    # 冷启动：该场景首次启用那一轮只登记当夜、不发送。
    monitor.begin_round()
    check("冷启动首轮：窗口内只登记当夜、不发（无候选）",
          monitor.night_candidate() is None
          and monitor.scenario_initialized(care.NIGHT_SCENARIO) is True)
    check("冷启动首轮把当夜登记为已处理",
          store.care_event_seen(OWNER, care.NIGHT_SCENARIO, "2026-10-09") is True)

    # 次夜＝新事件（新的夜键，尚未登记），用它验证窗口 / 近期活动两条规则。
    clock[0] = datetime(2026, 10, 10, 1, 30, 0)
    store.touch_owner_activity(OWNER, when=clock[0].strftime("%Y-%m-%d %H:%M:%S"))

    clock[0] = datetime(2026, 10, 10, 3, 0, 0)   # 窗口内，但活动已过 90 分钟
    monitor.begin_round()
    check("窗口内但近期无活动（>45 分钟）→ 无候选",
          monitor.night_candidate() is None)

    clock[0] = datetime(2026, 10, 10, 7, 0, 0)   # 窗口外
    monitor.begin_round()
    check("窗口外 → 无候选", monitor.night_candidate() is None)

    clock[0] = datetime(2026, 10, 10, 1, 30, 0)  # 回到窗口内 + 近期有活动
    store.touch_owner_activity(OWNER, when=clock[0].strftime("%Y-%m-%d %H:%M:%S"))
    monitor.begin_round()
    check("窗口内 + 近期有活动 → 有候选",
          monitor.night_candidate() is not None)

    context = FakeContext()
    care_engine = proactive.ProactiveCare(
        context, send=lambda text: (context.sent.append(("sync", text)) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    monitor.begin_round()
    first = asyncio.run(care_engine.run_night(monitor, OWNER))
    check("次夜第二轮：发出 1 条", first.get("sent") is True, f"result={first}")
    check("同夜再次判定 → 无候选（每夜一次）", monitor.night_candidate() is None)
    monitor.begin_round()
    second = asyncio.run(care_engine.run_night(monitor, OWNER))
    check("同夜再跑一轮：不再发送",
          second.get("sent") is False and second.get("reason") == "no_candidate",
          f"result={second}")
    check("发送记录只有 1 行",
          store.care_send_count_since(OWNER, "2026-10-10 00:00:00") == 1)
    check("事件去重键已落库",
          store.care_event_seen(OWNER, care.NIGHT_SCENARIO, "2026-10-10") is True)

    _section("C3. 每日额度余额：同轮第三个 reserve 被额度余额拦住")
    store3 = make_store(tmp_dir, "cap.db")
    clock3 = [datetime(2026, 10, 9, 2, 0, 0)]
    store3.touch_owner_activity(OWNER, when=clock3[0].strftime("%Y-%m-%d %H:%M:%S"))
    monitor3 = care.CareMonitor(store3, OWNER, settings, now=lambda: clock3[0],
                                daily_limit=2)
    monitor3.begin_round()
    reserved = [monitor3.reserve(care.CareFinding(scenario, f"cap-{scenario}", "事实"))
                for scenario in (care.NIGHT_SCENARIO, care.STRESS_SCENARIO,
                                 care.WAKEUP_SCENARIO)]
    check("同轮前两条 reserve 成功、第三条被额度余额拦住",
          reserved == [True, True, False],
          f"reserve={reserved} sends_today={monitor3.sends_today()}")
    check("被拦的第三条没有落库（当日仍 2 条）",
          store3.care_send_count_since(OWNER, "2026-10-09 00:00:00") == 2)
    check("当日已达每日上限", monitor3.daily_limit_reached() is True)

    # 场景侧同样在余额耗尽后由 daily_limit 分支拦住（断言 reason）。
    store4 = make_store(tmp_dir, "cap_run.db")
    store4.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 420.0,
        "wakeup_local": "2026-10-09 01:55:00"}])
    store4.record_care_send(OWNER, care.STRESS_SCENARIO, when="2026-10-09 00:10:00")
    store4.record_care_send(OWNER, care.NIGHT_SCENARIO, when="2026-10-09 00:20:00")
    monitor4 = care.CareMonitor(store4, OWNER, only_settings("wakeup"),
                                now=lambda: clock3[0], daily_limit=2)
    prime(monitor4, care.WAKEUP_SCENARIO)
    monitor4.begin_round()
    engine4 = proactive.ProactiveCare(
        FakeContext(), send=lambda text: True, authorized_getter=lambda: True,
        allowlist_getter=lambda: ["siliconflow"])
    result4 = asyncio.run(engine4.run_wakeup(monitor4, OWNER))
    check("当日额度已用尽 → 本轮不发且 reason=daily_limit",
          result4.get("sent") is False and result4.get("reason") == "daily_limit",
          f"result={result4}")

    _section("C4. 每轮每场景最多 1 条（同轮同场景第二次 reserve 被拦）")
    store5 = make_store(tmp_dir, "round_cap.db")
    monitor5 = care.CareMonitor(store5, OWNER, settings, now=lambda: clock3[0])
    monitor5.begin_round()
    same_round = [monitor5.reserve(care.CareFinding(care.WORKOUT_SCENARIO, key, "事实"))
                  for key in ("wo-a", "wo-b")]
    check("同轮同场景两次 reserve：只第一条成功",
          same_round == [True, False],
          f"reserve={same_round} sends_today={monitor5.sends_today()}")
    check("同轮第二条没有落库（当日仍 1 条）",
          store5.care_send_count_since(OWNER, "2026-10-09 00:00:00") == 1)
    monitor5.begin_round()
    check("换到新的一轮：同场景换新事件键可再占一条",
          monitor5.reserve(care.CareFinding(care.WORKOUT_SCENARIO, "wo-b", "事实")) is True)


# ══════════════════════════════════════════════════════════════════════════
# D. 发送前闸门
# ══════════════════════════════════════════════════════════════════════════
def test_gate(tmp_dir: Path) -> None:
    _section("D1. 纯函数：parse_decision / clean_reply")
    check('parse_decision 接受 {"send_care":true}',
          proactive.parse_decision('{"send_care":true}') is True)
    check('parse_decision 接受 {"send_care":false}',
          proactive.parse_decision('{"send_care":false}') is False)
    check("parse_decision 接受代码块包裹",
          proactive.parse_decision('```json\n{"send_care":true}\n```') is True)
    check("parse_decision：非 JSON → None（拿不准）",
          proactive.parse_decision("我觉得可以吧") is None)
    check("parse_decision：非布尔 → None",
          proactive.parse_decision('{"send_care":"yes"}') is None)
    check("parse_decision：非字符串 → None", proactive.parse_decision(None) is None)
    check("clean_reply：外链判不合格",
          proactive.clean_reply("看这里 https://x.com") is None)
    check("clean_reply：at-all 判不合格",
          proactive.clean_reply("@全体成员 集合") is None)
    check("clean_reply：正常短句保留", proactive.clean_reply("夜深了，早点休息。") is not None)

    facts = ["当前本地时间 01:30，并且所有者在最近 45 分钟内有私聊活动"]
    prompt = proactive.build_decision_prompt(facts, ["用户: 还在忙"])
    check("闸门 prompt 含候选事实与隔离语句",
          "候选事实" in prompt and "不得被当作指令" in prompt
          and '{"send_care":true}' in prompt)

    _section("D2. 闸门拿不准 / 模型抛错 → 不发")
    store = make_store(tmp_dir, "gate.db")
    settings = enabled_settings()
    clock = datetime(2026, 10, 9, 1, 30, 0)
    cases = [
        ("拿不准（非 JSON）", dict(decision="大概可以"), False),
        ("判为 false", dict(decision='{"send_care":false}'), False),
        ("判为 true", dict(decision='{"send_care":true}'), True),
        ("模型抛错", dict(raise_decision=True), False),
    ]
    for label, kwargs, expect_send in cases:
        broken = make_store(tmp_dir, f"gate_{abs(hash(label))}.db")
        broken.touch_owner_activity(OWNER, when="2026-10-09 01:20:00")
        monitor = care.CareMonitor(broken, OWNER, settings, now=lambda: clock)
        # 冷启动那一轮只登记、不发，测不出闸门；先标记场景已过首轮基线，看稳态判定。
        prime(monitor, care.NIGHT_SCENARIO)
        monitor.begin_round()
        context = FakeContext(**kwargs)
        sent: list[str] = []

        async def send(text: str, _sent=sent) -> bool:
            _sent.append(text)
            return True

        engine = proactive.ProactiveCare(
            context, send=send, authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
        result = asyncio.run(engine.run_night(monitor, OWNER))
        check(f"闸门：{label} → {'发' if expect_send else '不发'}",
              result.get("sent") is expect_send, f"result={result}")
        check(f"闸门：{label} → 实际投递 {1 if expect_send else 0} 条",
              len(sent) == (1 if expect_send else 0), f"sent={sent}")
        check(f"闸门：{label} → 闸门模型恰好被问 1 次，且拦在 gate",
              context.decision_calls == 1
              and (expect_send or result.get("reason") == "gate"),
              f"decision_calls={context.decision_calls} result={result}")

    _section("D3. provider 不在白名单 / 未授权 → 不发")
    store = make_store(tmp_dir, "gate2.db")
    store.touch_owner_activity(OWNER, when="2026-10-09 01:20:00")
    monitor = care.CareMonitor(store, OWNER, settings, now=lambda: clock)
    prime(monitor, care.NIGHT_SCENARIO)
    monitor.begin_round()

    off_list_ctx = FakeContext(provider="openai/gpt-4o")
    engine = proactive.ProactiveCare(
        off_list_ctx, send=lambda text: True, authorized_getter=lambda: True,
        allowlist_getter=lambda: ["siliconflow"])
    result = asyncio.run(engine.run_night(monitor, OWNER))
    check("provider 不在白名单 → 不发（且未调用闸门模型）",
          result.get("sent") is False and off_list_ctx.decision_calls == 0
          and result.get("reason") == "gate",
          f"result={result}")

    store2 = make_store(tmp_dir, "gate3.db")
    store2.touch_owner_activity(OWNER, when="2026-10-09 01:20:00")
    monitor2 = care.CareMonitor(store2, OWNER, settings, now=lambda: clock)
    prime(monitor2, care.NIGHT_SCENARIO)
    monitor2.begin_round()
    unauthorized = FakeContext()
    engine2 = proactive.ProactiveCare(
        unauthorized, send=lambda text: True, authorized_getter=lambda: False,
        allowlist_getter=lambda: ["siliconflow"])
    result2 = asyncio.run(engine2.run_night(monitor2, OWNER))
    check("未授权（隐私闸门关闭）→ 不发",
          result2.get("sent") is False and result2.get("reason") == "gate"
          and unauthorized.decision_calls == 0, f"result={result2}")

    _section("D4. 无上下文 → 闸门前置条件不满足 → 不发")
    store3 = make_store(tmp_dir, "gate4.db")
    store3.touch_owner_activity(OWNER, when="2026-10-09 01:20:00")
    monitor3 = care.CareMonitor(store3, OWNER, settings, now=lambda: clock)
    prime(monitor3, care.NIGHT_SCENARIO)
    monitor3.begin_round()
    empty_ctx = FakeContext()
    empty_ctx.conversation_manager = _ConversationManager("[]")
    engine3 = proactive.ProactiveCare(
        empty_ctx, send=lambda text: True, authorized_getter=lambda: True,
        allowlist_getter=lambda: ["siliconflow"])
    result3 = asyncio.run(engine3.run_night(monitor3, OWNER))
    check("无最近私聊上下文 → 不发",
          result3.get("sent") is False and empty_ctx.decision_calls == 0,
          f"result={result3}")


# ══════════════════════════════════════════════════════════════════════════
# E. 措辞退化 / 投递失败
# ══════════════════════════════════════════════════════════════════════════
def test_wording_fallback(tmp_dir: Path) -> None:
    _section("E1. 措辞模型失败 → 用程序侧信息展示文本发送，不报错")
    store = make_store(tmp_dir, "wording.db")
    clock = datetime(2026, 10, 9, 1, 30, 0)
    store.touch_owner_activity(OWNER, when="2026-10-09 01:20:00")
    monitor = care.CareMonitor(store, OWNER, enabled_settings(), now=lambda: clock)
    prime(monitor, care.NIGHT_SCENARIO)
    monitor.begin_round()
    context = FakeContext(raise_wording=True)
    delivered: list[str] = []

    async def send(text: str) -> bool:
        delivered.append(text)
        return True

    engine = proactive.ProactiveCare(
        context, send=send, authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    result = asyncio.run(engine.run_night(monitor, OWNER))
    check("措辞失败仍发送（退化为信息展示文本）",
          result.get("sent") is True and len(delivered) == 1, f"result={result}")
    check("兜底文案是程序拼的「夜深了…」",
          delivered and delivered[0].startswith("夜深了"), f"sent={delivered}")

    _section("E2. 投递失败 → 静默不发（不报错）+ 已占冷却（不重发）")
    store2 = make_store(tmp_dir, "deliver.db")
    store2.touch_owner_activity(OWNER, when="2026-10-09 01:20:00")
    monitor2 = care.CareMonitor(store2, OWNER, enabled_settings(), now=lambda: clock)
    prime(monitor2, care.NIGHT_SCENARIO)
    monitor2.begin_round()

    async def boom_send(text: str) -> bool:
        raise RuntimeError("send boom")

    engine2 = proactive.ProactiveCare(
        FakeContext(), send=boom_send, authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    result2 = asyncio.run(engine2.run_night(monitor2, OWNER))
    check("投递抛异常 → 整体不崩溃、sent=False",
          result2.get("ok") is True and result2.get("sent") is False,
          f"result={result2}")
    check("投递失败后事件已占（同夜不再重发）",
          monitor2.night_candidate() is None)


# ══════════════════════════════════════════════════════════════════════════
# F. main.py 接线
# ══════════════════════════════════════════════════════════════════════════
def test_main_wiring(tmp_dir: Path) -> None:
    _section("F1. 记录私聊活动：只记私聊主人")
    store = make_store(tmp_dir, "wiring.db")
    context = FakeContext()
    plugin = main_mod.HuaweiHealthPlugin(
        context, {"proactive_care": {"enable_proactive_care": True,
                                     "enable_night_care": True}})
    plugin.store = store
    plugin._reminder_state = reminder.ReminderState(tmp_dir / "reminder_state.json")

    asyncio.run(plugin.remember_owner_private_activity(FakeEvent(OWNER)))
    check("私聊主人活动已记录",
          store.last_owner_activity(OWNER) is not None)
    check("顺带绑定了发送目标", plugin._reminder_state.notify_umo == OWNER)

    group_umo = "aiocqhttp:GroupMessage:20002"
    asyncio.run(plugin.remember_owner_private_activity(FakeEvent(group_umo)))
    check("群聊来源不记为活动", store.last_owner_activity(group_umo) is None)

    outsider = "aiocqhttp:FriendMessage:99999"
    asyncio.run(plugin.remember_owner_private_activity(FakeEvent(outsider, admin=False)))
    check("非主人不记为活动", store.last_owner_activity(outsider) is None)

    _section("F2. 取不到发送目标 → 不发且不报错")
    plugin._reminder_state = reminder.ReminderState(tmp_dir / "empty_reminder.json")
    plugin._care = proactive.ProactiveCare(
        context, send=plugin._send_owner_message, authorized_getter=lambda: True,
        allowlist_getter=lambda: ["siliconflow"])
    plugin.care_settings = enabled_settings()
    result = asyncio.run(plugin._run_care_round())
    check("无目标 → skip no_target 且不抛错",
          result.get("ok") is True and result.get("reason") == "no_target",
          f"result={result}")
    check("无目标 → 未发任何消息", plugin.context.sent == [])

    _section("F3. 真实接线跑通一轮夜间关怀（含冷启动首轮只登记）")
    now = datetime.now()
    plugin.care_settings = enabled_settings(
        night_start=(now - timedelta(hours=1)).strftime("%H:%M"),
        night_end=(now + timedelta(hours=1)).strftime("%H:%M"))
    plugin._reminder_state = reminder.ReminderState(tmp_dir / "bound_reminder.json")
    plugin._reminder_state.notify_umo = OWNER
    plugin._care = proactive.ProactiveCare(
        context, send=plugin._send_owner_message, authorized_getter=lambda: True,
        allowlist_getter=lambda: ["siliconflow"])
    store.touch_owner_activity(OWNER)
    context.sent.clear()

    # 冷启动那一轮：接线本身跑通，但夜间场景只登记当夜、不发。
    cold = asyncio.run(plugin._run_care_round())
    check("接线冷启动一轮：夜间场景只登记当夜、不发",
          cold.get("sent") is False
          and cold["scenarios"]["night"]["reason"] == "no_candidate"
          and store.care_scenario_state(OWNER, care.NIGHT_SCENARIO) is not None,
          f"result={cold} sent={context.sent}")

    # 稳态：新库 + 该场景的基线行（＝「本夜之前就启用过」），本夜尚未登记去重键。
    store2 = make_store(tmp_dir, "wiring_live.db")
    store2.set_care_scenario_state(
        OWNER, care.NIGHT_SCENARIO,
        when=(now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"))
    store2.touch_owner_activity(OWNER)
    plugin.store = store2
    context.sent.clear()
    result = asyncio.run(plugin._run_care_round())
    check("接线一轮：真实私聊发送被调用 1 次",
          result.get("sent") is True and len(context.sent) == 1,
          f"result={result} sent={context.sent}")
    check("发往已绑定的主人私聊",
          context.sent and context.sent[0][0] == OWNER)
    second = asyncio.run(plugin._run_care_round())
    check("接线再跑一轮：同夜不重发",
          second.get("sent") is False, f"result={second}")


# ══════════════════════════════════════════════════════════════════════════
# G. 压力关怀（当日日均分定档）
# ══════════════════════════════════════════════════════════════════════════
def test_stress_care(tmp_dir: Path) -> None:
    _section("G1. 纯函数：压力档位边界与阈值比较")
    cases = [(0.9, None), (1, "relaxed"), (29, "relaxed"), (29.9, "normal"),
             (30, "normal"), (59, "normal"), (60, "moderate"), (79, "moderate"),
             (80, "high"), (99, "high"), (100, "high"), (None, None), ("", None)]
    check("stress_grade 边界（<1 无 / 1–29 放松 / 30–59 正常 / 60–79 中等 / ≥80 偏高；29 进 29.9 出）",
          all(care.stress_grade(v) == e for v, e in cases),
          f"{[(v, care.stress_grade(v)) for v, _ in cases]}")
    check("阈值 moderate：放松/正常不达标，中等/偏高达标",
          not care.stress_grade_meets("relaxed", "moderate")
          and not care.stress_grade_meets("normal", "moderate")
          and care.stress_grade_meets("moderate", "moderate")
          and care.stress_grade_meets("high", "moderate"))
    check("阈值 relaxed：放松即达标",
          care.stress_grade_meets("relaxed", "relaxed"))
    check("阈值 high：只有偏高达标",
          not care.stress_grade_meets("moderate", "high")
          and care.stress_grade_meets("high", "high"))
    tuned = care.care_settings_from_config({"proactive_care": {
        "enable_proactive_care": True, "enable_stress_care": True,
        "stress_threshold": "偏高"}})
    bogus = care.care_settings_from_config({"proactive_care": {
        "enable_proactive_care": True, "enable_stress_care": True,
        "stress_threshold": "不存在的档"}})
    check("档位配置可调：中文名归一 / 非法回退默认 moderate",
          tuned.stress_threshold == "high" and bogus.stress_threshold == "moderate",
          f"tuned={tuned.stress_threshold} bogus={bogus.stress_threshold}")
    check("压力措辞指令明确禁止医疗建议与诊断",
          "医疗建议" in proactive.STRESS_COMPOSE_INSTRUCTION
          and "诊断" in proactive.STRESS_COMPOSE_INSTRUCTION)

    now = datetime(2026, 10, 9, 10, 0, 0)

    _section("G2. 无数据 / 未达档位不触发")
    empty = make_store(tmp_dir, "stress_empty.db")
    empty_monitor = care.CareMonitor(empty, OWNER, only_settings("stress"),
                                     now=lambda: now)
    prime(empty_monitor, care.STRESS_SCENARIO)
    check("当日无压力行 → 无候选", empty_monitor.stress_candidate() is None)

    normal = make_store(tmp_dir, "stress_normal.db")
    normal.upsert_rows("stress_sample", [{"date": "2026-10-09", "source_type": 11, "average": 45.0}])
    moderate_monitor = care.CareMonitor(normal, OWNER, only_settings("stress"),
                                        now=lambda: now)
    prime(moderate_monitor, care.STRESS_SCENARIO)
    check("日均 45（正常）< 阈值 moderate → 无候选",
          moderate_monitor.stress_candidate() is None)
    relaxed_monitor = care.CareMonitor(
        normal, OWNER, only_settings("stress", stress_threshold="relaxed"),
        now=lambda: now)
    prime(relaxed_monitor, care.STRESS_SCENARIO)
    check("阈值调 relaxed：日均 45 触发",
          relaxed_monitor.stress_candidate() is not None)
    high_monitor = care.CareMonitor(
        normal, OWNER, only_settings("stress", stress_threshold="high"),
        now=lambda: now)
    prime(high_monitor, care.STRESS_SCENARIO)
    check("阈值调 high：日均 45 不触发",
          high_monitor.stress_candidate() is None)
    off_monitor = care.CareMonitor(
        normal, OWNER, care.CareSettings(master_enabled=True), now=lambda: now)
    prime(off_monitor, care.STRESS_SCENARIO)
    check("场景开关关闭 → 无候选", off_monitor.stress_candidate() is None)

    _section("G3. 冷启动只登记 / 达档触发一次 + 每天最多一条 + 数值不进日志")
    # 冷启动：该场景首次启用那一轮，当天已有压力行 → 只登记当天、不发。
    cold_store = make_store(tmp_dir, "stress_cold.db")
    cold_store.upsert_rows("stress_sample", [{"date": "2026-10-09", "source_type": 11, "average": 88.0}])
    cold_ctx = FakeContext(wording="今天压力有点大，别太累了。")
    cold_ctx.sent = []
    cold_monitor = care.CareMonitor(cold_store, OWNER, only_settings("stress"),
                                    now=lambda: now)
    cold_engine = proactive.ProactiveCare(
        cold_ctx, send=lambda text: (cold_ctx.sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    cold_monitor.begin_round()
    cold = asyncio.run(cold_engine.run_stress(cold_monitor, OWNER))
    check("冷启动首轮：只登记当天、不发（且未调用措辞模型）",
          cold.get("sent") is False and cold.get("reason") == "no_candidate"
          and cold_ctx.wording_calls == 0
          and cold_store.care_event_seen(OWNER, care.STRESS_SCENARIO, "2026-10-09") is True
          and cold_monitor.scenario_initialized(care.STRESS_SCENARIO) is True,
          f"result={cold}")

    store = make_store(tmp_dir, "stress_fire.db")
    store.upsert_rows("stress_sample", [{"date": "2026-10-09", "source_type": 11, "average": 88.0}])
    log = CollectLogger()
    monitor = care.CareMonitor(store, OWNER, only_settings("stress"),
                               now=lambda: now, logger=log)
    prime(monitor, care.STRESS_SCENARIO)      # 已过首轮基线 → 走稳态判定
    context = FakeContext(wording="今天压力有点大，别太累了。")
    context.sent = []
    engine = proactive.ProactiveCare(
        context, send=lambda text: (context.sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"], logger=log)
    log.lines.clear()
    monitor.begin_round()
    first = asyncio.run(engine.run_stress(monitor, OWNER))
    check("达档：发出 1 条",
          first.get("sent") is True and len(context.sent) == 1, f"result={first}")
    check("非夜间场景未调用发送前闸门模型", context.decision_calls == 0)
    check("措辞来自模型（非模板）",
          bool(context.sent) and context.sent[0] == "今天压力有点大，别太累了。",
          f"sent={context.sent}")
    monitor.begin_round()
    second = asyncio.run(engine.run_stress(monitor, OWNER))
    check("每天最多一条：同日再判不再发送",
          second.get("sent") is False and second.get("reason") == "no_candidate",
          f"result={second}")
    check("事件去重键按当日日期落库",
          store.care_event_seen(OWNER, care.STRESS_SCENARIO, "2026-10-09") is True)
    joined = "\n".join(log.lines)
    check("日志不含压力数值（88）", "88" not in joined, f"logs={log.lines}")

    _section("G4. 白名单外 / 未授权 / 措辞失败 → 退化为固定模板文本")
    store4 = make_store(tmp_dir, "stress_fb.db")
    store4.upsert_rows("stress_sample", [{"date": "2026-10-09", "source_type": 11, "average": 88.0}])
    off = FakeContext(provider="openai/gpt-4o")
    off.sent = []
    engine4 = proactive.ProactiveCare(
        off, send=lambda text: (off.sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    monitor4 = care.CareMonitor(store4, OWNER, only_settings("stress"), now=lambda: now)
    prime(monitor4, care.STRESS_SCENARIO)
    monitor4.begin_round()
    result4 = asyncio.run(engine4.run_stress(monitor4, OWNER))
    check("provider 不在白名单：仍发送且未调用措辞模型",
          result4.get("sent") is True and off.wording_calls == 0 and len(off.sent) == 1,
          f"result={result4}")
    check("模板含日均分与档位名、无医疗建议",
          bool(off.sent) and "88" in off.sent[0] and "偏高" in off.sent[0]
          and "医疗" not in off.sent[0] and "诊断" not in off.sent[0],
          f"sent={off.sent}")

    store5 = make_store(tmp_dir, "stress_boom.db")
    store5.upsert_rows("stress_sample", [{"date": "2026-10-09", "source_type": 11, "average": 88.0}])
    boom_sent: list[str] = []
    engine5 = proactive.ProactiveCare(
        FakeContext(raise_wording=True),
        send=lambda text: (boom_sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    monitor5 = care.CareMonitor(store5, OWNER, only_settings("stress"), now=lambda: now)
    prime(monitor5, care.STRESS_SCENARIO)
    monitor5.begin_round()
    result5 = asyncio.run(engine5.run_stress(monitor5, OWNER))
    check("措辞抛异常：静默退化为模板仍发送、不报错",
          result5.get("ok") is True and result5.get("sent") is True
          and len(boom_sent) == 1, f"result={result5}")

    store6 = make_store(tmp_dir, "stress_unauth.db")
    store6.upsert_rows("stress_sample", [{"date": "2026-10-09", "source_type": 11, "average": 88.0}])
    unauth = FakeContext()
    unauth.sent = []
    engine6 = proactive.ProactiveCare(
        unauth, send=lambda text: (unauth.sent.append(text) or True),
        authorized_getter=lambda: False, allowlist_getter=lambda: ["siliconflow"])
    monitor6 = care.CareMonitor(store6, OWNER, only_settings("stress"), now=lambda: now)
    prime(monitor6, care.STRESS_SCENARIO)
    monitor6.begin_round()
    result6 = asyncio.run(engine6.run_stress(monitor6, OWNER))
    check("未授权（隐私闸门关闭）：不把数值交给模型，退化为模板发送",
          result6.get("sent") is True and unauth.wording_calls == 0
          and len(unauth.sent) == 1, f"result={result6}")

    _section("G5. 投递失败 → 静默不发且占位不重发")
    store7 = make_store(tmp_dir, "stress_deliv.db")
    store7.upsert_rows("stress_sample", [{"date": "2026-10-09", "source_type": 11, "average": 88.0}])

    async def boom_send(text: str) -> bool:
        raise RuntimeError("send boom")

    monitor7 = care.CareMonitor(store7, OWNER, only_settings("stress"), now=lambda: now)
    prime(monitor7, care.STRESS_SCENARIO)
    monitor7.begin_round()
    engine7 = proactive.ProactiveCare(
        FakeContext(), send=boom_send, authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    result7 = asyncio.run(engine7.run_stress(monitor7, OWNER))
    check("投递抛异常：整体不崩溃、sent=False",
          result7.get("ok") is True and result7.get("sent") is False
          and result7.get("reason") == "delivery_failed", f"result={result7}")
    check("投递失败后当日不再重发", monitor7.stress_candidate() is None)


# ══════════════════════════════════════════════════════════════════════════
# H. 起床关怀（今日起床时间匹配 + 宽容度）
# ══════════════════════════════════════════════════════════════════════════
def test_wakeup_care(tmp_dir: Path) -> None:
    _section("H1. 纯函数：开场词四个时段分档")
    buckets = [(0, "晚上好"), (3, "晚上好"), (4, "上午好"), (11, "上午好"),
               (12, "下午好"), (17, "下午好"), (18, "晚上好"), (23, "晚上好")]
    check("开场词 04:00 / 12:00 / 18:00 三个切换点",
          all(care.greeting_for(h) == g for h, g in buckets),
          f"{[(h, care.greeting_for(h)) for h, _ in buckets]}")
    check("开场词对非法小时回退默认档", care.greeting_for(None) == care.DEFAULT_GREETING)

    now = datetime(2026, 10, 9, 7, 10, 0)

    _section("H2. 无数据 / 冷启动只登记 / 时间不匹配 / 日期不匹配")
    empty = make_store(tmp_dir, "wake_empty.db")
    check("无睡眠记录 → 无候选",
          care.CareMonitor(empty, OWNER, only_settings("wakeup"),
                           now=lambda: now).wakeup_candidates() == [])

    ok_store = make_store(tmp_dir, "wake_ok.db")
    ok_store.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 420.0, "score": 80.0,
        "fall_asleep_local": "2026-10-08 23:30:00",
        "wakeup_local": "2026-10-09 07:00:00"}])
    # 冷启动：首次启用那一轮只登记已有记录、不给候选。
    cold_store = make_store(tmp_dir, "wake_cold.db")
    cold_store.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 420.0,
        "wakeup_local": "2026-10-09 07:00:00"}])
    cold_monitor = care.CareMonitor(cold_store, OWNER, only_settings("wakeup"),
                                    now=lambda: now)
    cold_monitor.begin_round()
    check("冷启动首轮：只登记已有记录、不给候选",
          cold_monitor.wakeup_candidates() == []
          and cold_store.care_event_seen(
              OWNER, care.WAKEUP_SCENARIO, "2026-10-09|2026-10-09 07:00:00") is True
          and cold_monitor.scenario_initialized(care.WAKEUP_SCENARIO) is True)

    monitor_ok = care.CareMonitor(ok_store, OWNER, only_settings("wakeup"), now=lambda: now)
    prime(monitor_ok, care.WAKEUP_SCENARIO)   # 已过首轮基线 → 走稳态判定
    cands = monitor_ok.wakeup_candidates()
    check("起床时间匹配今日且在宽容度内（差 10 ≤ 30）→ 有候选",
          len(cands) == 1 and cands[0].data.get("greeting") == "上午好",
          f"findings={cands}")

    late_clock = [datetime(2026, 10, 9, 8, 0, 0)]
    late_monitor = care.CareMonitor(ok_store, OWNER, only_settings("wakeup"),
                                    now=lambda: late_clock[0])
    prime(late_monitor, care.WAKEUP_SCENARIO)
    check("起床距今 60 分钟（> 宽容度 30）→ 无候选",
          late_monitor.wakeup_candidates() == [])

    old_store = make_store(tmp_dir, "wake_old.db")
    old_store.upsert_rows("sleep_session", [{
        "date": "2026-10-08", "source_type": 9, "duration_min": 400.0,
        "wakeup_local": "2026-10-08 07:00:00"}])
    old_monitor = care.CareMonitor(old_store, OWNER, only_settings("wakeup"),
                                   now=lambda: now)
    prime(old_monitor, care.WAKEUP_SCENARIO)
    check("起床时间是昨天（不匹配当前日期）→ 无候选",
          old_monitor.wakeup_candidates() == [])

    _section("H3. 小睡不限时段（下午起床同样触发）")
    nap_store = make_store(tmp_dir, "wake_nap.db")
    nap_store.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 40.0,
        "wakeup_local": "2026-10-09 13:05:00"}])
    nap_now = datetime(2026, 10, 9, 13, 10, 0)
    nap_monitor = care.CareMonitor(nap_store, OWNER, only_settings("wakeup"),
                                   now=lambda: nap_now)
    prime(nap_monitor, care.WAKEUP_SCENARIO)
    nap_cands = nap_monitor.wakeup_candidates()
    check("13:05 起床、13:10 判到 → 有候选且开场词「下午好」",
          len(nap_cands) == 1 and nap_cands[0].data.get("greeting") == "下午好",
          f"findings={nap_cands}")

    _section("H4. 同一条睡眠记录只触发一次 + 开场词进措辞指令")
    store = make_store(tmp_dir, "wake_fire.db")
    store.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 420.0,
        "wakeup_local": "2026-10-09 07:00:00"}])
    monitor = care.CareMonitor(store, OWNER, only_settings("wakeup"), now=lambda: now)
    prime(monitor, care.WAKEUP_SCENARIO)
    context = FakeContext(wording="上午好，起来啦，先喝点水。")
    context.sent = []
    engine = proactive.ProactiveCare(
        context, send=lambda text: (context.sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    monitor.begin_round()
    first = asyncio.run(engine.run_wakeup(monitor, OWNER))
    check("起床：发出 1 条",
          first.get("sent") is True and len(context.sent) == 1, f"result={first}")
    check("非夜间场景未调用发送前闸门模型", context.decision_calls == 0)
    check("开场词被写进措辞指令",
          any("上午好" in item for item in context.prompts), f"prompts={context.prompts}")
    monitor.begin_round()
    second = asyncio.run(engine.run_wakeup(monitor, OWNER))
    check("同一条记录不重复触发",
          second.get("sent") is False and second.get("reason") == "no_candidate",
          f"result={second}")

    _section("H5. 白名单外退化模板 / 措辞失败兜底 / 投递失败静默")
    fb_store = make_store(tmp_dir, "wake_fb.db")
    fb_store.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 420.0,
        "wakeup_local": "2026-10-09 07:00:00"}])
    off = FakeContext(provider="openai/gpt-4o")
    off.sent = []
    engine_fb = proactive.ProactiveCare(
        off, send=lambda text: (off.sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    fb_monitor = care.CareMonitor(fb_store, OWNER, only_settings("wakeup"), now=lambda: now)
    prime(fb_monitor, care.WAKEUP_SCENARIO)
    fb_monitor.begin_round()
    result_fb = asyncio.run(engine_fb.run_wakeup(fb_monitor, OWNER))
    check("provider 不在白名单：发送程序侧模板且未调用模型",
          result_fb.get("sent") is True and off.wording_calls == 0
          and bool(off.sent) and off.sent[0].startswith("上午好"),
          f"result={result_fb} sent={off.sent}")

    boom_store = make_store(tmp_dir, "wake_boom.db")
    boom_store.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 420.0,
        "wakeup_local": "2026-10-09 07:00:00"}])
    boom_sent: list[str] = []
    engine_boom = proactive.ProactiveCare(
        FakeContext(raise_wording=True),
        send=lambda text: (boom_sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    boom_monitor = care.CareMonitor(boom_store, OWNER, only_settings("wakeup"),
                                    now=lambda: now)
    prime(boom_monitor, care.WAKEUP_SCENARIO)
    boom_monitor.begin_round()
    result_boom = asyncio.run(engine_boom.run_wakeup(boom_monitor, OWNER))
    check("措辞抛异常：静默退化为模板仍发送、不报错",
          result_boom.get("ok") is True and result_boom.get("sent") is True
          and len(boom_sent) == 1, f"result={result_boom}")

    deliv_store = make_store(tmp_dir, "wake_deliv.db")
    deliv_store.upsert_rows("sleep_session", [{
        "date": "2026-10-09", "source_type": 9, "duration_min": 420.0,
        "wakeup_local": "2026-10-09 07:00:00"}])

    async def boom_send(text: str) -> bool:
        raise RuntimeError("send boom")

    engine_deliv = proactive.ProactiveCare(
        FakeContext(), send=boom_send, authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    deliv_monitor = care.CareMonitor(deliv_store, OWNER, only_settings("wakeup"),
                                     now=lambda: now)
    prime(deliv_monitor, care.WAKEUP_SCENARIO)
    deliv_monitor.begin_round()
    result_deliv = asyncio.run(engine_deliv.run_wakeup(deliv_monitor, OWNER))
    check("投递抛异常：静默不报错、sent=False",
          result_deliv.get("ok") is True and result_deliv.get("sent") is False
          and result_deliv.get("reason") == "delivery_failed", f"result={result_deliv}")


# ══════════════════════════════════════════════════════════════════════════
# I. 运动后关怀（每条记录一条 + 时差分档）
# ══════════════════════════════════════════════════════════════════════════
def test_workout_care(tmp_dir: Path) -> None:
    now = datetime(2026, 10, 9, 18, 35, 0)

    _section("I1. 纯函数：运动名与详情")
    check("已知运动码给中文名，未知码不误标",
          care.sport_label(4) == "跑步" and care.sport_label(99) == "运动类型99"
          and care.sport_label(None) == "运动")
    check("详情缺项自动省略",
          care.workout_detail("跑步", 30, None) == "跑步 30 分钟"
          and care.workout_detail("跑步", None, 5230) == "跑步 5.23 公里"
          and care.workout_detail("跑步", 30, 5230) == "跑步 30 分钟、5.23 公里")

    _section("I2. 无数据 / 回看窗口外的旧记录不触发 / 冷启动只登记 / 碎片不触发")
    empty = make_store(tmp_dir, "wo_empty.db")
    check("无训练记录 → 无候选",
          care.CareMonitor(empty, OWNER, only_settings("workout"),
                           now=lambda: now).workout_candidates() == [])
    old = make_store(tmp_dir, "wo_old.db")
    old.upsert_rows("training_session", [{
        "session_key": "4:100", "sport_type": 4, "start_ms": 1, "end_ms": 2,
        "start_local": "2026-10-06 18:00:00", "end_local": "2026-10-06 18:30:00",
        "start_date": "2026-10-06", "duration_min": 30, "distance_m": 5000}])
    old_monitor = care.CareMonitor(old, OWNER, only_settings("workout"), now=lambda: now)
    prime(old_monitor, care.WORKOUT_SCENARIO)
    check("回看窗口（前 1 天）之外的旧记录 → 无候选",
          old_monitor.workout_candidates() == [])

    cold_store = make_store(tmp_dir, "wo_cold.db")
    cold_store.upsert_rows("training_session", [{
        "session_key": "4:9000", "sport_type": 4, "start_ms": 1, "end_ms": 2,
        "start_local": "2026-10-09 18:00:00", "end_local": "2026-10-09 18:30:00",
        "start_date": "2026-10-09", "duration_min": 30, "distance_m": 5000}])
    cold_monitor = care.CareMonitor(cold_store, OWNER, only_settings("workout"),
                                    now=lambda: now)
    cold_monitor.begin_round()
    check("冷启动首轮：只登记窗口内已有记录、不给候选",
          cold_monitor.workout_candidates() == []
          and cold_store.care_event_seen(OWNER, care.WORKOUT_SCENARIO, "4:9000") is True
          and cold_monitor.scenario_initialized(care.WORKOUT_SCENARIO) is True)

    # 碎片（1 分钟 / 几十米，含一条 sport_type=4 的「跑步」）不触发关怀；
    # 把判据阈值调松后同一批记录才成候选 → 证明过滤来自碎片判据本身。
    frag_store = make_store(tmp_dir, "wo_frag.db")
    frag_store.upsert_rows("training_session", [
        {"session_key": "4:1100", "sport_type": 4, "start_ms": 1, "end_ms": 2,
         "start_local": "2026-10-09 17:00:00", "end_local": "2026-10-09 17:01:00",
         "start_date": "2026-10-09", "duration_min": 1, "distance_m": 30},
        {"session_key": "9:1200", "sport_type": 9, "start_ms": 3, "end_ms": 4,
         "start_local": "2026-10-09 17:05:00", "end_local": "2026-10-09 17:06:00",
         "start_date": "2026-10-09", "duration_min": 1, "distance_m": 0},
    ])
    frag_monitor = care.CareMonitor(frag_store, OWNER, only_settings("workout"),
                                    now=lambda: now)
    prime(frag_monitor, care.WORKOUT_SCENARIO)
    check("1 分钟 / 几十米的碎片记录 → 无 workout 候选",
          frag_monitor.workout_candidates() == [],
          f"findings={frag_monitor.workout_candidates()}")
    loose_monitor = care.CareMonitor(
        frag_store, OWNER,
        only_settings("workout", training_min_duration_min=1,
                      training_min_distance_m=1),
        now=lambda: now)
    prime(loose_monitor, care.WORKOUT_SCENARIO)
    check("阈值调松后同一批碎片才成候选（确认是碎片判据在过滤）",
          len(loose_monitor.workout_candidates()) == 2,
          f"findings={[item.event_key for item in loose_monitor.workout_candidates()]}")

    _section("I3. 时差分档：宽容度内走及时，更远走滞后")
    timely_store = make_store(tmp_dir, "wo_timely.db")
    timely_store.upsert_rows("training_session", [{
        "session_key": "4:1000", "sport_type": 4, "start_ms": 1, "end_ms": 2,
        "start_local": "2026-10-09 18:00:00", "end_local": "2026-10-09 18:30:00",
        "start_date": "2026-10-09", "duration_min": 30, "distance_m": 5230,
        "kcal": 250.5}])
    timely_monitor = care.CareMonitor(timely_store, OWNER, only_settings("workout"),
                                      now=lambda: now)
    prime(timely_monitor, care.WORKOUT_SCENARIO)
    cands_t = timely_monitor.workout_candidates()
    check("结束 18:30、发现 18:35（差 5 ≤ 10）→ 及时分支",
          len(cands_t) == 1 and cands_t[0].data.get("timely") is True
          and cands_t[0].data.get("lag_minutes") == 5, f"findings={cands_t}")
    check("详情按记录自带字段渲染（跑步 30 分钟、5.23 公里）",
          bool(cands_t)
          and cands_t[0].data.get("detail") == "跑步 30 分钟、5.23 公里",
          f"data={cands_t[0].data if cands_t else None}")

    lag_store = make_store(tmp_dir, "wo_lag.db")
    lag_store.upsert_rows("training_session", [{
        "session_key": "4:2000", "sport_type": 4, "start_ms": 1, "end_ms": 2,
        "start_local": "2026-10-09 17:00:00", "end_local": "2026-10-09 17:30:00",
        "start_date": "2026-10-09", "duration_min": 30, "distance_m": 5230}])
    lag_monitor = care.CareMonitor(lag_store, OWNER, only_settings("workout"),
                                   now=lambda: now)
    prime(lag_monitor, care.WORKOUT_SCENARIO)
    cands_l = lag_monitor.workout_candidates()
    check("结束 17:30、发现 18:35（差 65 > 10）→ 滞后分支",
          len(cands_l) == 1 and cands_l[0].data.get("timely") is False,
          f"findings={cands_l}")

    timely_text = proactive.fallback_workout_text(cands_t[0].data)
    lagged_text = proactive.fallback_workout_text(cands_l[0].data)
    check("及时模板 = 关怀建议 + 信息展示",
          "补水" in timely_text and "5.23 公里" in timely_text, f"text={timely_text}")
    check("滞后模板 = 信息展示 + 致歉且不给建议",
          "抱歉" in lagged_text and "5.23 公里" in lagged_text
          and "补水" not in lagged_text, f"text={lagged_text}")

    _section("I4. 每轮每场景最多 1 条 + 按 session_key 去重 + 分支指令")
    store = make_store(tmp_dir, "wo_fire.db")
    store.upsert_rows("training_session", [
        {"session_key": "4:3000", "sport_type": 4, "start_ms": 1, "end_ms": 2,
         "start_local": "2026-10-09 18:00:00", "end_local": "2026-10-09 18:30:00",
         "start_date": "2026-10-09", "duration_min": 30, "distance_m": 5000},
        {"session_key": "9:4000", "sport_type": 9, "start_ms": 3, "end_ms": 4,
         "start_local": "2026-10-09 16:00:00", "end_local": "2026-10-09 16:40:00",
         "start_date": "2026-10-09", "duration_min": 40, "distance_m": 800},
    ])
    context = FakeContext(wording="练完啦，记得补水拉伸。")
    context.sent = []
    monitor = care.CareMonitor(store, OWNER, only_settings("workout"), now=lambda: now)
    prime(monitor, care.WORKOUT_SCENARIO)
    engine = proactive.ProactiveCare(
        context, send=lambda text: (context.sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    monitor.begin_round()
    first = asyncio.run(engine.run_workout(monitor, OWNER))
    check("同轮两条新记录 → 只发 1 条（每轮每场景最多 1 条）",
          first.get("sent") is True and len(context.sent) == 1,
          f"result={first} sent={context.sent}")
    check("非夜间场景未调用发送前闸门模型", context.decision_calls == 0)
    monitor.begin_round()
    second = asyncio.run(engine.run_workout(monitor, OWNER))
    check("下一轮补发剩下那条（只是压后一轮，不丢）",
          second.get("sent") is True and len(context.sent) == 2,
          f"result={second} sent={context.sent}")
    check("及时与滞后分别用对应措辞指令",
          any("关怀建议" in item for item in context.prompts)
          and any("致歉" in item for item in context.prompts),
          f"prompts={context.prompts}")
    monitor.begin_round()
    third = asyncio.run(engine.run_workout(monitor, OWNER))
    check("按 session_key 去重：两条都处理过后不再发送",
          third.get("sent") is False and third.get("reason") == "no_candidate",
          f"result={third}")
    check("两条会话都落了去重键",
          store.care_event_seen(OWNER, care.WORKOUT_SCENARIO, "4:3000")
          and store.care_event_seen(OWNER, care.WORKOUT_SCENARIO, "9:4000"))

    _section("I5. 白名单外退化为滞后模板 / 投递失败静默")
    fb_store = make_store(tmp_dir, "wo_fb.db")
    fb_store.upsert_rows("training_session", [{
        "session_key": "4:5000", "sport_type": 4, "start_ms": 1, "end_ms": 2,
        "start_local": "2026-10-09 17:00:00", "end_local": "2026-10-09 17:30:00",
        "start_date": "2026-10-09", "duration_min": 30, "distance_m": 5000}])
    off = FakeContext(provider="openai/gpt-4o")
    off.sent = []
    engine_fb = proactive.ProactiveCare(
        off, send=lambda text: (off.sent.append(text) or True),
        authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    fb_monitor = care.CareMonitor(fb_store, OWNER, only_settings("workout"), now=lambda: now)
    prime(fb_monitor, care.WORKOUT_SCENARIO)
    fb_monitor.begin_round()
    result_fb = asyncio.run(engine_fb.run_workout(fb_monitor, OWNER))
    check("白名单外：滞后记录发滞后模板且未调用模型",
          result_fb.get("sent") is True and off.wording_calls == 0
          and bool(off.sent) and "抱歉" in off.sent[0],
          f"result={result_fb} sent={off.sent}")

    deliv_store = make_store(tmp_dir, "wo_deliv.db")
    deliv_store.upsert_rows("training_session", [{
        "session_key": "4:6000", "sport_type": 4, "start_ms": 1, "end_ms": 2,
        "start_local": "2026-10-09 18:00:00", "end_local": "2026-10-09 18:30:00",
        "start_date": "2026-10-09", "duration_min": 30, "distance_m": 5000}])

    async def boom_send(text: str) -> bool:
        raise RuntimeError("send boom")

    engine_deliv = proactive.ProactiveCare(
        FakeContext(), send=boom_send, authorized_getter=lambda: True, allowlist_getter=lambda: ["siliconflow"])
    deliv_monitor = care.CareMonitor(deliv_store, OWNER, only_settings("workout"),
                                     now=lambda: now)
    prime(deliv_monitor, care.WORKOUT_SCENARIO)
    deliv_monitor.begin_round()
    result_deliv = asyncio.run(engine_deliv.run_workout(deliv_monitor, OWNER))
    check("投递抛异常：静默不报错、sent=False",
          result_deliv.get("ok") is True and result_deliv.get("sent") is False
          and result_deliv.get("reason") == "delivery_failed", f"result={result_deliv}")


# ══════════════════════════════════════════════════════════════════════════
# J. main.py 四场景接线
# ══════════════════════════════════════════════════════════════════════════
def test_all_scenarios_round(tmp_dir: Path) -> None:
    today = datetime.now().date().isoformat()
    wakeup_stamp = today_stamp(-5)
    workout_end = today_stamp(-3)

    _section("J1. 一轮巡检把四个场景都跑到")
    store = make_store(tmp_dir, "round_all.db")
    store.upsert_rows("stress_sample", [{"date": today, "source_type": 11, "average": 88.0}])
    store.upsert_rows("sleep_session", [{
        "date": today, "source_type": 9, "duration_min": 420.0, "wakeup_local": wakeup_stamp}])
    store.upsert_rows("training_session", [{
        "session_key": "4:7000", "sport_type": 4, "start_ms": 1, "end_ms": 2,
        "start_local": today_stamp(-33), "end_local": workout_end,
        "start_date": today, "duration_min": 30, "distance_m": 5000}])
    context = FakeContext()
    now = datetime.now()
    plugin = main_mod.HuaweiHealthPlugin(context, {"proactive_care": {
        "enable_proactive_care": True, "enable_night_care": True,
        "enable_stress_care": True, "enable_wakeup_care": True,
        "enable_workout_care": True}})
    plugin.care_settings = care.CareSettings(
        master_enabled=True, night_enabled=True,
        night_start=(now - timedelta(hours=1)).strftime("%H:%M"),
        night_end=(now + timedelta(hours=1)).strftime("%H:%M"),
        stress_enabled=True, wakeup_enabled=True, workout_enabled=True)
    plugin.store = store
    plugin._reminder_state = reminder.ReminderState(tmp_dir / "round_all.json")
    plugin._reminder_state.notify_umo = OWNER
    plugin._care = proactive.ProactiveCare(
        context, send=plugin._send_owner_message, authorized_getter=lambda: True,
        allowlist_getter=lambda: ["siliconflow"])
    store.touch_owner_activity(OWNER)
    context.sent.clear()
    first = asyncio.run(plugin._run_care_round())
    check("四个场景都跑了且都有结果",
          set(first.get("scenarios", {})) == {"night", "stress", "wakeup", "workout"},
          f"result={first}")
    check("冷启动首轮：四场景都只登记、不发",
          all(item.get("sent") is False for item in first["scenarios"].values())
          and context.sent == [], f"result={first} sent={context.sent}")
    check("冷启动首轮：四场景的首轮基线都就位",
          all(store.care_scenario_state(OWNER, name) is not None
              for name in ("night", "stress", "wakeup", "workout")))

    _section("J1b. 出现新事件的那一轮才发送（起床 / 运动两场景）")
    store.upsert_rows("sleep_session", [{
        "date": today, "source_type": 9, "duration_min": 420.0,
        "wakeup_local": today_stamp(-1)}])
    store.upsert_rows("training_session", [{
        "session_key": "4:7001", "sport_type": 4, "start_ms": 5, "end_ms": 6,
        "start_local": today_stamp(-8), "end_local": today_stamp(-2),
        "start_date": today, "duration_min": 25, "distance_m": 4200}])
    context.sent.clear()
    second = asyncio.run(plugin._run_care_round())
    check("夜间 / 压力本轮无新事件 → 无候选",
          second["scenarios"]["night"]["reason"] == "no_candidate"
          and second["scenarios"]["stress"]["reason"] == "no_candidate",
          f"result={second}")
    check("起床 / 运动的新事件各发 1 条",
          second["scenarios"]["wakeup"]["sent"] is True
          and second["scenarios"]["workout"]["sent"] is True
          and len(context.sent) == 2, f"result={second} sent={context.sent}")
    third = asyncio.run(plugin._run_care_round())
    check("再跑一轮：四场景均已去重，不再发送",
          third.get("sent") is False, f"result={third}")

    _section("J2. 单场景异常不影响其余场景（静默）")
    store2 = make_store(tmp_dir, "round_boom.db")
    store2.upsert_rows("sleep_session", [{
        "date": today, "source_type": 9, "duration_min": 400.0, "wakeup_local": wakeup_stamp}])
    context2 = FakeContext()
    plugin2 = main_mod.HuaweiHealthPlugin(context2, {"proactive_care": {
        "enable_proactive_care": True, "enable_stress_care": True,
        "enable_wakeup_care": True}})
    plugin2.care_settings = care.CareSettings(
        master_enabled=True, stress_enabled=True, wakeup_enabled=True)
    plugin2.store = store2
    plugin2._reminder_state = reminder.ReminderState(tmp_dir / "round_boom.json")
    plugin2._reminder_state.notify_umo = OWNER
    plugin2._care = proactive.ProactiveCare(
        context2, send=plugin2._send_owner_message, authorized_getter=lambda: True,
        allowlist_getter=lambda: ["siliconflow"])
    # 起床场景先过掉冷启动基线，否则首轮只登记不发，测不出「其余场景照常发」。
    store2.set_care_scenario_state(OWNER, care.WAKEUP_SCENARIO, when=wakeup_stamp)

    async def boom_run(*args, **kwargs):
        raise RuntimeError("scenario boom")

    plugin2._care.run_stress = boom_run
    context2.sent.clear()
    result2 = asyncio.run(plugin2._run_care_round())
    check("异常场景标记 error，起床场景照常发出",
          result2["scenarios"]["stress"]["ok"] is False
          and result2["scenarios"]["wakeup"]["sent"] is True
          and len(context2.sent) == 1, f"result={result2}")


# ══════════════════════════════════════════════════════════════════════════
# K. 碎片过滤端到端（命令渲染 / LLM 摘要 / 运动后关怀同口径）
# ══════════════════════════════════════════════════════════════════════════
def test_fragment_filtering_e2e(tmp_dir: Path) -> None:
    today = datetime.now().date().isoformat()

    def _row(key: str, sport: int, minutes: int, meters: int, hour: int) -> dict:
        return {
            "session_key": key, "sport_type": sport,
            "start_ms": hour * 3600000, "end_ms": hour * 3600000 + minutes * 60000,
            "start_local": f"{today} {hour:02d}:00:00",
            "end_local": f"{today} {hour:02d}:{minutes:02d}:00",
            "start_date": today, "duration_min": minutes, "distance_m": meters,
            "kcal": 10.0 * minutes, "segments": 1, "device_code": "band-x",
        }

    _section("K1. 6 条碎片 + 2 条有效训练：打标 / 入库 / 三处过滤")
    rows = [
        _row("4:1100", 4, 1, 30, 6),      # 碎片（sport_type=4 的「跑步」）
        _row("4:1200", 4, 1, 45, 7),
        _row("5:1300", 5, 1, 20, 8),
        _row("6:1400", 6, 1, 60, 9),
        _row("9:1500", 9, 1, 0, 10),
        _row("7:1600", 7, 1, 80, 11),
        _row("4:2100", 4, 30, 5000, 14),  # 有效训练
        _row("9:2200", 9, 45, 8000, 16),  # 有效训练
    ]
    marked = models.mark_training_fragments(rows)   # 走同步服务同一打标路径
    check("打标：6 条碎片（is_fragment=1）/ 2 条有效（is_fragment=0）",
          sum(1 for item in marked if item["is_fragment"] == 1) == 6
          and sum(1 for item in marked if item["is_fragment"] == 0) == 2,
          f"flags={[item['is_fragment'] for item in marked]}")
    store = make_store(tmp_dir, "frag_e2e.db")
    store.upsert_rows("training_session", marked)
    check("碎片只标记、不删除：8 行全部留在库里",
          store.count("training_session") == 8,
          f"count={store.count('training_session')}")

    # 一、健康训练命令渲染
    rendered = health_query.render_training(store, 7)
    check("命令渲染：只列 2 条有效训练 + 提示 6 条碎片未计入",
          "共 2 次训练" in rendered and "另有 6 条碎片记录未计入" in rendered
          and sum(1 for line in rendered.splitlines() if line.startswith("- ")) == 2
          and "1 分钟" not in rendered and "0.03 公里" not in rendered
          and "跑步 1 分钟" not in rendered,
          f"rendered={rendered!r}")

    # 二、LLM 摘要的训练段（与命令渲染逐字一致 = 同一套过滤）
    summary = llm_injection.health_summary(store, days=7)
    check("LLM 摘要训练段与命令渲染逐字一致（同样只含有效训练）",
          rendered in summary, f"summary={summary!r}")

    # 三、运动后关怀的 workout 候选
    monitor = care.CareMonitor(store, OWNER, only_settings("workout"),
                               now=lambda: datetime.now())
    prime(monitor, care.WORKOUT_SCENARIO)
    cands = monitor.workout_candidates()
    check("关怀 workout 候选只来自 2 条有效训练（6 条碎片被滤掉）",
          {item.event_key for item in cands} == {"4:2100", "9:2200"},
          f"findings={[item.event_key for item in cands]}")
    check("碎片会话未落任何去重键（从未进入关怀候选）",
          not store.care_event_seen(OWNER, care.WORKOUT_SCENARIO, "4:1100")
          and not store.care_event_seen(OWNER, care.WORKOUT_SCENARIO, "9:1500"))


def main() -> int:
    print("=" * 68)
    print("华为运动健康 —— 主动关怀（夜间 / 压力 / 起床 / 运动后）+ schema v4 自检（全桩）")
    print("=" * 68)
    tmp_dir = Path(tempfile.mkdtemp(prefix="hwhealth_care_"))
    print(f"临时目录：{tmp_dir}")
    try:
        test_migration_v4(tmp_dir)
        test_master_switch(tmp_dir)
        test_night_window_and_dedupe(tmp_dir)
        test_gate(tmp_dir)
        test_wording_fallback(tmp_dir)
        test_main_wiring(tmp_dir)
        test_stress_care(tmp_dir)
        test_wakeup_care(tmp_dir)
        test_workout_care(tmp_dir)
        test_all_scenarios_round(tmp_dir)
        test_fragment_filtering_e2e(tmp_dir)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 68)
    print(f"结果：{passed}/{total} PASS")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL: {name} {detail}")
    print("=" * 68)
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
