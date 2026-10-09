#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""插件静态 import 自检：用最小 astrbot 桩导入插件包，验证模块结构可加载。

不联网、不启动 AstrBot、不改动任何配置文件。本机 Python 环境没有安装 astrbot 包，
所以这里注入最小桩模块，只为验证 main.py / adapters/ 的模块级代码（import、类定义、
@register 装饰器）能顺利执行。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PKG = PLUGIN_ROOT.name


def _install_astrbot_stub() -> None:
    """注入最小 astrbot 桩：只提供被 import 的名字，不实现行为。"""
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")

    class _Logger:
        def _log(self, *args, **kwargs):
            return None

        info = warning = error = debug = _log

    api.AstrBotConfig = dict
    api.logger = _Logger()

    api_star = types.ModuleType("astrbot.api.star")

    class Star:
        def __init__(self, context=None):
            self.context = context
            self.name = PKG

    class Context:  # noqa: D401 - 占位类型
        pass

    class StarTools:
        @staticmethod
        def get_data_dir(name=None):
            return f"/tmp/{PKG}"

    def register(*args, **kwargs):
        def decorator(cls):
            cls._registered = args
            return cls

        return decorator

    api_star.Star = Star
    api_star.Context = Context
    api_star.StarTools = StarTools
    api_star.register = register

    api_event = types.ModuleType("astrbot.api.event")
    api_event.AstrMessageEvent = object

    class _EventMessageType:
        """桩 EventMessageType：只提供被引用的枚举成员。"""

        PRIVATE_MESSAGE = "FriendMessage"
        GROUP_MESSAGE = "GroupMessage"
        OTHER_MESSAGE = "OtherMessage"

    class _Filter:
        """桩 filter：把命令装饰器变成直通，供导入期类体执行。"""

        EventMessageType = _EventMessageType

        def command(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def event_message_type(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

        def on_llm_request(self, *args, **kwargs):
            def decorator(func):
                return func

            return decorator

    api_event.filter = _Filter()

    api_provider = types.ModuleType("astrbot.api.provider")

    class ProviderRequest:  # noqa: D401 - 占位类型
        """桩 ProviderRequest：只提供被 import 的名字。"""

    api_provider.ProviderRequest = ProviderRequest

    core_pkg = types.ModuleType("astrbot.core")
    core_agent = types.ModuleType("astrbot.core.agent")
    core_message = types.ModuleType("astrbot.core.agent.message")

    class TextPart:
        """桩 TextPart：带 mark_as_temp，供注入链探测与调用。"""

        def __init__(self, text: str = ""):
            self.text = text
            self.temp = False

        def mark_as_temp(self):
            self.temp = True
            return self

    core_message.TextPart = TextPart

    for name, module in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": api_star,
        "astrbot.api.event": api_event,
        "astrbot.api.provider": api_provider,
        "astrbot.core": core_pkg,
        "astrbot.core.agent": core_agent,
        "astrbot.core.agent.message": core_message,
    }.items():
        sys.modules[name] = module
    api.star = api_star
    api.event = api_event
    api.provider = api_provider
    astrbot.api = api
    astrbot.core = core_pkg
    core_pkg.agent = core_agent
    core_agent.message = core_message


def main() -> int:
    _install_astrbot_stub()
    if str(PLUGIN_ROOT.parent) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT.parent))

    print(f"插件包   ：{PKG}")
    print(f"插件根   ：{PLUGIN_ROOT}")

    import importlib

    adapters = importlib.import_module(f"{PKG}.adapters")
    print(f"[OK] import {PKG}.adapters -> {adapters.__file__}")
    print(f"     导出：{', '.join(adapters.__all__)}")

    main_mod = importlib.import_module(f"{PKG}.main")
    print(f"[OK] import {PKG}.main -> {main_mod.__file__}")
    plugin_cls = getattr(main_mod, "HuaweiHealthPlugin")
    print(f"     插件主类：{plugin_cls.__name__}")
    print(f"     注册参数：{getattr(plugin_cls, '_registered', None)}")

    commands_mod = importlib.import_module(f"{PKG}.commands")
    print(f"[OK] import {PKG}.commands -> {commands_mod.__file__}")
    print(f"     导出：{', '.join(commands_mod.__all__)}")

    # 实例化（桩 Context 为空对象，config 为空 dict）以验证 __init__ 不抛异常
    instance = plugin_cls(None, {})
    print(f"[OK] 实例化成功；uid={instance.uid!r} data_host={instance.data_host!r}")
    print("结果：全部通过（静态 import 自检）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
