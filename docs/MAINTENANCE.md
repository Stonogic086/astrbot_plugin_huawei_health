# 版本维护日志

本插件于 **2026-10-09 结束开发阶段，转入维护阶段**。本文件记录每一次版本发布与维护动作；发版口径见 [`RELEASE.md`](RELEASE.md)，代码结构与边界见 [`DEVELOPMENT.md`](DEVELOPMENT.md)。

原开发进度文档（《华为手环数据接入 AstrBot —— 开发计划》v0.2，含前期可行性验证、技术选型与逐项拍板记录）已归档，存放于内部开发工作区：

```text
develop/huawei_health_plugin/archive/开发计划_归档_2026-10-09.md
```

该路径属于内部资料（NAS 本地工作区），不随仓库发布；需要回溯设计决策时按此位置查阅。README 面向使用者，`DEVELOPMENT.md` 面向维护者，两份文档在维护期内持续更新。

---

## 发布记录

| 日期 | 版本 | 变更要点 | 状态 |
| --- | --- | --- | --- |
| 2026-10-09 | v0.1.0 | 首个公开版本：移植华为健康云协议层（异步门面 + 中文区主机）、SQLite 落地与版本迁移链、三条查询命令、LLM 最小摘要注入（默认关闭、白名单模型）、主动关怀四场景（出厂全关）、市场/链接/文件三种安装方式、两份上游 MIT 许可与 NOTICE 合规披露 | 已发布：GitHub Release（附 `astrbot_plugin_huawei_health-v0.1.0.zip`）+ AstrBot 插件市场上架 |

---

## 维护约定

- 改动存储层或规则层后，至少跑 `selftest_migrations`、`selftest_storage`、`selftest_care`、`selftest_commands`；涉及协议层时补跑 `selftest_protocol`。
- 每次发版严格按 `RELEASE.md` 走，不在别处另立口径。
- 每次发版后在本文件追加一行，注明日期、版本、变更要点与验收结果。
- 归档的开发计划为只读历史，不再回填；新决策写进本日志或 `DEVELOPMENT.md`。
- 待办：`metadata.yaml` 可补 `repo`、`tags`、`support_platforms` 三个可选字段，让市场页信息更完整；改动属于元数据微调，不构成功能变更，可随下一个版本一并提交。
