# -*- coding: utf-8 -*-
"""TG-Forwarder v3 Web 层。

FastAPI 应用工厂，全量对齐 v2 web_server.py 的 API 路由（路径/请求/响应结构
不改名，旧 Web UI index.html 零功能损失），并在此基础上加固：

- P5/R9 鉴权加固：HTTPBasic 密码支持 ``sha256$<hex>`` 哈希形态存储（配置文件
  中不再需要明文），比对一律走常量时间比较。
- Session 鉴权：登录成功后签发 ``secrets.token_urlsafe(32)`` session token
  （内存存储、默认 12h 过期），后续请求可带 X-Session-Token 头免 Basic 重发；
  旧 UI 的 Basic 认证仍完全可用。
- R4 落表即生效：写操作先改内存快照，再 await 仓储落表成功后才返回响应
  （修复 v2 fire-and-forget 漤写风险）。
- R8 原子化：写入成功后调 update_settings 回调，触发外部 RuntimeConfig
  重建 + forwarder.update_snapshot。
"""
import asyncio
import hashlib
import hmac
import os
import secrets
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel

from loguru import logger

# v3 单一配置源模型（config.py 已就绪；原 _local_models fallback 双份模型
# 系并行交付期临时产物，已删，消除 import 顺序依赖与漂移）
from tg_forwarder.config import (
    AdFilterConfig,
    ContentFilterConfig,
    RulesDatabase,
    SourceConfig,
    SystemSettings,
    TargetDistributionRule,
    WhitelistConfig,
)
from tg_forwarder.version import resolve_version

# 版本号单一事实源（启动期解析一次，见 tg_forwarder/version.py）：
# 优先镜像构建期注入的 TG_FORWARDER_VERSION，其次 git tag，最后 dev 占位。
# FastAPI 元信息与 /api/version、面板展示全部取此处——禁止再写第二份。
APP_VERSION = resolve_version()

# ---------------------------------------------------------------------------
# 鉴权（P5/R9 加固）
# ---------------------------------------------------------------------------

# Session 内存存储: token -> 过期时间戳（进程级缓存，重启即失效，单实例部署足够）
_SESSION_TTL_SECONDS = 12 * 3600  # 默认 12h
_sessions: Dict[str, float] = {}

security = HTTPBasic(auto_error=False)

# 应用级共享状态：由 create_app 注入，路由闭包读取。
# rules_db 为内存快照（Source of Truth for Runtime），db_lock 串行化写操作。
app_state: Dict[str, Any] = {
    "rules_db": None,  # RulesDatabase
    "get_snapshot": None,  # Callable[[], RuntimeConfig]
    "web_password": None,  # Optional[str]，无快照时的静态密码兜底
    "update_settings": None,  # Callable[[], Awaitable[None]] 触发 RuntimeConfig 重建
    "notify_bot": None,  # Callable[[str], Awaitable[None]]
    "account_manager": None,
    "forwarder": None,
}

db_lock = asyncio.Lock()


class ReorderRequest(BaseModel):
    """v2 兼容：/api/rules/reorder 请求体。"""

    names: List[str]


# ---------------------------------------------------------------------------
# 密码与 session 工具
# ---------------------------------------------------------------------------

def verify_password(candidate: str, stored: str) -> bool:
    """密码比对：支持 ``sha256$<hex>`` 哈希形态与明文，一律常量时间比较。

    - stored 形如 ``sha256$<hex>``: 与 sha256(candidate) 的 hex 比对
    - 否则视为明文直接比对
    """
    if not stored:
        return False
    if stored.startswith("sha256$"):
        expected = stored.split("$", 1)[1].strip().lower()
        actual = hashlib.sha256(candidate.encode("utf-8")).hexdigest()
        return hmac.compare_digest(actual, expected)
    return hmac.compare_digest(candidate, stored)


def _issue_session_token() -> str:
    """签发新 session token 并写入内存表（含过期时间），顺手清理过期 token。"""
    now = time.time()
    for t, exp in list(_sessions.items()):
        if exp <= now:
            _sessions.pop(t, None)

    token = secrets.token_urlsafe(32)
    _sessions[token] = now + _SESSION_TTL_SECONDS
    return token


def _session_token_valid(token: str) -> bool:
    """校验 session token 是否存在且未过期，过期即移除。"""
    exp = _sessions.get(token)
    if exp is None:
        return False
    if exp <= time.time():
        _sessions.pop(token, None)
        return False
    return True


