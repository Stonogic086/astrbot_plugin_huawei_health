#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两个授权 web api handler 的独立自检（沿用第一块 import_check.py 的桩注入方式）。

不启动 AstrBot、不改配置目录、不 push。token 全程只脱敏显示。
分四段：
  [1] 桩导入 + 实例化（同 import_check）
  [2] 注册校验：initialize() 是否登记了 auth/url(GET) 与 auth/code(POST)
  [3] 直接调用 handler 1：生成授权链接
  [4] 直接调用 handler 2：
        4a 离线：合成 hms:// 回调串 + 替换 _exchange_code，验证「解析→写回→脱敏」链路
        4b 在线：用 /…/huawei_health_adapter/.auth_state.json 的 refresh_token 真刷一次，
              再走 persist_tokens 验证写回逻辑（真实联网）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import import_check  # noqa: E402  （复用其 astrbot 桩）

import_check._install_astrbot_stub()
PKG = import_check.PKG
if str(import_check.PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(import_check.PLUGIN_ROOT.parent))

import importlib  # noqa: E402

main_mod = importlib.import_module(f"{PKG}.main")
adapters = importlib.import_module(f"{PKG}.adapters")

STATE_PATH = Path(
    "/vol1/@appdata/astrbot/data/projects/huawei_health_adapter/.auth_state.json"
)

FAILURES: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
    if not cond:
        FAILURES.append(name)


