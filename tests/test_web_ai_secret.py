# -*- coding: utf-8 -*-
"""AI 密钥只写通道（/api/ai-secret + config.set_yaml_secret）测试。

核心不变量，逐条钉住：
- **注释必须活着**：config.yaml 是带注释的手工维护文件，任何 yaml 往返回写都会
  抹掉注释——这正是本模块坚持行级定点改写、禁用 safe_dump 全量回写的原因。
- **密钥永不回读**：GET 只回布尔位；响应体里不能出现明文。
- **不进面板持久化**：不落 SystemSettings/sqlite，GET /api/settings 也不带。
- **写操作有闸**：段名白名单、值不许含换行、鉴权必过。
"""
import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from tg_forwarder.config import (
    AI_SECRET_SECTIONS,
    bootstrap_from_yaml,
    set_yaml_secret,
    yaml_secret_set,
)
from tg_forwarder.web.server import create_app
from tests.helpers import (
    MiniConfigRepository,
    MiniDatabase,
    MiniRuleRepository,
    MiniSourceRepository,
)

TEST_PASSWORD = "test_password_123"

SAMPLE_YAML = """# 头部注释
web_ui:
  password: "sha256$x"   # 行内注释

# AI 段说明（顶格注释，不应被吞进改写范围）
ai_content:
  enabled: false
  api_key: "old-key"     # 会被替换
  model: "glm-5.3-flash"

# 相邻段
translate:
  enabled: true
  api_key_env: "AXONHUB_API_KEY"
logging_level:
  app: "INFO"
"""


def _write_yaml(text=SAMPLE_YAML):
    d = tempfile.mkdtemp()
    p = os.path.join(d, "config.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


def make_client(config_path):
    """构建 TestClient（复用 tests/helpers 的内存仓储，不落真 sqlite）。"""
    tmpdir = tempfile.mkdtemp()
    db = MiniDatabase(os.path.join(tmpdir, "test.sqlite"))
    calls = {"update_settings": 0}

    def get_snapshot():
        cfg = type("Cfg", (), {})()
        cfg.web_ui = type("WebUI", (), {})()
        cfg.web_ui.password = TEST_PASSWORD
        return cfg

    async def update_settings():
        calls["update_settings"] += 1

    app = create_app(
        db=db,
        config_repo=MiniConfigRepository(db),
        source_repo=MiniSourceRepository(db),
        rule_repo=MiniRuleRepository(db),
        get_snapshot=get_snapshot,
        update_settings=update_settings,
        config_path=config_path,
    )
    return TestClient(app), calls


def basic_auth(password=TEST_PASSWORD):
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"admin:{password}".encode()).decode()}


# ---------------------------------------------------------------------------
# set_yaml_secret：注释保留 + 定点改写
# ---------------------------------------------------------------------------

class TestSetYamlSecret:
    def test_preserves_comments(self):
        """回归核心：全量 yaml 往返会把注释全抹掉，本实现必须一行不丢。"""
        p = _write_yaml()
        before = [l.strip() for l in open(p, encoding="utf-8") if l.startswith("#")]
        set_yaml_secret(p, "ai_content", "sk-new")
        after = [l.strip() for l in open(p, encoding="utf-8") if l.startswith("#")]
        # 整行注释全部保留（被替换行的行内尾注随该行一起换掉，属预期）
        assert after == before
        assert "AI 段说明" in "".join(after) and "相邻段" in "".join(after)

    def test_replaces_in_place_idempotently(self):
        p = _write_yaml()
        set_yaml_secret(p, "ai_content", "sk-1")
        set_yaml_secret(p, "ai_content", "sk-2")
        text = open(p, encoding="utf-8").read()
        assert text.count("api_key:") == 1
        assert yaml.safe_load(text)["ai_content"]["api_key"] == "sk-2"

    def test_neighbour_sections_untouched(self):
        p = _write_yaml()
        set_yaml_secret(p, "ai_content", "sk-new")
        d = yaml.safe_load(open(p, encoding="utf-8"))
        assert d["translate"]["api_key_env"] == "AXONHUB_API_KEY"
        assert d["translate"]["enabled"] is True
        assert d["ai_content"]["enabled"] is False
        assert d["ai_content"]["model"] == "glm-5.3-flash"
        assert d["logging_level"]["app"] == "INFO"

    def test_inserts_when_key_absent(self):
        p = _write_yaml()
        del_lines = [l for l in open(p, encoding="utf-8") if not l.startswith("  api_key:")]
        with open(p, "w", encoding="utf-8") as f:
            f.write("".join(del_lines))
        set_yaml_secret(p, "ai_content", "sk-inserted")
        assert yaml.safe_load(open(p, encoding="utf-8"))["ai_content"]["api_key"] == "sk-inserted"

    def test_appends_section_when_missing(self):
        p = _write_yaml("web_ui:\n  password: \"sha256$x\"\n")
        set_yaml_secret(p, "ai_content", "sk-newsec")
        d = yaml.safe_load(open(p, encoding="utf-8"))
        assert d["ai_content"]["api_key"] == "sk-newsec"
        assert d["web_ui"]["password"] == "sha256$x"

    def test_empty_value_allowed(self):
        """空串是合法语义：明确不带鉴权（会盖掉 api_key_env 备选通道）。"""
        p = _write_yaml()
        set_yaml_secret(p, "ai_content", "")
        assert yaml.safe_load(open(p, encoding="utf-8"))["ai_content"]["api_key"] == ""
        assert yaml_secret_set(p, "ai_content") is False

    def test_special_chars_survive(self):
        """key 里常有 `#`、`:`、`"`——引号不当会被 yaml 截断或当注释。"""
        p = _write_yaml()
        tricky = 'sk-a#b:c"d\\e'
        set_yaml_secret(p, "ai_content", tricky)
        assert yaml.safe_load(open(p, encoding="utf-8"))["ai_content"]["api_key"] == tricky

    def test_rejects_unknown_section(self):
        p = _write_yaml()
        with pytest.raises(ValueError):
            set_yaml_secret(p, "web_ui", "x")  # 越权写非白名单段
        with pytest.raises(ValueError):
            set_yaml_secret(p, "accounts", "x")

    def test_rejects_newline(self):
        """换行会破坏行级改写结构，必须在写入边界挡掉。"""
        p = _write_yaml()
        with pytest.raises(ValueError):
            set_yaml_secret(p, "ai_content", "sk-a\nweb_ui: {}")
        assert yaml.safe_load(open(p, encoding="utf-8"))["web_ui"]["password"] == "sha256$x"

    def test_backup_written(self):
        p = _write_yaml()
        original = open(p, encoding="utf-8").read()
        set_yaml_secret(p, "ai_content", "sk-new")
        assert os.path.exists(p + ".bak")
        assert open(p + ".bak", encoding="utf-8").read() == original
        assert not os.path.exists(p + ".tmp")  # 临时文件必须被 rename 掉

    def test_failed_write_leaves_original_intact(self):
        """校验不过时不能把文件改坏——原文件与备份都应完好。"""
        p = _write_yaml()
        original = open(p, encoding="utf-8").read()
        with pytest.raises(ValueError):
            set_yaml_secret(p, "nope", "x")
        assert open(p, encoding="utf-8").read() == original


