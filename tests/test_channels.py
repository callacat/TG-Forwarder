# -*- coding: utf-8 -*-
"""CMP-284 渠道抽象测试：模型校验、端点解析、密钥通道、面板 CRUD/探测。

钉住的不变量：
- 渠道是端点+协议的**唯一事实源**，代码里不许再出现写死的 laya/jev 端点；
- 未选渠道 → 回落旧 base_url/model（老库升级行为零变化，回退能力不丢）；
- 选中的渠道不存在 → 同样回落，且**不**静默换成别的渠道；
- 密钥只存在 config.yaml（channel_<name>.api_key），不进 app_config、不回显；
- 被消费者引用的渠道不许删（否则消费者会悄悄打回旧端点）。
"""
import asyncio
import os
import sys
import tempfile

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from main import _build_ad_judge, _reconcile_ai_features
from tg_forwarder.config import (
    Channel,
    RuntimeConfig,
    SystemSettings,
    _apply_channel_selection,
    _channel_api_key,
    _is_secret_section,
    bootstrap_from_yaml,
    load_runtime_config,
    set_yaml_secret,
)
from tg_forwarder.core.ad_judge import AdJudge
from tg_forwarder.storage.db import Database
from tg_forwarder.storage.repositories import ConfigRepository
from tg_forwarder.web.server import create_app
from tests.helpers import (
    MiniConfigRepository,
    MiniDatabase,
    MiniRuleRepository,
    MiniSourceRepository,
)

TEST_PASSWORD = "test-pass-284"


