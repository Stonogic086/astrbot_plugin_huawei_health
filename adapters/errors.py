"""华为运动健康插件 —— 取数门面的统一异常体系（本轮只定义，不改现有抛错行为）。

命名风格照小米插件（``astrbot_plugin_mi_fitness_health`` 的
``adapters/mi_fitness_cloud.py``）：异常取自 ``RuntimeError``，命名 ``<产品><原因>Error``，
且文档字符串写清「上层该拿它做什么」。

本插件已有协议层异常（``adapters.huawei_health_cloud`` 的 ``HuaweiError`` /
``HuaweiConnectionError`` / ``HuaweiApiError`` / ``HuaweiAuthError``）。那四个是「协议实现
细节」，由取数门面内部消化；门面向服务层只抛本模块这三类，服务层据此决定：

  * 认证失效        → 记重登提醒（``reminder.TokenReminder``）、暂停后台自动同步；
  * 网络/重试耗尽   → 本轮放弃，等下一轮同步重试（不触发重登提醒）；
  * 数据解析失败    → 该数据类别降级（跳过并标注「本类暂无」），不中断其余类别。

现有抛点 → 门面异常的映射见 ``PROTOCOL_ERROR_MAP``。本轮**只给建议清单，不改任何现有
抛错行为**（协议层抛的还是原来那四个异常）。

本模块不 import astrbot、不依赖第三方库。
"""

from __future__ import annotations

from .huawei_health_cloud import (
    HuaweiApiError,
    HuaweiAuthError,
    HuaweiConnectionError,
)

__all__ = [
    "HuaweiHealthError",
    "HuaweiHealthAuthenticationError",
    "HuaweiHealthNetworkError",
    "HuaweiHealthParseError",
    "PROTOCOL_ERROR_MAP",
]


# ── 门面统一异常（三类，都从 HuaweiHealthError 派生）──────────────────────


class HuaweiHealthError(RuntimeError):
    """取数门面统一异常基类：认证失效 / 网络重试耗尽 / 解析失败三类都从这里派生。

    只捕获 ``HuaweiHealthError`` 就能兜住门面所有已知失败；单类取数失败只记跳过原因、
    不打断整轮同步（与 ``services/sync_service.py`` 现有的「单类失败不致命」一致）。
    """


class HuaweiHealthAuthenticationError(HuaweiHealthError):
    """认证失效：refresh token 已被云端判死/被拒，只有重新登录能恢复。

    语义贴合本项目既有逻辑：
      * 触发 180 天重登提醒（``reminder.TokenReminder`` 的提前 5 天 / 1 天私聊提醒）；
      * 暂停后台自动同步——凭据没救回来之前，每轮同步只会重复失败
        （与小米 ``MiFitnessAuthenticationError`` 的 "must pause automatic synchronization" 同义）；
      * 对话按需刷新直接退回读旧数据（``OnDemandRefresher`` 兜住、不抛给使用者）。
    """


class HuaweiHealthNetworkError(HuaweiHealthError):
    """网络不可达，或协议层重试已耗尽（DNS 解析失败 / TLS 握手中断 / 连接超时）。

    语义：**可重试**。本轮放弃该次取数并记跳过原因，后台同步等下一轮再试；
    不触发重登提醒，也不改任何缓存数据（``sync_state`` 照记失败状态）。
    """


class HuaweiHealthParseError(HuaweiHealthError):
    """云端已应答，但内容无法解析成我们的数据模型（非 JSON / 非对象 / resultCode 非 0）。

    语义：**不可重试**。该数据类别降级——跳过并标注「本类数据暂无」，不中断其余类别，
    也绝不用编造的时间戳或默认数值填充（数据粒度不够只做降级标注）。
    """


# ── 现有抛点 → 门面异常：映射建议清单（本轮不改抛错行为）──────────────────
# 每项：(现有协议层异常, 对应门面异常, 说明)。仅作接线施工图，实现在接门面那一轮落地。
PROTOCOL_ERROR_MAP: tuple[tuple[type[BaseException], type[BaseException], str], ...] = (
    (
        HuaweiAuthError,
        HuaweiHealthAuthenticationError,
        "认证类：登录应答缺 accessToken（Tokens.apply）、refresh token 缺失或超 180 天"
        "（refresh_tokens 开头）、ensure_ok 命中致命码"
        "（20020003 / 20020001 / 20010004 / 1002 / 1004 / 1005）。",
    ),
    (
        HuaweiConnectionError,
        HuaweiHealthNetworkError,
        "网络类：_json_request_once 的 URLError / TimeoutError / OSError 分支；"
        "经 _json_request 重试 NETWORK_RETRY_ATTEMPTS 次后仍失败（即重试耗尽）。",
    ),
    (
        HuaweiApiError,
        HuaweiHealthParseError,
        "应答不可用类：_loads 的『非 JSON / 非对象』（code=0）与 _json_request_once 的"
        "HTTPError，以及 ensure_ok / HuaweiHealthClient.post 的非致命 resultCode。"
        "云端已应答但拿不到可用内容，按「解析/约定不符」降级处理、不重试。"
        "（建议：非致命 resultCode 一项为判断口径，需主人确认是否单列「服务端拒绝」类。）",
    ),
)
