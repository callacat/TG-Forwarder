# -*- coding: utf-8 -*-
"""版本号单一事实源（tg_forwarder/version.py）测试。

面板版本号必须与 git tag 一致（验收基线），故这里既验解析优先级，
也验「无 env 注入时 = git describe 归一化结果」这条一致性契约。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_forwarder import version as version_mod


class TestResolveVersion:
    def test_env_wins_and_strips_v(self, monkeypatch):
        monkeypatch.setenv(version_mod.ENV_KEY, "v3.0.0-rc.6")
        assert version_mod.resolve_version() == "3.0.0-rc.6"

    def test_env_without_v_prefix_untouched(self, monkeypatch):
        monkeypatch.setenv(version_mod.ENV_KEY, "3.0.0-rc.6")
        assert version_mod.resolve_version() == "3.0.0-rc.6"

    def test_blank_env_falls_back_to_git(self, monkeypatch):
        monkeypatch.setenv(version_mod.ENV_KEY, "   ")
        monkeypatch.setattr(version_mod, "_from_git", lambda: "v9.9.9-test")
        assert version_mod.resolve_version() == "9.9.9-test"

    def test_no_env_no_git_uses_dev_placeholder(self, monkeypatch):
        """既无注入又无 git：回落 0.0.0-dev（诚实占位，不冒充具体版本）。"""
        monkeypatch.delenv(version_mod.ENV_KEY, raising=False)
        monkeypatch.setattr(version_mod, "_from_git", lambda: "")
        assert version_mod.resolve_version() == "0.0.0-dev"

    def test_from_git_never_raises(self):
        """无 git/非仓库/超时一律返回 str（空串），不向上抛。"""
        assert isinstance(version_mod._from_git(), str)

    def test_matches_git_tag_when_no_env(self, monkeypatch):
        """单一事实源一致性：无 env 注入时，版本号 == git describe 归一化结果。"""
        monkeypatch.delenv(version_mod.ENV_KEY, raising=False)
        raw = version_mod._from_git()
        if not raw:
            pytest.skip("当前环境无 git 或非仓库（容器内运行时的正常形态）")
        expected = raw[1:] if raw.startswith("v") else raw
        assert version_mod.resolve_version() == expected