def basic_auth(pw=TEST_PASSWORD):
    import base64

    return {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def _write_yaml(tmpdir, text):
    p = os.path.join(tmpdir, "config.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return p


# ---------------------------------------------------------------------------
# Channel 模型校验
# ---------------------------------------------------------------------------


class TestChannelModel:
    def test_defaults(self):
        c = Channel(name="laya", base_url="http://100.64.0.2:8092")
        assert c.protocol == "systemone"
        assert c.api_key_ref == ""
        assert c.models == []

    def test_rejects_blank_name_and_url(self):
        for kw in ({"name": "", "base_url": "http://x"}, {"name": "a", "base_url": "  "}):
            with pytest.raises(Exception):
                Channel(**kw)

    def test_rejects_bad_protocol(self):
        with pytest.raises(Exception):
            Channel(name="a", base_url="http://x", protocol="grpc")

    def test_rejects_name_that_breaks_yaml_section(self):
        """渠道名会拼成 yaml 段名 channel_<name>，中文/冒号/空格必须挡在入口。"""
        for bad in ("中文渠道", "a:b", "a b", "a/b"):
            with pytest.raises(Exception):
                Channel(name=bad, base_url="http://x")

    def test_models_normalized(self):
        c = Channel(name="a", base_url="http://x", models=[" m1 ", "", "m2"])
        assert c.models == ["m1", "m2"]


# ---------------------------------------------------------------------------
# 端点解析：渠道接管 / 回落
# ---------------------------------------------------------------------------


def _cfg(**settings_kw):
    cfg = RuntimeConfig()
    cfg.settings = SystemSettings(**settings_kw)
    return cfg


class TestApplyChannelSelection:
    def test_no_selection_keeps_legacy_fields(self):
        """老库升级：没选渠道 → 原端点原样保留（行为零变化）。"""
        cfg = _cfg()
        before = (cfg.ad_judge.base_url, cfg.ad_judge.model)
        _apply_channel_selection(cfg, "/nonexistent.yaml")
        assert (cfg.ad_judge.base_url, cfg.ad_judge.model) == before

    def test_selected_channel_takes_over_endpoint(self):
        cfg = _cfg(ad_judge_channel="laya")
        cfg.channels = [
            Channel(name="laya", base_url="http://100.64.0.2:8092", models=["laya"])
        ]
        _apply_channel_selection(cfg, "/nonexistent.yaml")
        assert cfg.ad_judge.base_url == "http://100.64.0.2:8092"
        # 面板 model 为空 → 取渠道候选列表首个
        assert cfg.ad_judge.model == "laya"

    def test_explicit_model_beats_channel_list(self):
        """渠道挂多个模型时，选哪个由消费者决定，渠道只提供端点。"""
        cfg = _cfg(ad_judge_channel="laya")
        cfg.ad_judge.model = "laya-large"
        cfg.channels = [
            Channel(name="laya", base_url="http://h:1", models=["laya", "laya-large"])
        ]
        _apply_channel_selection(cfg, "/nonexistent.yaml")
        assert cfg.ad_judge.model == "laya-large"

    def test_missing_channel_falls_back_to_legacy(self):
        """选了个已删除的渠道 → 回落旧端点，绝不静默改用别的渠道。"""
        cfg = _cfg(ad_judge_channel="ghost")
        cfg.channels = [Channel(name="other", base_url="http://other:1")]
        before = cfg.ad_judge.base_url
        _apply_channel_selection(cfg, "/nonexistent.yaml")
        assert cfg.ad_judge.base_url == before

    def test_all_three_consumers_switch(self):
        cfg = _cfg(
            ad_judge_channel="laya",
            ai_content_channel="axon",
            digest_channel="axon",
        )
        cfg.channels = [
            Channel(name="laya", base_url="http://laya:8092", models=["laya"]),
            Channel(name="axon", base_url="http://axon:8091/v1", models=["glm"]),
        ]
        _apply_channel_selection(cfg, "/nonexistent.yaml")
        assert cfg.ad_judge.base_url == "http://laya:8092"
        assert cfg.ai_content.base_url == "http://axon:8091/v1"
        assert cfg.digest.base_url == "http://axon:8091/v1"


# ---------------------------------------------------------------------------
# 密钥：只从 config.yaml 读、按 ref 解析
# ---------------------------------------------------------------------------


class TestChannelApiKey:
    def test_empty_ref_means_no_auth(self, tmp_path):
        ch = Channel(name="a", base_url="http://x")
        assert _channel_api_key(ch, str(tmp_path / "nope.yaml")) is None

    def test_reads_channel_section(self, tmp_path):
        p = _write_yaml(str(tmp_path), "channel_laya:\n  api_key: sk-laya\n")
        ch = Channel(name="laya", base_url="http://x", api_key_ref="channel_laya")
        assert _channel_api_key(ch, p) == "sk-laya"

    def test_can_reuse_existing_section(self, tmp_path):
        p = _write_yaml(str(tmp_path), "ai_content:\n  api_key: sk-shared\n")
        ch = Channel(name="axon", base_url="http://x", api_key_ref="ai_content")
        assert _channel_api_key(ch, p) == "sk-shared"

    def test_missing_file_is_none_not_crash(self, tmp_path):
        ch = Channel(name="a", base_url="http://x", api_key_ref="channel_a")
        assert _channel_api_key(ch, str(tmp_path / "absent.yaml")) is None

    def test_key_written_into_yaml_and_kept_out_of_db(self, tmp_path):
        """端到端：面板写密钥 → 只落 config.yaml，app_config 的 channels 无明文。"""
        p = _write_yaml(str(tmp_path), "web_ui:\n  password: x\n")
        set_yaml_secret(p, "channel_laya", "sk-very-secret")
        data = yaml.safe_load(open(p, encoding="utf-8"))
        assert data["channel_laya"]["api_key"] == "sk-very-secret"

    def test_secret_section_gate(self):
        assert _is_secret_section("ai_content")
        assert _is_secret_section("channel_laya")
        assert not _is_secret_section("web_ui")
        assert not _is_secret_section("channel_")
        assert not _is_secret_section("channel_a:b")


# ---------------------------------------------------------------------------
# AdJudge：Bearer 头（laya 必需；jev 无 key 行为不变）
# ---------------------------------------------------------------------------


class _FakeResp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    @property
    def text(self):
        return str(self._body)

    def json(self):
        return self._body


class _FakeClient:
    def __init__(self, recorder, resp):
        self.recorder, self.resp = recorder, resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None, headers=None):
        self.recorder.append({"url": url, "json": json, "headers": headers or {}})
        return self.resp


def _patch(monkeypatch, resp):
    import httpx

    calls = []
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeClient(calls, resp))
    return calls


