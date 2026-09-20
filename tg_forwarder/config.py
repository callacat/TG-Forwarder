# -*- coding: utf-8 -*-
"""单一配置源（R4/P4）：app_config 表为运行时权威，yaml 只做首次 bootstrap。

- v2 痛点 P4（yaml + rules_db.json 双源漂移）的根治：
  Web 可编辑配置（settings/ad_filter/whitelist/content_filter/replacements/
  sources/rules）全部以 app_config 表与 sources/rules 表为准；
  yaml 仅在表为空时首次导入（bootstrap），之后不再读 yaml 里的规则段。
- 账号/代理/Web 密码等基础设施配置仍以 yaml 为源（无人值守场景的凭据入口）。
- RuntimeConfig 整体替换即热重载（R8）：处理消息持有旧引用，无半新半旧窗口。
"""
import os
from typing import Any, Dict, List, Optional, Union

import yaml
from pydantic import BaseModel, Field, field_validator

from loguru import logger

from tg_forwarder.storage.db import Database
from tg_forwarder.storage.repositories import (
    ConfigRepository,
    RuleRepository,
    SourceRepository,
)

# ---------------------------------------------------------------------------
# 基础设施配置模型（yaml 源）
# ---------------------------------------------------------------------------


class LoggingLevelConfig(BaseModel):
    app: str = "INFO"
    telethon: str = "WARNING"


class WebUIConfig(BaseModel):
    # 支持 ``sha256$<hex>`` 哈希形态（P5 加固），兼容旧明文
    password: str = "default_password_please_change"


class ProxyConfig(BaseModel):
    enabled: bool = False
    proxy_type: str = "socks5"
    addr: str = "127.0.0.1"
    port: int = 1080
    username: Optional[str] = None
    password: Optional[str] = None

    def get_telethon_proxy(self):
        """Telethon proxy tuple（python-socks 安装时生效）。"""
        if not self.enabled:
            return None
        return (self.proxy_type, self.addr, self.port, True, self.username, self.password)


class AccountConfig(BaseModel):
    api_id: int
    api_hash: str
    session_name: str
    enabled: bool = True

    @field_validator("session_name")
    @classmethod
    def check_session_name(cls, v: str) -> str:
        if not v:
            raise ValueError("必须提供 session_name (会话文件)。")
        if "/" in v or "\\" in v:
            raise ValueError("session_name 不能包含路径分隔符。")
        return v


class BotServiceConfig(BaseModel):
    enabled: bool = False
    bot_token: str = "YOUR_BOT_TOKEN_HERE"
    admin_user_ids: List[int] = Field(default_factory=list)
    # R6/P2：Bot 独立凭据（缺省回落 accounts[0]，显式配置优先）
    bot_api_id: Optional[int] = None
    bot_api_hash: Optional[str] = None


class DigestConfig(BaseModel):
    """F10 AI digest 配置（yaml 基础设施段；默认关=现网行为零变化）。

    - enabled: 是否启用 AI 摘要聚合（全局开关；源的 digest_enabled 为 per 源开关，AND）；
    - interval_seconds: 滚动窗口间隔（默认 1800s=30min）；
    - base_url / model: OpenAI 兼容端点（axonhub 默认，免费 glm-5.3-flash）；
    - api_key: 可选 Bearer 密钥（端点无需鉴权时留空）。
    """

    enabled: bool = False
    interval_seconds: int = 1800
    base_url: str = "http://100.64.0.2:8091/v1"
    model: str = "glm-5.3-flash"
    api_key: Optional[str] = None


class LinkCheckerConfig(BaseModel):
    """死链检测器（对齐 v2）+ F3 安全策略（默认开/默认向后兼容）。

    - mode：log(仅记录)/edit(仅标记)/delete(删除)/delete_marked(只删已标记)；
      F3 三档=仅标记(edit)/删除(delete)/只删已标记(delete_marked)，log 为历史占位；
    - recheck_before_delete：删除类模式删除前二次复核（防网络瞬断误删，默认开）；
    - delete_protect_domains：分域风控——这些域的失效链接只标记不自动删除
      （默认 123 盘：HEAD 不稳定易误判，单独处理）。
    """
    enabled: bool = False
    mode: str = "log"
    schedule: str = "0 3 * * *"
    recheck_before_delete: bool = True
    delete_protect_domains: List[str] = ["123pan.com"]

    @field_validator("mode")
    @classmethod
    def check_mode(cls, v):
        if v not in ["log", "edit", "delete", "delete_marked"]:
            raise ValueError("mode 必须是 'log'/'edit'/'delete'/'delete_marked'")
        return v


