# models.py
import logging
import re
from typing import List, Optional, Dict, Any, Union 
from pydantic import BaseModel, Field, field_validator, model_validator

from loguru import logger

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
    resolved_id: Optional[int] = None 

class TargetDistributionRule(BaseModel):
    name: str 
    all_keywords: List[str] = Field(default_factory=list) # AND 关系
    any_keywords: List[str] = Field(default_factory=list) # OR 关系
    file_types: List[str] = Field(default_factory=list)   # MIME types
    file_name_patterns: List[str] = Field(default_factory=list) 

    target_identifier: Union[int, str]
    topic_id: Optional[int] = None 
    
    resolved_target_id: Optional[int] = None
    
    def check(self, text: str, media: Any) -> bool:
        text_lower = text.lower() if text else ""
        
        # 1. 检查 [AND] all_keywords
        if self.all_keywords:
            if not all(kw.lower() in text_lower for kw in self.all_keywords):
                return False 
        
        # 2. 检查 [OR] 条件组
        has_or_conditions = bool(self.any_keywords or self.file_types or self.file_name_patterns)
        if not has_or_conditions:
            return True

        if self.any_keywords:
            if any(keyword.lower() in text_lower for keyword in self.any_keywords):
                return True
        
        try:
            from telethon.tl.types import MessageMediaDocument
        except ImportError:
            MessageMediaDocument = None

        if MessageMediaDocument and media and isinstance(media, MessageMediaDocument):
            doc = media.document
            if doc:
                if self.file_types and doc.mime_type:
                    if any(ft.lower() in doc.mime_type.lower() for ft in self.file_types):
                        return True

                if self.file_name_patterns:
                    file_name = next((attr.file_name for attr in doc.attributes if hasattr(attr, 'file_name')), None)
                    if file_name:
                        for pattern_str in self.file_name_patterns:
                            try:
                                pattern = re.compile(re.escape(pattern_str).replace(r'\*', r'.*'), re.IGNORECASE)
                                if re.search(pattern, file_name):
                                    return True
                            except re.error:
                                logger.warning(f"规则 '{self.name}' 中的文件名模式 '{pattern_str}' 无效")
        return False

class TargetConfig(BaseModel):
    default_target: Union[int, str]
    default_topic_id: Optional[int] = None 
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    resolved_default_target_id: Optional[int] = None

class SystemSettings(BaseModel):
    """可以从 Web UI 动态修改的系统设置"""
    dedup_retention_days: int = 30
    forwarding_mode: str = "copy"
    forward_new_only: bool = True
    mark_as_read: bool = False
    mark_target_as_read: bool = False
    default_target: str = "" 
    default_topic_id: Optional[int] = None
    
    @field_validator('forwarding_mode')
    def check_mode(cls, v):
        if v not in ['forward', 'copy']:
            raise ValueError("mode 必须是 'forward' 或 'copy'")
        return v

class AdFilterConfig(BaseModel):
    enable: bool = True
    keywords_substring: Optional[List[str]] = Field(default_factory=list)
    keywords_word: Optional[List[str]] = Field(default_factory=list)
    patterns: Optional[List[str]] = Field(default_factory=list)
    file_name_keywords: Optional[List[str]] = Field(default_factory=list)

# --- (修改) 内容过滤器模型 ---
class ContentFilterConfig(BaseModel):
    enable: bool = True
    meaningless_words: List[str] = Field(default_factory=list)
    min_meaningful_length: int = 5

class WhitelistConfig(BaseModel):
    enable: bool = False
    keywords: Optional[List[str]] = Field(default_factory=list)

class DeduplicationConfig(BaseModel):
    enable: bool = True
    db_path: Optional[str] = None

class LinkExtractionConfig(BaseModel):
    check_hyperlinks: bool = True
    check_bots: bool = True

class LinkCheckerConfig(BaseModel):
    enabled: bool = False
    mode: str = "log" 
    schedule: str = "0 3 * * *" 

class BotServiceConfig(BaseModel):
    enabled: bool = False
    bot_token: str = "YOUR_BOT_TOKEN_HERE" 
    admin_user_ids: List[int] = Field(default_factory=list)

class ForwardingConfig(BaseModel): # 保留用于读取旧配置
    mode: str = "forward"
    forward_new_only: bool = True
    mark_as_read: bool = False
    mark_target_as_read: bool = False

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

# --- Web UI 数据库 ---
class RulesDatabase(BaseModel):
    sources: List[SourceConfig] = Field(default_factory=list)
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    settings: SystemSettings = Field(default_factory=SystemSettings)
    
    # (新增) 动态管理内容过滤和替换
    content_filter: ContentFilterConfig = Field(default_factory=ContentFilterConfig)
    replacements: Dict[str, str] = Field(default_factory=dict)