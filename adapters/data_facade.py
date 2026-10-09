"""华为运动健康插件 —— 取数门面抽象骨架（本轮只落接口位，不接线、不实现取数）。

范式照小米插件 ``astrbot_plugin_mi_fitness_health`` 的 ``adapters/base.py``
（``DataAdapter`` 抽象基类）：
    1. **抽象接口**：一个 ABC，只声明方法，不含取数实现；
    2. **统一异常**：向上层只抛 ``adapters.errors`` 的三类失败，不泄漏协议层异常；
    3. **门面负责单位与时区重排**：把云端原始记录换算成存储层的字段口径与本地日期/时刻。

按本项目真实数据源的两处差异：
    * 方法集合按本项目**真实有的六类数据**——daily_activity / heart_rate / sleep /
      spo2 / stress / training；**不含** body_measurements（华为无数据源，已下线）；
    * 取数窗口以「本地日历日」表达（存储层按 'YYYY-MM-DD' 文本存日期），不沿用小米
      的 ``datetime(UTC)`` 口径。

【签名口径待主人确认】本模块只落「接口位」——常量、方法名、文档字符串；六个取数方法
一律 ``raise NotImplementedError``，不写实现体，也**不被任何现有代码 import**。
参数与返回的确切形状（本地日历日窗口 vs 纯天数、``list`` vs 异步迭代器）等主人拍板后再定；
把调用点接过来（``services/sync_service.py``）另起一轮，避免返工。

单位与时区的既有处理位置（接门面时复用，勿重复实现）：
    * 日汇总：``huawei_health_cloud.daily_activity`` 把 calorie（千分之一 kcal）换算成
      kcal；``storage.models.normalize_daily_activity`` 再做一次归一护栏；
    * health：``huawei_health_cloud.health_series`` 把毫秒时间戳整形成本地
      'YYYY-MM-DD HH:MM'；``storage.models.normalize_health`` 丢弃 0/负值并补秒；
    * 训练：``storage.models.merge_training_segments`` 把 epoch 毫秒转本地时间、
      calorie 由千分之一 kcal 换算成 kcal 并合并分钟段。

本模块不 import astrbot、不依赖第三方库。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from .errors import (  # noqa: F401 - 供子类实现时直接引用门面三类异常
    HuaweiHealthAuthenticationError,
    HuaweiHealthNetworkError,
    HuaweiHealthParseError,
)

__all__ = [
    "DATA_DAILY_ACTIVITY",
    "DATA_HEART_RATE",
    "DATA_SLEEP",
    "DATA_SPO2",
    "DATA_STRESS",
    "DATA_TRAINING",
    "DATA_TYPES",
    "HealthDataFacade",
]

# ── 六类数据名（本项目真实有的数据源；不含 body_measurements）──────────────
DATA_DAILY_ACTIVITY = "daily_activity"
DATA_HEART_RATE = "heart_rate"
DATA_SLEEP = "sleep"
DATA_SPO2 = "spo2"
DATA_STRESS = "stress"
DATA_TRAINING = "training"

# 门面对外声明的数据类别全集（get_available_data_types 的取值域）。
DATA_TYPES: tuple[str, ...] = (
    DATA_DAILY_ACTIVITY,
    DATA_HEART_RATE,
    DATA_SLEEP,
    DATA_SPO2,
    DATA_STRESS,
    DATA_TRAINING,
)


class HealthDataFacade(ABC):
    """取数门面抽象基类：把云端六类数据统一取成存储层口径。

    生命周期（与小米 ``DataAdapter`` 一致）：
        ``connect()`` → ``get_available_data_types()`` → ``iter_*()`` → ``close()``

    六个取数方法一律 ``raise NotImplementedError``：本模块只落接口位，等主人确认签名
    口径后再由子类实现。子类实现时必须：
        * 抛 ``adapters.errors`` 的三类异常，不把协议层内部异常漏到上层；
        * 自己负责单位换算与本地时区重排，返回可直接交给 ``storage`` 的字段口径；
        * 数据粒度不够只做降级标注（如心率 v1 只有日值），不编造时间戳或数值。
    """

    # ── 生命周期（照小米 DataAdapter 的三个方法）──────────────────────────
    @abstractmethod
    async def connect(self) -> bool:
        """认证并确认可用数据类别；返回是否拿到可用 access token。"""

    @abstractmethod
    async def close(self) -> None:
        """释放本门面持有的资源（协议层无持久连接时是空操作）。"""

    @abstractmethod
    def get_available_data_types(self) -> list[str]:
        """返回已确认可用的数据类别（取自 ``DATA_TYPES``），不再探测云端。"""

    # ── 六类取数：只留接口位（签名口径待主人确认，不写实现体）──────────────
    @abstractmethod
    async def iter_daily_activity(self, start: date, end: date) -> list[dict]:
        """[start, end] 本地日历日窗口内的日汇总。

        口径：步数 / 距离（米）/ 消耗（kcal）/ 各类活动时长，按本地日期唯一。
        """
        raise NotImplementedError

    @abstractmethod
    async def iter_heart_rate(self, start: date, end: date) -> list[dict]:
        """窗口内的心率日值（静息 / 日间 / 平均静息 / 最高 / 最低）。

        v1 只有日粒度（汇总型样本）；粒度不够就标注，不编造分钟级时间戳。
        """
        raise NotImplementedError

    @abstractmethod
    async def iter_sleep(self, start: date, end: date) -> list[dict]:
        """窗口内的睡眠汇总（时长 / 评分 / 效率 / HRV / 血氧 / 入睡起床 / 白天小睡）。"""
        raise NotImplementedError

    @abstractmethod
    async def iter_spo2(self, start: date, end: date) -> list[dict]:
        """窗口内的血氧（华为来源是睡眠响应里的 lastAvgSpO2，非独立采样）。"""
        raise NotImplementedError

    @abstractmethod
    async def iter_stress(self, start: date, end: date) -> list[dict]:
        """窗口内的压力日值（日均 / 最近一次 / 最高 / 最低 / 测量次数）。"""
        raise NotImplementedError

    @abstractmethod
    async def iter_training(self, start: date, end: date) -> list[dict]:
        """窗口内的训练会话（分钟段去重合并后成会话）。

        epoch 毫秒 → 本地时刻的换算与分钟段合并由门面/存储层负责，调用方不再整形。
        """
        raise NotImplementedError
