# web_server.py
import logging
import json
import os
import asyncio
import secrets 
from fastapi import FastAPI, HTTPException, Request, Depends 
from fastapi.security import HTTPBasic, HTTPBasicCredentials 
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi
from typing import List, Optional, Dict, Any, Callable, Awaitable
from pydantic import BaseModel 

from models import (
    Config, 
    SourceConfig, 
    TargetDistributionRule, 
    AdFilterConfig, 
    WhitelistConfig,
    ContentFilterConfig,
    RulesDatabase,
    SystemSettings
)
import database

from loguru import logger

# 内存缓存 (Source of Truth for Runtime)
rules_db: RulesDatabase = RulesDatabase() 
db_lock = asyncio.Lock() 

app = FastAPI(
    title="TG Forwarder Web UI",
    description="TG 终极转发器管理面板",
    version="2.6",
    docs_url=None, 
    redoc_url=None, 
    openapi_url=None 
)

_stats_provider: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None
_bot_notifier: Optional[Callable[[str], Awaitable[None]]] = None

def set_stats_provider(func): global _stats_provider; _stats_provider = func
def set_bot_notifier(func): global _bot_notifier; _bot_notifier = func

security = HTTPBasic()
WEB_UI_PASSWORD = "default_password_please_change" 

def set_web_ui_password(password: str):
    global WEB_UI_PASSWORD
    WEB_UI_PASSWORD = password

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    correct_password = secrets.compare_digest(credentials.password, WEB_UI_PASSWORD)
    if not correct_password: raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username

async def notify_bot(message: str):
    if _bot_notifier:
        try: await _bot_notifier(message)
        except: pass

# --- 核心加载逻辑 (SQLite <-> Pydantic) ---

async def load_rules_from_db(config: Optional[Config] = None): 
    """
    从 SQLite 加载所有规则到内存 rules_db。
    如果是首次运行且 SQLite 为空，则从 config.yaml 迁移数据。
    """
    global rules_db
    async with db_lock:
        try:
            # 1. 尝试从 SQLite 读取
            sources_data = await database.get_all_sources()
            rules_data = await database.get_all_rules()
            
            # 2. 如果 SQLite 为空，尝试迁移 config.yaml
            if not sources_data and not rules_data and config:
                logger.warning("SQLite 规则库为空，正在从 config.yaml 迁移数据...")
                
                # 迁移 System Settings
                initial_settings = SystemSettings(
                    dedup_retention_days=30,
                    forwarding_mode=config.forwarding.mode,
                    forward_new_only=config.forwarding.forward_new_only,
                    mark_as_read=config.forwarding.mark_as_read,
                    mark_target_as_read=config.forwarding.mark_target_as_read,
                    default_target=str(config.targets.default_target),
                    default_topic_id=config.targets.default_topic_id
                )
                await database.save_config_json('system_settings', initial_settings.model_dump())
                
                # 迁移其他配置
                await database.save_config_json('ad_filter', config.ad_filter.model_dump())
                await database.save_config_json('whitelist', config.whitelist.model_dump())
                await database.save_config_json('content_filter', config.content_filter.model_dump())
                await database.save_config_json('replacements', config.replacements or {})
                
                # 迁移源和规则
                for s in config.sources:
                    await database.save_source(s.model_dump())
                for r in config.targets.distribution_rules:
                    await database.save_rule(r.model_dump())
                
                logger.success("✅ 迁移完成！")
                
                # 重新读取
                sources_data = await database.get_all_sources()
                rules_data = await database.get_all_rules()

            # 3. 构建内存对象
            rules_db.sources = [SourceConfig(**s) for s in sources_data]
            rules_db.distribution_rules = [TargetDistributionRule(**r) for r in rules_data]
            
            # 读取 JSON 配置
            settings_json = await database.get_config_json('system_settings')
            if settings_json: rules_db.settings = SystemSettings(**settings_json)
            
            ad_json = await database.get_config_json('ad_filter')
            if ad_json: rules_db.ad_filter = AdFilterConfig(**ad_json)
            
            wl_json = await database.get_config_json('whitelist')
            if wl_json: rules_db.whitelist = WhitelistConfig(**wl_json)
            
            cf_json = await database.get_config_json('content_filter')
            if cf_json: rules_db.content_filter = ContentFilterConfig(**cf_json)
            
            rep_json = await database.get_config_json('replacements')
            if rep_json: rules_db.replacements = rep_json
            
            logger.info(f"✅ 规则库加载完毕 (Sources: {len(rules_db.sources)}, Rules: {len(rules_db.distribution_rules)})")
            
        except Exception as e:
            logger.error(f"❌ 加载规则失败: {e}")
            rules_db = RulesDatabase() # Fallback