class FakeConfig(dict):
    """模拟 AstrBotConfig：dict 本体 + save_config() 落盘钩子。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_calls = 0
        self.saved_snapshot: dict | None = None

    def save_config(self) -> None:
        self.save_calls += 1
        self.saved_snapshot = json.loads(json.dumps(self))


class FakeContext:
    """只记录 register_web_api 调用。"""

    def __init__(self):
        self.calls: list[tuple] = []

    def register_web_api(self, route, handler, methods, desc):
        self.calls.append((route, handler, tuple(methods), desc))
        return True


def hr(title: str) -> None:
    print("\n" + title)
    print("-" * max(8, len(title) * 2))


def main() -> int:
    hr("[1] 桩导入 + 实例化")
    plugin_cls = getattr(main_mod, "HuaweiHealthPlugin")
    ctx = FakeContext()
    config = FakeConfig({"account": {}})
    plugin = plugin_cls(ctx, config)
    print(f"  plugin={type(plugin).__name__}  config.account={dict(config.get('account', {}))}")
    check("实例化成功", plugin is not None)

    hr("[2] initialize() 注册校验")
    asyncio.run(plugin.initialize())
    routes = {c[0]: c for c in ctx.calls}
    for r in ctx.calls:
        print(f"  注册：route={r[0]!r}  methods={list(r[2])}  desc={r[3]!r}  同名进程={r[1].__name__}")
    # 宿主按「完整 plugin_path」匹配注册路由（plugin_path = 插件名 + 相对子路径），
    # 所以注册出的 route 字符串必须带插件名，否则永远匹配不上（原 bug）。
    url_route = f"/{plugin.name}/auth/url"
    code_route = f"/{plugin.name}/auth/code"
    check("auth/url 注册路径带插件名", url_route in routes,
          f"期望 {url_route!r}；实际 {sorted(routes)!r}")
    check("auth/url 注册路径里含插件名", plugin.name in url_route)
    check("auth/url 方法=GET", routes.get(url_route, (None,) * 3)[2] == ("GET",))
    check("auth/code 注册路径带插件名", code_route in routes,
          f"期望 {code_route!r}；实际 {sorted(routes)!r}")
    check("auth/code 方法=POST", routes.get(code_route, (None,) * 3)[2] == ("POST",))
    check("不再注册旧的相对路径 auth/url", "auth/url" not in routes)
    check("不再注册旧的相对路径 auth/code", "auth/code" not in routes)
    check("handler 均为独立可调用", callable(plugin.web_authorize_url) and callable(plugin.web_submit_code))

    hr("[3] handler 1：生成授权链接（直接调用）")
    out1 = asyncio.run(plugin.web_authorize_url())
    print(f"  返回键：{list(out1.keys())}")
    url = out1.get("url", "")
    print(f"  url（前 120 字符）：{url[:120]}...")
    check("返回恰好 {\"url\": ...}", list(out1.keys()) == ["url"])
    check("url 指向华为 oauth", url.startswith("https://oauth-login.cloud.huawei.com/oauth2"))
    check("url 含 redirect_uri=hms://redirect_url", "hms%3A%2F%2Fredirect_url" in url)

    hr("[4a] handler 2：离线（合成回调串 + 替换 _exchange_code）")
    synthetic = "hms://redirect_url?code=SYNTHETICCODE_not_a_real_code&state=deadbeef"
    print(f"  入参：{synthetic}")

    def fake_exchange(code: str):
        print(f"  [替换的 _exchange_code] 收到 code={code!r}（长度 {len(code)}）")
        return adapters.Tokens(
            access_token="FAKEACCESS_TOKEN_ABCDEF0123456789_END123456",
            refresh_token="FAKEREFRESH_TOKEN_ABCDEF0123456789_END654321",
            uid="1234567890",
            site_id=None,
            expires_at=4102444800.0,
            refresh_expires_at=4133980800.0,
            session_host="https://healthcommon-drcn.things.dbankcloud.com",
        )

    plugin._exchange_code = fake_exchange  # 离线替身，仅本段生效
    before = plugin.config.save_calls
    out2a = asyncio.run(plugin.web_submit_code(synthetic))
    print(f"  返回：{json.dumps(out2a, ensure_ascii=False)}")
    check("ok=True", out2a.get("ok") is True)
    check("uid 写回", plugin.uid == "1234567890")
    check("access_token 已脱敏（前后各 6 位）",
          str(out2a.get("access_token", "")).startswith("FAKEAC")
          and str(out2a.get("access_token", "")).endswith("123456"))
    check("配置已落盘 save_config 被调用", plugin.config.save_calls == before + 1)
    acc = plugin.config.get("account", {})
    check("config.account.access_token 落地", acc.get("access_token", "").startswith("FAKEAC"))
    check("config.account.uid 落地", acc.get("uid") == "1234567890")
    check("返回值内 token 非明文", "FAKEACCESS_TOKEN_ABCDEF0123456789_END123456" not in json.dumps(out2a))
    # P2-1：到期时间必须「落盘 + 回写内存」成对，否则进程内每次 build_tokens() 都拿旧值
    # （首次为 0），needs_rotation() 会恒判「需轮换」。
    check("access_token_expires_at 落盘",
          acc.get("access_token_expires_at") == 4102444800,
          f"disk={acc.get('access_token_expires_at')!r}")
    check("access_token_expires_at 回写内存（不再是 0）",
          plugin.access_token_expires_at == 4102444800.0,
          f"memory={plugin.access_token_expires_at!r}")
    check("refresh_token_expires_at 回写内存（与落盘对称）",
          plugin.refresh_token_expires_at == 4133980800.0,
          f"memory={plugin.refresh_token_expires_at!r}")
    rebuilt = plugin.build_tokens()
    check("下一轮 build_tokens() 带的是新到期时间（非 0 / 非旧值）",
          rebuilt.expires_at == 4102444800.0, f"expires_at={rebuilt.expires_at!r}")
    check("刚写回的 token 不再被判为需轮换（needs_rotation()=False）",
          rebuilt.needs_rotation() is False)

    hr("[4b] handler 2：在线（用 .auth_state.json 的 refresh_token 真刷一次）")
    if not STATE_PATH.exists():
        check("存在 .auth_state.json", False, str(STATE_PATH))
    else:
        state = json.loads(STATE_PATH.read_text("utf-8"))
        tk = state.get("tokens", {})
        tokens = adapters.Tokens(
            access_token=tk.get("access_token"),
            refresh_token=tk.get("refresh_token"),
            uid=tk.get("uid"),
            site_id=tk.get("site_id"),
            session_host=state.get("session_host") or adapters.const.SESSION_HOST_CN,
        )
        print(f"  载入 refresh_token={main_mod.mask_secret(tk.get('refresh_token'))}（脱敏）")
        adapter = adapters.HuaweiHealthCloudAdapter(
            tokens, host=adapters.const.APP_HOST_CN,
            session_host=tokens.session_host or adapters.const.SESSION_HOST_CN,
        )
        try:
            asyncio.run(adapter.refresh())
            print(f"  刷新成功：uid={tokens.uid}  "
                  f"access_token={main_mod.mask_secret(tokens.access_token)}（脱敏）  "
                  f"会话域={tokens.session_host}")
            check("在线刷新拿到新 access_token", bool(tokens.access_token))
        except Exception as error:
            print(f"  刷新失败：{type(error).__name__}: {error}")
            check("在线刷新拿到新 access_token", False, type(error).__name__)

        before = plugin.config.save_calls
        out2b = plugin.persist_tokens(tokens)
        print(f"  persist_tokens 返回：{json.dumps(out2b, ensure_ascii=False)}")
        check("真实 token 写回 ok=True", out2b.get("ok") is True)
        check("真实 token 写回后 save_config 被调用", plugin.config.save_calls == before + 1)
        acc = plugin.config.get("account", {})
        check("真实 access_token 落地且=内存值",
              acc.get("access_token") == plugin.access_token)

    hr("[4c] handler 2：quart request（模拟宿主从框架全局 request 读 JSON body）")

    class FakeQuartRequest:
        """模拟 quart 的全局 request：get_json() 返回前端提交的 {"text": ...}。"""

        def __init__(self, payload):
            self._payload = payload

        async def get_json(self, silent=False):
            return self._payload

    synth_code = "REALBRANCHCODE_from_quart_body"
    synth_text = f"hms://redirect_url?code={synth_code}&state=cafebabe"
    seen = {}

    def fake_exchange_quart(code: str):
        seen["code"] = code
        return adapters.Tokens(
            access_token="QUARTACCESS_TOKEN_ABCDEF0123456789_END111111",
            refresh_token="QUARTREFRESH_TOKEN_ABCDEF0123456789_END222222",
            uid="1234567890",
            site_id=None,
            expires_at=4102444800.0,
            refresh_expires_at=4133980800.0,
            session_host="https://healthcommon-drcn.things.dbankcloud.com",
        )

    plugin._exchange_code = fake_exchange_quart
    original_quart = getattr(main_mod, "_quart_request", None)
    main_mod._quart_request = FakeQuartRequest({"text": synth_text})
    try:
        # 关键：不带任何入参调用，模拟宿主 call_request_view 只传路径参数的真实调用方式。
        out2c = asyncio.run(plugin.web_submit_code())
    finally:
        main_mod._quart_request = original_quart
    print(f"  框架 request 的 JSON body：{json.dumps({'text': synth_text}, ensure_ascii=False)}")
    print(f"  解析出的 code={seen.get('code')!r}")
    print(f"  返回：{json.dumps(out2c, ensure_ascii=False)}")
    check("从 quart request 的 JSON body 取到回调串", seen.get("code") == synth_code,
          f"期望 {synth_code!r}；实际 {seen.get('code')!r}")
    check("进入换 token 分支并写回（ok=True）", out2c.get("ok") is True)
    check("quart 分支返回值内 token 非明文",
          "QUARTACCESS_TOKEN_ABCDEF0123456789_END111111" not in json.dumps(out2c))

    hr("[4d] handler 2：授权失败 → 错误文案人话化（原始 resultCode 保留）")

    def call_with_error(exc, pasted=synthetic):
        def raiser(code):
            raise exc
        plugin._exchange_code = raiser
        return asyncio.run(plugin.web_submit_code(pasted))

    # 20020001：HuaweiAuthError 不带 .code，只把码写进文本，需从文本抠码
    out_20020001 = call_with_error(adapters.HuaweiAuthError(
        "https://healthcommon-drcn.things.dbankcloud.com/commonAbility/"
        "userAccessToken/obtain: 20020001 - code used twice"))
    print(f"  20020001 → {json.dumps(out_20020001, ensure_ascii=False)}")
    check("20020001 命中人话「授权码已失效或已被使用过」",
          "授权码已失效或已被使用过" in out_20020001.get("error", ""))
    check("20020001 保留原始码 20020001", "20020001" in out_20020001.get("error", ""))
    check("20020001 失败态 ok=False / stage=exchange",
          out_20020001.get("ok") is False and out_20020001.get("stage") == "exchange")

    for code in (1002, 1004):
        out = call_with_error(adapters.HuaweiAuthError(
            "https://healthcommon-drcn.things.dbankcloud.com/commonAbility/"
            f"userAccessToken/refresh: {code} - session replaced"))
        print(f"  {code} → {json.dumps(out, ensure_ascii=False)}")
        check(f"{code} 命中「会话被手机端…抢走」",
              "会话被手机端的华为运动健康抢走了" in out.get("error", ""))
        check(f"{code} 保留原始码 {code}", str(code) in out.get("error", ""))

    out_20020003 = call_with_error(adapters.HuaweiAuthError(
        "https://healthcommon-drcn.things.dbankcloud.com/commonAbility/"
        "userAccessToken/refresh: 20020003 - refresh token invalid"))
    print(f"  20020003 → {json.dumps(out_20020003, ensure_ascii=False)}")
    check("20020003 命中「凭据已失效」", "凭据已失效" in out_20020003.get("error", ""))
    check("20020003 保留原始码 20020003", "20020003" in out_20020003.get("error", ""))

    # 未列出的码：保留类型 + resultCode，且绝不回显响应体原文（_safe_error_text）
    out_unknown = call_with_error(adapters.HuaweiApiError(
        99999, "https://healthdata.dbankcloud.cn/dataQuery/sport/v2/getSportsStat",
        "SECRETBODY-should-not-appear"))
    print(f"  未列出码 → {json.dumps(out_unknown, ensure_ascii=False)}")
    check("未列出码保留原始 resultCode 99999", "99999" in out_unknown.get("error", ""))
    check("未列出码不回显响应体原文",
          "SECRETBODY-should-not-appear" not in json.dumps(out_unknown))

    hr("小结")
    if FAILURES:
        print(f"失败 {len(FAILURES)} 项：" + "；".join(FAILURES))
        return 1
    print("全部通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