class DeliveryConfig(BaseModel):
    """F9 转发出口（delivery）抽象：外部可选出口（webhook/RSS/Apprise）。

    默认关（enabled=False，无后端）→ 现网行为零变化。
    - enabled：是否启用 delivery 出口；
    - webhooks：[{url, headers?, timeout?}] 列表，每项一个 webhook 后端。
    RSS/Apprise 后端待接入（同一 DeliveryBackend 抽象）。
    """
    enabled: bool = False
    webhooks: List[Dict[str, Any]] = Field(default_factory=list)


class ForwardingConfig(BaseModel):
    mode: str = "forward"
    forward_new_only: bool = True
    mark_as_read: bool = False
    mark_target_as_read: bool = False

    @field_validator("mode")
    @classmethod
    def check_mode(cls, v):
        if v not in ["forward", "copy"]:
            raise ValueError("mode 必须是 'forward' 或 'copy'")
        return v


class TranslateConfig(BaseModel):
    """F11 AI 翻译（同 axonhub OpenAI 兼容端点；默认关=现网行为零变化）。

    - enabled：全局开关，默认关；
    - sources：仅对列表内的源生效（按 resolved_id 匹配）；
    - endpoint/api_key_env/model：OpenAI 兼容端点（默认 axonhub glm-5.3-flash）；
    - api_key_env：从 OS 环境变量读取 API key（为空则翻译静默降级，不阻塞）；
    - timeout_seconds：单次请求超时（默认 8s）；
    - cache_max_entries/cache_ttl_seconds：结果缓存，避免重复计费。
    """

    enabled: bool = False
    sources: List[Union[int, str]] = Field(default_factory=list)
    endpoint: str = "http://100.64.0.2:8091/v1"
    api_key_env: str = "AXONHUB_API_KEY"
    model: str = "glm-5.3-flash"
    timeout_seconds: float = 8.0
    cache_max_entries: int = 128
    cache_ttl_seconds: int = 3600

    @field_validator("timeout_seconds", "cache_max_entries", "cache_ttl_seconds")
    @classmethod
    def check_positive(cls, v):
        if v is not None and v < 0:
            raise ValueError("timeout/cache 参数不能为负")
        return v


class LinkExtractionConfig(BaseModel):
    check_hyperlinks: bool = True
    check_bots: bool = True


class DeduplicationConfig(BaseModel):
    """跨源内容级去重（F1，默认全关=现网行为零变化）。

    - enable：既有单源 hash 去重（v2 语义）；
    - cross_source_enable：内容级跨源去重（链接指纹/文件名+大小指纹，v3 新增）；
    - auto_cleanup：自动清理历史重复只留最新（F1 可选开关，默认关）；
    - semantic_dedup_enabled：F12 语义去重（fastembed 向量相似度，默认关；
      模型缺失/加载失败自动降级为不启用，不影响既有 dedup 行为）。
    """
    enable: bool = True
    cross_source_enable: bool = False
    auto_cleanup: bool = False
    semantic_dedup_enabled: bool = False


class WatchdogConfig(BaseModel):
    """R1 看门狗 + R2 运行期维护参数。

    - timeout_minutes/interval_seconds：看门狗判"无可用账号"的阈值与检查周期；
    - maintain_interval_seconds：账号维护循环（探活+置 unhealthy+重连）周期（R2，P2 修复）。
    - probe_timeout_seconds / stale_seconds：黑洞式断连检测（老马 R1 复验）——
      维护循环主动发一次 RPC（updates.GetState）带超时，不信任 telethon is_connected()
      布尔（黑洞下它恒 True）；连续探活失败超过 stale_seconds（距上次成功往返）才判
      unhealthy，给瞬时抖动/重连留宽限。stale 必须 > probe 超时+一个维护周期。
    """

    timeout_minutes: int = 5
    interval_seconds: int = 60
    maintain_interval_seconds: int = 30
    probe_timeout_seconds: int = 15
    stale_seconds: int = 60


