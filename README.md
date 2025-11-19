# TG Ultimate Forwarder (TG 终极转发器)

这是一个功能强大、高度可配置的 Telegram 消息转发与聚合工具。它融合了多账号轮询、智能防重复、高级过滤规则、Web 可视化管理界面以及 Telegram Bot 交互控制等现代化功能。

本项目旨在解决传统转发器功能单一、配置繁琐、容易触发风控等痛点，提供企业级的转发稳定性。

---

## ✨ 核心亮点

### 1. 强大的转发核心
- **多账号轮换**: 支持配置多个用户账号 (`User Client`)，转发时自动轮询，有效降低单个账号触发 FloodWait 的风险。
- **智能 Copy 模式**: 也就是“无痕转发”。支持纯文本、多媒体、相册 (Album) 的完美复制，去除“转发自...”标签。
- **断点续传与去重**: 基于 SQLite 数据库记录转发进度和消息哈希，重启不丢失进度，且能防止重复消息发送。
- **话题 (Topic) 分发**: 完美支持 Telegram Forum 功能，可根据规则将消息分发到同一个群组的不同话题中。

### 2. 可视化 Web 管理面板
- **实时仪表盘**: 查看运行时间、在线账号数、消息处理统计、失效链接统计等。
- **在线配置**: 通过网页直接添加/删除监控源、修改转发规则、编辑黑白名单。
- **热重载**: 修改配置后，通过 Web 或 Bot 指令即可热重载，无需重启容器。
- **安全验证**: 内置登录验证，保护你的配置信息。

### 3. 交互式 Bot 控制
- **运维指令**: 支持 `/status` (状态)、`/reload` (重载)、`/check` (查死链) 等指令。
- **ID 获取**: 通过 `/ids` 指令快速获取监控源的真实 ID，无需繁琐的手动查询。

### 4. 高级过滤与处理
- **多维度过滤**: 支持正则 (`Regex`)、全词匹配、子字符串匹配、文件名匹配。
- **内容替换**: 在发送前对文本进行替换（如去除广告标签）。
- **死链检测**: 定时扫描目标频道，检测并标记/删除包含失效网盘链接（百度/阿里/夸克等）的消息。

---

## 🛠 环境要求