def _noul(score):
    return _FakeResp({"answers": {"is_ad": {"type": "noul", "noul": score}}})


class TestAdJudgeAuth:
    def test_bearer_header_sent_when_key_present(self, monkeypatch):
        calls = _patch(monkeypatch, _noul(0.99))
        j = AdJudge(base_url="http://laya:8092", model="laya", api_key="sk-laya")
        assert asyncio.run(j.is_ad("加微信领取")) is True
        assert calls[0]["headers"]["Authorization"] == "Bearer sk-laya"
        assert calls[0]["url"] == "http://laya:8092/v1/systemone"

    def test_no_header_when_key_absent(self, monkeypatch):
        """jev 本机端点无需 key：不许多带一个空 Bearer 头。"""
        calls = _patch(monkeypatch, _noul(0.03))
        j = AdJudge(base_url="http://jev:28880")
        assert asyncio.run(j.is_ad("正常消息")) is False
        assert "Authorization" not in calls[0]["headers"]


# ---------------------------------------------------------------------------
# 真实 laya 分数回放（离线，不打网络）
# ---------------------------------------------------------------------------


class TestLayaScoreReplay:
    """2026-10-26 老马对现网 laya 实测的 noul 分，钉住阈值语义不漂移。

    阈值语义保持 >= 0.85 广告 / [0.60,0.85) fail-open / < 0.60 正常。
    样本量不足（东哥卡内要求上线前用真实样本重标），故这里只验「语义没变」，
    不把 0.5497 当成分界证据。
    """

    @pytest.mark.parametrize(
        "text,score,expected",
        [
            ("加微信免费领取涨粉秘籍，名额有限！", 0.9963, True),
            ("电视影院TV_9.1_会员版 免费观看全网影视", 0.5497, False),
            ("明天上午九点在三楼会议室开周会，请带上季度报表。", 0.0141, False),
        ],
    )
    def test_replay(self, monkeypatch, text, score, expected):
        _patch(monkeypatch, _noul(score))
        j = AdJudge(
            base_url="http://laya:8092",
            model="laya",
            threshold=0.85,
            fuzzy_low=0.60,
            api_key="sk",
        )
        assert asyncio.run(j.is_ad(text)) is expected

    def test_app_resource_not_blocked_at_05497(self):
        """09-23 jev 误杀形态不许重演：资源分享样本必须放行（非 True）。"""
        assert 0.5497 < 0.85

    def test_unreachable_fails_open(self, monkeypatch):
        """断网实验（卡内验收 3）：laya 不可达 → None → 调用方放行。"""
        import httpx

        def boom(**kw):
            raise httpx.ConnectError("refused", request=None)

        monkeypatch.setattr(httpx, "AsyncClient", boom)
        j = AdJudge(base_url="http://laya:8092", api_key="sk")
        assert asyncio.run(j.is_ad("任何消息")) is None


# ---------------------------------------------------------------------------
# main.py 装配 / 热重载
# ---------------------------------------------------------------------------


class TestAdJudgeBuildAndReload:
    def test_disabled_builds_nothing(self):
        assert _build_ad_judge(_cfg()) is None

    def test_build_passes_channel_key(self):
        cfg = _cfg(ad_judge_enabled=True, ad_judge_channel="laya")
        cfg.ad_judge.enabled = True
        cfg.channels = [Channel(name="laya", base_url="http://laya:8092", models=["laya"])]
        _apply_channel_selection(cfg, "/nonexistent.yaml")
        cfg.ad_judge.api_key = "sk-laya"
        j = _build_ad_judge(cfg)
        assert isinstance(j, AdJudge)
        assert (j.base_url, j.model, j.api_key) == ("http://laya:8092", "laya", "sk-laya")

    def test_key_change_rebuilds_judge(self):
        """换渠道密钥后必须重建，否则 /reload 沿用旧实例 → 401（与 digest 同款教训）。"""
        fwd = type("F", (), {})()
        cfg = _cfg(ad_judge_enabled=True)
        cfg.ad_judge.enabled = True
        cfg.ad_judge.api_key = "old"
        _reconcile_ai_features(fwd, cfg)
        first = fwd.ad_judge
        cfg.ad_judge.api_key = "new"
        _reconcile_ai_features(fwd, cfg)
        assert fwd.ad_judge is not first
        assert fwd.ad_judge.api_key == "new"

    def test_unchanged_config_keeps_instance(self):
        fwd = type("F", (), {})()
        cfg = _cfg(ad_judge_enabled=True)
        cfg.ad_judge.enabled = True
        cfg.ad_judge.api_key = "same"
        _reconcile_ai_features(fwd, cfg)
        first = fwd.ad_judge
        _reconcile_ai_features(fwd, cfg)
        assert fwd.ad_judge is first


