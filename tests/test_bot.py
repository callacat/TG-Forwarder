# -*- coding: utf-8 -*-
"""TG-Forwarder v3 Bot 层测试。

mock TelegramClient，不真实连 Telegram。覆盖：
- is_admin 门禁（非 admin 拒绝 / admin 放行 / 群内匿名拒绝）
- 独立凭据回落逻辑（R6/P2）：bot_api_id 为空用 accounts[0]，非空用独立值
- notify_admin 发所有管理员
- 命令菜单 token 签名去重（bot_command_setup）
"""
import asyncio
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tg_forwarder.bot.service import BotService


# ---------------------------------------------------------------------------
# Mock 构造
# ---------------------------------------------------------------------------

class MockBotClient:
    """mock TelegramClient：记录调用，不真实连网。"""

    def __init__(self, connected=True):
        self._connected = connected
        self.sent_messages = []  # (chat_id, text)
        self.tl_calls = []  # SetBotCommandsRequest 调用
        self.handlers = []  # 注册的 events.NewMessage

    def is_connected(self):
        return self._connected

    async def send_message(self, chat_id, text):
        self.sent_messages.append((chat_id, text))

    def on(self, event):
        """mock 事件注册：telethon 把 pattern 编译成 re.compile(p).match 绑定方法，
        需从 __self__（编译后正则）取回 .pattern 原字符串。"""
        p = getattr(event, "pattern", None)
        if callable(p) and hasattr(p, "__self__"):
            p = getattr(p.__self__, "pattern", None)

        def deco(fn):
            self.handlers.append((p, fn))
            return fn

        return deco

    async def __call__(self, request):
        self.tl_calls.append(request)
        # 模拟 zh-hant 等语言代码可能报错
        if getattr(request, "lang_code", "") == "zh-hant":
            raise ValueError("lang_code not supported (mock)")


class MockAccount:
    def __init__(self, api_id, api_hash, session_name="acc1"):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_name = session_name
        self.enabled = True


class MockRuntimeConfig:
    """模拟 RuntimeConfig（接口约定字段）。"""

    def __init__(self, accounts, bot_api_id=None, bot_api_hash=None, bot_token="12345:AAABBB", admin_ids=None):
        bs = type("BS", (), {})
        bs.enabled = True
        bs.bot_token = bot_token
        bs.admin_user_ids = admin_ids or [111]
        bs.bot_api_id = bot_api_id
        bs.bot_api_hash = bot_api_hash

        self.bot_service = bs()
        self.accounts = accounts


class MockEvent:
    def __init__(self, sender_id, is_group=False):
        self.sender_id = sender_id
        self.is_group = is_group


def make_service(runtime_config=None, bot=None, **kwargs):
    if runtime_config is None:
        runtime_config = MockRuntimeConfig(
            accounts=[MockAccount(11111, "hash_acc1")], admin_ids=[111, 222]
        )
    if bot is None:
        bot = MockBotClient()
    service = BotService(
        config=runtime_config,
        bot_client=bot,
        **kwargs,
    )
    return service, bot, runtime_config


# ---------------------------------------------------------------------------
# is_admin 门禁
# ---------------------------------------------------------------------------

class TestIsAdmin:
    def test_admin_allowed(self):
        service, _, _ = make_service()
        assert service.is_admin(MockEvent(111)) is True
        assert service.is_admin(MockEvent(222)) is True

    def test_non_admin_rejected(self):
        service, _, _ = make_service()
        assert service.is_admin(MockEvent(999)) is False

    def test_anonymous_in_group_rejected(self):
        """群内 sender_id 为 None（匿名）时拒绝（v2 对齐）。"""
        service, _, _ = make_service()
        assert service.is_admin(MockEvent(None, is_group=True)) is False

    def test_dynamic_admin_ids_hot_reload(self):
        """forwarder.config 支持热重载时取最新管理员列表。"""
        service, _, _ = make_service()

        class _Fwd:
            class config:
                class bot_service:
                    admin_user_ids = [777]

        service.forwarder = _Fwd()
        assert service.is_admin(MockEvent(777)) is True
        assert service.is_admin(MockEvent(111)) is False


# ---------------------------------------------------------------------------
# 独立凭据回落（R6/P2 修复）
# ---------------------------------------------------------------------------

class TestBotCredentials:
    def test_independent_credentials_used(self):
        """bot_api_id/hash 非空时用独立值。"""
        rc = MockRuntimeConfig(
            accounts=[MockAccount(11111, "hash_acc1")],
            bot_api_id=99999,
            bot_api_hash="hash_bot",
        )
        service, _, _ = make_service(runtime_config=rc)
        api_id, api_hash = service.resolve_bot_credentials(rc)
        assert api_id == 99999
        assert api_hash == "hash_bot"

    def test_fallback_to_accounts0(self):
        """bot_api_id/hash 为空时回落 accounts[0]（v2 P2 痛点修复）。"""
        rc = MockRuntimeConfig(
            accounts=[MockAccount(11111, "hash_acc1")],
            bot_api_id=None,
            bot_api_hash=None,
        )
        service, _, _ = make_service(runtime_config=rc)
        api_id, api_hash = service.resolve_bot_credentials(rc)
        assert api_id == 11111
        assert api_hash == "hash_acc1"

    def test_fallback_empty_string_counts_as_empty(self):
        """bot_api_id/hash 为空字符串视为空，仍回落 accounts[0]。"""
        rc = MockRuntimeConfig(
            accounts=[MockAccount(11111, "hash_acc1")],
            bot_api_id="",
            bot_api_hash="",
        )
        service, _, _ = make_service(runtime_config=rc)
        api_id, api_hash = service.resolve_bot_credentials(rc)
        assert api_id == 11111
        assert api_hash == "hash_acc1"

    def test_no_accounts_no_credentials(self):
        rc = MockRuntimeConfig(accounts=[], bot_api_id=None, bot_api_hash=None)
        service, _, _ = make_service(runtime_config=rc)
        api_id, api_hash = service.resolve_bot_credentials(rc)
        assert api_id is None
        assert api_hash is None

    def test_no_runtime_config(self):
        service, _, _ = make_service()
        # 无 runtime_config 传入时，走 bot_service 段（空）+ 无 accounts
        api_id, api_hash = service.resolve_bot_credentials(None)
        assert api_id is None


