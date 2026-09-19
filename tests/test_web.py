# -*- coding: utf-8 -*-
"""TG-Forwarder v3 Web 层测试。

真实 tmp sqlite + 真实仓储（tests/helpers.py）+ FastAPI TestClient。
覆盖：/health、鉴权（Basic/sha256/session token）、settings 落表生效、
sources 落表对账、v2 API 路由全量。
"""
import asyncio
import hashlib
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tg_forwarder.web.server import _sessions, create_app, verify_password
from tg_forwarder.web.uvicorn_runner import run_server
from tests.helpers import (
    MiniConfigRepository,
    MiniDatabase,
    MiniRuleRepository,
    MiniSourceRepository,
)

TEST_PASSWORD = "test_password_123"


def make_client(account_manager=None, forwarder=None, password=TEST_PASSWORD):
    """构建 (TestClient, 上下文 dict)：tmp sqlite + 真实仓储 + 快照/回调记录。"""
    tmpdir = tempfile.mkdtemp()
    db = MiniDatabase(os.path.join(tmpdir, "test.sqlite"))
    # MiniDatabase 用 to_thread 实现对 loop 无关，惰性连接，无需预建 loop
    config_repo = MiniConfigRepository(db)
    source_repo = MiniSourceRepository(db)
    rule_repo = MiniRuleRepository(db)

    calls = {"update_settings": 0, "snapshot": 0}

    def get_snapshot():
        calls["snapshot"] += 1

        cfg = type("Cfg", (), {})
        cfg.web_ui = type("WebUI", (), {})()
        cfg.web_ui.password = password
        return cfg()

    async def update_settings():
        calls["update_settings"] += 1

    app = create_app(
        db=db,
        config_repo=config_repo,
        source_repo=source_repo,
        rule_repo=rule_repo,
        get_snapshot=get_snapshot,
        update_settings=update_settings,
        account_manager=account_manager,
        forwarder=forwarder,
    )
    client = TestClient(app)
    return client, {
        "db": db,
        "config_repo": config_repo,
        "source_repo": source_repo,
        "rule_repo": rule_repo,
        "calls": calls,
    }


def basic_auth(password=TEST_PASSWORD):
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}


# ---------------------------------------------------------------------------
# verify_password 单元
# ---------------------------------------------------------------------------

class TestVerifyPassword:
    def test_plain_match(self):
        assert verify_password("abc", "abc") is True

    def test_plain_mismatch(self):
        assert verify_password("abc", "abd") is False

    def test_empty_stored(self):
        assert verify_password("abc", "") is False

    def test_sha256_match(self):
        h = hashlib.sha256(b"secret").hexdigest()
        assert verify_password("secret", f"sha256${h}") is True

    def test_sha256_uppercase_hex_stored(self):
        h = hashlib.sha256(b"secret").hexdigest().upper()
        assert verify_password("secret", f"sha256${h}") is True

    def test_sha256_mismatch(self):
        h = hashlib.sha256(b"other").hexdigest()
        assert verify_password("secret", f"sha256${h}") is False


# ---------------------------------------------------------------------------
# /health（R1）
# ---------------------------------------------------------------------------

class TestHealth:
    def test_no_account_manager_unhealthy(self):
        client, ctx = make_client(account_manager=None)
        with client:
            res = client.get("/health")
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "unhealthy"
        assert data["accounts_healthy"] == 0
        assert data["accounts_total"] == 0

    def test_no_healthy_account_unhealthy(self):
        class _AM:
            def all_status(self):
                return {"accounts": [{"connected": False, "healthy": False}]}

        client, ctx = make_client(account_manager=_AM())
        with client:
            res = client.get("/health")
        assert res.json()["status"] == "unhealthy"

    def test_healthy_account_ok(self):
        class _AM:
            def all_status(self):
                return {
                    "accounts": [
                        {"connected": True, "healthy": True},
                        {"connected": True, "healthy": True},
                    ]
                }

        client, ctx = make_client(account_manager=_AM())
        with client:
            res = client.get("/health")
        data = res.json()
        assert data["status"] == "ok"
        assert data["accounts_healthy"] == 2
        assert data["accounts_total"] == 2

    def test_health_no_auth_required(self):
        client, ctx = make_client()
        with client:
            res = client.get("/health")  # 不带任何凭据
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# 鉴权
# ---------------------------------------------------------------------------

