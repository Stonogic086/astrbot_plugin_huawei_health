#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""定时同步自检：真实 token 真拉一次最近 N 天 → 走完整 SyncService 流程 → 从库里读回。

只读边界：
  * 只读凭据状态文件（默认 .auth_state.json），【不写回、不修改】；
  * 不改插件配置、不碰 /vol1/@appdata/astrbot/data/config/；
  * 对云端只做只读 GET（协议层取数接口），不写云端数据；
  * 【允许】写本地数据库：默认写到本次新建的临时库，避免污染真实 health.db；
    用 --db 指定真实库即可验证落库到生产库。

不 import astrbot（SyncService / storage / adapters 均不依赖框架）。

用法：
    python3 scripts/selftest_sync.py
    python3 scripts/selftest_sync.py --days 3 --db /path/to/health.db
    python3 scripts/selftest_sync.py --state /path/to/.auth_state.json
退出码：refresh 成功且六类均无「取数失败」→ 0；refresh 失败 → 1；参数/文件问题 → 2。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from adapters import HuaweiHealthCloudAdapter, Tokens, const  # noqa: E402
from adapters.huawei_health_cloud import HuaweiConnectionError  # noqa: E402
from privacy_gate import mask_secret  # noqa: E402 （脱敏只有一处实现）
from services import DATA_CLASSES, SyncService  # noqa: E402
from storage import HealthStore  # noqa: E402

DEFAULT_STATE = "/vol1/@appdata/astrbot/data/projects/huawei_health_adapter/.auth_state.json"
CONNECT_RETRIES = 3

# 取数失败的 stage 名：刷新 + 六类数据（新实现把 health 家族拆成心率/睡眠/压力/血氧
# 四类各自的 stage，不再是 stage="health"）。写库失败记 "<类>:write"，与本判定一致
# 不算取数失败（原判定同样不认）。
FETCH_FAIL_STAGES: tuple[str, ...] = ("refresh", *DATA_CLASSES)

# 模型名 → 读回时的关键字段。
READBACK_FIELDS = {
    "daily_activity": ("date", "sport_type", "steps", "distance_m", "kcal",
                       "duration_min", "walk_min", "active_hours"),
    "heart_rate_sample": ("date", "resting_hr", "day_hr", "average_resting_hr",
                          "max_hr", "min_hr"),
    "sleep_session": ("date", "duration_min", "score", "efficiency", "hrv", "spo2",
                      "fall_asleep_local", "wakeup_local", "nap_duration_min"),
    "stress_sample": ("date", "average", "last_value", "max_value", "min_value",
                      "measurements"),
    "spo2_sample": ("date", "spo2", "sample_kind"),
    "training_session": ("session_key", "sport_type", "start_local", "end_local",
                         "duration_min", "distance_m", "kcal", "segments",
                         "device_code"),
}


