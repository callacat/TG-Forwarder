import json
import os
import asyncio
import secrets 
from fastapi import FastAPI, HTTPException, Request, Depends 
from fastapi.security import HTTPBasic, HTTPBasicCredentials 
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
from typing import List, Optional, Dict, Any
from loguru import logger  # 使用 Loguru

from models import Config, SourceConfig, TargetDistributionRule, AdFilterConfig, WhitelistConfig, RulesDatabase
import database

RULES_DB_PATH = "/app/data/rules_db.json"
rules_db: RulesDatabase = RulesDatabase() 
_db_lock = None # 移除全局实例化

def get_rule_lock():
    """惰性获取锁"""
    global _db_lock
    if _db_lock is None:
        _db_lock = asyncio.Lock()
    return _db_lock

app = FastAPI(
    title="TG Forwarder Web UI",
    description="一个用于动态管理 TG-Forwarder 规则的 Web 面板。",
    version="9.5",
    docs_url=None, 
    redoc_url=None 
)

security = HTTPBasic()
WEB_UI_PASSWORD = "default_password_please_change" 

def set_web_ui_password(password: str):
    global WEB_UI_PASSWORD
    WEB_UI_PASSWORD = password
    logger.info("Web UI 密码已配置。")

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    correct_password = secrets.compare_digest(credentials.password, WEB_UI_PASSWORD)
    if not correct_password:
        raise HTTPException(
            status_code=401,
            detail="凭据不正确",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True 

# --- 数据库核心功能 ---

async def _save_rules_to_db_internal():
    try:
        with open(RULES_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(rules_db.model_dump(), f, indent=2)
        logger.info("规则已保存到 rules_db.json。")
    except Exception as e:
        logger.error(f"保存 rules_db.json 失败: {e}")

async def load_rules_from_db(config: Optional[Config] = None): 
    global rules_db
    async with get_rule_lock():
        if not os.path.exists(RULES_DB_PATH):
            logger.warning(f"未找到规则库 {RULES_DB_PATH}，尝试从 config.yaml 迁移...")
            if config:
                try:
                    rules_db = RulesDatabase(
                        sources=config.sources,
                        distribution_rules=config.targets.distribution_rules,
                        ad_filter=config.ad_filter,
                        whitelist=config.whitelist
                    )
                    logger.success("成功从 config.yaml 迁移规则。")
                except Exception as e:
                    logger.error(f"迁移规则失败: {e}，将使用空数据库。")
                    rules_db = RulesDatabase()
            else:
                rules_db = RulesDatabase()
            await _save_rules_to_db_internal()
        else:
            try:
                with open(RULES_DB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    rules_db = RulesDatabase(**data)
                    logger.success("已加载 rules_db.json。")
            except Exception as e:
                logger.error(f"加载 rules_db.json 失败: {e}。使用空规则启动。")
                rules_db = RulesDatabase()

async def save_rules_to_db():
    async with get_rule_lock():
        await _save_rules_to_db_internal() 

# --- API ---

@app.get("/api/stats")
async def get_stats(auth: bool = Depends(get_current_user)):
    try:
        db_stats = await database.get_db_stats()
        async with get_rule_lock():
            rule_stats = {
                "sources": len(rules_db.sources),
                "distribution_rules": len(rules_db.distribution_rules),
                "whitelist_keywords": len(rules_db.whitelist.keywords or []),
                "blacklist_substring": len(rules_db.ad_filter.keywords_substring or []),
                "blacklist_word": len(rules_db.ad_filter.keywords_word or []),
                "blacklist_file": len(rules_db.ad_filter.file_name_keywords or []),
                "blacklist_regex": len(rules_db.ad_filter.patterns or []),
            }
        return {**db_stats, **rule_stats}
    except Exception as e:
        logger.error(f"获取统计失败: {e}")
        raise HTTPException(status_code=500, detail="获取统计失败")

@app.get("/api/settings/dedup")
async def get_dedup_setting(auth: bool = Depends(get_current_user)):
    days = await database.get_dedup_retention()
    return {"dedup_retention_days": days}

class DedupSetting(BaseModel):
    days: int

@app.post("/api/settings/dedup")
async def set_dedup_setting(setting: DedupSetting, auth: bool = Depends(get_current_user)):
    try:
        await database.set_dedup_retention(setting.days)
        return {"status": "success", "message": "设置已保存"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/rules", response_model=RulesDatabase)
async def get_all_rules(auth: bool = Depends(get_current_user)):
    async with get_rule_lock():
        return rules_db

@app.get("/api/sources", response_model=List[SourceConfig])
async def get_sources(auth: bool = Depends(get_current_user)):
    return rules_db.sources

@app.post("/api/sources/add")
async def add_source(source: SourceConfig, auth: bool = Depends(get_current_user)):
    if source.identifier in [s.identifier for s in rules_db.sources]:
        raise HTTPException(status_code=400, detail="源已存在")
    rules_db.sources.append(source)
    await save_rules_to_db()
    return {"status": "success", "message": "已添加"}

@app.post("/api/sources/remove")
async def remove_source(data: Dict[str, Any], auth: bool = Depends(get_current_user)):
    identifier = data.get('identifier')
    if not identifier: raise HTTPException(status_code=400, detail="Missing identifier")
    original_count = len(rules_db.sources)
    rules_db.sources = [s for s in rules_db.sources if str(s.identifier) != str(identifier)]
    if len(rules_db.sources) == original_count: raise HTTPException(status_code=404, detail="未找到源")
    await save_rules_to_db()
    return {"status": "success", "message": "已移除"}

@app.get("/api/rules/list", response_model=List[TargetDistributionRule])
async def get_distribution_rules(auth: bool = Depends(get_current_user)):
    return rules_db.distribution_rules

@app.post("/api/rules/add", response_model=TargetDistributionRule)
async def add_distribution_rule(rule: TargetDistributionRule, auth: bool = Depends(get_current_user)):
    rules_db.distribution_rules.append(rule)
    await save_rules_to_db()
    return rule

@app.post("/api/rules/remove")
async def remove_distribution_rule(data: Dict[str, str], auth: bool = Depends(get_current_user)):
    rule_name = data.get('name')
    if not rule_name: raise HTTPException(status_code=400, detail="Missing name")
    original_count = len(rules_db.distribution_rules)
    rules_db.distribution_rules = [r for r in rules_db.distribution_rules if r.name != rule_name]
    if len(rules_db.distribution_rules) == original_count: raise HTTPException(status_code=404, detail="未找到规则")
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
        return HTMLResponse("Error: index.html not found", status_code=404)
    return FileResponse(ui_path)
