#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""网络层重试自检：把会话域 / 数据域临时指向一个解析不出来的域名，证明重试确实发生。

设计（只读、离线）：
  * host 只在【本进程内】临时指向 ``*.invalid``（RFC 2606 保留顶级域，永不解析），
    不写任何插件配置、不改状态文件、不联网、不 import astrbot；进程结束即自动恢复。
  * 在协议层 ``urllib.request.urlopen`` 外面套一层计数器，记录每次真实尝试的时间戳，
    从而打印「每次尝试的时间与次数」与相邻间隔。
  * 两条出网路径分别验证：
        1) 刷新 token：``HuaweiHealthCloudAdapter.refresh()`` → ``refresh_tokens()``
        2) 取数      ：``HuaweiHealthCloudAdapter.daily_summary()`` → ``..._send()``
    两次都应走到「全部重试耗尽 → 抛 HuaweiConnectionError」。

用法：python3 scripts/selftest_network_retry.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import adapters.huawei_health_cloud as hc  # noqa: E402

BOGUS_HOST = "https://no-such-host-retry-check.invalid"
EXPECTED_ATTEMPTS = 1 + int(hc.NETWORK_RETRY_ATTEMPTS)
EXPECTED_BACKOFF = tuple(hc.NETWORK_RETRY_BACKOFF)
TOLERANCE = 0.6  # 秒

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


def hr(title: str) -> None:
    print("\n" + title)
    print("-" * max(8, len(title) * 2))


def _make_counter():
    """包一层 urlopen，记录每次尝试的单调整时间戳。"""
    stamps: list[float] = []
    original = hc.urllib.request.urlopen

    def counting_urlopen(*args, **kwargs):
        stamps.append(time.monotonic())
        return original(*args, **kwargs)

    hc.urllib.request.urlopen = counting_urlopen
    return stamps, original


def _restore(original) -> None:
    hc.urllib.request.urlopen = original


def _report(label: str, stamps: list[float], error: Exception) -> None:
    print(f"  尝试次数：{len(stamps)}（期望 {EXPECTED_ATTEMPTS} = 1 次首发 + "
          f"{hc.NETWORK_RETRY_ATTEMPTS} 次重试）")
    base = stamps[0] if stamps else 0.0
    for index, stamp in enumerate(stamps):
        gap = "" if index == 0 else f"   距上次 {stamp - stamps[index - 1]:.2f}s"
        print(f"    第 {index + 1} 次尝试：t+{stamp - base:.2f}s{gap}")
    print(f"  最终异常：{type(error).__name__}: {error}")
    check(f"{label}：尝试次数 == {EXPECTED_ATTEMPTS}", len(stamps) == EXPECTED_ATTEMPTS,
          detail=f"实际 {len(stamps)}")
    check(f"{label}：最终抛 HuaweiConnectionError",
          isinstance(error, hc.HuaweiConnectionError), detail=type(error).__name__)
    gaps = [stamps[i] - stamps[i - 1] for i in range(1, len(stamps))]
    for i, expected in enumerate(EXPECTED_BACKOFF):
        if i >= len(gaps):
            break
        check(f"{label}：第 {i + 1} 次重试间隔 ≈ {expected:.1f}s",
              abs(gaps[i] - expected) <= TOLERANCE, detail=f"实际 {gaps[i]:.2f}s")


def run_refresh_path() -> None:
    hr("[1/2] 刷新 token 路径（adapter.refresh → refresh_tokens）")
    tokens = hc.Tokens(
        access_token=None,
        refresh_token="dummy-refresh-token-not-real",
        uid="0",
        refresh_expires_at=time.time() + 3600,
        session_host=BOGUS_HOST,
    )
    adapter = hc.HuaweiHealthCloudAdapter(tokens, host=BOGUS_HOST, session_host=BOGUS_HOST)
    print(f"  会话域（临时）：{adapter.tokens.session_host}")
    stamps, original = _make_counter()
    try:
        asyncio.run(adapter.refresh())
    except Exception as error:  # noqa: BLE001 - 自检就是要看异常
        _report("刷新 token 路径", stamps, error)
    else:
        check("刷新 token 路径：应当抛异常", False, detail="未抛异常")
    finally:
        _restore(original)


def run_data_path() -> None:
    hr("[2/2] 取数路径（adapter.daily_summary → _send）")
    tokens = hc.Tokens(
        access_token="dummy-access-token-not-real",
        refresh_token=None,  # 无 refresh_token → 不触发轮换，单测取数这条出网路径
        uid=None,
        expires_at=time.time() + 3600,
        session_host=BOGUS_HOST,
    )
    adapter = hc.HuaweiHealthCloudAdapter(tokens, host=BOGUS_HOST, session_host=BOGUS_HOST)
    print(f"  数据域（临时）：{adapter.host}")
    stamps, original = _make_counter()
    try:
        asyncio.run(adapter.daily_summary(3))
    except Exception as error:  # noqa: BLE001
        _report("取数路径", stamps, error)
    else:
        check("取数路径：应当抛异常", False, detail="未抛异常")
    finally:
        _restore(original)


def main() -> int:
    print("=" * 60)
    print("网络层重试自检（host 临时指向不可解析域名，不写任何配置）")
    print("=" * 60)
    print(f"临时域名      ：{BOGUS_HOST}")
    print(f"重试常量      ：attempts={hc.NETWORK_RETRY_ATTEMPTS}, "
          f"backoff={hc.NETWORK_RETRY_BACKOFF}")
    print(f"预期尝试次数  ：{EXPECTED_ATTEMPTS}")
    print(f"python        ：{sys.version.split()[0]}")

    run_refresh_path()
    run_data_path()

    hr("说明")
    print("  * host 只在本次进程内临时设为 *.invalid，未写入 _conf_schema / 配置文件，")
    print("    进程退出即恢复；不需要「改回来」这一步。")
    print("  * 本轮自检不联网：域名故意不可解析，命中的是 DNS 失败分支。")

    hr("小结")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：" + "；".join(FAILURES))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