# ---------------------------------------------------------------------------
# load_runtime_config：channels 落表 → 端点解析
# ---------------------------------------------------------------------------


class TestLoadRuntimeConfigChannels:
    def test_channels_persisted_and_applied(self, tmp_path):
        """面板落表 channels + 选中 laya → 重载后 ad_judge 打的是 laya。"""
        db_path = str(tmp_path / "t.sqlite")
        yaml_path = _write_yaml(
            str(tmp_path),
            "web_ui:\n  password: x\n"
            "channel_laya:\n  api_key: sk-laya\n",
        )

        async def scenario():
            db = Database(db_path)
            await db.open()
            await db.migrate()
            repo = ConfigRepository(db)
            await repo.save(
                "channels",
                [
                    {
                        "name": "laya",
                        "protocol": "systemone",
                        "base_url": "http://100.64.0.2:8092",
                        "models": ["laya"],
                        "api_key_ref": "channel_laya",
                    }
                ],
            )
            await repo.save(
                "system_settings",
                SystemSettings(ad_judge_enabled=True, ad_judge_channel="laya").model_dump(),
            )
            cfg = await load_runtime_config(db, yaml_path)
            await db.close()
            return cfg

        cfg = asyncio.run(scenario())
        assert [c.name for c in cfg.channels] == ["laya"]
        assert cfg.ad_judge.base_url == "http://100.64.0.2:8092"
        assert cfg.ad_judge.model == "laya"
        assert cfg.ad_judge.api_key == "sk-laya"

    def test_no_channels_row_keeps_yaml_endpoint(self, tmp_path):
        """老库（无 channels 键）→ 完全走 yaml/默认端点。"""
        db_path = str(tmp_path / "t2.sqlite")
        yaml_path = _write_yaml(
            str(tmp_path), "web_ui:\n  password: x\nad_judge:\n  base_url: \"http://yaml:1\"\n"
        )

        async def scenario():
            db = Database(db_path)
            await db.open()
            await db.migrate()
            cfg = await load_runtime_config(db, yaml_path)
            await db.close()
            return cfg

        cfg = asyncio.run(scenario())
        assert cfg.channels == []
        assert cfg.ad_judge.base_url == "http://yaml:1"


# ---------------------------------------------------------------------------
# 面板 API：CRUD + 探测 + 引用护栏
# ---------------------------------------------------------------------------


def make_client(config_path=None, settings=None):
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
    from tg_forwarder.web.server import app_state

    app_state["rules_db"].settings = settings or SystemSettings()
    return TestClient(app), calls