# ---------------------------------------------------------------------------
# notify_admin
# ---------------------------------------------------------------------------

class TestNotifyAdmin:
    def test_sends_to_all_admins(self):
        bot = MockBotClient(connected=True)
        service, _, _ = make_service(bot=bot)
        asyncio.new_event_loop().run_until_complete(service.notify_admin("hello"))
        assert len(bot.sent_messages) == 2
        assert bot.sent_messages[0][0] == 111
        assert bot.sent_messages[1][0] == 222

    def test_skips_when_not_connected(self):
        bot = MockBotClient(connected=False)
        service, _, _ = make_service(bot=bot)
        asyncio.new_event_loop().run_until_complete(service.notify_admin("hello"))
        assert bot.sent_messages == []

    def test_partial_failure_continues(self):
        """单个管理员发送失败不影响其余。"""
        bot = MockBotClient(connected=True)

        async def fail_send(chat_id, text):
            if chat_id == 111:
                raise RuntimeError("mock send failure")
            bot.sent_messages.append((chat_id, text))

        bot.send_message = fail_send
        service, _, _ = make_service(bot=bot)
        asyncio.new_event_loop().run_until_complete(service.notify_admin("hello"))
        assert bot.sent_messages == [(222, "hello")]


# ---------------------------------------------------------------------------
# register_commands + 命令菜单
# ---------------------------------------------------------------------------

class TestCommandMenu:
    def test_register_commands_adds_handlers(self):
        bot = MockBotClient()
        service, bot, _ = make_service(bot=bot)
        service.register_commands()
        patterns = [p for p, _ in bot.handlers]
        for cmd in ["/start", "/status", "/reload", "/check", "/ids"]:
            assert cmd in patterns, f"缺少命令 {cmd}"

    def test_command_menu_token_signature_dedup(self):
        """首次运行设置菜单并写 bot_command_setup；签名一致时跳过。"""
        import json

        bot = MockBotClient()
        service, bot, rc = make_service(bot=bot)

        # 内存版 app_config
        stored = {}

        async def get_cfg(key):
            return stored.get(key, {})

        async def save_cfg(key, data):
            stored[key] = data

        service._get_app_config = get_cfg
        service._save_app_config = save_cfg

        # 首次：应设置菜单（1 次默认 + zh/zh-hans 成功、zh-hant 失败）
        asyncio.new_event_loop().run_until_complete(service.setup_command_menu())
        assert len(bot.tl_calls) >= 1
        assert "bot_command_setup" in stored
        expected_sig = rc.bot_service.bot_token[:10] + "***" + rc.bot_service.bot_token[-5:]
        assert stored["bot_command_setup"]["token_signature"] == expected_sig

        # 第二次：签名一致，跳过（调用数不变）
        calls_before = len(bot.tl_calls)
        asyncio.new_event_loop().run_until_complete(service.setup_command_menu())
        assert len(bot.tl_calls) == calls_before

    def test_command_menu_skipped_without_token(self):
        """无 bot_token 时跳过菜单设置。"""
        bot = MockBotClient()
        rc = MockRuntimeConfig(accounts=[MockAccount(1, "h")], bot_token="")
        service, bot, _ = make_service(runtime_config=rc, bot=bot)
        asyncio.new_event_loop().run_until_complete(service.setup_command_menu())
        assert bot.tl_calls == []


# ---------------------------------------------------------------------------
# /status 处理器（admin 门禁内）
# ---------------------------------------------------------------------------

class TestStatusHandler:
    def _extract_handler(self, bot, pattern):
        for p, fn in bot.handlers:
            if p == pattern:
                return fn
        return None

    def test_status_handler_replies_stats(self):
        bot = MockBotClient()
        service, bot, rc = make_service(bot=bot)
        service.register_commands()
        handler = self._extract_handler(bot, "/status")
        assert handler is not None

        replies = []

        class _Msg:
            async def reply(self, text):
                replies.append(text)

        event = MockEvent(111)
        event.reply = _Msg().reply

        asyncio.new_event_loop().run_until_complete(handler(event))
        assert len(replies) == 1
        assert "核心指标" in replies[0]
        assert "规则统计" in replies[0]

    def test_status_handler_non_admin_silent(self):
        bot = MockBotClient()
        service, bot, _ = make_service(bot=bot)
        service.register_commands()
        handler = self._extract_handler(bot, "/status")

        replies = []

        async def reply(text):
            replies.append(text)

        event = MockEvent(999)  # 非 admin
        event.reply = reply

        asyncio.new_event_loop().run_until_complete(handler(event))
        assert replies == []
