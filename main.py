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

DOCKER_CONTAINER_NAME = "tgf"
START_TIME = datetime.now(timezone.utc)


async def cmd_run(db: Database, yaml_path: str) -> None:
    """正常运行模式：装配全部组件并运行至退出。"""
    from tg_forwarder.core.accounts import AccountManager
    from tg_forwarder.core.forwarder import Forwarder
    from tg_forwarder.core.supervision import Supervisor
    from tg_forwarder.web.server import create_app
    from tg_forwarder.web.uvicorn_runner import run_server

    config = await load_runtime_config(db, yaml_path)

    # 1. 账号生命周期（R1：指数退避重试 + proxy 降级链；P9：无 session 不交互登录）
    accounts = AccountManager(db)
    await accounts.start(config)

    # 2. 看门狗（R1：无可用账号 > N 分钟 → 结构化告警 + 非零退出，假活根治）
    supervisor = Supervisor(accounts, config)

    # 3. 转发引擎（R8：快照原子替换）
    forwarder = Forwarder(db, accounts)
    forwarder.update_snapshot(config)

    # 4. 主账号解析源/目标（保持 v2 行为）
    healthy = accounts.healthy_accounts()
    if healthy:
        await forwarder.resolve_targets(healthy[0])
    else:
        logger.warning("无可用用户账号，看门狗将在超时后触发退出。")

    # 5. Web + Bot
    app = create_app(
        db=db,
        get_snapshot=forwarder.get_snapshot,
        account_manager=accounts,
        forwarder=forwarder,
    )
    server = run_server(app)

    tasks = [server.serve(), supervisor.run()]
    for client in healthy:
        tasks.append(client.run_until_disconnected())

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
