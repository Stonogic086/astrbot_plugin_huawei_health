"""华为运动健康插件 —— 命令层（查询命令）。

本包只做「让人能在聊天里查健康数据」这一件事，且只读既有库：
  * health_query.py：参数解析 + 读库 + 中文渲染（纯函数，不 import astrbot）；
  * handlers.py   ：命令 handler（async generator，不 import astrbot，可 stub event 自检）。

命令装饰器（@filter.command）与 store 注入在 main.py 内完成，本包不依赖框架。
"""

from .health_query import (
    COMMAND_ACTIVITY,
    COMMAND_SLEEP,
    COMMAND_TRAINING,
    DEFAULT_ACTIVITY_DAYS,
    DEFAULT_TRAINING_DAYS,
    command_tail,
    parse_day,
    parse_days,
    render_activity,
    render_sleep,
    render_training,
)
from .handlers import (
    STORE_UNAVAILABLE,
    handle_activity,
    handle_sleep,
    handle_training,
    plain_result,
)

__all__ = [
    "COMMAND_ACTIVITY",
    "COMMAND_SLEEP",
    "COMMAND_TRAINING",
    "DEFAULT_ACTIVITY_DAYS",
    "DEFAULT_TRAINING_DAYS",
    "STORE_UNAVAILABLE",
    "command_tail",
    "parse_day",
    "parse_days",
    "render_activity",
    "render_sleep",
    "render_training",
    "handle_activity",
    "handle_sleep",
    "handle_training",
    "plain_result",
]
