# web_server.py
import logging
import json
import os
import asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

# (新) v8.0：我们从 forwarder_core 导入 Pydantic 模型
# 这能确保我们的数据结构 100% 一致
from forwarder_core import (
    SourceConfig, 
    TargetDistributionRule, 
    AdFilterConfig, 
    WhitelistConfig
)

logger = logging.getLogger(__name__)

# --- 数据库模型 ---
# 这是 rules_db.json 文件的 Pydantic 模型

class RulesDatabase(BaseModel):
    sources: List[SourceConfig] = Field(default_factory=list)
    distribution_rules: List[TargetDistributionRule] = Field(default_factory=list)
    ad_filter: AdFilterConfig = Field(default_factory=AdFilterConfig)
    whitelist: WhitelistConfig = Field(default_factory=WhitelistConfig)

# --- 全局变量 ---
RULES_DB_PATH = "/app/data/rules_db.json"
rules_db: RulesDatabase = RulesDatabase() # 内存中的数据库实例
db_lock = asyncio.Lock() # 异步锁，防止并发写入

app = FastAPI(
    title="TG Forwarder Web UI",
    description="一个用于动态管理 TG-Forwarder 规则的 Web 面板。",
    version="8.0"
)

# --- 数据库核心功能 ---

async def load_rules_from_db():
    """从 JSON 文件加载规则到内存"""
    global rules_db
    async with db_lock:
        if not os.path.exists(RULES_DB_PATH):
            logger.warning(f"未找到规则数据库 {RULES_DB_PATH}，将创建新的。")
            rules_db = RulesDatabase() # 使用默认空模型
            await save_rules_to_db() # 立即创建
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
    """将内存中的规则保存回 JSON 文件"""
    async with db_lock:
        try:
            with open(RULES_DB_PATH, 'w', encoding='utf-8') as f:
                json.dump(rules_db.model_dump(), f, indent=2)
            logger.info("✅ 规则已成功保存到 rules_db.json。")
        except Exception as e:
            logger.error(f"❌ 保存 rules_db.json 失败: {e}")

# --- API 路由 (Endpoints) ---

@app.get("/api/rules", response_model=RulesDatabase)
async def get_all_rules():
    """获取所有当前规则"""
    async with db_lock:
        return rules_db

# --- 源 (Sources) API ---

@app.post("/api/sources/add")
async def add_source(source: SourceConfig):
    """添加一个新的源频道"""
    if source.identifier in [s.identifier for s in rules_db.sources]:
        raise HTTPException(status_code=400, detail="源已存在")
    
    rules_db.sources.append(source)
    await save_rules_to_db()
    return {"status": "success", "message": f"源 {source.identifier} 已添加。"}

@app.post("/api/sources/remove")
async def remove_source(data: Dict[str, Any]):
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
async def add_distribution_rule(rule: TargetDistributionRule):
    """添加一条新的转发规则"""
    rules_db.distribution_rules.append(rule)
    await save_rules_to_db()
    return rule

@app.post("/api/rules/remove")
async def remove_distribution_rule(data: Dict[str, str]):
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
async def get_blacklist():
    return rules_db.ad_filter

@app.post("/api/blacklist/update")
async def update_blacklist(config: AdFilterConfig):
    """(推荐) 一次性更新所有黑名单"""
    rules_db.ad_filter = config
    await save_rules_to_db()
    return {"status": "success", "message": "黑名单已更新。"}

# --- 白名单 (Whitelist) API ---

@app.get("/api/whitelist", response_model=WhitelistConfig)
async def get_whitelist():
    return rules_db.whitelist

@app.post("/api/whitelist/update")
async def update_whitelist(config: WhitelistConfig):
    """(推荐) 一次性更新所有白名单"""
    rules_db.whitelist = config
    await save_rules_to_db()
    return {"status": "success", "message": "白名单已更新。"}


# --- Web UI 前端 ---
# 我们将在这里提供一个简单的 HTML 页面
# (在下一步，我们将创建一个漂亮的 index.html)

@app.get("/", response_class=HTMLResponse)
async def get_web_ui_placeholder():
    """
    这是一个*临时*的占位符。
    在下一步，我们将用一个漂亮的 JS 前端 (index.html) 替换它。
    """
    return """
    <html>
        <head>
            <title>TG Forwarder API</title>
        </head>
        <body>
            <h1>TG Forwarder API (v8.0) 正在运行</h1>
            <p>这是 Web API 后端。要查看可用的 API 接口，请访问 <a href="/docs">/docs</a>。</p>
            <p>在下一步，我们将构建 Web UI 前端 (index.html)。</p>
        </body>
    </html>
    """