async def save_rules_to_db():
    """
    (兼容接口) 实际上不需要做全量保存，因为我们在 API 调用时是实时写入 SQLite 的。
    但为了确保内存和 DB 一致，我们可以留空或者做一些清理工作。
    """
    pass 

# --- 文档路由 ---
@app.get("/docs", include_in_schema=False)
async def get_swagger_documentation(username: str = Depends(get_current_user)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title="API 文档")

@app.get("/redoc", include_in_schema=False)
async def get_redoc_documentation(username: str = Depends(get_current_user)):
    return get_redoc_html(openapi_url="/openapi.json", title="API 文档")

@app.get("/openapi.json", include_in_schema=False)
async def get_open_api_endpoint(username: str = Depends(get_current_user)):
    return get_openapi(title=app.title, version=app.version, routes=app.routes)

# --- 业务 API (写操作全部对接 SQLite) ---

@app.get("/api/stats")
async def get_stats(auth: str = Depends(get_current_user)):
    try:
        db_stats = await database.get_db_stats()
        runtime_stats = {}
        if _stats_provider:
            try:
                res = await _stats_provider() if asyncio.iscoroutinefunction(_stats_provider) else _stats_provider()
                runtime_stats = res
            except: pass

        async with db_lock:
            bl = rules_db.ad_filter
            bl_count = len(bl.keywords_substring or []) + len(bl.keywords_word or []) + len(bl.file_name_keywords or []) + len(bl.patterns or [])
            cf_count = len(rules_db.content_filter.meaningless_words) if rules_db.content_filter else 0
            
            rule_stats = {
                "sources": len(rules_db.sources),
                "distribution_rules": len(rules_db.distribution_rules),
                "whitelist_count": len(rules_db.whitelist.keywords or []),
                "blacklist_count": bl_count,
                "content_filter_count": cf_count,
                "replacements_count": len(rules_db.replacements or {})
            }
        return {**db_stats, **rule_stats, **runtime_stats}
    except Exception: return {}

@app.get("/api/settings", response_model=SystemSettings)
async def get_settings(auth: str = Depends(get_current_user)):
    return rules_db.settings

@app.post("/api/settings/update")
async def update_settings(settings: SystemSettings, auth: str = Depends(get_current_user)):
    rules_db.settings = settings
    await database.save_config_json('system_settings', settings.model_dump())
    await notify_bot("⚠️ **系统设置已更新**\n请发送 /reload 以应用更改。")
    return {"status": "success"}

@app.get("/api/rules", response_model=RulesDatabase)
async def get_all_rules(auth: str = Depends(get_current_user)):
    return rules_db

@app.post("/api/sources/add")
async def add_source(source: SourceConfig, auth: str = Depends(get_current_user)):
    # 内存查重
    if source.identifier in [s.identifier for s in rules_db.sources]:
        raise HTTPException(status_code=400, detail="源已存在")
    
    # 写入 DB
    await database.save_source(source.model_dump())
    # 更新内存
    rules_db.sources.append(source)
    
    await notify_bot(f"➕ **新增监控源**: `{source.identifier}`")
    return {"status": "success"}

@app.post("/api/sources/remove")
async def remove_source(data: Dict[str, Any], auth: str = Depends(get_current_user)):
    identifier = str(data.get('identifier'))
    await database.remove_source(identifier)
    rules_db.sources = [s for s in rules_db.sources if str(s.identifier) != identifier]
    return {"status": "success"}

