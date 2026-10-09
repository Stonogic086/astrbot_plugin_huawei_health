"""华为运动健康 AstrBot 插件 —— v1：插件骨架 + 协议层移植 + 授权后端路由。

本文件当前做三件事：
  1. 立起插件主类与配置读取骨架（token 字段按开发计划 3.7 用 password 字段做界面遮罩）；
  2. 提供插件内的诊断方法 ``diagnose()``：用已存 refresh token 走一次刷新 + 拉最近 3 天日汇总；
  3. 提供两个 web api 路由（本块只做后端，不做前端页面）。注册路径带插件名
     （``/<插件名>/auth/url``、``/<插件名>/auth/code``，宿主按完整 plugin_path 匹配）；
     前端页用相对子路径 ``auth/url`` / ``auth/code``，绝对前缀由前端桥补：
       - ``auth/url``  (GET)  生成完整授权链接，返回 {"url": "..."}；
       - ``auth/code`` (POST) 接收浏览器里复制到的 hms:// 回调串 → 解析 code → 换 token
                              → 写回插件配置 → 返回脱敏结果。
     两个 handler 都不依赖运行期框架上下文，可被脚本直接调用自检。

本块新增：services/ 同步服务（SyncService：最近 3 天 → 六类数据 → 存储层）与 main.py 的
后台同步循环接线（initialize 时 create_task，terminate 时取消；间隔取自 sync 分组）。

本块新增：查询命令层（commands/）。三个只读命令「健康活动 [N 天]」「健康睡眠 [日期]」
「健康训练 [N 天]」直接读存储层并渲染中文文本，装饰器在此接线，handler 不依赖运行期
框架上下文。commands/ 层自身只读（不写库、不调用任何 LLM）。

本块新增：隐私闸门（privacy_gate.py，集中一处、fail-closed，默认不允许外送）
与 180 天重登提醒（reminder.py，在既有后台同步循环里检查、去重状态落盘）。
隐私闸门由配置 ``privacy.allow_health_data_to_llm``（默认 false）控制；命令输出
仍直接回给使用者，不经任何 LLM 润色。

本块新增：对话「按需刷新」（services/ondemand_refresh.py）。查询命令读库前先判断
「距上次成功同步」是否超过按需刷新间隔（sync.natural_query_sync_minutes，默认 15
分钟）；到点先跑一轮 SyncService.run_once() 再读库，失败/超时一律兜住并退回读旧数据。
刷新由 main.py 在命令包装器里、读库之前完成，commands/ 层保持不变。

授权页面（前端）已交付：见 pages/huawei-auth（metadata.yaml 里 pages: [huawei-auth]）。

协议层位于 adapters/ 下，移植自 and7ey/huawei_health（MIT，见 LICENSE / NOTICE）。

本块新增：主动关怀（夜间 / 压力 / 起床 / 运动后四场景）。口径：夜间——深夜窗口 + 每夜一次
去重 + 所有者近期确有私聊活动 → 规则给候选（services/care_monitor.py）→ 发送前过模型布尔
闸门 → 措辞模型写一两句（features/proactive_care.py）→ 只投递给已绑定的 owner 私聊；
压力——当日日均分达配置档位，每天最多一条；起床——今日起床时间匹配当前日期 + 宽容度内，
按记录去重；运动后——本轮新发现的训练会话逐条触发，按 end_local 与发现时刻的差值分
「及时关怀」与「滞后致歉」两条分支。非夜间三场景不设模型闸门，措辞仍由模型按人格生成，
生成失败或 provider 不在白名单一律退化为固定模板文本。
出厂默认全部关闭；发送链路任一步失败一律静默不发。
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

try:  # 运行期由宿主框架提供 quart 的 request；脚本自检环境无框架时降级为 None。
    from quart import request as _quart_request
except ImportError:  # pragma: no cover - 仅在无框架的自检环境命中
    _quart_request = None

from .adapters import (
    HuaweiHealthAuthenticationError,
    HuaweiHealthCloudAdapter,
    HuaweiHealthFacade,
    HuaweiHealthNetworkError,
    HuaweiHealthParseError,
    Tokens,
    authorization_code_from,
    authorization_url,
    const,
    exchange_authorization_code,
)
from .commands import (
    COMMAND_ACTIVITY,
    COMMAND_SLEEP,
    COMMAND_TRAINING,
    handle_activity,
    handle_sleep,
    handle_training,
    plain_result,
)
from .features.llm_injection import build_part, decide, provider_allowed
from .features.proactive_care import ProactiveCare
from .privacy_gate import PrivacyGate, gate_from_config, mask_secret
from .reminder import (
    REMINDER_STATE_FILENAME,
    ReminderState,
    TokenReminder,
    is_friend_event,
    is_friend_umo,
)
from .services import (
    CARE_CHECK_INTERVAL_MINUTES,
    DEFAULT_REFRESH_TIMEOUT_SECONDS,
    SOFT_FRESH_FAILED_HINT,
    CareMonitor,
    OnDemandRefresher,
    SyncService,
    care_settings_from_config,
    interval_seconds_from_minutes,
    training_thresholds_from_config,
)
from .storage import HealthStore

PLUGIN_NAME = "astrbot_plugin_huawei_health"

# 脱敏只有一处实现：privacy_gate.mask_secret（本文件直接从那里 import）。
def _fmt_time(stamp: Any) -> str:
    """把 epoch 秒格式化成可读时间；缺失 / 非法值返回空串。"""
    try:
        value = float(stamp)
    except (TypeError, ValueError):
        return ""
    if value <= 0:
        return ""
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _safe_error_text(error: BaseException) -> str:
    """把异常压成「类型(+resultCode)」的脱敏文本。

    换 token 一类失败的异常原文可能夹带响应体（进而夹带 token 片段），所以凡是要写进
    日志或回给前端的错误文本，一律走这里：只保留异常类型与 resultCode。
    """
    text = type(error).__name__
    code = getattr(error, "code", None)
    if code is not None:
        text += f"(resultCode {code})"
    return text


# diagnose() 的失败分档：门面三类异常 → 类别名（与 adapters/errors 的三类一一对应）。
DIAGNOSE_FAILURE_KINDS: tuple[tuple[type[BaseException], str], ...] = (
    (HuaweiHealthAuthenticationError, "auth"),
    (HuaweiHealthNetworkError, "network"),
    (HuaweiHealthParseError, "parse"),
)


def diagnose_stage(phase: str, error: BaseException) -> str:
    """把诊断失败压成 ``<阶段>:<类别>``（门面三类之外的异常只给阶段名）。

    阶段是 refresh（刷新 token）/ pull（取数）；类别是 auth（认证失效，需重新登录）/
    network（网络类，可重试）/ parse（应答不可解析，该次取数降级）。上层据此分流：
    认证类走重登提醒，其余按次失败处理。
    """
    for error_type, kind in DIAGNOSE_FAILURE_KINDS:
        if isinstance(error, error_type):
            return f"{phase}:{kind}"
    return phase


# ── 授权失败 → 人话（原始 resultCode 仍随附，便于排查）────────────────────
AUTH_ERROR_HINTS: dict[int, str] = {
    20020001: "授权码已失效或已被使用过，请重新点『取授权链接』再走一遍",
    1002: "会话被手机端的华为运动健康抢走了，插件会自动重试；也可以用手机再同步一次",
    1004: "会话被手机端的华为运动健康抢走了，插件会自动重试；也可以用手机再同步一次",
    20020003: "凭据已失效，需要重新授权登录",
}


def _auth_result_code(error: BaseException) -> Any:
    """取授权失败的原始 resultCode。

    HuaweiApiError 自带 ``.code``；HuaweiAuthError 不带属性，只把码写进异常文本
    （``ensure_ok`` 的 ``f"{url}: {code} - {message}"``），所以回退到从文本里抠码：
    ``.../userAccessToken/obtain: 20020001 - code used twice`` → ``20020001``。
    """
    code = getattr(error, "code", None)
    if code is not None:
        return code
    head, sep, _ = str(error).partition(" - ")
    if not sep:
        return None
    candidate = head.rsplit(": ", 1)[-1].strip()
    return candidate if candidate.isdigit() else None


def humanize_auth_error(error: BaseException) -> str:
    """把授权失败异常翻成人话，并始终附上原始 resultCode（便于排查）。

    列出的码给固定中文释义；未列出的码保留原有「类型(resultCode)」文案——统一经
    ``_safe_error_text`` 出口，绝不回显响应体原文（可能夹带 token 片段）。
    """
    code = _auth_result_code(error)
    key: Any = code
    if isinstance(key, str):
        try:
            key = int(key)
        except ValueError:
            key = None
    hint = AUTH_ERROR_HINTS.get(key)
    if hint is not None:
        return f"{hint}（resultCode {code}）"
    text = _safe_error_text(error)
    if code is not None and str(code) not in text:
        text = f"{text}（resultCode {code}）"
    return text


def build_authorization_url() -> dict[str, Any]:
    """路由 1 的核心逻辑：生成完整授权链接。

    纯函数，不依赖插件实例与框架上下文，便于脚本直接调用自检。
    """
    return {"url": authorization_url()}


async def _read_quart_body() -> Any:
    """用宿主框架（quart）的全局 request 读 POST 请求体。

    宿主调用 handler 时只传路径参数、不把请求对象传进来，必须自己从 quart 取 body，
    否则 extract_pasted_text 取到空串（真机 bug 根因）。脚本自检环境没有加载框架
    （``_quart_request is None``）时返回 None，由原有兜底路径接管。
    优先读 JSON body（前端 apiPost 走 JSON），读不到再退回 form / args。
    """
    if _quart_request is None:
        return None
    try:
        payload = await _quart_request.get_json(silent=True)
    except Exception:
        payload = None
    if isinstance(payload, dict) and payload:
        return payload
    try:
        form = await _quart_request.form
    except Exception:
        form = None
    if form:
        return dict(form)
    try:
        args = _quart_request.args
    except Exception:
        args = None
    if args:
        return dict(args)
    return None


def extract_pasted_text(request: Any = None, **kwargs: Any) -> str:
    """从宿主请求 / 直接入参里取出「浏览器复制到的 hms:// 整行文字」。

    本函数不依赖任何框架上下文，便于脚本自检。兼容三种入参：
      * 直接传字符串（脚本自检走这条）；
      * 传 dict（取常见键 text / code / answer / redirect_url / url / content / reply）；
      * 传宿主请求对象（尝试 .json()/.get_json()/.json/.data/.body/.text）。

    未确认：宿主请求对象的精确形态未核实，这里只做最大兼容兜底。
    """
    if isinstance(request, str):
        return request.strip()
    if request is None:
        for key in ("text", "code", "answer", "redirect_url", "content", "reply"):
            value = kwargs.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(request, dict):
        for key in ("text", "code", "answer", "redirect_url", "url", "content", "reply"):
            value = request.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    # 宿主请求对象：逐个尝试常见承载字段。
    for attr in ("json", "get_json", "data", "body", "text"):
        value = getattr(request, attr, None)
        if value is None:
            continue
        if callable(value):
            try:
                value = value()
            except Exception:
                continue
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            for key in ("text", "code", "answer", "redirect_url", "url", "content"):
                inner = value.get(key)
                if isinstance(inner, str) and inner.strip():
                    return inner.strip()
    return ""


@register(PLUGIN_NAME, "huawei_health_adapter", "华为运动健康数据接入（v1 骨架）", "0.1.0")
class HuaweiHealthPlugin(Star):
    """持有华为账号凭证与协议层适配器；当前只提供连接与诊断能力。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        # 配置写回范式（开发计划约定）：self._config[key] = value → save_config()。
        # self.config 与 self._config 指向同一对象，两者读写等价。
        self._config = self.config
        try:
            self.data_dir = Path(StarTools.get_data_dir(self.name))
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except Exception as error:  # 数据目录不可用时不应阻断插件加载
            logger.warning(
                "[华为运动健康] 无法准备插件数据目录（%s）；本块不依赖它，继续加载",
                type(error).__name__,
            )
            self.data_dir = None

        # ── 凭证字段（只读取；写回见 persist_tokens，授权交互见 web_submit_code）──────
        self.uid = self._config_value("account", "uid", "")
        self.access_token = self._config_value("account", "access_token", "")
        self.refresh_token = self._config_value("account", "refresh_token", "")
        # 到期时间（epoch 秒）由授权成功 / 同步轮刷新时写回；前者用于到期前的重登提醒。
        # refresh 的到期时间兼容两个历史键名，故一次读多个键（见 _read_stamp）。
        self.refresh_token_expires_at = self._read_stamp(
            "refresh_token_expires_at", "refresh_expires_at"
        )
        self.access_token_expires_at = self._read_stamp("access_token_expires_at")
        self.session_host = self._config_value(
            "account", "session_host", const.SESSION_HOST_CN
        )
        self.data_host = self._config_value("account", "data_host", const.APP_HOST_CN)
        self.site_id = self._config_value("account", "site_id", "") or None

        # ── 同步节奏（自动同步与对话按需刷新均已接入，见 _start_sync_loop / _init_ondemand）──
        self.enable_auto_sync = self._config_value("sync", "enable_auto_sync", False)
        self.sync_interval_minutes = self._config_value(
            "sync", "sync_interval_minutes", 60
        )
        self.natural_query_sync_minutes = self._config_value(
            "sync", "natural_query_sync_minutes", 15
        )
        self.default_sync_days = self._config_value("sync", "default_sync_days", 3)
        # 训练碎片判据阈值（sync 分组）：命令展示 / LLM 摘要 / 入库时的 is_fragment 快照 /
        # 运动后关怀四处共用同一对已归一的值——与 care_settings_from_config 走同一函数、
        # 同一夹范围口径，避免配置写成 0 时只有关怀侧被夹到 1、其余几处仍拿 0 的口径分叉。
        self.training_min_duration_min, self.training_min_distance_m = (
            training_thresholds_from_config(self.config))

        self.adapter: HuaweiHealthCloudAdapter | None = None
        self.store: HealthStore | None = None
        self._sync_task: asyncio.Task | None = None
        # 对话「按需刷新」闸门：查询命令执行前判断是否到点先同步一轮（见 _init_ondemand）。
        self._ondemand: OnDemandRefresher | None = None

        # ── 主动关怀（四个场景；出厂默认全部关闭）──────────────────────────
        self.care_settings = care_settings_from_config(self.config)
        self._care: ProactiveCare | None = None
        self._care_task: asyncio.Task | None = None

        # ── 隐私闸门（集中一处，fail-closed：默认不允许外送）──────────────
        self.privacy_gate: PrivacyGate = gate_from_config(self.config)

        # ── 重登提醒（去重状态落盘为数据目录下的 JSON）────────────────────
        self._reminder_state: ReminderState | None = (
            ReminderState(Path(self.data_dir) / REMINDER_STATE_FILENAME)
            if self.data_dir
            else ReminderState(None)
        )
        self._reminder: TokenReminder | None = None

    # ── 配置读取 ─────────────────────────────────────────────────────────
    def _config_value(self, group: str, key: str, default: Any) -> Any:
        """读一个配置项，兼容「分组 schema」与「扁平 key」两种布局。

        小米插件的分组迁移（migrate_grouped_config）属于后续块；这里先同时接受
        config[group][key] 与 config[key]，避免骨架阶段读到 None。
        """
        grouped = self.config.get(group)
        if isinstance(grouped, dict) and key in grouped:
            return grouped.get(key)
        value = self.config.get(key)
        return default if value is None else value

    def _read_stamp(self, *keys: str) -> float:
        """按顺序读一组「epoch 秒」配置键，返回第一个合法正值；缺失/非法返回 0。"""
        for key in keys:
            raw = self._config_value("account", key, 0)
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
        return 0.0

    def build_tokens(self) -> Tokens:
        """把配置里的凭证装进协议层的 Tokens。

        两处易漏（都已接上）：
          * 带上两个到期时间——协议层靠 ``expires_at`` 判断 access token 是否还需轮换，
            靠 ``refresh_expires_at`` 判断 refresh token 是否已过期；
          * 挂上 ``on_change``——进程内没有别的持久化点，同步轮里刷新出的新 token 只有
            经它回写配置，才不会随下一轮新建的 Tokens 一起丢掉。
        """
        return Tokens(
            access_token=str(self.access_token or "") or None,
            refresh_token=str(self.refresh_token or "") or None,
            uid=str(self.uid) if self.uid else None,
            site_id=self.site_id,
            expires_at=self.access_token_expires_at,
            refresh_expires_at=self.refresh_token_expires_at,
            session_host=str(self.session_host or const.SESSION_HOST_CN),
            on_change=self._on_tokens_changed,
        )

    def _on_tokens_changed(self, tokens: Tokens) -> None:
        """协议层换到新 token 时的回写钩子（在工作线程里被调用）。

        刷新后立刻落盘：一旦云端真的轮换 refresh token，配置里必须跟着更新，否则下次
        重启就用旧值、直接要求重新登录。回写失败只告警——本轮数据仍然可用。
        """
        try:
            self.persist_tokens(tokens)
        except Exception as error:
            logger.warning(
                "[华为运动健康] token 回写配置失败（%s: %s），本轮数据仍可用",
                type(error).__name__,
                error,
            )

    # ── 生命周期 ─────────────────────────────────────────────────────────
    async def initialize(self) -> None:
        """初始化存储层（建库建表），注册授权后端路由，并启动后台同步循环。"""
        self._init_storage()
        self._init_ondemand()
        logger.info(
            "[华为运动健康] 插件已加载（v1）；凭证状态：refresh_token=%s",
            mask_secret(self.refresh_token) if self.refresh_token else "<未配置>",
        )
        self._register_web_apis()
        logger.info(
            "[华为运动健康] 隐私闸门：健康数据外送=%s（默认关闭=不允许外送）",
            "允许" if self.privacy_gate.is_open else "拒绝",
        )
        self._init_reminder()
        self._init_care()
        self._start_sync_loop()
        self._start_care_loop()

    def _init_storage(self) -> None:
        """建库建表（幂等）。库放插件持久化目录，配置了 storage.database_path 就用它。

        库路径 / 表结构缺失时自动创建；失败只告警不阻断加载。
        """
        try:
            configured = str(self._config_value("storage", "database_path", "") or "").strip()
            db_path = Path(configured) if configured else None
            if db_path is None and self.data_dir:
                db_path = Path(self.data_dir) / "health.db"
            self.store = HealthStore(db_path)
            path = self.store.initialize()
            logger.info("[华为运动健康] 存储层已初始化：%s", path)
        except Exception as error:
            self.store = None
            logger.warning(
                "[华为运动健康] 存储层初始化失败（%s: %s），本块继续加载",
                type(error).__name__,
                error,
            )

    # ── 对话按需刷新接线 ─────────────────────────────────────────────────
    def _init_ondemand(self) -> None:
        """构建「按需刷新」闸门：到点先把云端数据同步入库，再让命令读库。

        间隔取自 sync 分组 ``natural_query_sync_minutes``（默认 15 分钟，配置页可改）；
        与自动同步的 ``sync_interval_minutes``（默认 60 分钟）相互独立。失败只告警，
        不阻断加载——退化为「不按需刷新、只读库」。
        """
        try:
            interval_seconds = interval_seconds_from_minutes(
                self.natural_query_sync_minutes, 15
            )
            self._ondemand = OnDemandRefresher(
                self._run_sync_round,
                interval_seconds=interval_seconds,
                timeout_seconds=DEFAULT_REFRESH_TIMEOUT_SECONDS,
                logger=logger,
            )
            logger.info(
                "[华为运动健康] 按需刷新已就绪（间隔 %s 分钟，超时上限 %.0fs）",
                interval_seconds // 60,
                DEFAULT_REFRESH_TIMEOUT_SECONDS,
            )
        except Exception as error:
            self._ondemand = None
            logger.warning(
                "[华为运动健康] 按需刷新初始化失败（%s: %s），查询将只读既有库",
                type(error).__name__,
                error,
            )

    async def _refresh_hint(self) -> str:
        """跑一次按需刷新判断；返回需要额外提示使用者的一句轻描淡写文案（否则空串）。

        刷新失败的技术细节只进日志，绝不进入使用者看到的输出。
        """
        refresher = self._ondemand
        if refresher is None:
            return ""
        try:
            result = await refresher.refresh_if_due()
        except asyncio.CancelledError:
            raise
        except Exception as error:  # 兜底：刷新逻辑自身异常也不能影响查询
            logger.warning(
                "[华为运动健康] 按需刷新调用异常（%s: %s），直接读库",
                type(error).__name__,
                error,
            )
            return ""
        if result.get("attempted") and not result.get("ok"):
            return SOFT_FRESH_FAILED_HINT
        return ""

    # ── 隐私闸门 / 重登提醒接线 ──────────────────────────────────────────
    def _init_reminder(self) -> None:
        """构建重登提醒编排器（去重状态落盘、发送走真实私聊）。"""
        try:
            self._reminder = TokenReminder(
                self._reminder_state,
                get_expiry=lambda: self.refresh_token_expires_at,
                send=self._send_owner_message,
                logger=logger,
            )
        except Exception as error:
            self._reminder = None
            logger.warning(
                "[华为运动健康] 重登提醒初始化失败（%s: %s），继续加载",
                type(error).__name__,
                error,
            )

    async def _safe_check_reminder(self) -> dict[str, Any]:
        """跑一次重登提醒检查；任何异常都兜住，绝不影响同步主循环。

        ``run_once`` 已是 async（内部要 await 真实的私聊发送），故直接 await、不再丢线程池；
        这样 ReminderState 只在事件循环线程里读写，不需要额外加锁。
        """
        if self._reminder is None:
            return {"ok": False, "stage": "disabled"}
        try:
            return await self._reminder.run_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning(
                "[华为运动健康] 重登提醒检查异常（%s: %s），已忽略",
                type(error).__name__,
                error,
            )
            return {"ok": False, "stage": "error", "reason": type(error).__name__}

    # ── 主动关怀（夜间 / 压力 / 起床 / 运动后）──────────────────────────
    def _init_care(self) -> None:
        """构建主动关怀编排器（模型闸门 + 措辞 + 投递）。

        授权与白名单不固化在构造期，而是传取值回调（``_care_authorized`` /
        ``_care_allowlist``），在每轮判定 / 措辞时现取：用户改配置（关掉隐私开关或从
        白名单删 provider）无需重载插件即可立即生效。未授权或不在名单内一律不发
        （fail-closed）。
        """
        try:
            self._care = ProactiveCare(
                self.context,
                send=self._send_owner_message,
                authorized_getter=self._care_authorized,
                allowlist_getter=self._care_allowlist,
                logger=logger,
            )
            logger.info(
                "[华为运动健康] 主动关怀已就绪（总开关=%s，夜间=%s，压力=%s，起床=%s，"
                "运动后=%s，检查间隔 %s 分钟）",
                self.care_settings.master_enabled,
                self.care_settings.night_enabled,
                self.care_settings.stress_enabled,
                self.care_settings.wakeup_enabled,
                self.care_settings.workout_enabled,
                CARE_CHECK_INTERVAL_MINUTES,
            )
        except Exception as error:
            self._care = None
            logger.warning(
                "[华为运动健康] 主动关怀初始化失败（%s: %s），继续加载",
                type(error).__name__,
                error,
            )

    def _care_authorized(self) -> bool:
        """当轮隐私授权：现读隐私闸门是否打开（配置改动立即生效）。"""
        gate = self.privacy_gate
        return bool(gate is not None and gate.is_open)

    def _care_allowlist(self) -> Any:
        """当轮 provider 白名单：每轮重读配置，删掉 provider 后关怀不再向其注入数值。"""
        return self._config_value("privacy", "llm_provider_allowlist", [])

    def _start_care_loop(self) -> None:
        """起一个后台任务，按固定间隔（测试期 5 分钟）跑一轮关怀检查（四个场景共用）。"""
        try:
            self._care_task = asyncio.create_task(self._care_loop())
            logger.info(
                "[华为运动健康] 主动关怀循环已启动（间隔 %s 分钟，总开关=%s）",
                CARE_CHECK_INTERVAL_MINUTES,
                self.care_settings.master_enabled,
            )
        except Exception as error:
            self._care_task = None
            logger.warning(
                "[华为运动健康] 主动关怀循环启动失败（%s: %s），继续加载",
                type(error).__name__,
                error,
            )

    async def _stop_care_loop(self) -> None:
        """取消并等待关怀循环退出（幂等）。"""
        task = self._care_task
        self._care_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:
            logger.debug("[华为运动健康] 关怀循环退出时异常：%s", type(error).__name__)

    async def _care_loop(self) -> None:
        """按间隔 sleep 后跑一轮关怀；单轮失败不打断循环。"""
        while True:
            await asyncio.sleep(max(1, int(CARE_CHECK_INTERVAL_MINUTES)) * 60)
            if not self.care_settings.master_enabled:
                continue
            try:
                await self._run_care_round()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # 单轮异常必须兜住，循环继续
                logger.warning(
                    "[华为运动健康] 本轮主动关怀异常（%s: %s），循环继续",
                    type(error).__name__,
                    error,
                )

    def _owner_session(self) -> str:
        """已绑定的主人私聊会话标识（UMO）；未绑定返回空串（取不到目标就不发）。"""
        state = self._reminder_state
        if state is None:
            return ""
        try:
            state.load_if_needed()
        except Exception:
            pass
        return str(state.notify_umo or "")

    async def _run_care_round(self) -> dict[str, Any]:
        """跑一轮主动关怀检查（四个场景各自判定，互不影响）。异常都兜住，绝不报错到聊天里。"""
        if self._care is None or self.store is None:
            return {"ok": False, "stage": "disabled"}
        owner = self._owner_session()
        if not owner:
            logger.info("[华为运动健康] 主动关怀跳过：尚不知道该发给谁（先用一次查询命令即可）")
            return {"ok": True, "stage": "skip", "reason": "no_target"}
        monitor = CareMonitor(
            self.store, owner, self.care_settings, logger=logger)
        # 一轮 = 四个场景共用的一次 5 分钟检查：先开局本轮账本，各场景各自最多发 1 条。
        monitor.begin_round()
        runners = (
            ("night", self.care_settings.night_enabled, self._care.run_night),
            ("stress", self.care_settings.stress_enabled, self._care.run_stress),
            ("wakeup", self.care_settings.wakeup_enabled, self._care.run_wakeup),
            ("workout", self.care_settings.workout_enabled, self._care.run_workout),
        )
        scenarios: dict[str, Any] = {}
        for name, enabled, runner in runners:
            if not enabled:
                continue
            try:
                scenarios[name] = await runner(monitor, owner)
            except Exception as error:  # 单场景异常不能影响其余场景
                logger.warning(
                    "[华为运动健康] %s 关怀本轮异常（%s），其余场景继续",
                    name, type(error).__name__)
                scenarios[name] = {"ok": False, "stage": "error", "sent": False,
                                   "reason": type(error).__name__}
        if not scenarios:
            return {"ok": True, "stage": "skip", "reason": "no_scenario"}
        sent_scenarios = [name for name, result in scenarios.items()
                          if result.get("sent")]
        if sent_scenarios:
            logger.info("[华为运动健康] 已发送主动关怀：%s", "、".join(sent_scenarios))
        return {
            "ok": all(result.get("ok", True) for result in scenarios.values()),
            "stage": "done",
            "sent": bool(sent_scenarios),
            "reason": "",
            "scenarios": scenarios,
        }

    # ── 私聊活动记录（夜间关怀「还醒着」的唯一证据）──────────────────────
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def remember_owner_private_activity(self, event: AstrMessageEvent):
        """记住主人最近一次私聊活动；只记私聊、只记主人，其他来源一律忽略。

        健康数值与消息正文都不落库：这里只记「谁在什么时刻私聊过」。顺带把该私聊会话
        绑成重登提醒 / 关怀的发送目标（副作用与命令路径一致，都是记同一个 notify_umo）。
        """
        try:
            if not self._is_owner(event):
                return
            umo = getattr(event, "unified_msg_origin", None)
            if not umo:
                return
            structured = is_friend_event(event)
            if structured is False or (
                structured is None and not is_friend_umo(str(umo))
            ):
                return
            self._remember_notify_target(event)
            if self.store is not None:
                await asyncio.to_thread(self.store.touch_owner_activity, str(umo))
        except Exception as error:  # 记录活动失败绝不能影响正常聊天
            logger.debug(
                "[华为运动健康] 记录私聊活动失败（%s），已忽略", type(error).__name__
            )

    def _is_owner(self, event: Any) -> bool:
        """只放行框架管理员（= 使用者本人）。拿不到结构化字段一律拒绝（fail-closed）。

        判定顺序（都是 AstrMessageEvent 的公开接口，本机 astrbot 4.28.1 源码已核）：
          1. ``event.is_admin()``：框架在唤醒阶段会把 sender 命中全局配置 ``admins_id``
             的事件标成 ``role='admin'``（core/pipeline/waking_check/stage.py），
             所以 QQ 等平台上的主人同样能通过；
          2. 事件拿不到 ``is_admin``（例如自检桩 / 其他平台事件）→ 回落到全局配置
             ``admins_id`` 白名单，用 ``event.get_sender_id()`` 比对；
          3. 两条都拿不到、或比对不中 → 拒绝；判定过程中任何异常同样按拒绝处理。

        健康数据是私密数据：任何能给 Bot 发消息的人都不该读到所有者本人的心率 / 睡眠 /
        训练，所以这里宁可拒绝，绝不猜。
        """
        check = getattr(event, "is_admin", None)
        if callable(check):
            try:
                return check() is True
            except Exception:
                return False
        try:
            getter = getattr(self.context, "get_config", None)
            sender = str(getattr(event, "get_sender_id", lambda: "")() or "")
            admins = (getter() or {}).get("admins_id") if callable(getter) else None
        except Exception:
            return False
        return bool(sender) and sender in {str(x) for x in (admins or [])}

    def _remember_notify_target(self, event: Any) -> None:
        """记住使用者本人的私聊会话，供重登提醒直达。

        只认私聊：重登提醒定稿要求走私聊，群聊（或任何非私聊）来源一律直接丢弃、绝不写进
        状态文件——否则第一条命令来自群聊时，群 UMO 会被记成提醒目标、提醒发进群。
        已有目标（私聊）同样不覆盖，避免被后来的会话抢走。

        私聊判定优先用框架的结构化字段（``event.is_private_chat()`` 等，见
        reminder.is_friend_event），拿不到结构化字段时退回 UMO 字符串判定。
        读盘用 ``load_if_needed()``（只在内存尚未与磁盘对齐时读一次），不用 ``load()``：
        ``load()`` 会用磁盘内容整体替换内存里的去重标记，可能抹掉还没落盘的标记。
        """
        state = self._reminder_state
        if state is None:
            return
        umo = getattr(event, "unified_msg_origin", None)
        if not umo:
            return
        umo = str(umo)
        # 私聊判定优先走框架的结构化字段（event.is_private_chat() 等，见
        # reminder.is_friend_event）；拿不到结构化字段时退回 UMO 字符串判定。
        structured = is_friend_event(event)
        if structured is False or (structured is None and not is_friend_umo(umo)):
            logger.debug("[华为运动健康] 非私聊来源，不作为重登提醒目标，已忽略")
            return
        try:
            # 用 load_if_needed() 而不是 load()：run_once 里 mark() 与 save() 之间可能被本
            # 方法插进来，整体读盘会把尚未落盘的去重标记抹掉（极端时序下重复提醒）。
            state.load_if_needed()
            if state.notify_umo:
                return
            state.notify_umo = umo
            state.save()
            logger.info("[华为运动健康] 已记录重登提醒目标会话（私聊）")
        except Exception as error:
            logger.debug(
                "[华为运动健康] 记录提醒目标失败（%s），已忽略", type(error).__name__
            )

    async def _send_owner_message(self, text: str) -> bool:
        """给使用者本人发一条私聊。无目标 / 无发送通道 / 发送异常都返回 False（不抛）。"""
        state = self._reminder_state
        umo = state.notify_umo if state is not None else ""
        if not umo:
            logger.info("[华为运动健康] 重登提醒跳过：尚不知道该发给谁（先用一次查询命令即可）")
            return False
        send = getattr(self.context, "send_message", None)
        if not callable(send):
            logger.warning("[华为运动健康] 宿主未提供 send_message，无法发送重登提醒")
            return False
        try:
            await send(umo, self._build_chain(text))
            return True
        except Exception as error:
            logger.warning(
                "[华为运动健康] 重登提醒发送异常（%s），已忽略", type(error).__name__
            )
            return False

    @staticmethod
    def _build_chain(text: str) -> Any:
        """把文本封成宿主消息链；宿主模块不可用时退回纯字符串。"""
        try:
            from astrbot.core.message.message_event_result import MessageChain

            return MessageChain().message(text)
        except Exception:
            return text

    async def terminate(self) -> None:
        """取消后台同步循环与关怀循环，并释放适配器（协议层无持久连接）。"""
        await self._stop_sync_loop()
        await self._stop_care_loop()
        if self.adapter is not None:
            await self.adapter.close()
            self.adapter = None

    # ── 后台同步循环（最保守写法：create_task + sleep 间隔 + 停止时取消）────
    def _start_sync_loop(self) -> None:
        """初始化时起一个后台任务，按配置间隔周期跑一轮同步。"""
        try:
            self._sync_task = asyncio.create_task(self._sync_loop())
            logger.info(
                "[华为运动健康] 后台同步循环已启动（间隔 %s 分钟，自动同步开关=%s）",
                self.sync_interval_minutes,
                self.enable_auto_sync,
            )
        except Exception as error:  # 起不来也不能影响插件加载
            self._sync_task = None
            logger.warning(
                "[华为运动健康] 后台同步循环启动失败（%s: %s），继续加载",
                type(error).__name__,
                error,
            )

    async def _stop_sync_loop(self) -> None:
        """取消并等待后台同步任务退出（幂等）。"""
        task = self._sync_task
        self._sync_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as error:  # 退出时的异常只记，不上抛
            logger.debug("[华为运动健康] 同步循环退出时异常：%s", type(error).__name__)

    async def _sync_loop(self) -> None:
        """按配置间隔 sleep 后执行一轮同步；单轮失败不打断循环。"""
        try:
            interval_min = max(1, int(self.sync_interval_minutes or 60))
        except (TypeError, ValueError):
            interval_min = 60
        while True:
            # 重登提醒与同步共用本循环（不新建独立循环）；提醒失败不影响下方同步。
            await self._safe_check_reminder()
            await asyncio.sleep(interval_min * 60)
            if not self.enable_auto_sync:
                logger.debug("[华为运动健康] 自动同步未开启，跳过本轮")
                continue
            try:
                await self._run_sync_round()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # 单轮异常必须兜住，循环继续
                logger.warning(
                    "[华为运动健康] 本轮同步异常（%s: %s），循环继续",
                    type(error).__name__,
                    error,
                )

    async def _run_sync_round(self) -> dict[str, Any]:
        """跑一轮「最近 N 天 → 六类数据 → 存储层」，并打印本轮日志。"""
        if self.store is None:
            logger.info("[华为运动健康] 本轮同步跳过：存储层不可用")
            return {"ok": False, "skipped": [{"stage": "store", "reason": "存储层未初始化"}]}
        if not self.refresh_token:
            logger.info("[华为运动健康] 本轮同步跳过：未配置 refresh_token")
            return {
                "ok": False,
                "skipped": [{"stage": "config", "reason": "未配置 refresh_token"}],
            }
        adapter = HuaweiHealthCloudAdapter(
            self.build_tokens(),
            host=str(self.data_host or const.APP_HOST_CN),
            session_host=str(self.session_host or const.SESSION_HOST_CN),
        )
        service = SyncService(
            adapter, self.store, days=self.default_sync_days,
            training_min_duration_min=self.training_min_duration_min,
            training_min_distance_m=self.training_min_distance_m,
            logger=logger
        )
        summary = await service.run_once()
        # 成功同步后记下时间：自动同步与按需刷新共享这一时间，避免重复刷新。
        if summary.get("ok") and self._ondemand is not None:
            self._ondemand.mark_success()
        logger.info(
            "[华为运动健康] 同步 uid=%s 窗口=%s~%s 写入=%s 耗时=%.2fs",
            mask_secret(self.uid) if self.uid else "<未配置>",
            summary.get("window_start"),
            summary.get("window_end"),
            summary.get("written"),
            summary.get("elapsed_sec", 0.0),
        )
        if summary.get("skipped"):
            logger.warning("[华为运动健康] 同步跳过/失败项：%s", summary["skipped"])
        return summary

    # ── Web API：授权交互（本块只做后端两个路由，不做前端页面）──────────
    def _register_web_apis(self) -> None:
        """注册两个授权后端路由。宿主无 register_web_api 时只告警、不影响加载。"""
        register_api = getattr(self.context, "register_web_api", None)
        if not callable(register_api):
            logger.warning(
                "[华为运动健康] 当前宿主未提供 register_web_api，跳过注册授权路由"
            )
            return
        # 注册路径必须带插件名：宿主拿「注册 route 生成的 pattern」去 fullmatch
        # 前端传来的完整 plugin_path（形如 /<插件名>/<相对子路径>，见宿主
        # dashboard/api/plugins.py 的 _match_registered_web_api），不带插件名匹配不上。
        url_route = f"/{self.name}/auth/url"
        code_route = f"/{self.name}/auth/code"
        register_api(url_route, self.web_authorize_url, ["GET"], "生成华为健康授权链接")
        register_api(
            code_route, self.web_submit_code, ["POST"], "接收 hms:// 回调串并换取 token"
        )
        logger.info(
            "[华为运动健康] 已注册 web api 路由：%s (GET)、%s (POST)", url_route, code_route
        )

    async def web_authorize_url(self, request: Any = None, **kwargs: Any) -> dict[str, Any]:
        """路由 1：生成授权链接。返回 {"url": "<完整授权 URL>"}。"""
        return build_authorization_url()

    async def web_submit_code(self, request: Any = None, **kwargs: Any) -> dict[str, Any]:
        """路由 2：接收 hms:// 回调串 → 解析 code → 换 token → 写回配置 → 脱敏结果。

        成功返回：{"ok": True, "uid", "access_token", "refresh_token",
                   "access_token_expires_at", "refresh_token_expires_at"}（token 脱敏）。
        失败返回：{"ok": False, "stage", "error", ...}。
        """
        # 宿主调用 handler 时只传路径参数、不传请求对象，需自己从框架 request 读 body（真机 bug 根因）。
        body = request if request else await _read_quart_body()
        pasted = extract_pasted_text(body, **kwargs)
        if not pasted:
            return {"ok": False, "stage": "input", "error": "未收到回调串（hms:// 整行文字）"}
        code = authorization_code_from(pasted)
        if not code:
            return {
                "ok": False,
                "stage": "parse",
                "error": "未能从回调串中解析出 authorization code",
                "input_masked": mask_secret(pasted),
            }
        try:
            tokens = await asyncio.to_thread(self._exchange_code, code)
        except Exception as error:
            # 出口统一人话化 + 脱敏：先把 resultCode 翻成中文释义（原始码仍随附便于排查），
            # 异常原文可能夹带响应体（进而夹带 token 片段），绝不外泄。
            reason = humanize_auth_error(error)
            logger.warning("[华为运动健康] 换 token 失败：%s", reason)
            return {
                "ok": False,
                "stage": "exchange",
                "error": reason,
                "hint": "换 token 未成功：网络问题可稍后重试；若授权码已过期或已被使用，"
                        "请重新生成授权链接再走一次。",
                "code_masked": mask_secret(code),
            }
        return self.persist_tokens(tokens)

    def _exchange_code(self, code: str) -> Tokens:
        """用 authorization code 换 token 对（同步、会联网；独立方法便于自检替换）。"""
        return exchange_authorization_code(
            code,
            session_host=str(self.session_host or const.SESSION_HOST_CN),
            uid=str(self.uid) if self.uid else None,
        )

    def persist_tokens(self, tokens: Tokens) -> dict[str, Any]:
        """把一枚 token 对写回插件配置（account 分组），返回脱敏摘要。

        同时更新内存里的 self.uid / access_token / refresh_token / session_host / site_id /
        两个到期时间——落盘与回写内存必须成对，否则下一次 build_tokens() 又会拿旧的
        access_token 到期时间（首次为 0）当本轮值，needs_rotation() 会恒判需轮换。

        回执里的 uid 与两个 token 都是脱敏值（uid 同样是账号标识，不外回原文）；
        ``persisted`` 说明是否真的落盘（配置对象没有 save_config 或写盘失败时为 False）。
        """
        self.access_token = tokens.access_token or ""
        self.refresh_token = tokens.refresh_token or ""
        if tokens.uid:
            self.uid = str(tokens.uid)
        if tokens.session_host:
            self.session_host = tokens.session_host
        updates: dict[str, Any] = {
            "uid": self.uid,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
        }
        # siteId 是取数请求体的一部分，而它只在这次登录响应里出现；不写回就等于每次都丢。
        if tokens.site_id is not None:
            self.site_id = tokens.site_id
            updates["site_id"] = tokens.site_id
        # 顺手把到期时间落盘 + 回写内存，供 180 天重登提醒与下一轮 build_tokens 的轮换
        # 判断使用（未知=0 时不覆盖已有值）。只落盘不回写内存是半成品：进程内每次
        # build_tokens() 新建的 Tokens.expires_at 都会是旧值（首次为 0），
        # needs_rotation() 于是恒判需轮换。
        if tokens.expires_at and tokens.expires_at > 0:
            updates["access_token_expires_at"] = int(tokens.expires_at)
            self.access_token_expires_at = float(tokens.expires_at)
        if tokens.refresh_expires_at and tokens.refresh_expires_at > 0:
            updates["refresh_token_expires_at"] = int(tokens.refresh_expires_at)
            self.refresh_token_expires_at = float(tokens.refresh_expires_at)
        persisted = self._write_config(updates)
        if not persisted:
            logger.warning(
                "[华为运动健康] token 未落盘（配置对象不可写或 save_config 失败），"
                "本次写入只在内存中生效，重启后可能仍用旧凭据"
            )
        return {
            "ok": True,
            "stage": "done",
            "persisted": persisted,
            "uid": mask_secret(self.uid) if self.uid else "",
            "access_token": mask_secret(self.access_token),
            "refresh_token": mask_secret(self.refresh_token),
            "access_token_expires_at": _fmt_time(tokens.expires_at),
            "refresh_token_expires_at": _fmt_time(tokens.refresh_expires_at),
        }

    def _write_config(self, updates: dict[str, Any]) -> bool:
        """把字段原地写进 self._config["account"]，再落盘（save_config）。

        返回是否真的落盘：配置对象拿不到 / 不是 dict / 没有可调用的 save_config / 写盘抛
        异常，都返回 False（调用方据此告警，不再静默假装成功）。
        """
        target = getattr(self, "_config", None)
        if not isinstance(target, dict):
            target = self.config if isinstance(self.config, dict) else None
        if target is None:
            return False
        account = target.get("account")
        if not isinstance(account, dict):
            account = {}
            target["account"] = account
        for key, value in updates.items():
            account[key] = value
        save = getattr(target, "save_config", None)
        if not callable(save):
            return False
        try:
            save()
        except Exception as error:
            logger.warning(
                "[华为运动健康] 配置落盘失败（%s），本次写入只在内存中生效",
                type(error).__name__,
            )
            return False
        return True

    # ── 查询命令层（身份门 → 读库前按需刷新；不写库、不调用 LLM）──────────
    @filter.command(COMMAND_ACTIVITY)
    async def cmd_health_activity(self, event: AstrMessageEvent):
        """查最近 N 天活动汇总（步数/距离/卡路里/活动时长），默认 3 天。

        身份门：只放行使用者本人（框架管理员，见 ``_is_owner``）。未通过即静默结束，
        不回任何文案（避免用「被拒绝」当探测信号），也不记录提醒目标——门外记录会让
        最先私聊的那个人被写成重登提醒的收件人。
        """
        if not self._is_owner(event):
            logger.debug("[华为运动健康] 非管理员调用 %s，已拒绝", COMMAND_ACTIVITY)
            return
        self._remember_notify_target(event)
        hint = await self._refresh_hint()
        if hint:
            yield plain_result(event, hint)
        async for result in handle_activity(self.store, event):
            yield result

    @filter.command(COMMAND_SLEEP)
    async def cmd_health_sleep(self, event: AstrMessageEvent):
        """查某天睡眠汇总（时长/评分/效率/HRV/SpO2）与心率日值，默认今天。

        身份门与拒绝口径同 ``cmd_health_activity``。
        """
        if not self._is_owner(event):
            logger.debug("[华为运动健康] 非管理员调用 %s，已拒绝", COMMAND_SLEEP)
            return
        self._remember_notify_target(event)
        hint = await self._refresh_hint()
        if hint:
            yield plain_result(event, hint)
        async for result in handle_sleep(self.store, event):
            yield result

    @filter.command(COMMAND_TRAINING)
    async def cmd_health_training(self, event: AstrMessageEvent):
        """查最近 N 天训练会话列表，默认 7 天。

        身份门与拒绝口径同 ``cmd_health_activity``。
        """
        if not self._is_owner(event):
            logger.debug("[华为运动健康] 非管理员调用 %s，已拒绝", COMMAND_TRAINING)
            return
        self._remember_notify_target(event)
        hint = await self._refresh_hint()
        if hint:
            yield plain_result(event, hint)
        async for result in handle_training(
            self.store, event,
            min_duration_min=self.training_min_duration_min,
            min_distance_m=self.training_min_distance_m,
        ):
            yield result

    # ── 健康数据进 LLM 上下文（唯一注入点；多重前置门 + fail-closed）──────
    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """把本轮健康摘要注入 LLM 上下文；任一前置门不满足即静默不注入。

        前置门顺序：授权（隐私闸门打开）→ 本轮实际 provider 在白名单内 → 摘要非空 →
        追加带 mark_as_temp 的临时 part（不进长期会话历史）。框架能力探测不在这里做：
        ``features.llm_injection.build_part`` 自己探测 TextPart / mark_as_temp，任一
        拿不到就返回 None，本方法据此整轮不注入（fail-closed）。全程 try/except：任何一步
        失败都退化成「不带健康数据的普通回答」，不报错、不中断聊天。
        """
        try:
            gate = self.privacy_gate
            authorized = bool(gate is not None and gate.is_open)
            allowlist = self._config_value("privacy", "llm_provider_allowlist", [])
            # 框架的降级切换发生在钩子之后：本轮实际 provider 之后可能被换成备用模型，
            # 而钩子只看得到主 provider。故这里 fail-closed——备用名单里只要有一个不在
            # 白名单内，本轮就直接不注入（否则主 provider 命中、降级到名单外模型时会把
            # 健康数据外送）。备用名单为空 = 无降级风险，行为不变（白名单为空本就不注入）。
            for fallback_id in self._fallback_provider_ids():
                if not provider_allowed(fallback_id, allowlist):
                    return
            decision = decide(
                self.store,
                await self._current_provider_id(event),
                allowlist,
                authorized=authorized,
                training_min_duration_min=self.training_min_duration_min,
                training_min_distance_m=self.training_min_distance_m,
            )
            if not decision.inject:
                return
            parts = getattr(req, "extra_user_content_parts", None)
            if not hasattr(parts, "append"):
                return
            part = build_part(decision.text)
            if part is None:
                return
            parts.append(part)
            # 只记「已注入 + 摘要字符数」，不含任何健康数值，便于运维确认注入是否生效。
            logger.info(
                "[华为运动健康] 本轮已注入健康摘要（%d 字）",
                len(decision.text),
            )
        except Exception as error:
            logger.warning(
                "[华为运动健康] 健康摘要注入失败（%s），本轮按普通回答处理",
                type(error).__name__,
            )

    async def _current_provider_id(self, event: Any) -> str:
        """取本轮实际使用的 provider id：先读事件附带的 selected_provider，再回落宿主 API。

        回落只在事件没带 provider 时发生；两者都拿不到就返回空串（＝不在白名单，不注入）。
        """
        selected = ""
        getter = getattr(event, "get_extra", None)
        if callable(getter):
            try:
                selected = str(getter("selected_provider") or "")
            except Exception:
                selected = ""
        if selected:
            return selected
        umo = getattr(event, "unified_msg_origin", None)
        resolver = getattr(self.context, "get_current_chat_provider_id", None)
        if not umo or not callable(resolver):
            return ""
        try:
            return str(await resolver(umo) or "")
        except Exception:
            return ""

    def _fallback_provider_ids(self) -> list[Any]:
        """读全局配置里本地 Agent 的备用模型名单。

        路径 ``agent_runner.config.model.fallback_provider_ids``（框架在
        ``core/astr_agent_tool_exec`` / ``core/cron/manager`` 读的同一处）。读不到配置 /
        字段不是 list 一律当作「无备用模型」（返回空列表）——不引入额外风险，行为与从前一致。
        """
        getter = getattr(self.context, "get_config", None)
        if not callable(getter):
            return []
        try:
            config = getter() or {}
        except Exception:
            return []
        if not isinstance(config, dict):
            return []
        runner = config.get("agent_runner")
        section = runner.get("config") if isinstance(runner, dict) else None
        model = section.get("model") if isinstance(section, dict) else None
        ids = model.get("fallback_provider_ids") if isinstance(model, dict) else None
        if not isinstance(ids, (list, tuple)):
            return []
        return list(ids)

    # ── 诊断（最小自检）──────────────────────────────────────────────────
    async def diagnose(self, days: int = 3) -> dict[str, Any]:
        """刷新一次 token 并经取数门面拉最近 N 天日汇总，返回结构化结果（不打印 token）。

        取数与定时同步同一口径：走 ``HuaweiHealthFacade``（先 connect() 刷新 token、
        用完 close() 清掉窗口缓存），云端该类一个有效读数都没有时门面返回空 list
        —— 那是「无数据」，不是失败（与 services/sync_service.py 的判定一致）。

        返回键：ok / stage / message / rows / host / session_host / uid / token_masked
        （uid 与 token 都是脱敏值，绝不外回原文）。失败时 stage 形如 ``<阶段>:<类别>``
        （非门面三类异常只给阶段名），类别见 ``DIAGNOSE_FAILURE_KINDS``，供上层日志与
        重登逻辑分流；结构仍是原样的单字符串字段。
        """
        if not self.refresh_token:
            return {"ok": False, "stage": "config", "message": "未配置 refresh_token"}

        tokens = self.build_tokens()
        uid_masked = mask_secret(tokens.uid) if tokens.uid else ""
        adapter = HuaweiHealthCloudAdapter(
            tokens,
            host=str(self.data_host or const.APP_HOST_CN),
            session_host=str(self.session_host or const.SESSION_HOST_CN),
        )
        facade = HuaweiHealthFacade(adapter)
        end = date.today()
        start = end - timedelta(days=max(1, int(days)) - 1)
        try:
            await facade.connect()  # 刷新 access token：失败即抛，与原来 adapter.refresh() 同语义
        except Exception as error:
            return {
                "ok": False,
                "stage": diagnose_stage("refresh", error),
                "message": f"{type(error).__name__}: {error}",
                "host": adapter.host,
                "session_host": adapter.tokens.session_host,
                "uid": uid_masked,
                "token_masked": mask_secret(adapter.tokens.access_token),
            }
        try:
            rows = await facade.iter_daily_activity(start, end)
        except Exception as error:
            return {
                "ok": False,
                "stage": diagnose_stage("pull", error),
                "message": f"{type(error).__name__}: {error}",
                "rows": None,
                "host": adapter.host,
                "session_host": adapter.tokens.session_host,
                "uid": uid_masked,
                "token_masked": mask_secret(adapter.tokens.access_token),
            }
        finally:
            await facade.close()
        return {
            "ok": True,
            "stage": "done",
            "message": "刷新成功并取到数据" if rows else "刷新成功，但该时间段无数据行",
            "rows": len(rows),
            "host": adapter.host,
            "session_host": adapter.tokens.session_host,
            "uid": uid_masked,
            "token_masked": mask_secret(adapter.tokens.access_token),
        }
