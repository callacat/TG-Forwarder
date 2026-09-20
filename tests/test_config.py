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
