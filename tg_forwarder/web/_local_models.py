# -*- coding: utf-8 -*-
"""本地模型 fallback（仅当 tg_forwarder/config.py 未就绪时使用）。

字段与 v2 models.py 完全对齐，供 web/server.py 在并行子代理交付
config.py 之前可独立测试。config.py 就绪后 server.py 会优先 import 它。
"""
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator


class SystemSettings(BaseModel):
    """可以从 Web UI 动态修改的系统设置（v2 对齐）。"""

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
        """v2 对齐：mode 必须是 'forward' 或 'copy'。"""
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


class TargetDistributionRule(BaseModel):
    name: str
    all_keywords: List[str] = Field(default_factory=list)
    any_keywords: List[str] = Field(default_factory=list)
    file_types: List[str] = Field(default_factory=list)
    file_name_patterns: List[str] = Field(default_factory=list)
    target_identifier: Union[int, str]
    topic_id: Optional[int] = None
    resolved_target_id: Optional[int] = None


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


class RulesDatabase(BaseModel):
    """Web UI 内存规则库快照（v2 对齐）。"""

    sources: List[SourceConfig] = Field(default_factory=list)
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)
    settings: SystemSettings = Field(default_factory=SystemSettings)
    content_filter: ContentFilterConfig = Field(default_factory=ContentFilterConfig)
    replacements: Dict[str, str] = Field(default_factory=dict)
