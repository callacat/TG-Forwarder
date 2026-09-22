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


def _build_semantic_engine(config):
    """F12 语义去重装配（默认关=不构造；开启时注入外观类，惰性不触发模型下载）。

    main.py 是 F12 唯一生产装配点（Codex critical：先前 Forwarder 从不注入引擎，
    开关开与不开行为完全相同）。引擎内 ensure_ready 惰性加载模型，缺失/加载失败
    自动降级为不启用，不影响既有 dedup 行为。
    """
    dedup_cfg = getattr(config, "deduplication", None)
    if dedup_cfg is None or not getattr(dedup_cfg, "semantic_dedup_enabled", False):
        return None
    from tg_forwarder.core.semantic_dedup import SemanticDedup, SemanticDedupEngine

    # 阈值来自配置（默认 0.85 = SIMILARITY_THRESHOLD；Web 面板实验功能可调）
    threshold = float(getattr(dedup_cfg, "semantic_dedup_threshold", 0.85) or 0.85)
    return SemanticDedup(engine=SemanticDedupEngine(threshold=threshold))


def _build_ad_judge(config):
    """AI 广告判别器（jev）装配：仅 ad_judge.enabled 时构造，否则 None。

    与 _build_semantic_engine 同范式：main.py 唯一生产装配点。构造只持配置
    不发请求（请求在首次 is_ad 时发生），默认关=现网零变化。
    """
    aj = getattr(config, "ad_judge", None)
    if aj is None or not getattr(aj, "enabled", False):
        return None
    from tg_forwarder.core.ad_judge import AdJudge

    return AdJudge(
        base_url=aj.base_url,
        model=aj.model,
        threshold=float(getattr(aj, "threshold", 0.85) or 0.85),
        fuzzy_low=float(getattr(aj, "fuzzy_low", 0.60) or 0.60),
        timeout=float(getattr(aj, "timeout", 20.0) or 20.0),
    )


def _build_digest_pipeline(config, forwarder=None):
    """F10：按给定配置构建 AI digest 管线（默认关=不装配）。

    与 _build_semantic_engine 同类：装配期一次性组件，热重载需按新配置重建
    （Codex major：原只在 cmd_run 启动时构建）。仅在 config.digest.enabled 时
    返回管线；LLMClient 仅持有配置不发起网络请求。
    """
    digest_cfg = getattr(config, "digest", None)
    if digest_cfg is None or not getattr(digest_cfg, "enabled", False):
        return None
    from tg_forwarder.core.digest import DigestPipeline, LLMClient

    pipeline = DigestPipeline(
        llm=LLMClient(
            base_url=digest_cfg.base_url,
            model=digest_cfg.model,
            api_key=getattr(digest_cfg, "api_key", None),
        ),
        now_fn=time.time,
    )
    if forwarder is not None:
        pipeline._fwd = forwarder  # flush 集成（也可显式传）
    return pipeline


