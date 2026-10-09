# 版本发布流程

本文是**发版口径的唯一出处**，供后续版本维护者照做。开发期文档见 `DEVELOPMENT.md`，发版记录见 `MAINTENANCE.md`。

---

## 一、发版前需要准备的东西

1. **自检全绿**
   - 常规：`python3 scripts/selftest_migrations.py`、`selftest_storage.py`、`selftest_commands.py`、`selftest_privacy.py`、`selftest_ondemand.py`、`selftest_webapi.py`、`selftest_network_retry.py`、`selftest_facade.py`、`selftest_llm_injection.py`、`selftest_care.py`、`import_check.py`。
   - 真连云（需有效授权）：`selftest_protocol.py`、`selftest_sync.py` 各至少跑一次。
   - 改动了存储层或规则层的版本，至少保证 `selftest_migrations`、`selftest_storage`、`selftest_care`、`selftest_commands` 通过。
2. **版本号**
   - `metadata.yaml` 的 `version` 是唯一版本源，写法 `vX.Y.Z`。
   - Release 标签、安装包文件名、市场记录里的版本号，三处都必须与它一致。
3. **插件身份不能动**
   - 市场以 `author + "/" + name` 作为插件 id，本插件是 `Stonogic086/astrbot_plugin_huawei_health`。
   - 改 `name` 或 `author` 等于换插件身份，已安装用户会认不到更新。确需迁移时走市场的“仓库迁移/认领”流程，并按新 id 重新提交。
4. **合规与体积**
   - 三份许可文件（两份上游 MIT + 本项目 LICENSE）与 `NOTICE` 必须随包发布。
   - 安装包 **≤ 16MB**，这是 AstrBot 插件市场 CI 的硬限制；本插件当前约 0.5MB，正常不会踩线。超限只能联系市场维护者手动放行。
   - 发布前复核仓库无敏感产物：`git ls-files | grep -Ei "\.db|token|auth_state|secret"` 应输出为空。
5. **文档同步**
   - README 的安装方式、配置步骤、命令表、隐私说明如有变化，与代码同一版一起改。
   - 发版后在 `MAINTENANCE.md` 追加一条记录。

---

## 二、发布流程

1. `git pull`，确认工作区干净、`metadata.yaml` 版本号已递增。
2. 按第一节跑自检。
3. 提交并推送主分支。
4. 打标签：
   ```bash
   git tag -a vX.Y.Z -m "vX.Y.Z"
   git push origin vX.Y.Z
   ```
5. 用标签内容打安装包（**必须带顶层目录前缀**，面板“从文件安装”按这个结构识别插件）：
   ```bash
   git archive --format=zip --prefix=astrbot_plugin_huawei_health/ \
     -o astrbot_plugin_huawei_health-vX.Y.Z.zip vX.Y.Z
   ```
6. 发 GitHub Release：
   - Tag 选刚推的 `vX.Y.Z`，标题写 `vX.Y.Z`，说明里写本次变更与升级注意。
   - 把第 5 步的 zip 作为**附件**上传。GitHub 自动生成的 `Source code (zip)` 不是安装包，不要用它顶替。
7. 更新插件市场：打开 <https://cloud.astrbot.app/publish>，登录后二选一——连 GitHub 选中本仓库（自动解析 `metadata.yaml`），或直接上传同一个 zip；核对解析出的字段（name / author / version / repo）后提交。刚提交的版本要等 CI 跑完才会在市场生效。
8. 验收：
   - Release 页能下到 zip，文件名与版本号正确；
   - 面板“插件市场”搜得到，且版本号为新版；
   - 在干净环境分别用“从市场安装”和“从文件安装”各装一次，插件能正常加载；
   - 已装旧版的实例能在插件页检测到更新。

---

## 三、版本号约定

- 语义化 `X.Y.Z`，沿用 `v` 前缀（市场里带 `v` 前缀的记录是常态，如 `v1.2.6`）。
- 需要用户重新授权、改动存储结构或破坏配置兼容的改动 → 进大版本，并在 Release 说明里写清升级动作。
- 新增可选功能 → 小版本；修 bug 与文案 → 修订号。
- 数据库结构变更必须带迁移链与回滚自检（`selftest_migrations`），并在 Release 说明里提示会自动生成备份。

---

## 四、发版后

- 在 `MAINTENANCE.md` 追加一条：日期、版本、变更要点、验收结果。
- 若本次动了授权、注入或主动关怀，回头复核 README 的隐私说明与 `DEVELOPMENT.md` 的边界描述是否仍然成立。

---

## 五、常见驳回原因

- 包里缺 `metadata.yaml`，或包内版本号与市场记录不一致；
- `author` / `name` 与市场已有记录不一致（被当成另一个插件）；
- 安装包超过 16MB；
- 仓库里混进了数据库、令牌或本地状态文件。
