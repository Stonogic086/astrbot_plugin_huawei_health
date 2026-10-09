# astrbot_plugin_huawei_health

华为运动健康数据接入 AstrBot。目标：把华为手环的数据（步数、睡眠、心率、压力、
血氧、训练）走服务器侧轮询接入，不经手机 App。

当前进度：**v1 已完成** —— 协议层移植、授权交互（配置页 + 授权页回调）、定时同步、
对话按需刷新、SQLite 入库、查询命令、隐私闸门、180 天重登私聊提醒，配套 12 个自检脚本。

## 目录结构

```
astrbot_plugin_huawei_health/
├── main.py                       # 插件主类：配置读写、存储/同步/提醒接线、授权路由、查询命令
├── metadata.yaml                 # 插件元数据（pages: [huawei-auth]）
├── _conf_schema.json             # 配置 schema（token 用 password 字段做界面遮罩）
├── requirements.txt              # 无第三方依赖
├── LICENSE / NOTICE              # 上游 MIT 许可与来源声明
├── privacy_gate.py               # 隐私闸门（fail-closed）+ 唯一的脱敏实现 mask_secret
├── reminder.py                   # 180 天重登提醒（去重落盘、async 发送、只记私聊目标）
├── adapters/                     # 协议层（移植自 and7ey/huawei_health）
│   ├── const.py                  # 协议常量（含中国区主机，注明实测来源与日期）
│   └── huawei_health_cloud.py    # 同步协议客户端 + asyncio.to_thread 异步门面
├── storage/                      # 存储层（纯标准库 sqlite3，不依赖 astrbot）
│   ├── models.py                 # 数据模型与字段映射（纯函数归一化，含单位换算）
│   ├── schema.py                 # 唯一表结构定义：SCHEMA_VERSION / DDL / 字段口径注释
│   ├── migrations.py             # schema 版本迁移链 + 升级前备份（幂等、失败显式报错）
│   ├── health_store.py           # HealthStore：建库迁移 / 幂等写入 / 同步状态 / 查询
│   └── __init__.py               # 对外导出 HealthStore、SCHEMA_VERSION
├── services/                     # 服务层
│   ├── sync_service.py           # SyncService：一轮「最近 N 天 → 六类数据 → 写库」
│   └── ondemand_refresh.py       # OnDemandRefresher：查询前的按需刷新闸门
├── commands/                     # 查询命令层（只读库、不调用 LLM）
│   ├── health_query.py           # 参数解析 + 中文渲染（纯函数）
│   ├── handlers.py               # async generator handler（可 stub event 自检）
│   └── __init__.py               # 对外导出命令名与 handler
├── features/                     # 特性模块（纯逻辑，不依赖运行期框架上下文）
│   └── llm_injection.py          # 健康摘要注入 LLM 上下文（多重前置门 + fail-closed）
├── pages/huawei-auth/index.html  # 授权页（生成授权链接 + 回收回调串）
└── scripts/                      # 自检脚本（见「自检」一节）
```

## 运行期行为（v1）

- **定时同步**：`sync.enable_auto_sync`（默认关闭）打开后，后台循环按
  `sync.sync_interval_minutes`（默认 60 分钟）拉最近 `sync.default_sync_days`（默认 3 天）
  的六类数据并入库；同一循环顺带做一次重登提醒检查。
- **对话按需刷新**：查询命令读库前，若距上次成功同步已超过
  `sync.natural_query_sync_minutes`（默认 15 分钟），先同步一轮再读库；失败 / 超时一律兜住并回退读旧数据。
- **重登提醒**：refresh token 到期前 5 天、1 天各提醒一次，同一窗口只提醒一次（去重落盘，重启不重复）；
  只记私聊来源的会话，提醒走私聊。
- **授权**：配置页的授权页生成链接 → 浏览器里走一次 → 把 `hms://` 回调串贴回 →
  `POST /<插件名>/auth/code` 换 token 并写回配置；同步轮里刷新出的新 token 也会立刻回写
  （云端真轮换 refresh token 时，配置里不会留旧值）。
- **隐私**：健康数据默认不外送（`privacy.allow_health_data_to_llm=false`）；命令输出只回给使用者本人。

## 存储层（SQLite）

- 库文件：默认 `<插件数据目录>/health.db`
  （即 `/vol1/@appdata/astrbot/data/plugin_data/astrbot_plugin_huawei_health/health.db`）；
  也可在配置里用 `storage.database_path` 指定路径。目录 / 文件缺失自动创建，落盘后收 0600。
- 时间一律按本地时间（CST）存文本：日期 `YYYY-MM-DD`，时间 `YYYY-MM-DD HH:MM:SS`。
- 幂等：每张表以日期或 `session_key` 作主键，写入用 `INSERT ... ON CONFLICT(...) DO UPDATE`，
  冲突时逐列 `COALESCE(新值, 旧值)`——重复同步不产生重复行，且本次没取到的字段保留库中旧值。
- 写入接口：`upsert_daily_activity(records)`（日汇总，带「聚合行 / 读数非空」护栏）与
  `upsert_rows(model, rows)`（门面口径的归一化行直写）；
  查询接口：`query(model, start, end)` / `get(model, key)` / `count(model)` / `tables()`。
- **schema 版本**：`SCHEMA_VERSION = 2`。新库一次建到最新版；旧库在 `initialize()` 里先在同目录生成
  带时间戳的备份副本，再按版本号顺序迁移（每步在事务里，失败回滚并显式抛错，不静默半迁移；
  备份失败同样报错停下）。库版本高于插件支持的版本时拒绝打开，不做降级迁移。