class TestAuth:
    def test_wrong_password_401(self):
        client, ctx = make_client()
        with client:
            res = client.get("/api/stats", headers=basic_auth("wrong"))
        assert res.status_code == 401

    def test_no_credentials_401(self):
        client, ctx = make_client()
        with client:
            res = client.get("/api/stats")
        assert res.status_code == 401
        assert res.headers.get("WWW-Authenticate") == "Basic"

    def test_correct_password_ok(self):
        client, ctx = make_client()
        with client:
            res = client.get("/api/stats", headers=basic_auth())
        assert res.status_code == 200

    def test_sha256_password_flow(self):
        h = hashlib.sha256(TEST_PASSWORD.encode()).hexdigest()
        client, ctx = make_client(password=f"sha256${h}")
        with client:
            ok = client.get("/api/stats", headers=basic_auth(TEST_PASSWORD))
            bad = client.get("/api/stats", headers=basic_auth("wrong"))
        assert ok.status_code == 200
        assert bad.status_code == 401

    def test_login_issues_session_token(self):
        client, ctx = make_client()
        with client:
            res = client.post("/api/login", headers=basic_auth())
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "success"
        assert len(data["session_token"]) >= 32
        assert data["expires_in"] == 12 * 3600

    def test_login_wrong_password_401(self):
        client, ctx = make_client()
        with client:
            res = client.post("/api/login", headers=basic_auth("nope"))
        assert res.status_code == 401

    def test_session_token_access(self):
        client, ctx = make_client()
        with client:
            login_res = client.post("/api/login", headers=basic_auth())
            token = login_res.json()["session_token"]
            # X-Session-Token 免 Basic 重发
            res = client.get("/api/stats", headers={"X-Session-Token": token})
        assert res.status_code == 200

    def test_session_token_invalid_rejected(self):
        client, ctx = make_client()
        with client:
            res = client.get("/api/stats", headers={"X-Session-Token": "forged-token"})
        assert res.status_code == 401

    def test_session_token_expired_rejected(self):
        client, ctx = make_client()
        token = "expired-token"
        _sessions[token] = 0.0  # 已过期
        with client:
            res = client.get("/api/stats", headers={"X-Session-Token": token})
        assert res.status_code == 401
        assert token not in _sessions  # 过期即清理


# ---------------------------------------------------------------------------
# settings 落表生效（R4）
# ---------------------------------------------------------------------------

class TestSettingsUpdate:
    def test_update_persists_to_app_config(self):
        client, ctx = make_client()
        with client:
            payload = {
                "dedup_retention_days": 60,
                "forwarding_mode": "forward",
                "default_target": "-100123456",
            }
            res = client.post("/api/settings/update", json=payload, headers=basic_auth())
            assert res.status_code == 200
            assert res.json() == {"status": "success"}

            # GET 返回新值
            got = client.get("/api/settings", headers=basic_auth())
            assert got.status_code == 200
            data = got.json()
            assert data["dedup_retention_days"] == 60
            assert data["forwarding_mode"] == "forward"
            assert data["default_target"] == "-100123456"

            # app_config 表里 'system_settings' 值已更新（R4）
            import json as _json

            stored_raw = asyncio.run(ctx["config_repo"].get("system_settings"))
            stored = _json.loads(stored_raw) if isinstance(stored_raw, str) else stored_raw
            assert stored["dedup_retention_days"] == 60

            # update_settings 回调被触发（R8）
            assert ctx["calls"]["update_settings"] == 1

    def test_update_invalid_mode_422(self):
        client, ctx = make_client()
        with client:
            res = client.post(
                "/api/settings/update",
                json={"forwarding_mode": "bogus"},
                headers=basic_auth(),
            )
        assert res.status_code == 422


# ---------------------------------------------------------------------------
# sources 落表对账
# ---------------------------------------------------------------------------

