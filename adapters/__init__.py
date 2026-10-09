"""华为健康云数据适配层。

协议层移植自 and7ey/huawei_health（MIT，见插件根目录 LICENSE / NOTICE）：
    const.py                  —— 协议常量（含本插件实测补充的中国区主机）
    huawei_health_cloud.py    —— 同步协议客户端 + asyncio.to_thread 异步门面
    huawei_health_facade.py   —— 取数门面实现（六类数据 → storage/models 口径，异常只抛三类）
    errors.py                 —— 取数门面的统一异常体系（认证失效 / 网络重试耗尽 / 解析失败）
    data_facade.py            —— 取数门面抽象骨架（六类数据的接口位与常量；实现见 huawei_health_facade）
"""

from . import const
from .data_facade import (
    DATA_DAILY_ACTIVITY,
    DATA_HEART_RATE,
    DATA_SLEEP,
    DATA_SPO2,
    DATA_STRESS,
    DATA_TRAINING,
    DATA_TYPES,
    HealthDataFacade,
)
from .errors import (
    PROTOCOL_ERROR_MAP,
    HuaweiHealthAuthenticationError,
    HuaweiHealthError,
    HuaweiHealthNetworkError,
    HuaweiHealthParseError,
)
from .huawei_health_facade import HuaweiHealthFacade
from .huawei_health_cloud import (
    HuaweiApiError,
    HuaweiAuthError,
    HuaweiConnectionError,
    HuaweiError,
    HuaweiHealthClient,
    HuaweiHealthCloudAdapter,
    Tokens,
    authorization_code_from,
    authorization_url,
    daily_activity,
    day_int,
    exchange_authorization_code,
    health_series,
    query_access_token,
    refresh_tokens,
)

__all__ = [
    "const",
    "HuaweiHealthCloudAdapter",
    "HuaweiHealthClient",
    "Tokens",
    "HuaweiError",
    "HuaweiConnectionError",
    "HuaweiApiError",
    "HuaweiAuthError",
    "authorization_url",
    "authorization_code_from",
    "exchange_authorization_code",
    "refresh_tokens",
    "query_access_token",
    "daily_activity",
    "health_series",
    "day_int",
    "HuaweiHealthError",
    "HuaweiHealthAuthenticationError",
    "HuaweiHealthNetworkError",
    "HuaweiHealthParseError",
    "PROTOCOL_ERROR_MAP",
    "HealthDataFacade",
    "HuaweiHealthFacade",
    "DATA_TYPES",
    "DATA_DAILY_ACTIVITY",
    "DATA_HEART_RATE",
    "DATA_SLEEP",
    "DATA_SPO2",
    "DATA_STRESS",
    "DATA_TRAINING",
]
