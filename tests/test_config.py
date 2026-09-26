# -*- coding: utf-8 -*-
"""config.py 测试（R4 单一配置源 + bootstrap 语义 + R8 快照）。"""
import os

import pytest

from tg_forwarder.config import (
    RuntimeConfig,
    bootstrap_from_yaml,
    load_runtime_config,
)
from tg_forwarder.storage.db import Database

FULL_YAML = """
web_ui:
  password: "sha256$abcd1234"
logging_level:
  app: "INFO"
  telethon: "WARNING"
bot_service:
  enabled: true
  bot_token: "123456:ABC"
  admin_user_ids: [100, 200]
  bot_api_id: 777
  bot_api_hash: "b0thash"
accounts:
  - api_id: 123456
    api_hash: "accthash"
    session_name: "account_1"
    enabled: true
forwarding:
  mode: "copy"
  forward_new_only: true
link_checker:
  enabled: true
  mode: "edit"
  schedule: "0 3 * * *"
ad_filter:
  enable: true
  keywords_substring: ["广告"]
whitelist:
  enable: false
sources:
  - identifier: "-100111"
    cached_title: "源频道"
targets:
  default_target: "-100222"
  default_topic_id: 7
  distribution_rules:
    - name: "视频"
      any_keywords: ["4K"]
      target_identifier: "-100333"
      topic_id: 9
"""


def _write_yaml(tmp_path, content: str) -> str:
    p = os.path.join(str(tmp_path), "config.yaml")
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return p


async def _fresh_db(tmp_path) -> Database:
    db = Database(os.path.join(str(tmp_path), "fwd.sqlite"))
    await db.open()
    await db.migrate()
    return db


class TestBootstrap:
    async def test_bootstrap_imports_yaml_to_table(self, tmp_path):
        """表空 → yaml 数据写入表（R4 bootstrap）。"""
        yp = _write_yaml(tmp_path, FULL_YAML)
        db = await _fresh_db(tmp_path)
        cfg = await load_runtime_config(db, yp)
        assert len(cfg.accounts) == 1
        assert cfg.accounts[0].api_id == 123456
        assert cfg.bot_service.bot_api_id == 777
        assert cfg.web_ui.password == "sha256$abcd1234"
        assert len(cfg.sources) == 1
        assert cfg.sources[0].identifier == "-100111"
        assert cfg.settings.default_target == "-100222"
        assert cfg.settings.default_topic_id == 7
        assert len(cfg.distribution_rules) == 1
        assert cfg.distribution_rules[0].name == "视频"
        assert cfg.ad_filter.keywords_substring == ["广告"]
        assert cfg.link_checker.mode == "edit"
        await db.close()

    async def test_table_data_survives_yaml_removal(self, tmp_path):
        """表已有数据 → yaml 规则段删除不丢配置（R4 单源核心）。"""
        yp = _write_yaml(tmp_path, FULL_YAML)
        db = await _fresh_db(tmp_path)
        await load_runtime_config(db, yp)

        # 模拟运维改坏/删空 yaml
        yp2 = _write_yaml(tmp_path, "accounts:\n  - api_id: 123456\n    api_hash: \"accthash\"\n    session_name: \"account_1\"\n")
        cfg2 = await load_runtime_config(db, yp2)
        assert len(cfg2.sources) == 1  # 仍在表里
        assert cfg2.settings.default_target == "-100222"
        assert len(cfg2.distribution_rules) == 1
        await db.close()

    async def test_web_edit_effective_after_reload(self, tmp_path):
        """Web 改配置落表 → 重建 RuntimeConfig 生效（R4 落表即生效）。"""
        yp = _write_yaml(tmp_path, FULL_YAML)
        db = await _fresh_db(tmp_path)
        cfg = await load_runtime_config(db, yp)

        # 模拟 Web 层落表（server.py 写 app_config）
        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        settings = cfg.settings.model_dump()
        settings["forwarding_mode"] = "forward"
        await repo.save("system_settings", settings)

        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.settings.forwarding_mode == "forward"
        await db.close()

    async def test_yaml_without_rules_section(self, tmp_path):
        """无规则段的 yaml（v2 config_template 占位形态）可正常加载。"""
        yp = _write_yaml(
            tmp_path,
            "accounts:\n  - api_id: 1\n    api_hash: \"h\"\n    session_name: \"s\"\n"
            "sources: []\ntargets:\n  default_target: 0\n",
        )
        db = await _fresh_db(tmp_path)
        cfg = await load_runtime_config(db, yp)
        assert len(cfg.sources) == 0
        assert cfg.settings.default_target in ("", "0")
        await db.close()

    async def test_missing_yaml_ok_when_table_populated(self, tmp_path):
        """表已有数据时 yaml 缺失不致命。"""
        yp = _write_yaml(tmp_path, FULL_YAML)
        db = await _fresh_db(tmp_path)
        await load_runtime_config(db, yp)
        cfg2 = await load_runtime_config(db, "/nonexistent/config.yaml")
        assert len(cfg2.sources) == 1
        await db.close()