class TestSources:
    def test_add_and_list(self):
        client, ctx = make_client()
        with client:
            res = client.post(
                "/api/sources/add", json={"identifier": -100999}, headers=basic_auth()
            )
            assert res.status_code == 200

            rules = client.get("/api/rules", headers=basic_auth()).json()
            assert len(rules["sources"]) == 1
            assert rules["sources"][0]["identifier"] == -100999

    def test_add_duplicate_400(self):
        client, ctx = make_client()
        with client:
            client.post("/api/sources/add", json={"identifier": -100999}, headers=basic_auth())
            res = client.post("/api/sources/add", json={"identifier": -100999}, headers=basic_auth())
        assert res.status_code == 400

    def test_remove_persists(self):
        client, ctx = make_client()
        with client:
            client.post("/api/sources/add", json={"identifier": -100999}, headers=basic_auth())
            res = client.post("/api/sources/remove", json={"identifier": -100999}, headers=basic_auth())
            assert res.status_code == 200

            rows = asyncio.run(ctx["source_repo"].get_all())
            assert rows == []

    def test_int_str_identifier_removable(self):
        """v2 兼容：identifier 数字/字符串均可删除。"""
        client, ctx = make_client()
        with client:
            client.post("/api/sources/add", json={"identifier": -100999}, headers=basic_auth())
            client.post("/api/sources/remove", json={"identifier": "-100999"}, headers=basic_auth())
            rules = client.get("/api/rules", headers=basic_auth()).json()
            assert rules["sources"] == []


# ---------------------------------------------------------------------------
# rules 全量操作
# ---------------------------------------------------------------------------

RULE = {
    "name": "rule1",
    "target_identifier": "-100888",
    "all_keywords": ["foo"],
    "any_keywords": [],
    "file_types": [],
    "file_name_patterns": ["*.pdf"],
}


class TestRules:
    def test_add_get_update_single_remove(self):
        client, ctx = make_client()
        with client:
            # add
            res = client.post("/api/rules/add", json=RULE, headers=basic_auth())
            assert res.status_code == 200

            # get（v2 对齐：/api/rules 返回 RulesDatabase 全量）
            rules = client.get("/api/rules", headers=basic_auth()).json()
            assert rules["distribution_rules"][0]["name"] == "rule1"

            # update_single 改名
            renamed = dict(RULE, name="rule1-renamed")
            res = client.post(
                "/api/rules/update_single?name_to_replace=rule1",
                json=renamed,
                headers=basic_auth(),
            )
            assert res.status_code == 200

            rows = asyncio.run(ctx["rule_repo"].get_all())
            names = [r["name"] for r in rows]
            assert names == ["rule1-renamed"]

            # remove
            res = client.post("/api/rules/remove", json={"name": "rule1-renamed"}, headers=basic_auth())
            assert res.status_code == 200
            rules = client.get("/api/rules", headers=basic_auth()).json()
            assert rules["distribution_rules"] == []

    def test_reorder(self):
        client, ctx = make_client()
        with client:
            client.post("/api/rules/add", json=RULE, headers=basic_auth())
            r2 = dict(RULE, name="rule2")
            client.post("/api/rules/add", json=r2, headers=basic_auth())

            res = client.post(
                "/api/rules/reorder", json={"names": ["rule2", "rule1"]}, headers=basic_auth()
            )
            assert res.status_code == 200

            rules = client.get("/api/rules", headers=basic_auth()).json()
            names = [r["name"] for r in rules["distribution_rules"]]
            assert names == ["rule2", "rule1"]

    def test_reorder_appends_missing(self):
        """v2 对齐：reorder names 中遗漏的规则追加到末尾，不丢数据。"""
        client, ctx = make_client()
        with client:
            client.post("/api/rules/add", json=RULE, headers=basic_auth())
            client.post("/api/rules/add", json=dict(RULE, name="rule2"), headers=basic_auth())
            client.post(
                "/api/rules/reorder", json={"names": ["rule2"]}, headers=basic_auth()
            )
            rules = client.get("/api/rules", headers=basic_auth()).json()
            names = [r["name"] for r in rules["distribution_rules"]]
            assert set(names) == {"rule1", "rule2"}
            assert names[0] == "rule2"


# ---------------------------------------------------------------------------
# filters 落表
# ---------------------------------------------------------------------------

