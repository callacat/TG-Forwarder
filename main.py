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

    # 5. Web + 看门狗（R1 假活根治）
    app = create_app(
        db=db,
        config_repo=config_repo,
        source_repo=source_repo,
        rule_repo=rule_repo,
        get_snapshot=forwarder.get_snapshot,
        update_settings=update_settings_cb,
        account_manager=accounts,
        forwarder=forwarder,
    )
    server = run_server(app)

    # 6. Bot 服务（R6：独立凭据可选；告警推送 + 运维指令）
    tasks = [server.serve()]
    supervisor = Supervisor(accounts, config)
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

        # 死链检测（仅启用时）
        if config.link_checker.enabled:
            from tg_forwarder.core.link_checker import LinkChecker

            checker = LinkChecker(db, healthy[0], config)
            try:
                scheduler.add_job(
                    checker.run,
                    CronTrigger.from_crontab(config.link_checker.schedule),
                    name="link_checker",
                )
            except Exception as e:
                logger.warning(f"死链检测排程失败（忽略）: {e}")

        scheduler.start()

    for client in healthy:
        tasks.append(client.run_until_disconnected())

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