def _reconcile_ai_features(forwarder, new_cfg) -> None:
    """F10/F12 热重载对齐：按新配置重建/摘除装配期一次性组件。

    背景（Codex major）：F12 语义引擎与 F10 digest 管线仅在 cmd_run 启动时构建，
    原 update_settings_cb 只换快照不重建 → 面板开启/关闭/改阈值后实际行为不变
    （需重启），与面板「保存后自动热重载生效」声明不符。F11 翻译读快照动态生效，
    无需重建。

    设计：
    - F12 引擎仅在 dedup 开关或阈值变化时重建——无变化保留，不丢已加载模型与内存窗口；
    - F10 管线仅在「关闭→开启」时新建、关闭时摘除——已启用未变化时保留原管线
      与滚动窗口缓冲（普通设置保存不丢摘要进度；间隔改动由快照驱动，新窗口即用新间隔）。
    """
    # --- F12 语义引擎 ---
    dedup = getattr(new_cfg, "deduplication", None)
    want = bool(dedup and getattr(dedup, "semantic_dedup_enabled", False))
    cur = getattr(forwarder, "semantic_engine", None)
    if want:
        new_th = float(getattr(dedup, "semantic_dedup_threshold", 0.85) or 0.85)
        cur_th = None
        if cur is not None:
            try:
                cur_th = float(getattr(cur.engine, "threshold", 0.85))
            except Exception:  # noqa: BLE001 —— 兼容假引擎/测试替身
                cur_th = None
        if cur is None or cur_th != new_th:
            forwarder.semantic_engine = _build_semantic_engine(new_cfg)
            logger.info(f"F12 语义去重引擎已热重载重建（threshold={new_th}）")
    elif cur is not None:
        forwarder.semantic_engine = None
        logger.info("F12 语义去重已热重载关闭（引擎摘除）")

    # --- AI 广告判别器（jev） ---
    aj = getattr(new_cfg, "ad_judge", None)
    want_aj = bool(aj and getattr(aj, "enabled", False))
    cur_aj = getattr(forwarder, "ad_judge", None)
    if want_aj:
        # 重建条件 = 全配置元组变化（Codex minor：原先只比 threshold，其它字段
        # 改动后 /reload 静默无效——ad_judge 仅 yaml 不经面板，/reload 是唯一通道）。
        # base_url 归一化与 AdJudge.__init__ 的 rstrip("/") 对齐，避免无谓重建。
        new_key = (
            str(getattr(aj, "base_url", "") or "").rstrip("/"),
            str(getattr(aj, "model", "") or ""),
            float(getattr(aj, "threshold", 0.85) or 0.85),
            float(getattr(aj, "fuzzy_low", 0.60) or 0.60),
            float(getattr(aj, "timeout", 20.0) or 20.0),
        )
        cur_key = None
        if cur_aj is not None:
            try:
                cur_key = (
                    str(getattr(cur_aj, "base_url", "") or "").rstrip("/"),
                    str(getattr(cur_aj, "model", "") or ""),
                    float(getattr(cur_aj, "threshold", 0.85)),
                    float(getattr(cur_aj, "fuzzy_low", 0.60)),
                    float(getattr(cur_aj, "timeout", 20.0)),
                )
            except Exception:  # noqa: BLE001 —— 兼容假实例/测试替身
                cur_key = None
        if cur_aj is None or cur_key != new_key:
            forwarder.ad_judge = _build_ad_judge(new_cfg)
            logger.info(f"AI 广告判别器已热重载重建（threshold={new_key[2]}）")
    elif cur_aj is not None:
        forwarder.ad_judge = None
        logger.info("AI 广告判别器已热重载关闭（摘除）")

    # --- F10 digest 管线 ---
    new_pipe = _build_digest_pipeline(new_cfg, forwarder)
    if new_pipe is not None:
        if getattr(forwarder, "digest_pipeline", None) is None:
            forwarder.digest_pipeline = new_pipe
            logger.info(
                f"F10 AI digest 已热重载启用: {new_cfg.digest.base_url} / "
                f"{new_cfg.digest.model}, 间隔 {new_cfg.digest.interval_seconds}s"
            )
    elif getattr(forwarder, "digest_pipeline", None) is not None:
        forwarder.digest_pipeline = None
        logger.info("F10 AI digest 已热重载关闭（管线摘除）")


