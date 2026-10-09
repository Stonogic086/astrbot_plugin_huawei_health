#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""协议层最小自检：用已有 refresh token 走一次「刷新 + 拉最近 N 天日汇总」。

只读设计：
  * 读取凭据状态文件（默认指向项目目录下的 .auth_state.json），【不写回、不复制】；
  * 只调用协议层的刷新与取数，不改动账号云端数据；
  * 不 import astrbot（协议层本身也不依赖 astrbot）。

用法：
  python3 scripts/selftest_protocol.py
  python3 scripts/selftest_protocol.py --days 3 --state /path/to/.auth_state.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from adapters import HuaweiHealthCloudAdapter, Tokens, const  # noqa: E402
from adapters.huawei_health_cloud import HuaweiConnectionError  # noqa: E402
from privacy_gate import mask_secret  # noqa: E402 （脱敏只有一处实现）

DEFAULT_STATE = "/vol1/@appdata/astrbot/data/projects/huawei_health_adapter/.auth_state.json"
CONNECT_RETRIES = 3


async def with_conn_retry(label: str, coro_factory):
    """对可重试的连接类错误（DNS/网络抖动）最多重试 3 次。"""
    last = None
    for attempt in range(1, CONNECT_RETRIES + 1):
        try:
            return await coro_factory()
        except HuaweiConnectionError as error:
            last = error
            print(f"      连接类错误（第 {attempt}/{CONNECT_RETRIES} 次）：{error}")
    raise last


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


async def run(state_path: str, days: int, data_host: str | None,
              session_host: str | None) -> int:
    print("=" * 60)
    print("华为运动健康 —— 协议层自检（刷新 token + 拉日汇总）")
    print("=" * 60)
    print(f"状态文件  ：{state_path}")
    print(f"文件权限  ：{oct(os.stat(state_path).st_mode)[-3:]}（只读引用，不写回）")

    state = load_state(state_path)
    tokens = tokens_from_state(state)
    if not tokens.refresh_token:
        print("失败：状态文件里没有 refresh_token。")
        return 2

    print(f"uid       ：{tokens.uid}")
    print(f"会话域    ：{session_host or tokens.session_host or const.SESSION_HOST_CN}")
    print(f"数据域    ：{data_host or const.APP_HOST_CN}")
    print(f"refreshToken（脱敏）：{mask_secret(tokens.refresh_token)}")
    print("-" * 60)

    adapter = HuaweiHealthCloudAdapter(
        tokens,
        host=data_host or const.APP_HOST_CN,
        session_host=session_host or tokens.session_host or const.SESSION_HOST_CN,
    )

    print("[1/2] 刷新 access token ...")
    try:
        await with_conn_retry("refresh", adapter.refresh)
    except Exception as error:
        print(f"      失败：{type(error).__name__}: {error}")
        print("      -> 刷新失败，未取数。")
        return 1
    print(f"      成功。accessToken（脱敏）：{mask_secret(tokens.access_token)}")

    print(f"[2/2] 拉最近 {days} 天日汇总（getSportsStat）...")
    try:
        rows = await with_conn_retry("pull", lambda: adapter.daily_summary(days))
    except Exception as error:
        print(f"      失败：{type(error).__name__}: {error}")
        print("      -> 刷新成功，但取数失败。")
        return 1

    print("-" * 60)
    print(f"结果      ：成功")
    print(f"数据域    ：{adapter.host}")
    print(f"拿到几行  ：{len(rows)} 行")
    for row in rows[:10]:
        print("  " + json.dumps(row, ensure_ascii=False))
    if not rows:
        print("  警告：0 行。可能该时间段无记录，或数据域选错（EU 域会返回 resultCode 0 但空数组）。")
    print("=" * 60)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="华为健康协议层最小自检（只读）")
    parser.add_argument("--state", default=DEFAULT_STATE, help="凭据状态文件路径")
    parser.add_argument("--days", type=int, default=3, help="取最近多少天（默认 3）")
    parser.add_argument("--data-host", default=None, help="覆盖数据域")
    parser.add_argument("--session-host", default=None, help="覆盖会话域")
    args = parser.parse_args()
    if not os.path.exists(args.state):
        print(f"失败：状态文件不存在：{args.state}")
        return 2
    return asyncio.run(run(args.state, max(1, args.days), args.data_host, args.session_host))


if __name__ == "__main__":
    raise SystemExit(main())