# ---------------------------------------------------------------------------
# yaml_secret_set：只回布尔
# ---------------------------------------------------------------------------

class TestYamlSecretSet:
    def test_returns_bool_not_value(self):
        p = _write_yaml()
        r = yaml_secret_set(p, "ai_content")
        assert r is True and isinstance(r, bool)
        assert "old-key" not in repr(r)

    def test_missing_file_is_false(self):
        assert yaml_secret_set("/nonexistent/config.yaml", "ai_content") is False

    def test_missing_section_is_false(self):
        p = _write_yaml()
        assert yaml_secret_set(p, "translate") is False  # 段在但没配 key


# ---------------------------------------------------------------------------
# /api/ai-secret 端点
# ---------------------------------------------------------------------------

class TestAiSecretEndpoint:
    def test_requires_auth(self):
        client, _ = make_client(_write_yaml())
        assert client.get("/api/ai-secret").status_code == 401
        assert client.post("/api/ai-secret", json={"section": "ai_content", "api_key": "x"}).status_code == 401

    def test_get_returns_booleans_only(self):
        client, _ = make_client(_write_yaml())
        r = client.get("/api/ai-secret", headers=basic_auth())
        assert r.status_code == 200
        body = r.json()
        assert body == {"ai_content": True, "translate": False}
        assert "old-key" not in r.text  # 明文绝不外泄

    def test_post_writes_and_never_echoes(self):
        p = _write_yaml()
        client, calls = make_client(p)
        r = client.post(
            "/api/ai-secret",
            json={"section": "ai_content", "api_key": "sk-secret-value"},
            headers=basic_auth(),
        )
        assert r.status_code == 200
        assert r.json() == {"status": "success", "section": "ai_content", "api_key_set": True}
        assert "sk-secret-value" not in r.text
        assert yaml.safe_load(open(p, encoding="utf-8"))["ai_content"]["api_key"] == "sk-secret-value"
        assert calls["update_settings"] == 1  # 热重载已触发

    def test_post_rejects_section_off_whitelist(self):
        p = _write_yaml()
        original = open(p, encoding="utf-8").read()
        client, calls = make_client(p)
        r = client.post(
            "/api/ai-secret",
            json={"section": "web_ui", "api_key": "evil"},
            headers=basic_auth(),
        )
        assert r.status_code == 400
        assert "evil" not in r.text
        assert open(p, encoding="utf-8").read() == original  # 文件没被动
        assert calls["update_settings"] == 0

    def test_not_in_settings_channel(self):
        """密钥不能经 /api/settings 侧漏进面板 sqlite。"""
        p = _write_yaml()
        client, _ = make_client(p)
        client.post(
            "/api/ai-secret",
            json={"section": "ai_content", "api_key": "sk-secret-value"},
            headers=basic_auth(),
        )
        r = client.get("/api/settings", headers=basic_auth())
        assert r.status_code == 200
        assert "sk-secret-value" not in r.text
        assert "api_key" not in r.text

    def test_503_without_config_path(self):
        """未注入 config_path 时端点要明确不可用，而不是静默成功。"""
        tmpdir = tempfile.mkdtemp()
        db = MiniDatabase(os.path.join(tmpdir, "t.sqlite"))

        def get_snapshot():
            cfg = type("Cfg", (), {})()
            cfg.web_ui = type("WebUI", (), {})()
            cfg.web_ui.password = TEST_PASSWORD
            return cfg

        async def update_settings():
            pass

        app = create_app(
            db=db,
            config_repo=MiniConfigRepository(db),
            source_repo=MiniSourceRepository(db),
            rule_repo=MiniRuleRepository(db),
            get_snapshot=get_snapshot,
            update_settings=update_settings,
        )
        client = TestClient(app)
        assert client.get("/api/ai-secret", headers=basic_auth()).status_code == 503
        assert client.post(
            "/api/ai-secret",
            json={"section": "ai_content", "api_key": "x"},
            headers=basic_auth(),
        ).status_code == 503