# ---------------------------------------------------------------------------
# Web 可编辑配置模型（app_config 表权威；字段与 v2 完全对齐，server.py 引用）
# ---------------------------------------------------------------------------


class SystemSettings(BaseModel):
    dedup_retention_days: int = 30
    forwarding_mode: str = "copy"
    forward_new_only: bool = True
    mark_as_read: bool = False
    mark_target_as_read: bool = False
    default_target: str = ""
    default_topic_id: Optional[int] = None

    @field_validator("forwarding_mode")
    @classmethod
    def check_mode(cls, v):
        if v not in ["forward", "copy"]:
            raise ValueError("mode 必须是 'forward' 或 'copy'")
        return v


class SourceConfig(BaseModel):
    identifier: Union[int, str]
    check_replies: bool = False
    replies_limit: int = 10
    forward_new_only: Optional[bool] = None
    resolved_id: Optional[int] = None
    cached_title: Optional[str] = None
    # F2 编辑/删除实时同步（per 源，默认关=现网行为零变化）
    sync_edits: bool = False
    sync_deletes: bool = False
    # F4 年龄截断（per 源，默认 None=不限制；单位小时）
    age_cutoff_hours: Optional[float] = None
    # F6 源标注模板（默认 None=不标注；占位符 {title}/{link}/{time}）
    header_template: Optional[str] = None
    # F10 AI digest per 源开关（默认关=现网行为零变化；与全局 digest.enabled AND）
    digest_enabled: bool = False


class TargetDistributionRule(BaseModel):
    name: str
    all_keywords: List[str] = Field(default_factory=list)
    any_keywords: List[str] = Field(default_factory=list)
    file_types: List[str] = Field(default_factory=list)
    file_name_patterns: List[str] = Field(default_factory=list)
    # F5 媒体类型/大小过滤（per 规则，默认空=不限制）
    media_types: List[str] = Field(default_factory=list)
    max_file_size: int = 0  # 0=不限制；单位字节
    target_identifier: Union[int, str]
    topic_id: Optional[int] = None
    resolved_target_id: Optional[int] = None

    def check(self, text: str, media: Any) -> bool:
        """分发规则命中判断（对齐 v2 TargetDistributionRule.check + F5 媒体过滤）。

        F5: media_types（mime 子串匹配）与 max_file_size（字节上限）双重过滤。
        二者为 AND 关系：设了 media_types 且无匹配 → 不命中；超 max_file_size → 不命中。
        未设时不限制（默认=0 或空列表）。
        """
        import re

        text_lower = text.lower() if text else ""

        if self.all_keywords:
            if not all(kw.lower() in text_lower for kw in self.all_keywords):
                return False

        has_or_conditions = bool(
            self.any_keywords or self.file_types or self.file_name_patterns
        )
        if not has_or_conditions:
            # F5：无关键字条件时仍需检查媒体过滤
            if self._media_disqualified(media):
                return False
            return True

        if self.any_keywords:
            if any(keyword.lower() in text_lower for keyword in self.any_keywords):
                # F5：关键字命中仍需过媒体过滤
                if self._media_disqualified(media):
                    return False
                return True

        try:
            from telethon.tl.types import MessageMediaDocument
        except ImportError:
            MessageMediaDocument = None

        if MessageMediaDocument and media and isinstance(media, MessageMediaDocument):
            doc = media.document
            if doc:
                if self.file_types and doc.mime_type:
                    if any(
                        ft.lower() in doc.mime_type.lower() for ft in self.file_types
                    ):
                        if self._media_disqualified(media):
                            return False
                        return True

                if self.file_name_patterns:
                    file_name = next(
                        (
                            attr.file_name
                            for attr in doc.attributes
                            if hasattr(attr, "file_name")
                        ),
                        None,
                    )
                    if file_name:
                        for pattern_str in self.file_name_patterns:
                            try:
                                pattern = re.compile(
                                    re.escape(pattern_str).replace(r"\*", r".*"),
                                    re.IGNORECASE,
                                )
                                if re.search(pattern, file_name):
                                    if self._media_disqualified(media):
                                        return False
                                    return True
                            except re.error:
                                logger.warning(
                                    f"规则 '{self.name}' 中的文件名模式 '{pattern_str}' 无效"
                                )

        # F5：无关键字命中时也检查媒体过滤
        if self._media_disqualified(media):
            return False
        return False

    def _media_disqualified(self, media: Any) -> bool:
        """F5：媒体不通过该规则的类型/大小过滤（空=不限制）。

        返回 True = 媒体不合格（需跳过该规则）。
        media_types: 按逗号分隔的 mime 子串列表，设了必须至少匹配一个；
        max_file_size: >0 时文件大小必须 <= 此值。
        如果无媒体（media=None）且规则有限制 → 不合格（不能放行纯文本进媒体规则）。
        """
        if not self.media_types and not self.max_file_size:
            return False  # 无限制

        if not media:
            return True  # 有媒体限制但无媒体 → 不合格

        # 两层形状兼容：media 可能是真 MessageMediaDocument，也可能是
        # wrapper（_Media/_FilterDocMedia 测试 mock：.document 属性承载它）。
        # 统一解到最内层 Document：media.document[.document]，取不到即视为不合格。
        doc = getattr(media, "document", None)
        if not doc:
            return True
        doc = getattr(doc, "document", doc)
        if not doc:
            return True

        # media_types 过滤
        if self.media_types:
            mime = (doc.mime_type or "").lower()
            if not any(mt.lower() in mime for mt in self.media_types):
                return True  # mime 不匹配

        # max_file_size 过滤
        if self.max_file_size > 0:
            if (doc.size or 0) > self.max_file_size:
                return True  # 超大小

        return False


