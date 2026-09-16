# -*- coding: utf-8 -*-
"""link_checker 策略测试（F3：分域风控/删除前二次复核/delete_marked 档/失败视为有效）。

mock 为主：fake client（get_entity/iter_messages/get_messages/delete/edit），
_check_link_validity 打 patch 成可控结果。
"""
import asyncio
from types import SimpleNamespace

import pytest

from tg_forwarder.config import LinkCheckerConfig, RuntimeConfig
from tg_forwarder.core.link_checker import LinkChecker, extract_links


class _DeadClient:
    """fake TelegramClient：扫描/读取/删除/编辑记录。"""

    def __init__(self, messages):
        # messages: {mid: SimpleNamespace(id, text)}
        self.messages = messages
        self.deleted = []
        self.edited = []

    async def get_entity(self, identifier):
        return SimpleNamespace(id=-100999)

    def iter_messages(self, channel_id, min_id=0):
        async def gen():
            for m in sorted(self.messages.values(), key=lambda x: x.id):
                if m.id > min_id:
                    yield m
        return gen()

    async def get_messages(self, channel_id, ids=None):
        if isinstance(ids, list):
            return [self.messages.get(i) for i in ids]
        return self.messages.get(ids)

    async def delete_messages(self, channel_id, message_ids):
        ids = message_ids if isinstance(message_ids, list) else [message_ids]
        self.deleted.extend(ids)

    async def edit_message(self, channel_id, message_id, new_text):
        self.edited.append(message_id)


def _mk_msg(mid, text):
    return SimpleNamespace(id=mid, text=text)


async def _make_checker(tmp_path, client, mode, recheck=True, protect=None):
    import os

    from tg_forwarder.storage.db import Database

    db = Database(os.path.join(str(tmp_path), "lc.sqlite"))
    await db.open()
    await db.migrate()

    config = RuntimeConfig()
    config.link_checker = LinkCheckerConfig(
        enabled=True, mode=mode, recheck_before_delete=recheck,
        delete_protect_domains=protect or [],
    )
    config.settings.default_target = "-100999"
    return db, LinkChecker(db, client, config)


class TestExtractLinks:
    def test_filters_to_net_disk_domains(self):
        text = "啊 https://pan.quark.cn/s/abc 普通 https://example.com/x"
        links = extract_links(text)
        assert links == ["https://pan.quark.cn/s/abc"]

    def test_dedups_links(self):
        links = extract_links("https://pan.baidu.com/s/1 https://pan.baidu.com/s/1")
        assert links == ["https://pan.baidu.com/s/1"]


class TestCheckLinkValidity:
    async def test_request_error_treated_as_valid(self, tmp_path, monkeypatch):
        """检测请求失败一律视为有效（防误判，F3）。"""
        db, checker = await _make_checker(tmp_path, _DeadClient({}), "edit")
        import httpx

        async def head(*a, **k):
            raise httpx.RequestError("down", request=None)
        monkeypatch.setattr(httpx.AsyncClient, "head", head)
        valid = await checker._check_link_validity("https://pan.quark.cn/s/x")
        assert valid is True
        await db.close()

    async def test_http_error_means_invalid(self, tmp_path, monkeypatch):
        """HTTP >=400 → 无效（可删）。"""
        db, checker = await _make_checker(tmp_path, _DeadClient({}), "edit")
        import httpx

        async def head(*a, **k):
            class _R:
                status_code = 404
            return _R()
        monkeypatch.setattr(httpx.AsyncClient, "head", head)
        assert await checker._check_link_validity("https://pan.quark.cn/s/y") is False
        await db.close()


class TestDeadLinkStrategy:
    async def _run(self, tmp_path, mode, validity_seq, protect=None, recheck=True, messages=None):
        """跑一次 run()；validity_seq: 对每个失效链接依次返回的 is_valid 序列。"""
        msgs = messages or {
            11: _mk_msg(11, "资源 https://pan.quark.cn/s/quark1"),
        }
        client = _DeadClient(msgs)
        db, checker = await _make_checker(tmp_path, client, mode, recheck=recheck, protect=protect)
        seq = iter(validity_seq)

        async def fake_valid(url):
            try:
                return next(seq)
            except StopIteration:
                return True  # 兜底：视为有效
        checker._check_link_validity = fake_valid
        await checker.run()
        await db.close()
        return client

    async def test_edit_mode_marks_all(self, tmp_path):
        """edit：失效链接被标记（保护域不受影响，都标记）。"""
        client = await self._run(tmp_path, "edit", [False])
        assert client.edited == [11]

    async def test_delete_mode_deletes_non_protected(self, tmp_path):
        """delete：非保护域失效链接删除（隔离二次复核）。"""
        client = await self._run(tmp_path, "delete", [False], recheck=False)
        assert client.deleted == [11]

    async def test_protect_domain_marked_not_deleted(self, tmp_path):
        """分域风控：保护域失效链接只标记不删除。"""
        client = await self._run(
            tmp_path, "delete", [False], protect=["pan.quark.cn"], recheck=False
        )
        assert client.deleted == []
        assert client.edited == [11]

    async def test_recheck_saves_recovered_link(self, tmp_path):
        """二次复核：首次失效、复核有效 → 跳过删除（防误删）。"""
        # 首轮 False（判失效），二次复核 True（链接恢复）→ 删除前跳过
        client = await self._run(tmp_path, "delete", [False, True], recheck=True)
        assert client.deleted == []  # 二次复核有效 → 不删

    async def test_delete_marked_only_deletes_marked(self, tmp_path):
        """delete_marked：已标记消息删除，未标记先标记（隔离二次复核）。"""
        msgs = {
            11: _mk_msg(11, "含失效 [链接已失效] https://pan.quark.cn/s/a"),
            12: _mk_msg(12, "未标记 https://pan.quark.cn/s/b"),
            13: _mk_msg(13, "失效未标记 https://pan.quark.cn/s/c"),
        }
        client = await self._run(
            tmp_path, "delete_marked", [False, False, False], recheck=False, messages=msgs
        )
        # 11 已标记 → 删；12/13 未标记 → 先标记
        assert client.deleted == [11]
        assert client.edited == [12, 13]

    async def test_log_mode_no_action(self, tmp_path):
        """log：仅记录，无删除/编辑。"""
        client = await self._run(tmp_path, "log", [False])
        assert client.deleted == []
        assert client.edited == []