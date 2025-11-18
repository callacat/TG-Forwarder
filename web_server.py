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

from forwarder_core import (
    SourceConfig, 
    TargetDistributionRule, 
    AdFilterConfig, 
    WhitelistConfig
)

logger = logging.getLogger(__name__)

class RulesDatabase(BaseModel):
    sources: List[SourceConfig] = Field(default_factory=list)
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)

# --- 全局变量 ---
RULES_DB_PATH = "/app/data/rules_db.json"
rules_db: RulesDatabase = RulesDatabase() 
db_lock = asyncio.Lock() 

# (新) v8.3：禁用 /docs 和 /redoc API 文档页面
app = FastAPI(
    title="TG Forwarder Web UI",
    description="一个用于动态管理 TG-Forwarder 规则的 Web 面板。",
    version="8.3",
    docs_url=None, # 禁用 /docs
    redoc_url=None # 禁用 /redoc
)

# (新) v8.1：安全配置
security = HTTPBasic()
WEB_UI_PASSWORD = "default_password_please_change" # 将由 ultimate_forwarder.py 覆盖

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
    return True # 验证成功

# --- 数据库核心功能 ---

async def _save_rules_to_db_internal():
    """内部保存函数，不获取锁。假定调用方已持有锁。"""
    try:
        with open(RULES_DB_PATH, 'w', encoding='utf-8') as f:
            json.dump(rules_db.model_dump(), f, indent=2)
        logger.info("✅ 规则已成功保存到 rules_db.json。")
    except Exception as e:
        logger.error(f"❌ 保存 rules_db.json 失败: {e}")

async def load_rules_from_db():
    """从 JSON 文件加载规则到内存"""
    global rules_db
    async with db_lock:
        if not os.path.exists(RULES_DB_PATH):
            logger.warning(f"未找到规则数据库 {RULES_DB_PATH}，将创建新的。")
            rules_db = RulesDatabase() 
            await _save_rules_to_db_internal() 
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

# --- API 路由 (Endpoints) ---

@app.get("/api/rules", response_model=RulesDatabase)
async def get_all_rules(auth: bool = Depends(get_current_user)):
    """获取所有当前规则"""
    async with db_lock:
        return rules_db

# --- 源 (Sources) API ---

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
    (新) v8.2：提供 index.html 
    这个 HTML 文件是我们的单页应用 (SPA) 前端。
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