# ---------------------------------------------------------------------------
# 端到端：面板写入 → config.yaml → 装配时真的拿到 key
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_panel_write_reaches_processor(self):
        """面板存了 key，bootstrap + 装配链路必须真能取到——否则面板是摆设。"""
        p = _write_yaml()
        client, _ = make_client(p)
        client.post(
            "/api/ai-secret",
            json={"section": "ai_content", "api_key": "sk-e2e"},
            headers=basic_auth(),
        )
        cfg = bootstrap_from_yaml(p)
        assert cfg.ai_content.api_key == "sk-e2e"

        from main import _build_ai_content

        cfg.ai_content.enabled = True
        proc = _build_ai_content(cfg)
        assert proc is not None and proc._api_key == "sk-e2e"

    def test_whitelist_matches_sections_with_key_field(self):
        """白名单里的段必须真的有 api_key 字段，否则面板能写但代码不认。"""
        from tg_forwarder.config import AiContentConfig, TranslateConfig

        for s in AI_SECRET_SECTIONS:
            assert s == "ai_content" or s == "translate"
        assert "api_key" in AiContentConfig.model_fields
        assert "api_key" in TranslateConfig.model_fields

    @pytest.mark.asyncio
    async def test_key_change_picked_up_by_reload(self, tmp_path):
        """面板写完密钥必须**不用重启容器**就生效。

        load_runtime_config 每次都重新 bootstrap_from_yaml（只有「写进表」那步是
        一次性的），热重载链路才吃得到新 key——若哪天改成只 bootstrap 一次，这里
        就会红，表现为「面板显示已保存、功能却还在静默降级」。
        """
        from tg_forwarder.config import load_runtime_config
        from tg_forwarder.storage.db import Database

        yp = str(tmp_path / "config.yaml")
        with open(yp, "w", encoding="utf-8") as f:
            f.write('web_ui:\n  password: "sha256$ab"\nlogging_level:\n  app: "INFO"\naccounts: []\n')

        db = Database(str(tmp_path / "t.sqlite"))
        await db.open()
        await db.migrate()
        try:
            cfg1 = await load_runtime_config(db, yp)
            assert cfg1.ai_content.api_key is None

            set_yaml_secret(yp, "ai_content", "sk-after-reload")
            cfg2 = await load_runtime_config(db, yp)
            assert cfg2.ai_content.api_key == "sk-after-reload"
        finally:
            await db.close()


class TestPanelUi:
    """面板侧防回归：密钥输入框必须在 aiSecretInput 上，绝不能进 settings。"""

    def _html(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "config.yaml")
        with open(p, "w", encoding="utf-8") as f:
            f.write(SAMPLE_YAML)
        client, _ = make_client(p)
        with client:
            return client.get("/").text

    def test_secret_inputs_present_and_masked(self):
        html = self._html()
        assert 'id="acApiKey" type="password"' in html
        assert 'id="trApiKey" type="password"' in html
        assert "saveAiSecret('ai_content')" in html
        assert "saveAiSecret('translate')" in html
        assert "/api/ai-secret" in html

    def test_key_never_bound_into_settings(self):
        """回归核心：settings 会被 POST 到 /api/settings/update 并落 sqlite——
        密钥一旦绑进去就等于明文入库且可 GET 回读。"""
        html = self._html()
        for binding in ("settings.ai_content_api_key", "settings.translate_api_key"):
            assert binding not in html, f"密钥不得绑进 settings（{binding}）"
        # settings 字面量里不能出现裸 api_key 字段
        start = html.index("settings: {")
        literal = html[start : html.index("},", start)]
        assert "api_key" not in literal

    def test_status_shown_as_boolean_only(self):
        """面板只能显示布尔状态位，没有任何把明文读回前端的绑定。"""
        html = self._html()
        assert "aiSecretSet.ai_content" in html
        assert "aiSecretSet.translate" in html
        assert "aiSecretSet.ai_content = " not in html  # 赋值只来自 /api/ai-secret 返回