def load_state(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def tokens_from_state(state: dict) -> Tokens:
    data = state.get("tokens") or {}
    return Tokens(
        access_token=data.get("access_token"),
        refresh_token=data.get("refresh_token"),
        uid=data.get("uid"),
        site_id=data.get("site_id"),
        expires_at=data.get("expires_at") or 0.0,
        refresh_expires_at=data.get("refresh_expires_at") or 0.0,
        session_host=data.get("session_host") or state.get("session_host"),
    )


def _pick(row: dict, fields) -> str:
    parts = [f"{key}={row.get(key)}" for key in fields if key in row]
    return "  ".join(parts)


async def run(args) -> int:
    print("=" * 68)
    print("华为运动健康 —— 定时同步自检（真拉最近 N 天 → SyncService → 读回）")
    print("=" * 68)
    print(f"状态文件 ：{args.state}")
    print(f"文件权限 ：{oct(os.stat(args.state).st_mode)[-3:]}（只读引用，不写回）")

    state = load_state(args.state)
    tokens = tokens_from_state(state)
    if not tokens.refresh_token:
        print("失败：状态文件里没有 refresh_token。")
        return 2

    if args.db:
        db_path = Path(args.db)
    else:
        db_path = Path(tempfile.mkdtemp(prefix="hwhealth_sync_")) / "health.db"
    store = HealthStore(db_path)
    store.initialize()

    print(f"uid      ：{tokens.uid}")
    print(f"数据域   ：{args.data_host or const.APP_HOST_CN}")
    print(f"会话域   ：{args.session_host or tokens.session_host or const.SESSION_HOST_CN}")
    print(f"refreshToken（脱敏）：{mask_secret(tokens.refresh_token)}")
    print(f"库文件   ：{db_path}")
    print(f"窗口天数 ：{args.days}（含今天）")
    print("-" * 68)

    adapter = HuaweiHealthCloudAdapter(
        tokens,
        host=args.data_host or const.APP_HOST_CN,
        session_host=args.session_host or tokens.session_host or const.SESSION_HOST_CN,
    )
    service = SyncService(adapter, store, days=args.days)

    # ── 1. 完整跑一轮同步 ──────────────────────────────────────────────
    print("[1/2] 执行 SyncService.run_once()（刷新 token → 拉六类 → 写库）...")
    summary = await service.run_once()
    print("      summary = " + json.dumps(summary, ensure_ascii=False))
    print(f"      status={summary['status']} ok={summary['ok']} "
          f"耗时={summary['elapsed_sec']}s 窗口={summary['window_start']}~{summary['window_end']}")

    if summary["status"] == "failed":
        print("      -> 刷新 token 失败，未落库。")
        return 1

    # ── 2. 从库里读回 ──────────────────────────────────────────────────
    print("\n[2/2] 从库里读回每类行数与关键字段 ...")
    start = (date.today() - timedelta(days=args.days - 1)).isoformat()
    end = date.today().isoformat()

    print(f"      区间 {start} ~ {end}")
    total = 0
    for model in READBACK_FIELDS:
        rows = store.query(model, start, end)
        total += len(rows)
        print(f"      [{model}] 行数={len(rows)}")
        for row in rows[:5]:
            print("        " + _pick(row, READBACK_FIELDS[model]))
        if len(rows) > 5:
            print(f"        ...（其余 {len(rows) - 5} 行省略）")

    print("-" * 68)
    # ── 判定 ───────────────────────────────────────────────────────────
    # 用 s.get("stage")：新实现的 skipped 项多带一个 "kind" 键，取键时不该报错。
    hard_fail = [s for s in summary["skipped"] if s.get("stage") in FETCH_FAIL_STAGES]
    print(f"本轮写入合计（六类）= {summary['written']}；库里区间读数合计 = {total} 行")
    if summary["skipped"]:
        print(f"跳过/失败项：{summary['skipped']}")
    if summary.get("no_data"):
        print(f"云端无数据的类别（标注「无」，不算失败）：{summary['no_data']}")
    if hard_fail:
        print("结果：部分完成（存在取数失败，见上）。")
        return 1
    print("结果：通过（刷新成功、六类均取数成功并已落库）。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="华为健康定时同步自检（云端只读）")
    parser.add_argument("--state", default=DEFAULT_STATE, help="凭据状态文件路径")
    parser.add_argument("--days", type=int, default=3, help="取最近多少天（默认 3）")
    parser.add_argument("--db", default=None,
                        help="写入的库文件；缺省用新建临时库（不碰真实 health.db）")
    parser.add_argument("--data-host", default=None, help="覆盖数据域")
    parser.add_argument("--session-host", default=None, help="覆盖会话域")
    args = parser.parse_args()
    if not os.path.exists(args.state):
        print(f"失败：状态文件不存在：{args.state}")
        return 2
    args.days = max(1, args.days)

    async def go() -> int:
        last = None
        for attempt in range(1, CONNECT_RETRIES + 1):
            try:
                return await run(args)
            except HuaweiConnectionError as error:
                last = error
                print(f"      连接类错误（第 {attempt}/{CONNECT_RETRIES} 次）：{error}")
        print(f"失败：连接类错误重试 {CONNECT_RETRIES} 次仍未成功：{last}")
        return 1

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(main())
