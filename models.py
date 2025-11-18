# models.py
# (新) v8.5：这是一个新文件，用于解决循环导入 (Circular Import) 错误。
# 它包含了所有 Pydantic 数据模型，并且不依赖于任何其他项目文件。

import logging
from typing import List, Optional, Tuple, Dict, Set, Any, Union 
from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator

logger = logging.getLogger(__name__)

# --- 日志配置模型 ---
class LoggingLevelConfig(BaseModel):
    app: str = "INFO"
    telethon: str = "WARNING"

class WebUIConfig(BaseModel):
    password: str = "default_password_please_change"

# --- 配置模型 ---

class ProxyConfig(BaseModel):
    enabled: bool = False
    proxy_type: str = "socks5"
    addr: str = "127.0.0.1"
    port: int = 1080
    username: Optional[str] = None
    password: Optional[str] = None
    
    def get_telethon_proxy(self):
        if not self.enabled:
            return None
        return (self.proxy_type, self.addr, self.port, True, self.username, self.password)

class AccountConfig(BaseModel):
    api_id: int
    api_hash: str
    session_name: str
    enabled: bool = True

    @model_validator(mode='before')
    @classmethod
    def check_session_auth(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if not data.get('session_name'): 
                raise ValueError("必须提供 session_name (会话文件)。")
            
            if data.get('session_name'):
                name = data['session_name']
                if '/' in name or '\\' in name:
                    raise ValueError("session_name 不能包含路径分隔符。")
        return data

class SourceConfig(BaseModel):
    identifier: Union[int, str]
    check_replies: bool = False
    replies_limit: int = 10
    forward_new_only: Optional[bool] = None
    resolved_id: Optional[int] = Field(None, exclude=True) 

class TargetDistributionRule(BaseModel):
    name: str 
    all_keywords: List[str] = Field(default_factory=list)
    any_keywords: List[str] = Field(default_factory=list)
    file_types: List[str] = Field(default_factory=list) 
    file_name_patterns: List[str] = Field(default_factory=list) 

    target_identifier: Union[int, str]
    topic_id: Optional[int] = None 
    
    resolved_target_id: Optional[int] = Field(None, exclude=True)
    
    def check(self, text: str, media: Any) -> bool:
        # (新) v8.5：我们将 check 逻辑移到了模型内部，
        # 因为它不依赖于 forwarder_core 的其他部分。
        
        text_lower = text.lower() if text else ""
        
        # 1. 检查 [AND] all_keywords
        if self.all_keywords:
            if not all(kw.lower() in text_lower for kw in self.all_keywords):
                return False 
        
        # 2. 检查 [OR] 条件组
        or_group_matched = False
        
        if self.any_keywords:
            if any(keyword.lower() in text_lower for keyword in self.any_keywords):
                or_group_matched = True
        
        # (新) v8.5：我们需要导入 MessageMediaDocument
        try:
            from telethon.tl.types import MessageMediaDocument
        except ImportError:
            # 在一个纯 Pydantic 的环境中，我们可能没有 telethon
            # 我们可以安全地跳过这个检查
            MessageMediaDocument = None

        if MessageMediaDocument and not or_group_matched and media and isinstance(media, MessageMediaDocument):
            doc = media.document
            if doc:
                if self.file_types and doc.mime_type:
                    if any(ft.lower() in doc.mime_type.lower() for ft in self.file_types):
                        or_group_matched = True

                if not or_group_matched and self.file_name_patterns:
                    file_name = next((attr.file_name for attr in doc.attributes if hasattr(attr, 'file_name')), None)
                    if file_name:
                        # (新) v8.5：导入 re
                        import re
                        for pattern_str in self.file_name_patterns:
                            try:
                                pattern = re.compile(re.escape(pattern_str).replace(r'\*', r'.*'), re.IGNORECASE)
                                if re.search(pattern, file_name):
                                    or_group_matched = True
                                    break 
                            except re.error:
                                logger.warning(f"规则 '{self.name}' 中的文件名模式 '{pattern_str}' 无效")
        
        # 3. 最终逻辑判断
        has_all_keywords = bool(self.all_keywords)
        has_or_group = bool(self.any_keywords or self.file_types or self.file_name_patterns)

        if has_all_keywords and not has_or_group:
            return True
        elif not has_all_keywords and has_or_group:
            return or_group_matched
        elif has_all_keywords and has_or_group:
            return or_group_matched
        else:
            return False

class TargetConfig(BaseModel):
    default_target: Union[int, str]
    default_topic_id: Optional[int] = None 
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    
    resolved_default_target_id: Optional[int] = Field(None, exclude=True)


class ForwardingConfig(BaseModel):
    mode: str = "forward" 
    forward_new_only: bool = True 
    mark_as_read: bool = False
    mark_target_as_read: bool = False 
    
    @field_validator('mode')
    def check_mode(cls, v):
        if v not in ['forward', 'copy']:
            raise ValueError("forwarding.mode 必须是 'forward' 或 'copy'")
        return v

class AdFilterConfig(BaseModel):
    enable: bool = True
    keywords_substring: Optional[List[str]] = Field(default_factory=list)
    keywords_word: Optional[List[str]] = Field(default_factory=list)
    patterns: Optional[List[str]] = Field(default_factory=list)
    file_name_keywords: Optional[List[str]] = Field(default_factory=list)

class ContentFilterConfig(BaseModel):
    enable: bool = True
    meaningless_words: Optional[List[str]] = Field(default_factory=list)
    min_meaningful_length: int = 5

class WhitelistConfig(BaseModel):
    enable: bool = False
    keywords: Optional[List[str]] = Field(default_factory=list)

class DeduplicationConfig(BaseModel):
    enable: bool = True
    db_path: Optional[str] = "/app/data/dedup_db.json" 

class LinkExtractionConfig(BaseModel):
    check_hyperlinks: bool = True
    check_bots: bool = True

class LinkCheckerConfig(BaseModel):
    enabled: bool = False
    mode: str = "log" 
    schedule: str = "0 3 * * *" 
    
    @field_validator('mode')
    def check_mode(cls, v):
        if v not in ['log', 'edit', 'delete']:
            raise ValueError("link_checker.mode 必须是 'log', 'edit', 或 'delete'")
        return v

class BotServiceConfig(BaseModel):
    enabled: bool = False
    bot_token: str = "YOUR_BOT_TOKEN_HERE" 
    admin_user_ids: List[int] 
    
    @field_validator('bot_token', mode='before')
    def check_bot_token(cls, v, info: Any):
        values = info.data
        if values.get('enabled') and (not v or v == "YOUR_BOT_TOKEN_HERE"):
            raise ValueError("Bot 服务已启用，但 bot_token 未设置。")
        return v
    
    @field_validator('admin_user_ids', mode='before')
    def check_admin_ids(cls, v, info: Any):
        values = info.data
        if values.get('enabled') and (not v):
            raise ValueError("Bot 服务已启用，但 admin_user_ids 列表为空。")
        return v

class Config(BaseModel):
    docker_container_name: Optional[str] = "tg-forwarder"
    logging_level: Optional[LoggingLevelConfig] = Field(default_factory=LoggingLevelConfig)
    web_ui: Optional[WebUIConfig] = Field(default_factory=WebUIConfig) 
    
    proxy: Optional[ProxyConfig] = Field(default_factory=ProxyConfig)
    accounts: List[AccountConfig]
    
    sources: List[SourceConfig]
    targets: TargetConfig
    forwarding: ForwardingConfig = Field(default_factory=ForwardingConfig)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    content_filter: ContentFilterConfig = Field(default_factory=ContentFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    deduplication: DeduplicationConfig = Field(default_factory=DeduplicationConfig)
    
    link_extraction: LinkExtractionConfig = Field(default_factory=LinkExtractionConfig)
    replacements: Optional[Dict[str, str]] = Field(default_factory=dict)
    link_checker: Optional[LinkCheckerConfig] = Field(default_factory=LinkCheckerConfig)
    bot_service: Optional[BotServiceConfig] = Field(default_factory=BotServiceConfig) 

# (新) v8.5：Web UI 数据库的模型
class RulesDatabase(BaseModel):
    sources: List[SourceConfig] = Field(default_factory=list)
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)