def _get_web_password() -> Optional[str]:
    """从当前运行时快照读取 Web UI 密码（热重载后自动取新密码），异常时兜底静态密码。"""
    getter = app_state.get("get_snapshot")
    if getter is not None:
        try:
            cfg = getter() if not asyncio.iscoroutinefunction(getter) else None
            if cfg is not None:
                web_ui = getattr(cfg, "web_ui", None)
                if web_ui is not None:
                    pwd = getattr(web_ui, "password", None)
                    if pwd:
                        return pwd
        except Exception as e:
            logger.warning(f"读取运行时 Web 密码失败，退回静态密码: {e}")
    return app_state.get("web_password")


def _check_auth(
    credentials: Optional[HTTPBasicCredentials],
    request: Request,
) -> str:
    """统一鉴权入口：X-Session-Token 优先，Basic（明文/sha256）兜底。

    返回用户名（Basic 时）或 "session"（token 时），失败抛 401。
    """
    # 1. Session token（免 Basic 重发）
    session_token = request.headers.get("X-Session-Token")
    if session_token and _session_token_valid(session_token):
        return "session"

    # 2. Basic 认证（旧 UI 兼容）
    if credentials is not None:
        stored = _get_web_password()
        if stored and verify_password(credentials.password, stored):
            return credentials.username or "admin"

    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )


# ---------------------------------------------------------------------------
# 应用工厂
# ---------------------------------------------------------------------------

