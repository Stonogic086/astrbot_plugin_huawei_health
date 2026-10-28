# LLM 主动工具（本插件新增）

## 为什么用工具，而不是每轮注入

每轮注入会把最近 3 天摘要无条件附到每次请求上：模型没问健康时也会收到数据，token 成本固定，数据外送频率也更高。主动工具把“要不要取数、取哪一段”交给模型在当前对话里决定：

- 用户问“昨晚睡得怎么样” → `health_sleep`
- 用户问“这周睡眠有没有变好” → `health_sleep_trend`
- 用户问“最近心率怎么样” → `health_heart`
- 用户问“我最近身体怎么样” → `health_overview`
- 用户问“数据同步了吗” → `health_status`

工具结果是本轮工具调用的返回值，插件不主动发消息，也不额外写入长期记忆；宿主是否保留工具消息取决于 AstrBot 的会话策略。插件返回的是整理后的中文摘要，不是原始数据库行。

## 工具清单

| 工具名 | 参数 | 返回 |
| --- | --- | --- |
| `health_overview` | `days`（1-30，默认 3） | 活动/训练按窗口，睡眠/压力/血氧按当天 |
| `health_sleep` | `day`（`YYYY-MM-DD`，默认今天） | 单日睡眠 + 心率日值 |
| `health_sleep_trend` | `days`（1-30，默认 7） | 逐日睡眠 + 平均值 |
| `health_heart` | `days`（1-30，默认 7） | 逐日心率 + 静息均值 |
| `health_activity` | `days`（1-30，默认 3） | 步数/距离/卡路里/活动时长 |
| `health_training` | `days`（1-30，默认 7） | 训练会话列表 |
| `health_status` | 无 | 凭证、同步时间、各表覆盖范围（不含健康数值） |

所有天数参数都会夹到 1..30，避免模型一次拉取过多历史。

## 四道门

工具结果会进入 LLM 上下文，因此和旧的注入路径使用同一套严格前置门：

1. **所有者**：`event.is_admin()`（或全局 `admins_id` 回落）必须为真；
2. **私聊**：`is_private_chat()` / 消息类型 / UMO 字符串判定必须为私聊；
3. **隐私闸门**：`privacy.allow_health_data_to_llm` 必须显式打开；
4. **provider 白名单**：本轮 `selected_provider` 必须在 `privacy.llm_provider_allowlist` 内；如果全局配置了备用模型，任一备用模型不在名单内也拒绝（与注入路径的降级保护一致）。

任一道门不通过都返回一句不含健康数值的拒绝说明；非所有者只看到通用话术。查询命令与旧的每轮注入路径也同样先过“所有者 + 私聊”门。

## 按需刷新

每个查询工具在读库前复用 `services/ondemand_refresh.py` 的 `refresh_if_due()`：

- 距上次成功同步超过 `sync.natural_query_sync_minutes`（默认 15 分钟）才触发一轮同步；
- 刷新超时/失败只返回一句“先显示缓存”的轻提示，不阻断工具；
- `health_status` 不触发刷新，避免为了看状态而等待网络。

## 配置

```json
{
  "privacy": {
    "allow_health_data_to_llm": true,
    "inject_health_context": false,
    "llm_provider_allowlist": ["your-provider-id"]
  }
}
```

- `allow_health_data_to_llm`：总开关，默认关闭；
- `inject_health_context`：旧的每轮注入模式，默认关闭；工具模式不需要打开；
- `llm_provider_allowlist`：注入与工具共用，留空即不允许任何外送。

## 自检

```bash
python3 scripts/import_check.py
python3 scripts/selftest_commands.py
python3 scripts/selftest_llm_tools.py
```

`selftest_llm_tools.py` 使用最小 AstrBot 桩和临时 SQLite 库，覆盖七个工具的正常路径、四道门、注入开关、天数夹取和日期解析；不联网、不接触真实健康库。

## 已知限制

- 数据来自华为云端已同步的日汇总，不是实时心率/睡眠监护；
- 工具只在主人私聊中可用，群聊不返回健康数据；
- 当前环境没有安装 AstrBot，也没有华为账号凭证，因此未做真实宿主/真实云端的端到端测试；
- 模型是否调用工具取决于其 function calling / Agent 能力；不支持工具时可退回命令或旧注入模式。
