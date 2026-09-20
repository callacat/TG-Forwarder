"""TG-Forwarder v3 入口：装配 + 生命周期（启动/优雅退出）。

模式：
  run       - 正常运行（默认）
  checklinks- 立即跑一次死链检测后退出
  export    - 导出会话可见的频道/群 ID 列表
"""
import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timezone

from loguru import logger

from tg_forwarder.config import load_runtime_config
from tg_forwarder.storage.db import Database
from tg_forwarder.storage.repositories import (
    ConfigRepository,
    RuleRepository,
    SourceRepository,
)

DOCKER_CONTAINER_NAME = "tgf"
START_TIME = datetime.now(timezone.utc)


def _sync_web_rules_db(cfg) -> None:
    """把 RuntimeConfig 的 Web 可编辑段同步进 web 层内存 rules_db。"""
    from tg_forwarder.web import server as web_server

    rules_db = web_server.app_state.get("rules_db")
    if rules_db is None:
        return
    rules_db.sources = cfg.sources
    rules_db.distribution_rules = cfg.distribution_rules
    rules_db.settings = cfg.settings
    rules_db.ad_filter = cfg.ad_filter
    rules_db.whitelist = cfg.whitelist
    rules_db.content_filter = cfg.content_filter
    rules_db.replacements = dict(cfg.replacements)


def resolve_bot_credentials(config):
    """R6 凭据解析链：bot_service.bot_api_id/hash 优先，缺省回落 accounts[0]。"""
    bs = getattr(config, "bot_service", None)
    if bs is not None:
        api_id = getattr(bs, "bot_api_id", None)
        api_hash = getattr(bs, "bot_api_hash", None)
        if api_id and api_hash:
            return api_id, api_hash
    accounts = getattr(config, "accounts", None) or []
    if accounts:
        return accounts[0].api_id, accounts[0].api_hash
    return None, None


async def _initialize_bot(
    config, forwarder, accounts, link_checker, db, repos,
    reload_func, get_clients_func,
):
    """装配 Bot 运维服务（T1：修复 v3 主流程从未实例化 BotService）。

    v2 initialize_bot 等价：连接 bot TelegramClient（bot_token + R6 凭据链）→
    BotService(...) → register_commands() → setup_command_menu()。
    Bot 是运维/告警通道，非转发核心：连接失败仅告警、不阻断进程（对齐 v2）。
    返回 (bot_service, bot_client)，未启用/失败返回 (None, None)。
    """
    from telethon import TelegramClient

    from tg_forwarder.bot.service import BotService

    bs = getattr(config, "bot_service", None)
    if bs is None or not getattr(bs, "enabled", False):
        return None, None
    if not getattr(bs, "bot_token", "") or bs.bot_token == "YOUR_BOT_TOKEN_HERE":
        logger.info("Bot 服务已启用但 token 未配置，跳过。")
        return None, None

    api_id, api_hash = resolve_bot_credentials(config)
    if not api_id or not api_hash:
        logger.error("❌ Bot 无可用凭据（独立与 accounts[0] 皆缺），跳过 Bot。")
        return None, None

    proxy = config.proxy.get_telethon_proxy() if getattr(config, "proxy", None) else None
    bot_client = TelegramClient(None, api_id, api_hash, proxy=proxy, use_ipv6=False)
    try:
        await asyncio.wait_for(bot_client.start(bot_token=bs.bot_token), 45)
        me = await bot_client.get_me()
        logger.success(f"✅ Bot 登录成功: @{getattr(me, 'username', '?')}")
    except Exception as e:
        logger.error(f"❌ Bot 启动失败（不阻断转发核心，运维指令将不可用）: {e}")
        return None, None

    bot_service = BotService(
        config,
        bot_client,
        account_manager=accounts,
        forwarder=forwarder,
        reload_func=reload_func,
        get_clients_func=get_clients_func,
        db=db,
        repos=repos,
        link_checker=link_checker,
    )
    bot_service.register_commands()
    await bot_service.setup_command_menu()
    logger.success("🤖 Bot 运维服务已装配（/status /reload /ids /check）。")
    return bot_service, bot_client


async def _supervised_poll(client, accounts, name, *, is_bot=False) -> None:
    """客户端轮询包装（R1 关键）：run_until_disconnected 的异常不得外泄给 gather。

    旧版直接 gather(client.run_until_disconnected())：telethon 重连耗尽抛
    ConnectionError → gather 传播 → 进程从**异常旁路**退出，绕过 Supervisor 的
    [account_all_dead] 结构化告警 + on_critical（老马断网注入复验抓到：exit1 有、
    JSON/CRITICAL 全无）。改法：异常/断开 → 标记该账号 unhealthy → 交维护循环重连
    → 5s 后再接轮询；网络长期不可用时由看门狗计时走**唯一权威退出路径**（带告警）。
    """
    while True:
        err = "poll ended (disconnected)"
        try:
            await client.run_until_disconnected()
            logger.warning(f"⚠️ 账号 {name} run_until_disconnected 返回（连接断开）")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            logger.error(f"❌ 账号 {name} 轮询循环异常退出: {err}")
        if not is_bot and accounts is not None:
            accounts.mark_unhealthy(name, err)
        # 等维护循环把连接重建后再挂回轮询；持续失败则由看门狗结构化退出
        await asyncio.sleep(5)


