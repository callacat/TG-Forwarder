# web_server.py
import logging
import json
import os
import asyncio
import secrets 
from fastapi import FastAPI, HTTPException, Request, Depends 
from fastapi.security import HTTPBasic, HTTPBasicCredentials 
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

from models import (
    Config, 
    SourceConfig, 
    TargetDistributionRule, 
    AdFilterConfig, 
    WhitelistConfig,
    RulesDatabase
)
# (新) v9.1：导入 database
import database

from loguru import logger

# --- 全局变量 ---
RULES_DB_PATH = "/app/data/rules_db.json"
rules_db: RulesDatabase = RulesDatabase() 
db_lock = asyncio.Lock() 

app = FastAPI(
    title="TG Forwarder Web UI",
    description="一个用于动态管理 TG-Forwarder 规则的 Web 面板。",
    version="9.1",
    docs_url=None, 
    redoc_url=None 
)

# --- 安全配置 ---
security = HTTPBasic()
WEB_UI_PASSWORD = "default_password_please_change" 

def set_web_ui_password(password: str):
    """由 ultimate_forwarder.py 在启动时调用以注入密码"""
    global WEB_UI_PASSWORD
    WEB_UI_PASSWORD = password
    logger.info("Web UI 密码已设置。")

def get_current_user(credentials: HTTPBasicCredentials = Depends(security)):
    """FastAPI 依赖项，用于检查密码"""
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
    """内部保存函数，不获取锁。假定调用方已持有锁。"""
    try:
        with open(RULES_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(rules_db.model_dump(), f, indent=2)
        logger.info("✅ 规则已成功保存到 rules_db.json。")
    except Exception as e:
        logger.error(f"❌ 保存 rules_db.json 失败: {e}")

async def load_rules_from_db(config: Optional[Config] = None): 
    """从 JSON 文件加载规则到内存，如果文件不存在，则从 config.yaml 迁移"""
    global rules_db
    async with db_lock:
        if not os.path.exists(RULES_DB_PATH):
            logger.warning(f"未找到规则数据库 {RULES_DB_PATH}，将尝试从 config.yaml 迁移...")
            
            if config:
                try:
                    rules_db = RulesDatabase(
                        sources=config.sources,
                        distribution_rules=config.targets.distribution_rules,
                        ad_filter=config.ad_filter,
                        whitelist=config.whitelist
                    )
                    logger.info("✅ 成功从 config.yaml 提取旧规则。")
                except Exception as e:
                    logger.error(f"❌ 迁移旧规则失败: {e}。将创建空数据库。")
                    rules_db = RulesDatabase()
            else:
                logger.warning("未提供 config 对象，将创建空数据库。")
                rules_db = RulesDatabase()
            
            await _save_rules_to_db_internal() # 保存迁移后的/新的空数据库
        else:
            try:
                with open(RULES_DB_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    rules_db = RulesDatabase(**data)
                    logger.info("✅ 成功从 rules_db.json 加载规则。")
            except Exception as e:
                logger.error(f"❌ 加载 rules_db.json 失败: {e}。将使用空规则启动。")
                rules_db = RulesDatabase()

async def save_rules_to_db():
    """将内存中的规则保存回 JSON 文件 (供 API 安全调用)"""
    async with db_lock:
        await _save_rules_to_db_internal() 

# --- (新) v9.1：统计 & 设置 API ---

@app.get("/api/stats")
async def get_stats(auth: bool = Depends(get_current_user)):
    """获取所有统计数据 (SQLite + JSON)"""
    try:
        # 1. 从 SQLite 获取统计
        db_stats = await database.get_db_stats()
        
        # 2. 从内存 (rules_db) 获取规则统计
        async with db_lock:
            rule_stats = {
                "sources": len(rules_db.sources),
                "distribution_rules": len(rules_db.distribution_rules),
                "whitelist_keywords": len(rules_db.whitelist.keywords or []),
                "blacklist_substring": len(rules_db.ad_filter.keywords_substring or []),
                "blacklist_word": len(rules_db.ad_filter.keywords_word or []),
                "blacklist_file": len(rules_db.ad_filter.file_name_keywords or []),
                "blacklist_regex": len(rules_db.ad_filter.patterns or []),
            }
            
        # 3. 合并
        full_stats = {**db_stats, **rule_stats}
        return full_stats
        
    except Exception as e:
        logger.error(f"获取 /api/stats 失败: {e}")
        raise HTTPException(status_code=500, detail="获取统计数据失败")

@app.get("/api/settings/dedup")
async def get_dedup_setting(auth: bool = Depends(get_current_user)):
    """获取去重保留天数"""
    days = await database.get_dedup_retention()
    return {"dedup_retention_days": days}

class DedupSetting(BaseModel):
    days: int

@app.post("/api/settings/dedup")
async def set_dedup_setting(setting: DedupSetting, auth: bool = Depends(get_current_user)):
    """设置去重保留天数"""
    try:
        await database.set_dedup_retention(setting.days)
        return {"status": "success", "message": f"去重保留天数已设置为 {setting.days} 天。"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"设置失败: {e}")


# --- API 路由 (Endpoints) ---

@app.get("/api/rules", response_model=RulesDatabase)
async def get_all_rules(auth: bool = Depends(get_current_user)):
    """获取所有当前规则"""
    async with db_lock:
        return rules_db

# --- 源 (Sources) API ---
@app.get("/api/sources", response_model=List[SourceConfig])
async def get_sources(auth: bool = Depends(get_current_user)):
    """获取所有监控源规则"""
    return rules_db.sources

@app.post("/api/sources/add")
async def add_source(source: SourceConfig, auth: bool = Depends(get_current_user)):
    """添加一个新的源频道"""
    if source.identifier in [s.identifier for s in rules_db.sources]:
        raise HTTPException(status_code=400, detail="源已存在")
    
    rules_db.sources.append(source)
    await save_rules_to_db()
    return {"status": "success", "message": f"源 {source.identifier} 已添加。"}

@app.post("/api/sources/remove")
async def remove_source(data: Dict[str, Any], auth: bool = Depends(get_current_user)):
    """移除一个源频道"""
    identifier = data.get('identifier')
    if not identifier:
        raise HTTPException(status_code=400, detail="未提供 identifier")
        
    original_count = len(rules_db.sources)
    rules_db.sources = [s for s in rules_db.sources if str(s.identifier) != str(identifier)]
    
    if len(rules_db.sources) == original_count:
        raise HTTPException(status_code=404, detail="未找到要删除的源")

    await save_rules_to_db()
    return {"status": "success", "message": f"源 {identifier} 已移除。"}

# --- 转发规则 (Distribution Rules) API ---
@app.get("/api/rules/list", response_model=List[TargetDistributionRule])
async def get_distribution_rules(auth: bool = Depends(get_current_user)):
    """获取所有转发规则"""
    return rules_db.distribution_rules

@app.post("/api/rules/add", response_model=TargetDistributionRule)
async def add_distribution_rule(rule: TargetDistributionRule, auth: bool = Depends(get_current_user)):
    """添加一条新的转发规则"""
    rules_db.distribution_rules.append(rule)
    await save_rules_to_db()
    return rule

@app.post("/api/rules/remove")
async def remove_distribution_rule(data: Dict[str, str], auth: bool = Depends(get_current_user)):
    """根据名称移除一条转发规则"""
    rule_name = data.get('name')
    if not rule_name:
        raise HTTPException(status_code=400, detail="未提供规则名称 'name'")
    
    original_count = len(rules_db.distribution_rules)
    rules_db.distribution_rules = [r for r in rules_db.distribution_rules if r.name != rule_name]

    if len(rules_db.distribution_rules) == original_count:
        raise HTTPException(status_code=404, detail="未找到要删除的规则")
        
    await save_rules_to_db()
    return {"status": "success", "message": f"规则 '{rule_name}' 已移除。"}

# --- 黑名单 (Ad Filter) API ---

@app.get("/api/blacklist", response_model=AdFilterConfig)
async def get_blacklist(auth: bool = Depends(get_current_user)):
    return rules_db.ad_filter

@app.post("/api/blacklist/update")
async def update_blacklist(config: AdFilterConfig, auth: bool = Depends(get_current_user)):
    """(推荐) 一次性更新所有黑名单"""
    rules_db.ad_filter = config
    await save_rules_to_db()
    return {"status": "success", "message": "黑名单已更新。"}

# --- 白名单 (Whitelist) API ---

@app.get("/api/whitelist", response_model=WhitelistConfig)
async def get_whitelist(auth: bool = Depends(get_current_user)):
    return rules_db.whitelist

@app.post("/api/whitelist/update")
async def update_whitelist(config: WhitelistConfig, auth: bool = Depends(get_current_user)):
    """(推荐) 一次性更新所有白名单"""
    rules_db.whitelist = config
    await save_rules_to_db()
    return {"status": "success", "message": "白名单已更新。"}


# --- Web UI 前端 ---
@app.get("/", response_class=HTMLResponse)
async def get_web_ui():
    """
    提供 index.html 
    """
    ui_path = "/app/index.html"
    if not os.path.exists(ui_path):
        return HTMLResponse(content="""
        <html><body>
            <h1>错误：未找到 <code>index.html</code>。</h1>
            <p>Web 服务器正在运行，但前端文件丢失。</p>
        </body></html>
        """, status_code=404)
        
    return FileResponse(ui_path)