def create_app(
    db: Any,
    config_repo: Any,
    source_repo: Any,
    rule_repo: Any,
    get_snapshot: Callable[[], Any],
    update_settings: Callable[[], Awaitable[None]],
    account_manager: Any = None,
    forwarder: Any = None,
    static_index_path: Optional[str] = None,
    bot_notifier: Optional[Callable[[str], Awaitable[None]]] = None,
) -> FastAPI:
    """构建 FastAPI 应用。

    参数:
        db: tg_forwarder.storage.db.Database 实例（get_db_stats 等）
        config_repo/source_repo/rule_repo: 仓储（get/save/remove，dict 进出）
        get_snapshot: 返回当前 RuntimeConfig 快照（读 web_ui.password 等）
        update_settings: 写操作成功后的回调（触发 RuntimeConfig 重建 + forwarder 热更新）
        account_manager/forwarder: 可选，用于 /health 与 /api/status（R1/R9）
        static_index_path: index.html 绝对路径（默认取包内 static/index.html）
        bot_notifier: Bot 通知回调（T1：Web 写配置成功后推送 admin；缺省 None 不推送）
    """
    # 内存规则库快照：初始为空对象，真实数据由 main 启动时经仓储加载
    rules_db = RulesDatabase()
    app_state["rules_db"] = rules_db
    app_state["get_snapshot"] = get_snapshot
    app_state["update_settings"] = update_settings
    app_state["account_manager"] = account_manager
    app_state["forwarder"] = forwarder
    # 重置注入型状态，避免多实例/测试间残留；notify_bot 由 bot_notifier 注入（T1）
    app_state["notify_bot"] = bot_notifier
    app_state["web_password"] = None

    if static_index_path is None:
        static_index_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "static", "index.html"
        )

    app = FastAPI(
        title="TG Forwarder Web UI",
        description="TG 终极转发器管理面板",
        version=APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    async def require_auth(
        request: Request,
        credentials: Optional[HTTPBasicCredentials] = Depends(security),
    ) -> str:
        """鉴权依赖：security(auto_error=False) 无 Basic 头时凭据为 None，统一校验。"""
        return _check_auth(credentials, request)

    async def notify_bot(message: str):
        """Bot 通知（外部注入，v2 set_bot_notifier 等价物）。"""
        notifier = app_state.get("notify_bot")
        if notifier:
            try:
                await notifier(message)
            except Exception as e:
                logger.warning(f"Bot 通知失败（忽略）: {e}")

    async def _reload_runtime() -> None:
        """R8 热重载：触发 update_settings 回调（重建 RuntimeConfig + 快照替换）。

        M3：写操作落表成功后调用，实现「Web 改配置 → 自动热重载 → 无需重启容器」。
        与 /api/reload 一致，热重载失败向上抛：数据已持久化，响应如实标注
        「已保存但热重载失败需手动重载」，不再静默吞错（Codex review major）。
        """
        cb = app_state.get("update_settings")
        if cb is None:
            return
        try:
            await cb()
        except Exception as e:
            logger.error(f"热重载失败（配置已保存，需手动重载）: {e}")
            raise HTTPException(
                status_code=500,
                detail=f"配置已保存，但热重载失败: {e}（请手动重载）",
            )

    # --- 新增：登录签发 session token（P5/R9）---

    @app.post("/api/login")
    async def login(
        request: Request,
        credentials: Optional[HTTPBasicCredentials] = Depends(security),
    ):
        """Basic 登录成功后签发 session token，后续请求带 X-Session-Token 免 Basic。"""
        stored = _get_web_password()
        if (
            credentials is None
            or not stored
            or not verify_password(credentials.password, stored)
        ):
            raise HTTPException(
                status_code=401,
                detail="Unauthorized",
                headers={"WWW-Authenticate": "Basic"},
            )
        token = _issue_session_token()
        return {
            "status": "success",
            "session_token": token,
            "expires_in": _SESSION_TTL_SECONDS,
        }

    # --- 文档路由（v2 对齐，需鉴权）---

    @app.get("/docs", include_in_schema=False)
    async def get_swagger_documentation(username: str = Depends(require_auth)):
        return get_swagger_ui_html(openapi_url="/openapi.json", title="API 文档")

    @app.get("/redoc", include_in_schema=False)
    async def get_redoc_documentation(username: str = Depends(require_auth)):
        return get_redoc_html(openapi_url="/openapi.json", title="API 文档")

    @app.get("/openapi.json", include_in_schema=False)
    async def get_open_api_endpoint(username: str = Depends(require_auth)):
        return get_openapi(title=app.title, version=app.version, routes=app.routes)

    # --- 业务 API（写操作：内存快照 + 仓储落表，await 成功后才返回，R4）---

    @app.get("/api/stats")
    async def get_stats(username: str = Depends(require_auth)):
        """合并 db_stats + 规则计数 + runtime（uptime/bot_status/账号数），对齐 v2 字段。"""
        try:
            db_stats: Dict[str, Any] = {}
            if db is not None:
                try:
                    db_stats = await db.get_db_stats() or {}
                except Exception as e:
                    logger.warning(f"读取数据库统计失败: {e}")

            runtime_stats: Dict[str, Any] = {}
            fwd = app_state.get("forwarder")
            if fwd is not None:
                try:
                    res = fwd.all_status()
                    if asyncio.iscoroutine(res):
                        res = await res
                    if isinstance(res, dict):
                        runtime_stats = res
                except Exception as e:
                    logger.warning(f"读取 runtime 统计失败: {e}")

            async with db_lock:
                bl = rules_db.ad_filter
                bl_count = (
                    len(bl.keywords_substring or [])
                    + len(bl.keywords_word or [])
                    + len(bl.file_name_keywords or [])
                    + len(bl.patterns or [])
                )
                cf_count = (
                    len(rules_db.content_filter.meaningless_words)
                    if rules_db.content_filter
                    else 0
                )

                rule_stats = {
                    "sources": len(rules_db.sources),
                    "distribution_rules": len(rules_db.distribution_rules),
                    "whitelist_count": len(rules_db.whitelist.keywords or []),
                    "blacklist_count": bl_count,
                    "content_filter_count": cf_count,
                    "replacements_count": len(rules_db.replacements or {}),
                }
            return {**db_stats, **rule_stats, **runtime_stats}
        except Exception as e:
            logger.error(f"/api/stats 异常: {e}")
            return {}

    @app.get("/api/settings")
    async def get_settings(username: str = Depends(require_auth)):
        return rules_db.settings

    @app.get("/api/version")
    async def get_version(username: str = Depends(require_auth)):
        """面板当前版本号（系统设置页展示）。

        唯一来源 = APP_VERSION（镜像注入 / git tag，见 tg_forwarder/version.py），
        前端不得硬编码第二份。
        """
        return {"version": APP_VERSION}

    @app.post("/api/settings/update")
    async def update_settings_endpoint(settings: SystemSettings, username: str = Depends(require_auth)):
        # 1. 改内存快照
        rules_db.settings = settings
        # 2. await 落表成功（R4，不 fire-and-forget）
        await config_repo.save("system_settings", settings.model_dump())
        # 3. 触发 RuntimeConfig 重建 + forwarder.update_snapshot（R8 原子化，失败上抛如实标注）
        await _reload_runtime()
        await notify_bot("✅ **系统设置已更新**\n已热重载生效。")
        return {"status": "success"}

    @app.get("/api/rules")
    async def get_all_rules(username: str = Depends(require_auth)):
        return rules_db

    @app.post("/api/sources/add")
    async def add_source(source: SourceConfig, username: str = Depends(require_auth)):
        # 内存查重（v2 对齐）
        if source.identifier in [s.identifier for s in rules_db.sources]:
            raise HTTPException(status_code=400, detail="源已存在")
        # await 落表成功后更新内存（R4）
        await source_repo.save(source.model_dump())
        rules_db.sources.append(source)
        # M3：写后立即热重载（新增源即时生效，无需 Bot /reload）
        await _reload_runtime()
        await notify_bot(f"➕ **新增监控源**: `{source.identifier}`（已热重载生效）")
        return {"status": "success"}

    @app.post("/api/sources/remove")
    async def remove_source(data: Dict[str, Any], username: str = Depends(require_auth)):
        identifier = str(data.get("identifier"))
        await source_repo.remove(identifier)
        rules_db.sources = [s for s in rules_db.sources if str(s.identifier) != identifier]
        await _reload_runtime()  # M3：删除立即生效
        await notify_bot(f"➖ **移除监控源**: `{identifier}`（已热重载生效）")
        return {"status": "success"}

    @app.post("/api/rules/add")
    async def add_rule(rule: TargetDistributionRule, username: str = Depends(require_auth)):
        await rule_repo.save(rule.model_dump())
        rules_db.distribution_rules.append(rule)
        await _reload_runtime()  # M3：新增规则立即生效
        await notify_bot(f"➕ **新增分发规则**: `{rule.name}`（已热重载生效）")
        return rule

    @app.post("/api/rules/update_single")
    async def update_single_rule(
        rule: TargetDistributionRule,
        name_to_replace: str = "",
        username: str = Depends(require_auth),
    ):
        """更新单条规则；若改名，同时移除旧名（v2 对齐）。"""
        target_name = name_to_replace if name_to_replace else rule.name

        async with db_lock:
            # 更新内存（找不到则追加，v2 对齐）
            found = False
            for index, r in enumerate(rules_db.distribution_rules):
                if r.name == target_name:
                    rules_db.distribution_rules[index] = rule
                    found = True
                    break
            if not found:
                rules_db.distribution_rules.append(rule)

            # 更新 DB（改了名，还要删除旧的）
            if name_to_replace and name_to_replace != rule.name:
                await rule_repo.remove(name_to_replace)
            await rule_repo.save(rule.model_dump())
            await _reload_runtime()  # M3：规则更新立即生效
        return {"status": "success"}

    @app.post("/api/rules/reorder")
    async def reorder_rules(data: ReorderRequest, username: str = Depends(require_auth)):
        """重排规则顺序；SQLite 无原生顺序，清空重写（v2 对齐）。"""
        async with db_lock:
            # 内存重排
            name_map = {r.name: r for r in rules_db.distribution_rules}
            new_list = []
            for name in data.names:
                if name in name_map:
                    new_list.append(name_map[name])
            # 追加遗漏的
            processed = set(data.names)
            for r in rules_db.distribution_rules:
                if r.name not in processed:
                    new_list.append(r)

            rules_db.distribution_rules = new_list

            # DB 重排：事务化全量重写（C5，中途失败整体回滚）
            await rule_repo.replace_all([r.model_dump() for r in new_list])
            await _reload_runtime()  # M3：规则顺序立即生效
        return {"status": "success"}

    @app.post("/api/rules/remove")
    async def remove_rule(data: Dict[str, str], username: str = Depends(require_auth)):
        name = data.get("name")
        await rule_repo.remove(name)
        rules_db.distribution_rules = [r for r in rules_db.distribution_rules if r.name != name]
        await _reload_runtime()  # M3：规则删除立即生效
        await notify_bot(f"➖ **移除分发规则**: `{name}`（已热重载生效）")
        return {"status": "success"}

    # --- Filters（v2 对齐）---

    @app.get("/api/blacklist")
    async def get_blacklist(username: str = Depends(require_auth)):
        return rules_db.ad_filter

    @app.post("/api/blacklist/update")
    async def update_blacklist(config: AdFilterConfig, username: str = Depends(require_auth)):
        rules_db.ad_filter = config
        await config_repo.save("ad_filter", config.model_dump())
        await _reload_runtime()  # M3：过滤类写端点同样落表即重载，快照不刷新不得声称已生效
        await notify_bot("🛡 **黑名单已更新**\n已热重载生效。")
        return {"status": "success"}

    @app.get("/api/whitelist")
    async def get_whitelist(username: str = Depends(require_auth)):
        return rules_db.whitelist

    @app.post("/api/whitelist/update")
    async def update_whitelist(config: WhitelistConfig, username: str = Depends(require_auth)):
        rules_db.whitelist = config
        await config_repo.save("whitelist", config.model_dump())
        await _reload_runtime()  # M3：过滤类写端点同样落表即重载，快照不刷新不得声称已生效
        await notify_bot("🛡 **白名单已更新**\n已热重载生效。")
        return {"status": "success"}

    @app.get("/api/content_filter")
    async def get_content_filter(username: str = Depends(require_auth)):
        return rules_db.content_filter

    @app.post("/api/content_filter/update")
    async def update_content_filter(config: ContentFilterConfig, username: str = Depends(require_auth)):
        rules_db.content_filter = config
        await config_repo.save("content_filter", config.model_dump())
        await _reload_runtime()  # M3：过滤类写端点同样落表即重载，快照不刷新不得声称已生效
        await notify_bot("🛡 **内容过滤已更新**\n已热重载生效。")
        return {"status": "success"}

    @app.get("/api/replacements")
    async def get_replacements(username: str = Depends(require_auth)):
        return rules_db.replacements

    @app.post("/api/replacements/update")
    async def update_replacements(data: Dict[str, str], username: str = Depends(require_auth)):
        rules_db.replacements = data
        await config_repo.save("replacements", data)
        await _reload_runtime()  # M3：过滤类写端点同样落表即重载，快照不刷新不得声称已生效
        await notify_bot("🔁 **替换规则已更新**\n已热重载生效。")
        return {"status": "success"}

    # --- 新增：健康检查（R1，供 Docker HEALTHCHECK）---

    @app.get("/health")
    async def health():
        """健康检查：无鉴权（容器探针用）。

        status=ok 需要至少一个健康账号；account_manager 未注入或无账号时 unhealthy。
        """
        accounts_healthy = 0
        accounts_total = 0
        am = app_state.get("account_manager")
        if am is not None:
            try:
                status = am.all_status()
                if asyncio.iscoroutine(status):
                    status = await status
                if isinstance(status, dict):
                    accounts = status.get("accounts") or []
                else:
                    accounts = []
                accounts_total = len(accounts)
                accounts_healthy = sum(
                    1 for a in accounts if a.get("healthy") or a.get("connected")
                )
            except Exception as e:
                logger.warning(f"/health 读取账号状态失败: {e}")
        return {
            "status": "ok" if accounts_healthy > 0 else "unhealthy",
            "accounts_healthy": accounts_healthy,
            "accounts_total": accounts_total,
        }

    # --- 新增：运行状态（R9，账号健康/降级/FloodWait 可观测）---

    @app.get("/api/status")
    async def api_status(username: str = Depends(require_auth)):
        """运行状态：每账号 connected/authorized/flood_wait/proxy_level/last_error、
        proxy_fallback 生效级别、uptime、bot_status、message_stats。"""
        accounts: List[Dict[str, Any]] = []
        proxy_fallback: Dict[str, Any] = {}
        uptime: Any = None
        bot_status: Any = "未启用"
        message_stats: Dict[str, Any] = {}

        fwd = app_state.get("forwarder")
        if fwd is not None:
            try:
                res = fwd.all_status()
                if asyncio.iscoroutine(res):
                    res = await res
                if isinstance(res, dict):
                    accounts = res.get("accounts") or []
                    proxy_fallback = res.get("proxy_fallback") or {}
                    uptime = res.get("uptime")
                    bot_status = res.get("bot_status", bot_status)
                    message_stats = res.get("message_stats") or {}
            except Exception as e:
                logger.warning(f"/api/status 读取 forwarder 状态失败: {e}")

        return {
            "accounts": accounts,
            "proxy_fallback": proxy_fallback,
            "uptime": uptime,
            "bot_status": bot_status,
            "message_stats": message_stats,
        }

    # --- 新增：热重载（M3，Web 改配置 → reload → 无需重启容器）---

    @app.post("/api/reload")
    async def reload_config(username: str = Depends(require_auth)):
        """显式热重载：触发 update_settings 全链路（load→快照替换→web 同步）。"""
        if not app_state.get("update_settings"):
            raise HTTPException(status_code=503, detail="热重载回调未注入")
        try:
            await app_state["update_settings"]()
        except Exception as e:
            logger.error(f"/api/reload 热重载失败: {e}")
            raise HTTPException(status_code=500, detail=f"热重载失败: {e}")
        await notify_bot("♻️ **Web 面板触发热重载完成**")
        return {"status": "success"}

    # --- Web UI（v2 对齐）---

    @app.get("/", response_class=HTMLResponse)
    async def get_web_ui():
        if not os.path.exists(static_index_path):
            return HTMLResponse("<h1>Error: index.html missing</h1>", status_code=404)
        return FileResponse(static_index_path)

    return app
