#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康数据进 LLM 上下文自检：用桩替换框架与网络，验证注入链的六类行为。

覆盖：
  1. 名单内注入（前缀命中 / 完整 id 命中 / event 回落到宿主 API 取 provider）；
  2. 名单外不注入（含「子串不算命中」「空名单＝永不注入」）；
  3. 降级到名单外模型不注入（同一插件与配置，仅 provider 不同 → 结论随 provider 变）；
  4. 未授权不注入（隐私闸门关闭）；
  5. 摘要渲染缺失一律写「无」（含部分缺失、store 不可用返回空摘要）；
  6. 任一步失败退化成「不注入」且不抛错（存储抛错 / 事件与回落都抛错 / req 无承载处 /
     框架挂载点缺失 / TextPart 无 mark_as_temp / 空文本）。

设计约定（沿用本插件其余 selftest）：
  * 不联网、不启动 AstrBot：复用 import_check 的最小 astrbot 桩；
  * 全部数据写在 tempfile 临时库，不碰真实 health.db、不改插件配置、不写日志；
  * 退出码：全部 PASS → 0；任一 FAIL → 1。

用法：
    python3 scripts/selftest_llm_injection.py
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPTS_DIR.parent
PKG = PLUGIN_ROOT.name

import_check = importlib.import_module("import_check")
import_check._install_astrbot_stub()
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

features = importlib.import_module(f"{PKG}.features.llm_injection")
main_mod = importlib.import_module(f"{PKG}.main")
storage = importlib.import_module(f"{PKG}.storage")

HealthStore = storage.HealthStore

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))


def _section(title: str) -> None:
    print(f"\n{title}")


# ── 桩 ───────────────────────────────────────────────────────────────────
class FakeContext:
    """只提供 get_current_chat_provider_id：模拟宿主回落取本轮 provider。"""

    def __init__(self, provider_id: object = None, raise_on_resolve: bool = False):
        self.provider_id = provider_id
        self.raise_on_resolve = raise_on_resolve

    async def get_current_chat_provider_id(self, umo) -> object:
        await asyncio.sleep(0)
        if self.raise_on_resolve:
            raise RuntimeError("resolve boom")
        return self.provider_id


class FakeEvent:
    """最小 event 桩：selected_provider 走 get_extra，拿不到交给宿主回落。"""

    def __init__(self, selected: object = None, umo: str = "aiocqhttp:FriendMessage:10001"):
        self.unified_msg_origin = umo
        self._selected = selected

    def get_extra(self, key, default=None):
        if key == "selected_provider":
            return self._selected
        return default


class AngryEvent:
    """get_extra 抛异常的 event：验证注入链不因此中断。"""

    unified_msg_origin = "aiocqhttp:FriendMessage:10001"

    def get_extra(self, key, default=None):
        raise RuntimeError("extra boom")


class FakeRequest:
    """最小 ProviderRequest 桩：只提供 extra_user_content_parts。"""

    def __init__(self, parts=None):
        self.extra_user_content_parts = [] if parts is None else parts


class BareRequest:
    """没有 extra_user_content_parts 的 req：验证「无承载处」即不注入。"""


class BoomStore:
    """读库即抛异常的 store：验证任一步失败都退化成「不注入」。"""

    def query(self, *args, **kwargs):
        raise RuntimeError("query boom")

    def get(self, *args, **kwargs):
        raise RuntimeError("get boom")


def make_plugin(privacy: dict, store, context=None):
    """按给定 privacy 分组配置造一个真实插件实例（只替换 store 与 context）。"""
    plugin = main_mod.HuaweiHealthPlugin(context or FakeContext(), {"privacy": privacy})
    plugin.store = store
    return plugin


def allowed(allowlist, provider="siliconflow/Qwen/Qwen3.5-4B"):
    return {"allow_health_data_to_llm": True, "llm_provider_allowlist": allowlist}


