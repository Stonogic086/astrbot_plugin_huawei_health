"""华为运动健康插件 —— 服务层（把协议层与存储层接起来）。

模块：
    sync_service.py     —— SyncService：一轮「最近 N 天 → 六类数据 → 写 storage」；
    ondemand_refresh.py —— OnDemandRefresher：查询命令执行前的「按需刷新」闸门
                           （到点跑一轮 sync，失败/超时兜住并退回读旧数据）；
    care_monitor.py     —— CareMonitor：主动关怀的规则层（夜间：深夜窗口 + 每夜一次去重
                           + 近期私聊活动 + 冷却 + 每日上限；压力 / 起床 / 运动后：
                           当日压力日均分定档、起床时间匹配 + 宽容度、训练会话逐条判定）。

对外暴露 SyncService、DATA_CLASSES、OnDemandRefresher 与关怀规则层；
不 import astrbot（可被脚本直接调用自检），不 import 任何第三方库。
"""

from .care_monitor import (
    CARE_CHECK_INTERVAL_MINUTES,
    CARE_CONF_GROUP,
    DEFAULT_ACTIVITY_WINDOW_MINUTES,
    DEFAULT_COOLDOWN_MINUTES,
    DEFAULT_DAILY_LIMIT,
    NIGHT_SCENARIO,
    STRESS_SCENARIO,
    WAKEUP_SCENARIO,
    WORKOUT_SCENARIO,
    CareFinding,
    CareMonitor,
    CareSettings,
    care_settings_from_config,
    training_thresholds_from_config,
)
from .ondemand_refresh import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    DEFAULT_REFRESH_TIMEOUT_SECONDS,
    REASON_EMPTY,
    REASON_ERROR,
    REASON_FRESH,
    REASON_REFRESHED,
    REASON_TIMEOUT,
    SOFT_FRESH_FAILED_HINT,
    OnDemandRefresher,
    interval_seconds_from_minutes,
)
from .sync_service import DATA_CLASSES, SyncService

__all__ = [
    "SyncService",
    "DATA_CLASSES",
    "OnDemandRefresher",
    "CARE_CHECK_INTERVAL_MINUTES",
    "CARE_CONF_GROUP",
    "DEFAULT_ACTIVITY_WINDOW_MINUTES",
    "DEFAULT_COOLDOWN_MINUTES",
    "DEFAULT_DAILY_LIMIT",
    "NIGHT_SCENARIO",
    "STRESS_SCENARIO",
    "WAKEUP_SCENARIO",
    "WORKOUT_SCENARIO",
    "CareFinding",
    "CareMonitor",
    "CareSettings",
    "care_settings_from_config",
    "training_thresholds_from_config",
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "DEFAULT_REFRESH_TIMEOUT_SECONDS",
    "SOFT_FRESH_FAILED_HINT",
    "REASON_FRESH",
    "REASON_REFRESHED",
    "REASON_EMPTY",
    "REASON_TIMEOUT",
    "REASON_ERROR",
    "interval_seconds_from_minutes",
]