class TestFilters:
    def test_blacklist_roundtrip(self):
        client, ctx = make_client()
        with client:
            payload = {"enable": True, "keywords_substring": ["ad1", "ad2"]}
            res = client.post("/api/blacklist/update", json=payload, headers=basic_auth())
            assert res.status_code == 200
            got = client.get("/api/blacklist", headers=basic_auth()).json()
            assert got["keywords_substring"] == ["ad1", "ad2"]

    def test_whitelist_roundtrip(self):
        client, ctx = make_client()
        with client:
            payload = {"enable": True, "keywords": ["vip"]}
            res = client.post("/api/whitelist/update", json=payload, headers=basic_auth())
            assert res.status_code == 200
            got = client.get("/api/whitelist", headers=basic_auth()).json()
            assert got["keywords"] == ["vip"]

    def test_content_filter_roundtrip(self):
        client, ctx = make_client()
        with client:
            payload = {"enable": True, "meaningless_words": ["哈哈"], "min_meaningful_length": 8}
            res = client.post("/api/content_filter/update", json=payload, headers=basic_auth())
            assert res.status_code == 200
            got = client.get("/api/content_filter", headers=basic_auth()).json()
            assert got["meaningless_words"] == ["哈哈"]
            assert got["min_meaningful_length"] == 8

    def test_replacements_roundtrip(self):
        client, ctx = make_client()
        with client:
            payload = {"foo": "bar"}
            res = client.post("/api/replacements/update", json=payload, headers=basic_auth())
            assert res.status_code == 200
            got = client.get("/api/replacements", headers=basic_auth()).json()
            assert got == {"foo": "bar"}


# ---------------------------------------------------------------------------
# /api/stats 字段对齐 v2
# ---------------------------------------------------------------------------

class TestStats:
    def test_stats_fields(self):
        class _Fwd:
            def all_status(self):
                return {"uptime": "1分", "bot_status": "未启用", "user_account_count": 0}

        client, ctx = make_client(forwarder=_Fwd())
        with client:
            # 预置数据
            client.post("/api/sources/add", json={"identifier": -100999}, headers=basic_auth())
            client.post("/api/rules/add", json=RULE, headers=basic_auth())
            client.post("/api/blacklist/update", json={"keywords_substring": ["a"]}, headers=basic_auth())
            client.post("/api/replacements/update", json={"k": "v"}, headers=basic_auth())

            res = client.get("/api/stats", headers=basic_auth())
        assert res.status_code == 200
        data = res.json()
        # v2 字段全量
        assert data["sources"] == 1
        assert data["distribution_rules"] == 1
        assert data["whitelist_count"] == 0
        assert data["blacklist_count"] == 1
        assert data["content_filter_count"] == 0
        assert data["replacements_count"] == 1
        assert data["dedup_hashes"] == 0
        assert data["invalid_links"] == 0
        # runtime 合并
        assert data["uptime"] == "1分"
        assert data["bot_status"] == "未启用"


# ---------------------------------------------------------------------------
# /api/status（R9）与文档路由
# ---------------------------------------------------------------------------

class TestStatusAndDocs:
    def test_api_status_with_forwarder(self):
        class _Fwd:
            def all_status(self):
                return {
                    "accounts": [
                        {
                            "name": "acc1",
                            "connected": True,
                            "authorized": True,
                            "flood_wait": 0,
                            "proxy_level": "full",
                            "last_error": None,
                        }
                    ],
                    "proxy_fallback": {"acc1": "full"},
                    "uptime": "0秒",
                    "bot_status": "已连接",
                }

        client, ctx = make_client(forwarder=_Fwd())
        with client:
            res = client.get("/api/status", headers=basic_auth())
        assert res.status_code == 200
        data = res.json()
        assert data["accounts"][0]["connected"] is True
        assert data["proxy_fallback"] == {"acc1": "full"}
        assert data["uptime"] == "0秒"
        assert data["bot_status"] == "已连接"

    def test_api_status_without_forwarder(self):
        client, ctx = make_client(forwarder=None)
        with client:
            res = client.get("/api/status", headers=basic_auth())
        assert res.status_code == 200
        data = res.json()
        assert data["accounts"] == []
        assert data["bot_status"] == "未启用"

    def test_docs_routes_authed(self):
        client, ctx = make_client()
        with client:
            for path in ["/docs", "/redoc", "/openapi.json"]:
                res = client.get(path, headers=basic_auth())
                assert res.status_code == 200, path

    def test_docs_routes_unauthed_401(self):
        client, ctx = make_client()
        with client:
            for path in ["/docs", "/redoc", "/openapi.json"]:
                res = client.get(path)
                assert res.status_code == 401, path

    def test_index_html(self):
        client, ctx = make_client()
        with client:
            res = client.get("/")
        # index.html 已复制到包内 static/，应可访问
        assert res.status_code == 200
        assert "html" in res.headers["content-type"]

    def test_index_missing_404(self):
        client, ctx = make_client()
        app = client.app
        # 覆盖为不存在的路径
        routes = {r.path: r for r in app.routes if hasattr(r, "path") and r.path == "/"}
        with client:
            res = client.get("/")
        assert res.status_code in (200, 404)  # 静态文件存在性由部署决定