- **Docker** (推荐)
- 或者 Python 3.13+ (如果你选择源码部署)
- Telegram API ID & Hash (获取自 [my.telegram.org](https://my.telegram.org))
- Telegram Bot Token (获取自 [@BotFather](https://t.me/BotFather))

---

## 🚀 Docker 快速部署 (推荐)

### 1. 准备目录与配置
创建一个部署目录，并准备 `config.yaml` 文件。

```bash
mkdir -p ~/tg-forwarder/data
cd ~/tg-forwarder
# 下载或创建 config.yaml (参考下文配置详解)
```

### 2. 启动容器
使用构建好的镜像启动。

```bash
docker run -d \
  -it \
  --name tgf \
  -p 8080:8080 \
  -v $(pwd)/config.yaml:/app/config.yaml \
  -v $(pwd)/data:/app/data \
  --restart always \
  dswang2233/tg-forwarder:latest
```

> **注意**: `-p 8080:8080` 用于暴露 Web UI 端口，`-it` 用于首次交互式登录。

### 3. 首次登录
容器启动后，需要进行交互式登录以生成 Session 文件。

```bash
docker attach tgf
```

按照提示输入你的手机号（带国家代码，如 `+86...`）和验证码。登录成功后，按 `Ctrl+P` 然后 `Ctrl+Q` 退出挂载，**不要按 Ctrl+C**。

---

## ⚙️ 配置文件详解 (config.yaml)

```yaml
# 容器名称，用于日志提示
docker_container_name: "tgf"

# 日志级别: INFO / DEBUG / WARNING
logging_level:
  app: "INFO"
  telethon: "WARNING"

# Web UI 登录密码 (强烈建议修改)
web_ui:
  password: "your_secure_password"

# Bot 服务配置 (用于运维)
bot_service:
  enabled: true
  bot_token: "123456:ABC-DEF..."
  admin_user_ids: [123456789] # 你的 TG ID，只有管理员能使用 Bot

# 用户账号 (用于转发消息)
accounts:
  - api_id: 123456
    api_hash: "abcdef..."
    session_name: "user_1"
    enabled: true

# 监控源配置
sources:
  - identifier: -1001234567890 # 支持 ID、@username 或 https://t.me/link
    forward_new_only: true     # 是否仅转发新消息

# 转发目标与分发规则
targets:
  default_target: -1009876543210 # 默认转发到的频道 ID
  default_topic_id: null       # 默认话题 ID (可选)
  
  # 分发规则 (优先级从上到下)
  distribution_rules:
    - name: "安卓应用"
      file_name_patterns: ["*.apk", "*.xapk"]
      target_identifier: -1009876543210
      topic_id: 101            # 转发到特定话题

# 转发行为设置
forwarding:
  mode: "copy"           # copy (无痕复制) 或 forward (普通转发)
  forward_new_only: true # 启动时是否忽略历史消息
  mark_as_read: false    # 是否自动已读源频道

# 广告/垃圾信息过滤
ad_filter:
  enable: true
  keywords_substring: ["加微信", "赌博"] # 中文模糊匹配
  keywords_word: ["ad", "promo"]       # 英文全词匹配
  file_name_keywords: ["宣传.pdf"]      # 文件名过滤

# 死链检测器
link_checker:
  enabled: true
  mode: "edit"           # log (仅记录), edit (标记消息), delete (删除消息)
  schedule: "0 3 * * *"  # 每天凌晨 3 点运行 (Cron 表达式)
```

---

## 🖥 Web UI 使用说明

访问 `http://你的IP:8080` 进入管理后台。

- **Dashboard**: 查看系统运行状态、数据库统计、规则命中情况。
- **在线配置**: 通过网页直接添加/删除监控源、修改转发规则、编辑黑白名单。
- **热重载**: 修改配置后，通过 Web 或 Bot 指令即可热重载，无需重启容器。
- **安全验证**: 内置登录验证，保护你的配置信息。

---

## 🤖 Bot 指令说明

对你的 Bot 发送以下指令（需在 `config.yaml` 中配置管理员 ID）：

- `/status`: 查看详细的系统运行仪表盘（运行时间、FloodWait 状态、数据库统计）。
- `/reload`: 热重载配置文件和规则数据库（Web 修改配置后需执行此操作）。
- `/ids`: 导出当前所有监控源的名称及其解析后的 ID。
- `/check`: 立即手动触发一次死链检测任务。

---

## ❓ 常见问题

**Q: 为什么配置了频道却收不到消息？**

**A:**

    * 确保你的 User Client（用户账号）已经加入了该频道。
    * 检查该频道是否被你的账号“归档”或“静音”，Telegram 有时会停止向 API 推送静音频道的更新。
    * 检查日志 `docker logs tgf`，看是否有报错信息。

**Q: 什么是 FloodWait？**
**A:** 这是 Telegram 对账号的速率限制。如果触发，日志会显示“FloodWait X seconds”。程序会自动暂停该账号的使用并尝试切换其他账号（如果配置了多账号）。

**Q: Copy 模式和 Forward 模式的区别？**
**A:** `Forward` 会保留消息的原始来源（显示“转发自...”），如果原频道禁止转发，则会失败。`Copy` 模式会提取内容重新发送，看起来像是你发送的新消息，且能突破禁止转发的限制。

---

# 鸣谢

本项目的设计和功能灵感来源于以下项目和贡献者：

1.  [fish2018/tgforwarder](https://github.com/fish2018/TGForwarder)
2.  [ccrsrg/tg_zf](https://github.com/CCRSRG/TG_ZF)
3.  [Google Gemini](https://gemini.google.com/) (AI 助手，协助进行代码重构、功能融合和文档编写)

## 📜 许可证

本项目基于 Apache License 2.0 开源。
```