class TestRuntimeConfig:
    def test_snapshot_returns_self(self):
        cfg = RuntimeConfig()
        assert cfg.snapshot() is cfg

    def test_bot_independent_credentials_default_none(self):
        cfg = RuntimeConfig()
        assert cfg.bot_service.bot_api_id is None
        assert cfg.bot_service.bot_api_hash is None


class TestM4ExperimentalMerging:
    """M4 实验功能：面板落表覆盖 yaml 摘要段 + bootstrap 不预置键的回落语义。"""

    async def test_fresh_bootstrap_does_not_clobber_yaml_state(self, tmp_path):
        """yaml 里 digest.enabled=true，空库 bootstrap 后 reload 不覆盖为 false（面板如实显示）。"""
        yp = _write_yaml(
            tmp_path,
            FULL_YAML
            + "\ndigest:\n  enabled: true\n  interval_seconds: 900\n",
        )
        db = await _fresh_db(tmp_path)
        cfg = await load_runtime_config(db, yp)
        # 首次加载即触发 bootstrap：digest 摘要段来自 yaml（true/900）
        assert cfg.digest.enabled is True
        assert cfg.digest.interval_seconds == 900
        # 展示值同步为 yaml 实际状态 —— 面板开关如实显示「开」
        assert cfg.settings.digest_enabled is True
        assert cfg.settings.digest_interval_seconds == 900
        # 二次加载（模拟重启/reload）依旧不被 bootstrap 默认键覆盖
        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.digest.enabled is True
        assert cfg2.settings.digest_enabled is True
        await db.close()

    async def test_web_saved_values_override_yaml(self, tmp_path):
        """面板落表 M4 键后，reload 以落表值为准（热重载生效）。"""
        yp = _write_yaml(tmp_path, FULL_YAML)
        db = await _fresh_db(tmp_path)
        cfg = await load_runtime_config(db, yp)

        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        settings = cfg.settings.model_dump()
        settings.update(
            digest_enabled=True,
            digest_interval_seconds=7200,
            translate_enabled=True,
            semantic_dedup_enabled=True,
            semantic_dedup_threshold=0.88,
        )
        await repo.save("system_settings", settings)

        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.digest.enabled is True
        assert cfg2.digest.interval_seconds == 7200
        assert cfg2.translate.enabled is True
        assert cfg2.deduplication.semantic_dedup_enabled is True
        assert cfg2.deduplication.semantic_dedup_threshold == 0.88
        # 展示值同步为生效值
        assert cfg2.settings.digest_enabled is True
        assert cfg2.settings.digest_interval_seconds == 7200
        await db.close()

    async def test_yaml_translate_dedup_display_fallback(self, tmp_path):
        """yaml 摘要段开启的项目，未存过 M4 键时展示值一并回落（语义去重/翻译）。"""
        yaml_cfg = (
            FULL_YAML
            + "\ntranslate:\n  enabled: true\n"
            + "deduplication:\n  semantic_dedup_enabled: true\n  semantic_dedup_threshold: 0.9\n"
        )
        yp = _write_yaml(tmp_path, yaml_cfg)
        db = await _fresh_db(tmp_path)
        cfg = await load_runtime_config(db, yp)
        assert cfg.translate.enabled is True
        assert cfg.deduplication.semantic_dedup_enabled is True
        assert cfg.deduplication.semantic_dedup_threshold == 0.9
        assert cfg.settings.translate_enabled is True
        assert cfg.settings.semantic_dedup_enabled is True
        assert cfg.settings.semantic_dedup_threshold == 0.9
        await db.close()

    async def test_translate_sources_override_and_fallback(self, tmp_path):
        """translate.sources：面板落表覆盖 yaml；未存过该键时回落 yaml 源列表。"""
        yaml_cfg = FULL_YAML + "\ntranslate:\n  enabled: true\n  sources: [-1001, -1002]\n"
        yp = _write_yaml(tmp_path, yaml_cfg)
        db = await _fresh_db(tmp_path)

        # 未存过 M4 键：源列表回落 yaml，且展示值同步
        cfg1 = await load_runtime_config(db, yp)
        assert cfg1.translate.sources == [-1001, -1002]
        assert cfg1.settings.translate_sources == [-1001, -1002]

        # 面板保存（含 translate_sources 新列表）→ 覆盖 yaml
        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        s = cfg1.settings.model_dump()
        s["translate_sources"] = [-1003, "-1004"]
        await repo.save("system_settings", s)
        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.translate.sources == [-1003, "-1004"]
        assert cfg2.settings.translate_sources == [-1003, "-1004"]
        await db.close()

    async def test_old_db_partial_m4_keys_do_not_wipe_yaml(self, tmp_path):
        """升级防御：旧库仅存过部分 M4 键（如 digest_enabled），缺失的其他键不回退
        默认值清掉 yaml 里仍开启的实验功能与源列表（Codex minor 配套）。"""
        yaml_cfg = (
            FULL_YAML
            + "\ndigest:\n  enabled: true\n  interval_seconds: 900\n"
            + "\ntranslate:\n  enabled: true\n  sources: [-1001]\n"
        )
        yp = _write_yaml(tmp_path, yaml_cfg)
        db = await _fresh_db(tmp_path)
        cfg = await load_runtime_config(db, yp)
        assert cfg.digest.enabled is True
        assert cfg.translate.enabled is True
        assert cfg.translate.sources == [-1001]

        # 模拟旧版面板只保存过 digest_enabled（其他 M4 键未落表）
        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        await repo.save("system_settings", {"digest_enabled": False, "default_target": "0"})
        cfg2 = await load_runtime_config(db, yp)
        # digest 被面板管理过 → 以落表值为准
        assert cfg2.digest.enabled is False
        # translate/semantic 从未被面板管理过 → 保持 yaml 状态（旧实现会误清掉）
        assert cfg2.translate.enabled is True
        assert cfg2.translate.sources == [-1001]
        assert cfg2.deduplication.semantic_dedup_enabled is False
        await db.close()

    async def test_ad_judge_panel_values_override_yaml(self, tmp_path):
        """F13：面板落表 ad_judge_* 覆盖 yaml ad_judge 段；未存过则回落 yaml 并如实展示。"""
        yaml_cfg = FULL_YAML + (
            "\nad_judge:\n  enabled: true\n  base_url: \"http://yaml-host:28880\"\n"
            "  model: \"yaml-model\"\n  threshold: 0.7\n  fuzzy_low: 0.4\n  timeout: 30\n"
        )
        yp = _write_yaml(tmp_path, yaml_cfg)
        db = await _fresh_db(tmp_path)

        # 未存过镜像键：runtime 取 yaml，展示值同步（面板如实显示当前状态）
        cfg1 = await load_runtime_config(db, yp)
        assert cfg1.ad_judge.enabled is True
        assert cfg1.ad_judge.base_url == "http://yaml-host:28880"
        assert cfg1.ad_judge.fuzzy_low == 0.4
        assert cfg1.settings.ad_judge_enabled is True
        assert cfg1.settings.ad_judge_threshold == 0.7
        assert cfg1.settings.ad_judge_model == "yaml-model"

        # 面板保存 → 以落表值为准（热重载生效路径）
        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        s = cfg1.settings.model_dump()
        s.update(
            ad_judge_enabled=False,
            ad_judge_base_url="http://panel-host:28880",
            ad_judge_model="panel-model",
            ad_judge_threshold=0.95,
            ad_judge_fuzzy_low=0.55,
            ad_judge_timeout=12,
        )
        await repo.save("system_settings", s)
        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.ad_judge.enabled is False
        assert cfg2.ad_judge.base_url == "http://panel-host:28880"
        assert cfg2.ad_judge.model == "panel-model"
        assert cfg2.ad_judge.threshold == 0.95
        assert cfg2.ad_judge.fuzzy_low == 0.55
        assert cfg2.ad_judge.timeout == 12
        assert cfg2.settings.ad_judge_enabled is False
        await db.close()

    async def test_ad_judge_partial_keys_do_not_wipe_yaml(self, tmp_path):
        """升级防御：旧库只存过 ad_judge_enabled 时，缺失的参数键不得回退默认值
        清掉 yaml 里的端点/模型（与 M4 部分键同款语义）。"""
        yaml_cfg = FULL_YAML + (
            "\nad_judge:\n  enabled: true\n  base_url: \"http://yaml-host:28880\"\n"
            "  model: \"yaml-model\"\n"
        )
        yp = _write_yaml(tmp_path, yaml_cfg)
        db = await _fresh_db(tmp_path)
        await load_runtime_config(db, yp)

        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        # 模拟旧版面板只保存过开关（其余 ad_judge 键未落表）
        await repo.save(
            "system_settings",
            {"ad_judge_enabled": True, "default_target": "0"},
        )
        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.ad_judge.enabled is True
        # 未被面板管过的参数键 → 保持 yaml 值（旧实现会误清成 AdJudgeConfig 默认）
        assert cfg2.ad_judge.base_url == "http://yaml-host:28880"
        assert cfg2.ad_judge.model == "yaml-model"
        await db.close()

    async def test_ai_content_panel_values_override_yaml(self, tmp_path):
        """F14：面板落表 ai_content_* 覆盖 yaml ai_content 段；未存过则回落 yaml 并如实展示。

        进阶项（sources/json_mode/护栏）刻意不进面板镜像：面板保存不得冲掉它们。
        """
        yaml_cfg = FULL_YAML + (
            '\nai_content:\n  enabled: true\n  base_url: "http://yaml-host:8091/v1"\n'
            '  model: "yaml-model"\n  threshold: 0.7\n  sources: [-100777]\n'
            "  json_mode: false\n  min_ratio: 0.4\n"
        )
        yp = _write_yaml(tmp_path, yaml_cfg)
        db = await _fresh_db(tmp_path)

        # 未存过镜像键：runtime 取 yaml，展示值同步（面板如实显示当前状态）
        cfg1 = await load_runtime_config(db, yp)
        assert cfg1.ai_content.enabled is True
        assert cfg1.ai_content.base_url == "http://yaml-host:8091/v1"
        assert cfg1.ai_content.threshold == 0.7
        assert cfg1.settings.ai_content_enabled is True
        assert cfg1.settings.ai_content_model == "yaml-model"
        assert cfg1.settings.ai_content_threshold == 0.7

        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        s = cfg1.settings.model_dump()
        s.update(
            ai_content_enabled=True,
            ai_content_base_url="http://panel-host:8091/v1",
            ai_content_model="panel-model",
            ai_content_threshold=0.95,
            ai_content_clean_enabled=False,
            ai_content_timeout=12,
        )
        await repo.save("system_settings", s)
        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.ai_content.base_url == "http://panel-host:8091/v1"
        assert cfg2.ai_content.model == "panel-model"
        assert cfg2.ai_content.threshold == 0.95
        assert cfg2.ai_content.clean_enabled is False
        assert cfg2.ai_content.timeout == 12
        assert cfg2.settings.ai_content_enabled is True
        # 面板没管的进阶项 → 保持 yaml
        assert cfg2.ai_content.sources == [-100777]
        assert cfg2.ai_content.json_mode is False
        assert cfg2.ai_content.min_ratio == 0.4
        await db.close()

    async def test_ai_content_untouched_by_ad_judge_panel_save(self, tmp_path):
        """F13/F14 镜像键互不干扰：只存 ad_judge_* 不得把 ai_content 拽回默认值。"""
        yaml_cfg = FULL_YAML + (
            '\nai_content:\n  enabled: true\n  base_url: "http://yaml-host:8091/v1"\n'
            '  model: "yaml-model"\n  threshold: 0.7\n'
        )
        yp = _write_yaml(tmp_path, yaml_cfg)
        db = await _fresh_db(tmp_path)
        cfg1 = await load_runtime_config(db, yp)

        from tg_forwarder.storage.repositories import ConfigRepository

        repo = ConfigRepository(db)
        s = cfg1.settings.model_dump()
        s["ad_judge_enabled"] = True
        await repo.save("system_settings", s)
        cfg2 = await load_runtime_config(db, yp)
        assert cfg2.ad_judge.enabled is True
        assert cfg2.ai_content.enabled is True
        assert cfg2.ai_content.base_url == "http://yaml-host:8091/v1"
        assert cfg2.ai_content.threshold == 0.7
        await db.close()