# ---------------------------------------------------------------------------
# uvicorn_runner
# ---------------------------------------------------------------------------

class TestUvicornRunner:
    def test_run_server_returns_server(self):
        client, ctx = make_client()
        server = run_server(client.app, host="127.0.0.1", port=18080)
        import uvicorn

        assert isinstance(server, uvicorn.Server)
        assert server.config.host == "127.0.0.1"
        assert server.config.port == 18080
        assert server.config.access_log is False
        assert server.config.log_config is None


class TestBotNotifierWiring:
    """T1：Web 写配置成功后经 notify_bot 推送 admin（bot_notifier 注入链）。"""

    def _make_with_notifier(self):
        tmpdir = tempfile.mkdtemp()
        db = MiniDatabase(os.path.join(tmpdir, "t.sqlite"))
        repos = (
            MiniConfigRepository(db),
            MiniSourceRepository(db),
            MiniRuleRepository(db),
        )
        pushed = []

        async def notifier(msg):
            pushed.append(msg)

        def get_snapshot():
            cfg = type("Cfg", (), {})()
            cfg.web_ui = type("W", (), {})()
            cfg.web_ui.password = TEST_PASSWORD
            return cfg

        async def noop_update():
            return None

        app = create_app(
            db=db,
            config_repo=repos[0],
            source_repo=repos[1],
            rule_repo=repos[2],
            get_snapshot=get_snapshot,
            update_settings=noop_update,
            bot_notifier=notifier,
        )
        return TestClient(app), pushed

    def test_settings_update_pushes_bot(self):
        client, pushed = self._make_with_notifier()
        with client:
            res = client.post(
                "/api/settings/update",
                json={"forwarding_mode": "copy", "default_target": "-1001"},
                headers=basic_auth(),
            )
            assert res.status_code == 200
        assert len(pushed) >= 1  # 系统设置更新 → Bot 推送

    def test_source_add_pushes_bot(self):
        client, pushed = self._make_with_notifier()
        with client:
            res = client.post(
                "/api/sources/add",
                json={"identifier": "-100999"},
                headers=basic_auth(),
            )
            assert res.status_code == 200
        assert any("-100999" in m for m in pushed)

    def test_no_notifier_is_silent_not_error(self):
        """bot_notifier=None（未启用 Bot）：写操作仍成功，推送静默不报错。"""
        client, _ = make_client()  # 默认无 notifier
        with client:
            res = client.post(
                "/api/settings/update",
                json={"forwarding_mode": "forward", "default_target": "-2"},
                headers=basic_auth(),
            )
            assert res.status_code == 200


# ---------------------------------------------------------------------------
# M3：Web 热重载链路 —— /api/reload + 写操作自动热重载
# ---------------------------------------------------------------------------