@app.post("/api/rules/add")
async def add_rule(rule: TargetDistributionRule, auth: str = Depends(get_current_user)):
    await database.save_rule(rule.model_dump())
    rules_db.distribution_rules.append(rule)
    await notify_bot(f"➕ **新增分发规则**: `{rule.name}`")
    return rule

@app.post("/api/rules/update_single")
async def update_single_rule(rule: TargetDistributionRule, name_to_replace: str = "", auth: str = Depends(get_current_user)):
    target_name = name_to_replace if name_to_replace else rule.name
    
    # 更新内存
    found = False
    for index, r in enumerate(rules_db.distribution_rules):
        if r.name == target_name:
            rules_db.distribution_rules[index] = rule
            found = True
            break
    
    if not found:
        rules_db.distribution_rules.append(rule)
        
    # 更新 DB (如果改了名，还要删除旧的)
    if name_to_replace and name_to_replace != rule.name:
        await database.remove_rule(name_to_replace)
        
    await database.save_rule(rule.model_dump())
    return {"status": "success"}

class ReorderRequest(BaseModel):
    names: List[str]

@app.post("/api/rules/reorder")
async def reorder_rules(data: ReorderRequest, auth: str = Depends(get_current_user)):
    # 内存重排
    name_map = {r.name: r for r in rules_db.distribution_rules}
    new_list = []
    for name in data.names:
        if name in name_map:
            new_list.append(name_map[name])
    # 追加遗漏的
    processed = set(data.names)
    for r in rules_db.distribution_rules:
        if r.name not in processed:
            new_list.append(r)
    
    rules_db.distribution_rules = new_list
    
    # DB 重排：SQLite 没有原生顺序，我们必须清空重写
    await database.clear_rules()
    for r in new_list:
        await database.save_rule(r.model_dump())
        
    return {"status": "success"}

@app.post("/api/rules/remove")
async def remove_rule(data: Dict[str, str], auth: str = Depends(get_current_user)):
    name = data.get('name')
    await database.remove_rule(name)
    rules_db.distribution_rules = [r for r in rules_db.distribution_rules if r.name != name]
    return {"status": "success"}

# --- Filters ---

@app.get("/api/blacklist", response_model=AdFilterConfig)
async def get_blacklist(auth: str = Depends(get_current_user)):
    return rules_db.ad_filter

@app.post("/api/blacklist/update")
async def update_blacklist(config: AdFilterConfig, auth: str = Depends(get_current_user)):
    rules_db.ad_filter = config
    await database.save_config_json('ad_filter', config.model_dump())
    return {"status": "success"}

@app.get("/api/whitelist", response_model=WhitelistConfig)
async def get_whitelist(auth: str = Depends(get_current_user)):
    return rules_db.whitelist

@app.post("/api/whitelist/update")
async def update_whitelist(config: WhitelistConfig, auth: str = Depends(get_current_user)):
    rules_db.whitelist = config
    await database.save_config_json('whitelist', config.model_dump())
    return {"status": "success"}

@app.get("/api/content_filter", response_model=ContentFilterConfig)
async def get_content_filter(auth: str = Depends(get_current_user)):
    return rules_db.content_filter

@app.post("/api/content_filter/update")
async def update_content_filter(config: ContentFilterConfig, auth: str = Depends(get_current_user)):
    rules_db.content_filter = config
    await database.save_config_json('content_filter', config.model_dump())
    return {"status": "success"}

@app.get("/api/replacements", response_model=Dict[str, str])
async def get_replacements(auth: str = Depends(get_current_user)):
    return rules_db.replacements

@app.post("/api/replacements/update")
async def update_replacements(data: Dict[str, str], auth: str = Depends(get_current_user)):
    rules_db.replacements = data
    await database.save_config_json('replacements', data)
    return {"status": "success"}

@app.get("/", response_class=HTMLResponse)
async def get_web_ui():
    ui_path = "/app/index.html"
    if not os.path.exists(ui_path):
        return HTMLResponse("<h1>Error: index.html missing</h1>", status_code=404)
    return FileResponse(ui_path)