# web_server.py
import logging
import json
import os
import asyncio
import secrets 
from fastapi import FastAPI, HTTPException, Request, Depends 
from fastapi.security import HTTPBasic, HTTPBasicCredentials 
from fastapi.responses import HTMLResponse, FileResponse
from typing import List, Optional, Dict, Any

from models import (
    Config, 
    SourceConfig, 
    TargetDistributionRule, 
    AdFilterConfig, 
    WhitelistConfig,
    RulesDatabase,
    SystemSettings
)
import database

from loguru import logger

RULES_DB_PATH = "/app/data/rules_db.json"
rules_db: RulesDatabase = RulesDatabase() 
db_lock = asyncio.Lock() 

app = FastAPI(title="TG Forwarder Web UI")

security = HTTPBasic()
WEB_UI_PASSWORD = "default_password_please_change" 

def set_web_ui_password(password: str):
    global WEB_UI_PASSWORD
    WEB_UI_PASSWORD = password

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    correct_password = secrets.compare_digest(credentials.password, WEB_UI_PASSWORD)
    if not correct_password:
        raise HTTPException(status_code=401, detail="凭据不正确", headers={"WWW-Authenticate": "Basic"})
    return True 

async def _save_rules_to_db_internal():
    try:
        with open(RULES_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(rules_db.model_dump(), f, indent=2)
        logger.info("✅ 规则与设置已保存到 rules_db.json")
    except Exception as e:
        logger.error(f"❌ 保存 rules_db.json 失败: {e}")

async def load_rules_from_db(config: Optional[Config] = None): 
    """加载规则，并负责将 config.yaml 中的设置迁移到 rules_db.json"""
    global rules_db
    async with db_lock:
        if not os.path.exists(RULES_DB_PATH):
            logger.warning(f"未找到数据库 {RULES_DB_PATH}，正在从 config.yaml 迁移...")
            if config:
                try:
                    # 迁移初始化
                    initial_settings = SystemSettings(
                        dedup_retention_days=30,
                        forwarding_mode=config.forwarding.mode,
                        forward_new_only=config.forwarding.forward_new_only,
                        mark_as_read=config.forwarding.mark_as_read,
                        mark_target_as_read=config.forwarding.mark_target_as_read,
                        # (新增) 迁移默认目标
                        default_target=str(config.targets.default_target),
                        default_topic_id=config.targets.default_topic_id
                    )
                    
                    rules_db = RulesDatabase(
                        sources=config.sources,
                        distribution_rules=config.targets.distribution_rules,
                        ad_filter=config.ad_filter,
                        whitelist=config.whitelist,
                        settings=initial_settings
                    )
                    logger.info("✅ 成功迁移旧配置到 Web 数据库。")
                except Exception as e:
                    logger.error(f"❌ 迁移失败: {e}，使用空数据库。")
                    rules_db = RulesDatabase()
            else:
                rules_db = RulesDatabase()
            await _save_rules_to_db_internal()
        else:
            try:
                with open(RULES_DB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    rules_db = RulesDatabase(**data)
                    logger.info("✅ 成功加载 rules_db.json")
            except Exception as e:
                logger.error(f"❌ 加载 rules_db.json 失败: {e}")
                rules_db = RulesDatabase()

async def save_rules_to_db():
    async with db_lock:
        await _save_rules_to_db_internal() 

# --- 统计 API ---
@app.get("/api/stats")
async def get_stats(auth: bool = Depends(get_current_user)):
    try:
        db_stats = await database.get_db_stats()
        async with db_lock:
            rule_stats = {
                "sources": len(rules_db.sources),
                "distribution_rules": len(rules_db.distribution_rules),
                "whitelist_keywords": len(rules_db.whitelist.keywords or []),
                "blacklist_substring": len(rules_db.ad_filter.keywords_substring or []),
                "blacklist_word": len(rules_db.ad_filter.keywords_word or []),
            }
        return {**db_stats, **rule_stats}
    except Exception:
        return {}

# --- 设置 API (新) ---
@app.get("/api/settings", response_model=SystemSettings)
async def get_settings(auth: bool = Depends(get_current_user)):
    """获取当前的系统设置"""
    return rules_db.settings

@app.post("/api/settings/update")
async def update_settings(settings: SystemSettings, auth: bool = Depends(get_current_user)):
    """更新系统设置"""
    rules_db.settings = settings
    await database.set_dedup_retention(settings.dedup_retention_days)
    await save_rules_to_db()
    return {"status": "success", "message": "系统设置已更新。"}

# 兼容旧 API
@app.get("/api/settings/dedup")
async def get_dedup_legacy(auth: bool = Depends(get_current_user)):
    return {"dedup_retention_days": rules_db.settings.dedup_retention_days}

# --- 规则与黑白名单 API ---
@app.get("/api/rules", response_model=RulesDatabase)
async def get_all_rules(auth: bool = Depends(get_current_user)):
    return rules_db

@app.post("/api/sources/add")
async def add_source(source: SourceConfig, auth: bool = Depends(get_current_user)):
    if source.identifier in [s.identifier for s in rules_db.sources]:
        raise HTTPException(status_code=400, detail="源已存在")
    rules_db.sources.append(source)
    await save_rules_to_db()
    return {"status": "success"}

@app.post("/api/sources/remove")
async def remove_source(data: Dict[str, Any], auth: bool = Depends(get_current_user)):
    rules_db.sources = [s for s in rules_db.sources if str(s.identifier) != str(data.get('identifier'))]
    await save_rules_to_db()
    return {"status": "success"}

@app.post("/api/rules/add")
async def add_rule(rule: TargetDistributionRule, auth: bool = Depends(get_current_user)):
    rules_db.distribution_rules.append(rule)
    await save_rules_to_db()
    return rule

@app.post("/api/rules/remove")
async def remove_rule(data: Dict[str, str], auth: bool = Depends(get_current_user)):
    rules_db.distribution_rules = [r for r in rules_db.distribution_rules if r.name != data.get('name')]
    await save_rules_to_db()
    return {"status": "success"}

@app.get("/api/blacklist", response_model=AdFilterConfig)
async def get_blacklist(auth: bool = Depends(get_current_user)):
    return rules_db.ad_filter

@app.post("/api/blacklist/update")
async def update_blacklist(config: AdFilterConfig, auth: bool = Depends(get_current_user)):
    rules_db.ad_filter = config
    await save_rules_to_db()
    return {"status": "success"}

@app.get("/api/whitelist", response_model=WhitelistConfig)
async def get_whitelist(auth: bool = Depends(get_current_user)):
    return rules_db.whitelist

@app.post("/api/whitelist/update")
async def update_whitelist(config: WhitelistConfig, auth: bool = Depends(get_current_user)):
    rules_db.whitelist = config
    await save_rules_to_db()
    return {"status": "success"}

@app.get("/", response_class=HTMLResponse)
async def get_web_ui():
    ui_path = "/app/index.html"
    if not os.path.exists(ui_path):
        return HTMLResponse("<h1>Error: index.html missing</h1>", status_code=404)
    return FileResponse(ui_path)