async def cmd_run(db: Database, yaml_path: str) -> None:
    """正常运行模式：装配全部组件并运行至退出。"""
    from tg_forwarder.core.accounts import AccountManager
    from tg_forwarder.core.forwarder import Forwarder
    from tg_forwarder.core.supervision import Supervisor
    from tg_forwarder.web.server import create_app
    from tg_forwarder.web.uvicorn_runner import run_server

    config = await load_runtime_config(db, yaml_path)

    # 仓储（Web 写操作的落表通道）
    config_repo = ConfigRepository(db)
    source_repo = SourceRepository(db)
    rule_repo = RuleRepository(db)

    # 1. 账号生命周期（R1：指数退避重试 + proxy 降级链；P9：无 session 不交互登录）
    accounts = AccountManager(db)
    await accounts.start(config)

    # 2. 转发引擎（R8：快照原子替换）
    forwarder = Forwarder(db, accounts)
    forwarder.update_snapshot(config)
    forwarder.start_prune_task()  # P8：dedup TTL 清理

    # F10：AI digest 滚动窗口聚合（默认关=不装配，零影响现网）
    digest_pipeline = None
    if (
        config.digest is not None
        and getattr(config.digest, "enabled", False)
    ):
        from tg_forwarder.core.digest import DigestPipeline, LLMClient

        llm_cfg = config.digest
        digest_pipeline = DigestPipeline(
            llm=LLMClient(
                base_url=llm_cfg.base_url,
                model=llm_cfg.model,
                api_key=getattr(llm_cfg, "api_key", None),
            ),
            now_fn=time.time,
        )
        digest_pipeline._fwd = forwarder  # flush 集成（也可显式传）
        forwarder.digest_pipeline = digest_pipeline
        logger.info(
            f"F10 AI digest 已启用: 端点 {llm_cfg.base_url}, "
            f"模型 {llm_cfg.model}, 间隔 {llm_cfg.interval_seconds}s"
        )

    # 3. 主账号解析源/目标 + 注册事件（保持 v2 行为）
    healthy = accounts.healthy_accounts()
    if healthy:
        await forwarder.resolve_targets(healthy[0])
        forwarder.register_handlers(healthy[0])
    else:
        logger.warning("无可用用户账号，看门狗将在超时后触发退出。")

    # 4. Web 层 rules_db 初始装载
    _sync_web_rules_db(config)

    # R8 热重载回调：Web 改配置落表成功后 → 重建配置 → 整体替换快照
    async def update_settings_cb() -> None:
        try:
            new_cfg = await load_runtime_config(db, yaml_path)
            forwarder.update_snapshot(new_cfg)
            _sync_web_rules_db(new_cfg)
            logger.info("♻️ Web 配置变更已热重载（快照整体替换）。")
        except Exception as e:
            logger.error(f"热重载失败: {e}")
            raise

    # 5. 死链检测器实例（/check 命令与 scheduler 共用，避免双实例）
    link_checker = None
    if config.link_checker and config.link_checker.enabled and healthy:
        from tg_forwarder.core.link_checker import LinkChecker

        link_checker = LinkChecker(db, healthy[0], config)

    # 6. Bot 服务（T1：修复 v3 从未接线 BotService；R6 独立凭据；/reload 全链路）
    async def reload_func() -> str:
        await update_settings_cb()  # 热重载全链路：load→update_snapshot→_sync_web
        return "配置与规则已从 app_config 表重载并生效。"

    bot_service, bot_client = await _initialize_bot(
        config,
        forwarder,
        accounts,
        link_checker,
        db,
        (config_repo, source_repo, rule_repo),
        reload_func,
        lambda: accounts.healthy_accounts(),
    )
    bot_notify = bot_service.notify_admin if bot_service else None

    # 7. Web（写配置成功经 bot_notify 推送 admin，T1 通知通道）
    app = create_app(
        db=db,
        config_repo=config_repo,
        source_repo=source_repo,
        rule_repo=rule_repo,
        get_snapshot=forwarder.get_snapshot,
        update_settings=update_settings_cb,
        account_manager=accounts,
        forwarder=forwarder,
        bot_notifier=bot_notify,
    )
    server = run_server(app)

    tasks = [server.serve()]

    # 运行期维护循环（R2/P2 修复）：探活断连账号→立即置 unhealthy→后台重连。
    # 与看门狗并存：维护循环把真实连接态写回 state，看门狗据此判"无可用账号"超时退出。
    # 旧版此循环未接线（maintain_once 零调用）→ 运行期假活防线失效。
    tasks.append(
        accounts.maintenance_loop(
            config.watchdog.maintain_interval_seconds,
            probe_timeout=config.watchdog.probe_timeout_seconds,
            stale_seconds=config.watchdog.stale_seconds,
        )
    )

    # 看门狗（R1）：无可用账号超阈值 → on_critical 经 Bot 触达 admin → 非零退出
    supervisor = Supervisor(accounts, config, on_critical=bot_notify)
    tasks.append(supervisor.run())

    # 定时任务：catchup 兜底扫描（P1 修复）+ 死链检测（v2 对齐 cron）
    if healthy:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger

        scheduler = AsyncIOScheduler(timezone="UTC")

        # catchup：每 300s 增量补齐事件漏送（对齐 v2 IntervalTrigger 300s）
        try:
            scheduler.add_job(
                forwarder.catchup_once,
                IntervalTrigger(
                    seconds=Forwarder._CATCHUP_INTERVAL_SECONDS
                ),
                name="catchup",
            )
            logger.info(
                f"catchup 兜底扫描已排程: 每 {Forwarder._CATCHUP_INTERVAL_SECONDS}s"
            )
        except Exception as e:
            logger.warning(f"catchup 排程失败（忽略）: {e}")

        # F10：AI digest 滚动窗口 flush（仅启用时排程；间隔取配置）
        if digest_pipeline is not None:
            try:
                digest_interval = max(
                    int(getattr(config.digest, "interval_seconds", 1800) or 1800), 1
                )
                scheduler.add_job(
                    digest_pipeline.flush,
                    IntervalTrigger(seconds=digest_interval),
                    name="ai_digest",
                )
                logger.info(
                    f"F10 AI digest 已排程: 每 {digest_interval}s 滚动窗口出摘要"
                )
            except Exception as e:
                logger.warning(f"F10 digest 排程失败（忽略）: {e}")

        # 死链检测（复用实例）
        if link_checker is not None:
            try:
                scheduler.add_job(
                    link_checker.run,
                    CronTrigger.from_crontab(config.link_checker.schedule),
                    name="link_checker",
                )
            except Exception as e:
                logger.warning(f"死链检测排程失败（忽略）: {e}")

        scheduler.start()

    for client in healthy:
        client_name = getattr(client, "session_name_for_forwarder", "client")
        tasks.append(_supervised_poll(client, accounts, client_name))
    if bot_client is not None and bot_client.is_connected():
        tasks.append(_supervised_poll(bot_client, None, "bot", is_bot=True))

    logger.success("🚀 TG-Forwarder v3 就绪。Web UI: http://localhost:8080")
    try:
        await asyncio.gather(*tasks)
    finally:
        await accounts.stop()