class AdFilterConfig(BaseModel):
    enable: bool = True
    keywords_substring: Optional[List[str]] = Field(default_factory=list)
    keywords_word: Optional[List[str]] = Field(default_factory=list)
    patterns: Optional[List[str]] = Field(default_factory=list)
    file_name_keywords: Optional[List[str]] = Field(default_factory=list)


class ContentFilterConfig(BaseModel):
    enable: bool = True
    meaningless_words: List[str] = Field(default_factory=list)
    min_meaningful_length: int = 5


class WhitelistConfig(BaseModel):
    enable: bool = False
    keywords: Optional[List[str]] = Field(default_factory=list)


class TargetConfig(BaseModel):
    default_target: Union[int, str]
    default_topic_id: Optional[int] = None
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)


class RulesDatabase(BaseModel):
    """Web UI 内存规则库快照（v2 对齐，server.py 引用）。"""

    sources: List[SourceConfig] = Field(default_factory=list)
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    settings: SystemSettings = Field(default_factory=SystemSettings)
    content_filter: ContentFilterConfig = Field(default_factory=ContentFilterConfig)
    replacements: Dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 运行时配置聚合（R8 快照整体替换）
# ---------------------------------------------------------------------------


class RuntimeConfig(BaseModel):
    """全量运行时配置。热重载 = 整体替换本对象引用（forwarder.update_snapshot）。

    snapshot() 返回自身：调用方在处理开始时取一次引用即可获得一致视图——
    reload 替换的是 forwarder 持有的引用，旧对象不会被修改。
    """

    # 基础设施（yaml 源）
    accounts: List[AccountConfig] = Field(default_factory=list)
    proxy: ProxyConfig = Field(default_factory=ProxyConfig)
    web_ui: WebUIConfig = Field(default_factory=WebUIConfig)
    logging_level: LoggingLevelConfig = Field(default_factory=LoggingLevelConfig)
    bot_service: BotServiceConfig = Field(default_factory=BotServiceConfig)
    link_checker: LinkCheckerConfig = Field(default_factory=LinkCheckerConfig)
    forwarding: ForwardingConfig = Field(default_factory=ForwardingConfig)
    link_extraction: LinkExtractionConfig = Field(default_factory=LinkExtractionConfig)
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)
    watchdog: WatchdogConfig = Field(default_factory=WatchdogConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    # F10：AI digest（默认关=现网行为零变化）
    digest: DigestConfig = Field(default_factory=DigestConfig)
    translate: TranslateConfig = Field(default_factory=TranslateConfig)

    # Web 可编辑（app_config 表权威）
    sources: List[SourceConfig] = Field(default_factory=list)
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    settings: SystemSettings = Field(default_factory=SystemSettings)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    content_filter: ContentFilterConfig = Field(default_factory=ContentFilterConfig)
    replacements: Dict[str, str] = Field(default_factory=dict)

    # 运行期解析结果（resolve_targets 写入；默认目标 resolved ID）
    targets_resolved_default: Optional[int] = None

    def snapshot(self) -> "RuntimeConfig":
        """返回当前配置视图（整体替换语义，见类 docstring）。"""
        return self


# ---------------------------------------------------------------------------
# yaml 解析 + bootstrap（R4）
# ---------------------------------------------------------------------------


def bootstrap_from_yaml(path: str) -> RuntimeConfig:
    """解析 v2 兼容 yaml（config_template.yaml 结构），构建初始配置。"""
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    cfg = RuntimeConfig()

    # 基础设施段
    if data.get("logging_level"):
        cfg.logging_level = LoggingLevelConfig(**data["logging_level"])
    if data.get("web_ui"):
        cfg.web_ui = WebUIConfig(**data["web_ui"])
    if data.get("proxy"):
        cfg.proxy = ProxyConfig(**data["proxy"])
    if data.get("accounts"):
        cfg.accounts = [AccountConfig(**a) for a in data["accounts"]]
    if data.get("bot_service"):
        cfg.bot_service = BotServiceConfig(**data["bot_service"])
    if data.get("forwarding"):
        cfg.forwarding = ForwardingConfig(**data["forwarding"])
    if data.get("link_extraction"):
        cfg.link_extraction = LinkExtractionConfig(**data["link_extraction"])
    if data.get("deduplication"):
        cfg.deduplication = DeduplicationConfig(**data["deduplication"])
    if data.get("link_checker"):
        cfg.link_checker = LinkCheckerConfig(**data["link_checker"])
    if data.get("watchdog"):
        cfg.watchdog = WatchdogConfig(**data["watchdog"])
    if data.get("delivery"):
        cfg.delivery = DeliveryConfig(**data["delivery"])
    if data.get("digest"):
        cfg.digest = DigestConfig(**data["digest"])
    if data.get("translate"):
        cfg.translate = TranslateConfig(**data["translate"])

    # 规则段（仅 bootstrap 时有意义；表已有数据时不会覆盖，见 load_runtime_config）
    if data.get("sources"):
        cfg.sources = [SourceConfig(**s) for s in data["sources"]]
    if data.get("targets"):
        t = data["targets"]
        cfg.settings = SystemSettings(
            forwarding_mode=cfg.forwarding.mode,
            forward_new_only=cfg.forwarding.forward_new_only,
            mark_as_read=cfg.forwarding.mark_as_read,
            mark_target_as_read=cfg.forwarding.mark_target_as_read,
            default_target=str(t.get("default_target", "")),
            default_topic_id=t.get("default_topic_id"),
        )
        if t.get("distribution_rules"):
            cfg.distribution_rules = [
                TargetDistributionRule(**r) for r in t["distribution_rules"]
            ]
    if data.get("ad_filter"):
        cfg.ad_filter = AdFilterConfig(**data["ad_filter"])
    if data.get("whitelist"):
        cfg.whitelist = WhitelistConfig(**data["whitelist"])
    if data.get("content_filter"):
        cfg.content_filter = ContentFilterConfig(**data["content_filter"])
    if data.get("replacements"):
        cfg.replacements = dict(data["replacements"])

    return cfg


def _has_real_rules(cfg: RuntimeConfig) -> bool:
    """yaml 里是否存在真实规则（sources 非空或 default_target 非占位）。"""
    if cfg.sources:
        return True
    dt = (cfg.settings.default_target or "").strip()
    return bool(dt and dt not in ("0", ""))


async def load_runtime_config(db: Database, yaml_path: str) -> RuntimeConfig:
    """启动装配（R4）：
    1. app_config 表为空且 yaml 存在 → bootstrap（yaml 数据写入表）；
    2. 从表读取全部 Web 可编辑配置构建 RuntimeConfig；
    3. 账号/代理等基础设施段始终从 yaml 读取（凭据不入库）。
    """
    config_repo = ConfigRepository(db)
    source_repo = SourceRepository(db)
    rule_repo = RuleRepository(db)

    yaml_cfg: Optional[RuntimeConfig] = None
    if os.path.exists(yaml_path):
        try:
            yaml_cfg = bootstrap_from_yaml(yaml_path)
        except Exception as e:
            logger.error(f"配置文件解析失败: {e}")
            raise
    else:
        logger.warning(f"配置文件 '{yaml_path}' 未找到（表内已有配置时可忽略）。")

    # bootstrap 判定：app_config 表为空 → 首次运行
    existing_keys = await config_repo.keys()
    if not existing_keys and yaml_cfg is not None:
        logger.info("app_config 表为空，从 yaml 首次导入配置（bootstrap）...")
        initial = yaml_cfg
        # 系统设置
        if _has_real_rules(initial) or initial.settings.default_target:
            await config_repo.save("system_settings", initial.settings.model_dump())
        await config_repo.save("ad_filter", initial.ad_filter.model_dump())
        await config_repo.save("whitelist", initial.whitelist.model_dump())
        await config_repo.save("content_filter", initial.content_filter.model_dump())
        await config_repo.save("replacements", initial.replacements)
        # 源与规则（占位不导入）
        for s in initial.sources:
            await source_repo.save(s.model_dump())
        for r in initial.distribution_rules:
            await rule_repo.save(r.model_dump())
        logger.success("✅ yaml → app_config 表 bootstrap 完成。")

    # 从表构建 Web 可编辑配置
    cfg = RuntimeConfig(
        accounts=yaml_cfg.accounts if yaml_cfg else [],
        proxy=yaml_cfg.proxy if yaml_cfg else ProxyConfig(),
        web_ui=yaml_cfg.web_ui if yaml_cfg else WebUIConfig(),
        logging_level=yaml_cfg.logging_level if yaml_cfg else LoggingLevelConfig(),
        bot_service=yaml_cfg.bot_service if yaml_cfg else BotServiceConfig(),
        link_checker=yaml_cfg.link_checker if yaml_cfg else LinkCheckerConfig(),
        forwarding=yaml_cfg.forwarding if yaml_cfg else ForwardingConfig(),
        link_extraction=(
            yaml_cfg.link_extraction if yaml_cfg else LinkExtractionConfig()
        ),
        deduplication=(
            yaml_cfg.deduplication if yaml_cfg else DeduplicationConfig()
        ),
        watchdog=yaml_cfg.watchdog if yaml_cfg else WatchdogConfig(),
        delivery=yaml_cfg.delivery if yaml_cfg else DeliveryConfig(),
        digest=yaml_cfg.digest if yaml_cfg else DigestConfig(),
        translate=yaml_cfg.translate if yaml_cfg else TranslateConfig(),
    )

    settings_json = await config_repo.get("system_settings")
    if settings_json:
        cfg.settings = SystemSettings(**settings_json)

    ad_json = await config_repo.get("ad_filter")
    if ad_json:
        cfg.ad_filter = AdFilterConfig(**ad_json)

    wl_json = await config_repo.get("whitelist")
    if wl_json:
        cfg.whitelist = WhitelistConfig(**wl_json)

    cf_json = await config_repo.get("content_filter")
    if cf_json:
        cfg.content_filter = ContentFilterConfig(**cf_json)

    rep_json = await config_repo.get("replacements")
    if rep_json:
        cfg.replacements = dict(rep_json)

    sources_data = await source_repo.get_all()
    cfg.sources = [SourceConfig(**s) for s in sources_data]

    rules_data = await rule_repo.get_all()
    cfg.distribution_rules = [TargetDistributionRule(**r) for r in rules_data]

    logger.info(
        f"✅ 配置加载完毕 (Sources: {len(cfg.sources)}, "
        f"Rules: {len(cfg.distribution_rules)}, Accounts: {len(cfg.accounts)})"
    )
    return cfg