- **迁移链**：v1 = 六张业务表（含 `sleep_session` 的入睡/起床/小睡三列）与训练索引；
  v2 = `sync_state`（每类数据的同步状态）与 `meta`（插件名、首次建库、最近升级与备份记录）。
- **状态与元信息表**（非业务数据）：`sync_state(data_type 主键：最近尝试/成功时间、状态、窗口上界、失败原因)`
  与 `meta(key/value：plugin、schema_created_at、schema_upgraded_at、last_backup_file、last_backup_at)`，
  另有单行 `schema_version`。

| 表 | 主键 | 关键字段（类型） | 来源 |
| --- | --- | --- | --- |
| `daily_activity` | date | steps(int)/distance_m(int,米)/kcal(real,千卡)/duration_min(int)/walk_min(int)/active_hours(int)/exercise_min(int)/step_goal(int) | getSportsStat（按日期唯一） |
| `heart_rate_sample` | date | resting_hr/day_hr/average_resting_hr/max_hr/min_hr(real)；sample_kind='daily_summary'（汇总型样本） | getHealthStat type 7 heartRateBasic |
| `sleep_session` | date | duration_min/score/efficiency/hrv/spo2/nap_duration_min(real)；fall_asleep_local/wakeup_local(text, `YYYY-MM-DD HH:MM:SS`) | getHealthStat type 9 professionalSleep（入睡/起床取 fallAsleepTime/wakeupTime；小睡取 daySleepTime） |
| `stress_sample` | date | average/last_value/max_value/min_value/measurements | getHealthStat type 11 stressBasic |
| `spo2_sample` | date | spo2(real)；sample_kind='sleep_last_avg' | 睡眠响应 professionalSleep.lastAvgSpO2 |
| `training_session` | session_key=`<sport_type>:<start_ms>` | sport_type/duration_min/distance_m/kcal/segments/device_code | getSportsDataByTime（dataId 去重，间隔 ≤15 分钟合并） |

单位换算：上游 `calorie` 为千分之一 kcal，入协议层整形时 `/1000` 存为 `kcal`（存储层原样入库）；
`distance` 上游即米，原样存 `distance_m`。`BodyMeasurement` 已下线：不建表、不提供接口。

## 关键事实（已实测）

- 中国区会话域（换 token）：`https://healthcommon-drcn.things.dbankcloud.com`，实测 `resultCode 0`。
- 中国区数据域（取数）：`https://healthdata.dbankcloud.cn`，实测取到最近 3 天日汇总。
  EU 域 `sportdata-dre.things.dbankcloud.com` 会返回 `resultCode 0` 但空数组，不能当作成功。
- `client_id / appId = 10414141`，对中国区账号实测可用。

## 自检

12 个脚本，`selftest_protocol.py` / `selftest_sync.py` / `selftest_webapi.py` 会真连华为云（只读），
其余全部离线；都不启动 AstrBot、不改插件配置、不碰真实 `health.db`。

```bash
python3 scripts/import_check.py           # 静态 import 自检（astrbot 桩，不联网）
python3 scripts/selftest_protocol.py      # 协议层：刷新 token + 拉最近 N 天日汇总（只读、联网）
python3 scripts/selftest_webapi.py        # 两条授权路由 + persist_tokens（含一次真刷新）
python3 scripts/selftest_network_retry.py # 网络层重试（域名故意不可解析，不联网）
python3 scripts/selftest_storage.py       # 存储层：建表/写入/幂等/区间/单位换算（临时库）
python3 scripts/selftest_migrations.py    # 迁移链：新库建到最新版/幂等/旧库无损升级/备份/失败回滚（临时库）
python3 scripts/selftest_sync.py          # 完整一轮同步 → 落库 → 读回（云端只读，临时库）
python3 scripts/selftest_commands.py      # 三个查询命令的渲染、无数据分支与身份门（临时库）
python3 scripts/selftest_privacy.py       # 隐私闸门 + 重登提醒（含 async 发送路径、私聊目标）
python3 scripts/selftest_ondemand.py      # 按需刷新（假时钟 + 假 run_once）
python3 scripts/selftest_facade.py        # 取数门面：云端原始响应 → 存储层口径（桩适配器）
python3 scripts/selftest_llm_injection.py # LLM 注入的多重前置门（闸门 / provider 白名单 / fail-closed）
```

## 关键约束

- token 只在配置页做界面遮罩（框架不加密），不进日志、不进报告；插件内脱敏只有
  `privacy_gate.mask_secret` 一处实现。
- 库文件与提醒状态文件落盘后收 0600。
- 健康数据只回给使用者本人：查询命令有身份门（`_is_owner`，只放行框架管理员=本人，拿不到
  结构化身份字段即拒绝）；出站闸门（`privacy_gate.py`）默认关闭，当前唯一的出站路径是
  `features/llm_injection.py`（闸门打开且本轮 provider 在白名单内才注入 LLM 上下文）。
- v2 待做：睡眠分期明细入库、分钟级心率逆向探索、训练效率等扩展指标。

## 许可

协议层移植自 [and7ey/huawei_health](https://github.com/and7ey/huawei_health)（MIT），
见 `LICENSE` 与 `NOTICE`。使用非官方接口可能违反华为服务条款，责任在使用者本人。