async def cmd_checklinks(db: Database, yaml_path: str) -> None:
    """立即跑一次死链检测。"""
    from tg_forwarder.core.accounts import AccountManager
    from tg_forwarder.core.link_checker import LinkChecker

    config = await load_runtime_config(db, yaml_path)
    accounts = AccountManager(db)
    await accounts.start(config)
    healthy = accounts.healthy_accounts()
    if not healthy:
        logger.error("无可用账号，无法运行死链检测。")
        sys.exit(1)
    checker = LinkChecker(db, healthy[0], config)
    await checker.run()
    await accounts.stop()


async def cmd_export(db: Database, yaml_path: str) -> None:
    """导出会话可见频道/群 ID。"""
    from tg_forwarder.core.accounts import AccountManager

    config = await load_runtime_config(db, yaml_path)
    accounts = AccountManager(db)
    await accounts.start(config)
    healthy = accounts.healthy_accounts()
    if healthy:
        async for d in healthy[0].iter_dialogs():
            if d.is_channel or d.is_group:
                print(f"{d.id:<20} | {d.title}")
    await accounts.stop()


async def main() -> None:
    parser = argparse.ArgumentParser(prog="tg-forwarder")
    parser.add_argument("mode", choices=["run", "checklinks", "export"], nargs="?", default="run")
    parser.add_argument("-c", "--config", default="/app/config.yaml")
    args = parser.parse_args()

    data_dir = os.environ.get("TGF_DATA_DIR", "/app/data")
    os.makedirs(data_dir, exist_ok=True)

    db = Database(os.path.join(data_dir, "forwarder.sqlite"))
    await db.open()
    try:
        # 迁移框架：v2 现网库直接挂载 → 自动迁移到 v3 schema（R3）
        await db.migrate()

        if args.mode == "run":
            await cmd_run(db, args.config)
        elif args.mode == "checklinks":
            await cmd_checklinks(db, args.config)
        elif args.mode == "export":
            await cmd_export(db, args.config)
    finally:
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