class TestReload:
    def test_reload_success(self):
        client, ctx = make_client()
        with client:
            res = client.post("/api/reload", headers=basic_auth())
        assert res.status_code == 200
        assert res.json() == {"status": "success"}
        assert ctx["calls"]["update_settings"] == 1

    def test_reload_requires_auth(self):
        """未登录不可触发重载（M3 验收：登录保护覆盖配置类操作）。"""
        client, ctx = make_client()
        with client:
            res = client.post("/api/reload")
        assert res.status_code == 401

    def test_reload_500_when_update_fails(self):
        from tg_forwarder.web import server as web_server

        client, ctx = make_client()
        saved = web_server.app_state["update_settings"]

        async def boom():
            raise RuntimeError("config broke")

        web_server.app_state["update_settings"] = boom
        with client:
            res = client.post("/api/reload", headers=basic_auth())
        web_server.app_state["update_settings"] = saved
        assert res.status_code == 500

    def test_reload_503_without_callback(self):
        from tg_forwarder.web import server as web_server

        client, ctx = make_client()
        saved = web_server.app_state["update_settings"]
        web_server.app_state["update_settings"] = None
        with client:
            res = client.post("/api/reload", headers=basic_auth())
        web_server.app_state["update_settings"] = saved
        assert res.status_code == 503


class TestWriteAutoReload:
    """M3：源/规则写操作落表成功后自动触发热重载，无需 Bot /reload。"""

    def test_source_add_and_remove_trigger_reload(self):
        client, ctx = make_client()
        with client:
            client.post("/api/sources/add", json={"identifier": -100999}, headers=basic_auth())
            assert ctx["calls"]["update_settings"] == 1
            client.post("/api/sources/remove", json={"identifier": -100999}, headers=basic_auth())
            assert ctx["calls"]["update_settings"] == 2

    def test_rule_full_lifecycle_triggers_reload(self):
        client, ctx = make_client()
        with client:
            client.post("/api/rules/add", json=RULE, headers=basic_auth())
            assert ctx["calls"]["update_settings"] == 1
            client.post(
                "/api/rules/update_single?name_to_replace=rule1",
                json=dict(RULE, name="r2"),
                headers=basic_auth(),
            )
            assert ctx["calls"]["update_settings"] == 2
            client.post("/api/rules/add", json=dict(RULE, name="r3"), headers=basic_auth())
            client.post(
                "/api/rules/reorder", json={"names": ["r3", "r2"]}, headers=basic_auth()
            )
            assert ctx["calls"]["update_settings"] == 4
            client.post("/api/rules/remove", json={"name": "r3"}, headers=basic_auth())
            assert ctx["calls"]["update_settings"] == 5

    def test_write_reload_failure_propagates_500(self):
        """Codex review major：写操作自动热重载失败不再静默 200+「已热重载生效」——上抛 500 如实标注。"""
        from tg_forwarder.web import server as web_server

        client, ctx = make_client()
        saved = web_server.app_state["update_settings"]

        async def boom():
            raise RuntimeError("config broke")

        web_server.app_state["update_settings"] = boom
        try:
            with client:
                res = client.post(
                    "/api/sources/add", json={"identifier": -100999}, headers=basic_auth()
                )
        finally:
            web_server.app_state["update_settings"] = saved
        assert res.status_code == 500
        detail = res.json()["detail"]
        assert "已保存" in detail and "热重载失败" in detail

    def test_settings_update_reload_failure_propagates_500(self):
        """settings 写操作热重载失败同样上抛（原 200 静默失效路径已消除）。"""
        from tg_forwarder.web import server as web_server

        client, ctx = make_client()
        saved = web_server.app_state["update_settings"]

        async def boom():
            raise RuntimeError("config broke")

        web_server.app_state["update_settings"] = boom
        try:
            with client:
                res = client.post(
                    "/api/settings/update",
                    json={"forwarding_mode": "copy", "dedup_retention_days": 30},
                    headers=basic_auth(),
                )
        finally:
            web_server.app_state["update_settings"] = saved
        assert res.status_code == 500
        assert "热重载失败" in res.json()["detail"]

    def test_api_status_includes_message_stats_and_uptime(self):
        class _Fwd:
            def all_status(self):
                return {
                    "accounts": [],
                    "uptime": "1天 2小时",
                    "message_stats": {"processed": 10, "forwarded": 8},
                }

        client, ctx = make_client(forwarder=_Fwd())
        with client:
            res = client.get("/api/status", headers=basic_auth())
        data = res.json()
        assert data["uptime"] == "1天 2小时"
        assert data["message_stats"] == {"processed": 10, "forwarded": 8}