def seed(store: HealthStore) -> dict:
    """造六类数据（今天），返回用到的日期。"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    start_ms = int(datetime(today.year, today.month, today.day, 8, 0).timestamp() * 1000)
    store.upsert_rows("daily_activity", [
        {"date": today.isoformat(), "sport_type": 0, "steps": 3172,
         "distance_m": 2448, "kcal": 121.7, "duration_min": 0,
         "walk_min": 63, "active_hours": 5, "exercise_min": 12, "step_goal": 10000},
        {"date": yesterday.isoformat(), "sport_type": 0, "steps": 1000,
         "distance_m": 900, "kcal": 30.0, "walk_min": 0, "active_hours": 0,
         "exercise_min": 0},
    ])
    store.upsert_rows("heart_rate_sample", [
        {"date": today.isoformat(), "resting_hr": 58, "day_hr": 72,
         "average_resting_hr": 60, "max_hr": 135, "min_hr": 49,
         "sample_kind": "daily_summary", "source_type": 7},
    ])
    store.upsert_rows("sleep_session", [
        {"date": today.isoformat(), "duration_min": 432, "score": 83, "efficiency": 92,
         "hrv": 45.5, "spo2": 97,
         "fall_asleep_local": f"{today.isoformat()} 03:14",
         "wakeup_local": f"{today.isoformat()} 09:42", "nap_duration_min": 74,
         "source_type": 9},
    ])
    store.upsert_rows("stress_sample", [
        {"date": today.isoformat(), "average": 35, "last_value": 41,
         "max_value": 62, "min_value": 18, "measurements": 12, "source_type": 11},
    ])
    store.upsert_rows("spo2_sample", [
        {"date": today.isoformat(), "spo2": 97, "sample_kind": "day"},
    ])
    store.upsert_rows("training_session", [
        {"session_key": "run-a1", "sport_type": 4, "start_date": today.isoformat(),
         "start_ms": start_ms, "end_ms": start_ms + 1800_000,
         "start_local": f"{today.isoformat()} 08:00", "duration_min": 30,
         "distance_m": 5000, "kcal": 300.0, "segments": 1, "device_code": "band-x"},
    ])
    return {"today": today.isoformat(), "yesterday": yesterday.isoformat()}


async def main() -> int:
    print("=" * 68)
    print("华为运动健康 —— 健康数据进 LLM 上下文自检（全桩：不联网、不启框架）")
    print("=" * 68)

    tmp_dir = Path(tempfile.mkdtemp(prefix="hwhealth_llm_"))
    filled = HealthStore(tmp_dir / "filled.db")
    filled.initialize()
    days = seed(filled)
    empty = HealthStore(tmp_dir / "empty.db")
    empty.initialize()
    print(f"有数据库：{filled.db_path}")
    print(f"空数据库：{empty.db_path}")

    # ── 1. 名单内注入 ────────────────────────────────────────────────
    _section("[1/6] 名单内注入（前缀命中 / 完整 id 命中 / 回落到宿主 API）")
    check("provider_allowed：条目＝来源前缀即命中",
          features.provider_allowed("siliconflow/Qwen/Qwen3.5-4B", ["siliconflow"]) is True)
    check("provider_allowed：条目＝完整 id 命中",
          features.provider_allowed("siliconflow/Qwen/Qwen3.5-4B",
                                    ["siliconflow/Qwen/Qwen3.5-4B"]) is True)
    check("provider_source：取第一个「/」之前的一段",
          features.provider_source("siliconflow/Qwen/Qwen3.5-4B") == "siliconflow")

    plugin = make_plugin(allowed(["siliconflow"]), filled)
    req = FakeRequest()
    await plugin.on_llm_request(FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), req)
    parts = req.extra_user_content_parts
    check("名单内注入：追加 1 个 part", len(parts) == 1, f"len={len(parts)}")
    if parts:
        text = str(getattr(parts[0], "text", ""))
        check("含摘要表头", features.SUMMARY_HEADER in text)
        check("含真实读数（步数来自库）", "步数 3172（目标 10000）" in text)
        check("覆盖六类：活动/睡眠/心率/训练/压力/血氧",
              "最近 3 天活动" in text and "睡眠与心率" in text
              and "最近 3 天训练" in text and f"压力（{days['today']}）" in text
              and f"血氧（{days['today']}）" in text)
        check("part 已标临时（不写进长期会话历史）",
              getattr(parts[0], "temp", False) is True)

    # event 没带 provider 时回落到宿主 API
    fallback_plugin = make_plugin(
        allowed(["siliconflow"]), filled,
        FakeContext(provider_id="siliconflow/Qwen/Qwen3.5-4B"))
    fallback_req = FakeRequest()
    await fallback_plugin.on_llm_request(FakeEvent(selected=None), fallback_req)
    check("event 未带 provider → 回落到宿主 API 取到并注入",
          len(fallback_req.extra_user_content_parts) == 1,
          f"len={len(fallback_req.extra_user_content_parts)}")

    # ── 2. 名单外不注入 ─────────────────────────────────────────────
    _section("[2/6] 名单外不注入（含子串不算命中、空名单＝永不注入）")
    check("provider_allowed：子串不算命中",
          features.provider_allowed("siliconflow/Qwen/Qwen3.5-4B", ["silicon"]) is False)
    check("provider_allowed：空名单不命中",
          features.provider_allowed("siliconflow/Qwen/Qwen3.5-4B", []) is False)

    out_plugin = make_plugin(allowed(["siliconflow"]), filled)
    out_req = FakeRequest()
    await out_plugin.on_llm_request(FakeEvent(selected="openai/gpt-4o"), out_req)
    check("名单外 provider 不注入", len(out_req.extra_user_content_parts) == 0,
          f"len={len(out_req.extra_user_content_parts)}")

    sub_plugin = make_plugin(allowed(["silicon"]), filled)
    sub_req = FakeRequest()
    await sub_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), sub_req)
    check("名单里只有子串时不注入", len(sub_req.extra_user_content_parts) == 0)

    blank_plugin = make_plugin(allowed([]), filled)
    blank_req = FakeRequest()
    await blank_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), blank_req)
    check("空名单＝永不注入（默认配置）",
          len(blank_req.extra_user_content_parts) == 0)

    # ── 3. 降级到名单外模型不注入 ───────────────────────────────────
    _section("[3/6] 降级到名单外模型不注入（同插件同配置，仅 provider 不同）")
    degrade_plugin = make_plugin(allowed(["siliconflow"]), filled)
    ok_req = FakeRequest()
    await degrade_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), ok_req)
    degraded_req = FakeRequest()
    await degrade_plugin.on_llm_request(
        FakeEvent(selected="deepseek/deepseek-chat"), degraded_req)
    check("同一插件/配置：名单内模型仍注入",
          len(ok_req.extra_user_content_parts) == 1)
    check("降级到名单外模型不注入",
          len(degraded_req.extra_user_content_parts) == 0,
          f"provider=deepseek/deepseek-chat len={len(degraded_req.extra_user_content_parts)}")

    # ── 4. 未授权不注入 ─────────────────────────────────────────────
    _section("[4/6] 未授权不注入（隐私闸门关闭）")
    closed_plugin = make_plugin(
        {"allow_health_data_to_llm": False, "llm_provider_allowlist": ["siliconflow"]},
        filled)
    check("隐私闸门关闭", closed_plugin.privacy_gate.is_open is False)
    closed_req = FakeRequest()
    await closed_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), closed_req)
    check("未授权不注入（即使 provider 在名单内、库里有数据）",
          len(closed_req.extra_user_content_parts) == 0)

    default_plugin = make_plugin({}, filled)
    default_req = FakeRequest()
    await default_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), default_req)
    check("默认配置（无 privacy 分组）同样不注入",
          len(default_req.extra_user_content_parts) == 0)
    check("features.decide：未授权 → not_authorized",
          features.decide(filled, "siliconflow/Qwen/Qwen3.5-4B", ["siliconflow"],
                          False).reason == "not_authorized")

    # ── 5. 摘要渲染缺失写「无」───────────────────────────────────────
    _section("[5/6] 摘要渲染缺失一律写「无」")
    empty_text = features.health_summary(empty)
    check("空库仍渲染六类区间（不抛错、不编数据）",
          "最近 3 天活动" in empty_text and "最近 3 天训练" in empty_text)
    check("无活动写「没有活动记录」", "没有活动记录" in empty_text)
    check("无睡眠/心率写「没有睡眠或心率记录」", "没有睡眠或心率记录" in empty_text)
    check("无训练写「没有训练记录」", "没有训练记录" in empty_text)
    check("无压力写「无」", f"压力（{days['today']}）：无" in empty_text)
    check("无血氧写「无」", f"血氧（{days['today']}）：无" in empty_text)

    partial = HealthStore(tmp_dir / "partial.db")
    partial.initialize()
    partial.upsert_rows("daily_activity", [
        {"date": days["today"], "sport_type": 0, "steps": 3172, "distance_m": 2448,
         "kcal": 121.7, "walk_min": 63, "active_hours": 5, "exercise_min": 12,
         "step_goal": 10000},
    ])
    partial_text = features.health_summary(partial)
    check("部分缺失：有值照常显示", "步数 3172（目标 10000）" in partial_text)
    check("部分缺失：缺失项写「无」而不是 0/空壳",
          f"压力（{days['today']}）：无" in partial_text
          and f"血氧（{days['today']}）：无" in partial_text
          and "没有睡眠或心率记录" in partial_text
          and "没有训练记录" in partial_text)
    check("store 不可用 → 空摘要（不注入）", features.health_summary(None) == "")
    check("features.decide：空摘要 → empty_summary",
          features.decide(None, "siliconflow/Qwen/Qwen3.5-4B", ["siliconflow"],
                          True).reason == "empty_summary")

    # ── 6. 任一步失败退化且不抛错 ───────────────────────────────────
    _section("[6/6] 任一步失败退化且不抛错")
    boom_decision = features.decide(BoomStore(), "siliconflow/Qwen/Qwen3.5-4B",
                                    ["siliconflow"], True)
    check("features.decide：存储抛错 → 不注入",
          boom_decision.inject is False and boom_decision.text == "")
    check("features.decide：失败原因是异常类型（不含健康数值）",
          boom_decision.reason == "error:RuntimeError", f"reason={boom_decision.reason}")

    boom_plugin = make_plugin(allowed(["siliconflow"]), BoomStore())
    boom_req = FakeRequest()
    await boom_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), boom_req)
    check("接线层：存储抛错 → 无 part 且未抛错",
          len(boom_req.extra_user_content_parts) == 0)

    angry_plugin = make_plugin(
        allowed(["siliconflow"]), filled,
        FakeContext(raise_on_resolve=True))
    angry_req = FakeRequest()
    await angry_plugin.on_llm_request(AngryEvent(), angry_req)
    check("接线层：事件取 provider 与回落都抛错 → 不注入且不抛错",
          len(angry_req.extra_user_content_parts) == 0)

    bare_plugin = make_plugin(allowed(["siliconflow"]), filled)
    await bare_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), BareRequest())
    none_parts_req = FakeRequest(parts=None)
    none_parts_req.extra_user_content_parts = None
    await bare_plugin.on_llm_request(
        FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), none_parts_req)
    check("接线层：req 无承载处 / 承载处为 None → 不注入且不抛错",
          none_parts_req.extra_user_content_parts is None)

    # 框架挂载点缺失 → 整轮不注入（fail-closed）
    api_mod = sys.modules["astrbot.api"]
    saved_module = sys.modules.pop("astrbot.api.provider")
    saved_attr = getattr(api_mod, "provider", None)
    try:
        if hasattr(api_mod, "provider"):
            delattr(api_mod, "provider")
        gate_req = FakeRequest()
        await bare_plugin.on_llm_request(
            FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), gate_req)
        check("框架挂载点缺失（ProviderRequest 导入失败）→ 整轮不注入",
              len(gate_req.extra_user_content_parts) == 0)
        check("挂载点缺失时页面不抛错", True)
    finally:
        sys.modules["astrbot.api.provider"] = saved_module
        api_mod.provider = saved_attr

    # TextPart 无 mark_as_temp → 整轮不注入
    class NoMarkPart:
        def __init__(self, text: str = ""):
            self.text = text

    saved_text_part = features.TextPart
    features.TextPart = NoMarkPart
    try:
        check("build_part：探测不到 mark_as_temp → None",
              features.build_part("x") is None)
        check("build_part：空文本 → None", features.build_part("   ") is None)
        nomark_req = FakeRequest()
        await bare_plugin.on_llm_request(
            FakeEvent(selected="siliconflow/Qwen/Qwen3.5-4B"), nomark_req)
        check("接线层：TextPart 无 mark_as_temp → 整轮不注入",
              len(nomark_req.extra_user_content_parts) == 0)
    finally:
        features.TextPart = saved_text_part

    # ── 汇总 ───────────────────────────────────────────────────────
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
    raise SystemExit(asyncio.run(main()))