def _sync_web_rules_db(cfg) -> None:
    """把 RuntimeConfig 的 Web 可编辑段同步进 web 层内存 rules_db。"""
    from tg_forwarder.web import server as web_server

    rules_db = web_server.app_state.get("rules_db")
    if rules_db is None:
        logger.warning("rules_db 尚未创建（create_app 未调用），Web 规则库同步被跳过")
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

    # 2. F12 语义去重装配（默认关=不构造；开启时惰性注入 + 启动预热）
    semantic_engine = _build_semantic_engine(config)
    preheat_task = None
    if semantic_engine is not None:
        # 预热：启动协程内线程加载模型，避免装配后首条消息在事件循环内同步加载阻塞
        preheat_task = asyncio.create_task(semantic_engine.ensure_ready())
        logger.info("F12 语义去重已接线，模型预热任务已启动（缺失/失败自动降级）。")

    # 3. 转发引擎（R8：快照原子替换）
    forwarder = Forwarder(db, accounts, semantic_engine, _build_ad_judge(config))
    forwarder.update_snapshot(config)
    forwarder.start_prune_task()  # P8：dedup TTL 清理
    if forwarder.ad_judge is not None:
        logger.info(
            f"AI 广告判别器已接线: {config.ad_judge.base_url} / "
            f"{config.ad_judge.model}, threshold={config.ad_judge.threshold}"
        )

    # F10：AI digest 滚动窗口聚合（默认关=不装配，零影响现网）
    digest_pipeline = _build_digest_pipeline(config, forwarder)
    forwarder.digest_pipeline = digest_pipeline
    if digest_pipeline is not None:
        logger.info(
            f"F10 AI digest 已启用: 端点 {config.digest.base_url}, "
            f"模型 {config.digest.model}, 间隔 {config.digest.interval_seconds}s"
        )

    # 4. 主账号解析源/目标 + 注册事件（保持 v2 行为）
    healthy = accounts.healthy_accounts()
    if healthy:
        await forwarder.resolve_targets(healthy[0])
        forwarder.register_handlers(healthy[0])
    else:
        logger.warning("无可用用户账号，看门狗将在超时后触发退出。")

    # R8 热重载回调：Web 改配置落表成功后 → 重建配置 → 整体替换快照
    # → 重建 F10/F12 装配期一次性组件（Codex major：原只换快照，面板开关/阈值修改需重启才生效）
    async def update_settings_cb() -> None:
        try:
            new_cfg = await load_runtime_config(db, yaml_path)
            forwarder.update_snapshot(new_cfg)
            _sync_web_rules_db(new_cfg)
            _reconcile_ai_features(forwarder, new_cfg)
            logger.info("♻️ Web 配置变更已热重载（快照 + 实验功能组件重建）。")
        except Exception as e:
            logger.error(f"热重载失败: {e}")
            raise

    # 6. 死链检测器实例（/check 命令与 scheduler 共用，避免双实例）
    link_checker = None
    if config.link_checker and config.link_checker.enabled and healthy:
        from tg_forwarder.core.link_checker import LinkChecker

        link_checker = LinkChecker(db, healthy[0], config)

    # 7. Bot 服务（T1：修复 v3 从未接线 BotService；R6 独立凭据；/reload 全链路）
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

    # 8. Web（写配置成功经 bot_notify 推送 admin，T1 通知通道）
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
    # 5. Web 层 rules_db 初始装载：必须紧随 create_app（彼时 app_state["rules_db"]
    #    才被创建）。放在 create_app 之前会被 _sync_web_rules_db 的
    #    `rules_db is None` 早退静默跳过 → 面板黑名单/白名单/过滤显示为空
    #    （运行时过滤正常，纯展示层 bug，t_57f13176）。
    _sync_web_rules_db(config)
    server = run_server(app)

    tasks = [server.serve()]
    if preheat_task is not None:
        tasks.append(preheat_task)

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

        # F10：AI digest 滚动窗口 flush——固定 60s 扫一次，wrapper 读当前管线
        # （Codex major：原仅启用时排程且绑定启动实例 → 面板后开 digest 永不
        #  flush；现关闭态 no-op、热重载开启后即时生效；窗口到期间隔由快照
        #  驱动，面板改间隔无需重排）。
        async def _digest_flush_cb() -> None:
            pipe = forwarder.digest_pipeline
            if pipe is not None:
                await pipe.flush()

        try:
            scheduler.add_job(
                _digest_flush_cb,
                IntervalTrigger(seconds=60),
                name="ai_digest",
            )
            logger.info("F10 AI digest 排程就绪: 每 60s 扫描滚动窗口（按配置间隔出摘要）")
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