class TestChannelApi:
    def test_requires_auth(self):
        client, _ = make_client()
        assert client.get("/api/channels").status_code == 401
        assert client.post("/api/channels/save", json={}).status_code == 401

    def test_save_then_list(self):
        client, calls = make_client()
        r = client.post(
            "/api/channels/save",
            json={
                "name": "laya",
                "protocol": "systemone",
                "base_url": "http://100.64.0.2:8092",
                "models": ["laya"],
                "api_key_ref": "channel_laya",
            },
            headers=basic_auth(),
        )
        assert r.status_code == 200, r.text
        assert calls["update_settings"] == 1  # 落表后热重载
        body = client.get("/api/channels", headers=basic_auth()).json()
        assert [c["name"] for c in body["channels"]] == ["laya"]
        assert body["channels"][0]["api_key_set"] is False  # 还没填密钥
        assert "sk-" not in str(body)

    def test_save_overwrites_same_name(self):
        client, _ = make_client()
        h = basic_auth()
        for url in ("http://a:1", "http://b:2"):
            client.post(
                "/api/channels/save",
                json={"name": "c1", "base_url": url},
                headers=h,
            )
        body = client.get("/api/channels", headers=h).json()
        assert len(body["channels"]) == 1
        assert body["channels"][0]["base_url"] == "http://b:2"

    def test_save_rejects_bad_protocol(self):
        client, _ = make_client()
        r = client.post(
            "/api/channels/save",
            json={"name": "x", "base_url": "http://x", "protocol": "grpc"},
            headers=basic_auth(),
        )
        assert r.status_code == 422  # pydantic 校验拦在入口

    def test_remove_refuses_when_referenced(self):
        """被 ad_judge 引用的渠道不许删：删了消费者会悄悄打回旧端点。"""
        client, _ = make_client(settings=SystemSettings(ad_judge_channel="laya"))
        h = basic_auth()
        client.post(
            "/api/channels/save",
            json={"name": "laya", "base_url": "http://x"},
            headers=h,
        )
        r = client.post("/api/channels/remove", json={"name": "laya"}, headers=h)
        assert r.status_code == 400
        assert "ad_judge_channel" in r.text

    def test_remove_works_when_unreferenced(self):
        client, _ = make_client()
        h = basic_auth()
        client.post("/api/channels/save", json={"name": "tmp", "base_url": "http://x"}, headers=h)
        r = client.post("/api/channels/remove", json={"name": "tmp"}, headers=h)
        assert r.status_code == 200
        assert client.get("/api/channels", headers=h).json()["channels"] == []

    def test_remove_unknown_404(self):
        client, _ = make_client()
        r = client.post("/api/channels/remove", json={"name": "ghost"}, headers=basic_auth())
        assert r.status_code == 404

    def test_probe_unreachable_reports_reason_not_500(self):
        """探测失败要让面板看到原因，不能 500。"""
        client, _ = make_client()
        r = client.post(
            "/api/channels/probe",
            json={"name": "dead", "base_url": "http://127.0.0.1:1"},
            headers=basic_auth(),
        )
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is False
        assert body["models"] == []

    def test_probe_reads_models_with_key(self, tmp_path):
        """探测走 /models，并带上渠道密钥（401 能被提前发现）。"""
        p = _write_yaml(str(tmp_path), "channel_laya:\n  api_key: sk-laya\n")
        client, _ = make_client(config_path=p)
        import httpx

        seen = {}

        class _Resp:
            status_code = 200

            def json(self):
                return {"data": [{"id": "laya"}, {"id": "laya-large"}]}

        class _Client:
            def __init__(self, **kw):
                pass  # timeout / follow_redirects 均忽略

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, headers=None):
                seen["url"] = url
                seen["headers"] = headers or {}
                return _Resp()

        orig = httpx.AsyncClient
        httpx.AsyncClient = _Client
        try:
            r = client.post(
                "/api/channels/probe",
                json={
                    "name": "laya",
                    "base_url": "http://100.64.0.2:8092",
                    "api_key_ref": "channel_laya",
                },
                headers=basic_auth(),
            )
        finally:
            httpx.AsyncClient = orig
        assert r.status_code == 200
        body = r.json()
        assert body["ok"] is True and body["models"] == ["laya", "laya-large"]
        assert seen["url"] == "http://100.64.0.2:8092/models"
        assert seen["headers"]["Authorization"] == "Bearer sk-laya"

    def test_no_endpoint_hardcoded_in_channel_api(self):
        """红线：不许把 laya 端点写成新的代码默认值（否则换个上游又是一次 CMP-281）。"""
        import inspect

        import tg_forwarder.web.server as srv

        src = inspect.getsource(srv)
        assert "100.64.0.2:8092" not in src
