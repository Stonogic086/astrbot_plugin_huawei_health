"""华为运动健康插件 —— 存储层（SQLite）。

模块：
    models.py        —— 数据模型与字段映射（纯函数归一化，无 DB）
    schema.py        —— 表结构与字段口径、schema 版本号、模型 → 表映射（无 DB 读写）
    migrations.py    —— schema 版本迁移链 + 升级前备份（可重复运行、幂等、失败显式报错）
    health_store.py  —— HealthStore：建表建库 / 幂等写入 / 同步状态 / 元信息 / 查询

对外暴露 HealthStore、DEFAULT_DB_PATH、MODEL_TABLES 与 schema/migrations 两个子模块；
不 import 第三方库，不 import astrbot。
"""

from . import migrations, models, schema
from .health_store import DEFAULT_DB_PATH, MODEL_TABLES, HealthStore
from .schema import SCHEMA_VERSION

__all__ = [
    "models",
    "schema",
    "migrations",
    "HealthStore",
    "DEFAULT_DB_PATH",
    "MODEL_TABLES",
    "SCHEMA_VERSION",
]
