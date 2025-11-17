# TG Ultimate Forwarder - 终极 Telegram 转发器

本项目融合了 [fish2018/tgforwarder](https://github.com/fish2018/TGForwarder) 和 [ccrsrg/tg_zf](https://github.com/CCRSRG/TG_ZF) 的核心优势，并加入了话题分发、多模式转发、相册处理、智能 `copy` 模式以及更多健壮性功能，旨在提供一个稳定、强大且高度可配置的 Telegram 内容聚合工具。

# ✨ 核心功能

* **多账号支持**: 使用多个账号轮换转发，有效规避 FloodWait 和账号限制。
* **多源监控**: 可在 `config.yaml` 中配置任意多个源频道。
* **灵活的标识符**: 源和目标均支持数字ID (-100...)、用户名 (@username) 和链接 (https://t.me/...)。
* **智能 `copy` 模式 (v5.2)**: 真正的“复制”模式，**完全去除“转发自...”**。
    * **智能 Markdown**：在 `copy` 模式下，纯文本消息的 Markdown 链接 (`[]()`) 会被保留；带文件的消息会安全发送，避免 API 崩溃。
* **相册 (Album) 支持 (v4.6)**: 完整转发多图/多文件消息，不再拆分成多条。
* **高级内容过滤 (v6.0)**:
    * **全词匹配**: `ad_filter.keywords_word` (如 "ad")，用于英文，避免误伤 "download"。
    * **子字符串匹配**: 新增 `ad_filter.keywords_substring`，(如 "发财娱乐")，用于中文、词组。
    * **文件名过滤**: (新) `ad_filter.file_name_keywords` 支持根据文件名（如 "芒果VPN.apk"）过滤广告文件。
    * **正则表达式**: `ad_filter.patterns` 仍用于高级正则匹配。
    * **优先级**: 清晰的过滤优先级： 白名单 (高) > 黑名单 (中) > 默认通过 (低)。
* **精准分发 (v5.0)**:
    * **话题分发**: 根据关键词将消息精准分发到目标群组的 **不同话题 (Topics)** 中。
    * **文件名匹配**: 支持 `file_name_patterns: ["*.apk", "*.exe"]` 规则，用于按文件类型分发。
    * **匹配逻辑**: `(满足所有 all_keywords) AND (满足任一 any_keywords OR 满足任一 file_types OR 满足任一 file_name_patterns)`
* **Bot 交互控制 (v5.1)**:
    * 通过私聊或群组（需为管理员且非匿名）实时管理转发器。
    * `/status`: 查看服务运行状态和账号健康度（包括 FloodWait）。
    * `/reload`: 热重载 `config.yaml`，无需重启 Docker 容器即可应用新规则。
    * `/export_sources`: (新) 精确导出你配置的源频道及其解析后的数字 ID。
    * `/run_checklinks`: 手动触发一次失效链接检测。
* **健壮性设计**:
    * **内容去重**: 基于消息哈希防止重复转发。
    * **断点续传**: 自动记录每个频道的转发进度，重启后不丢失。
    * **强迫症选项 (v5.6)**:
        * `mark_as_read: true`: 自动将*源*频道标记为已读。
        * `mark_target_as_read: true`: 自动将*目标*（非话题）群组标记为已读。
    * **稳定的依赖**: `Dockerfile` 会自动升级 `pip`，`requirements.txt` 使用稳定的 `apscheduler v3` 版本，避免依赖崩溃。
* **内容替换 (v5.0)**:
    * 在消息*发送前*才执行替换，避免了替换内容（如 `**`）干扰过滤规则。

# 🚀 部署指南 (Docker)

这是最简单、最推荐的部署方式。

1.  **准备配置文件**:
    * 在你的服务器上创建一个目录，例如` ~/tg_forwarder`。
    * `mkdir -p ~/tg_forwarder/data` (此目录用于存放 `.session` 登录文件和数据库)
    * 将 `config_template.yaml` 复制到该目录，并重命名为 `config.yaml`。`

2.  **创建你的 Bot**:
    * 私聊 @BotFather，发送 `/newbot`，按提示创建你的 Bot，获取 `bot_token`。
    * 私聊 @userinfobot，查看回复，获取你自己的 `Id` (一串数字)。

3.  **编辑** `config.yaml`:
    * `docker_container_name`: 填入你下一步 `docker run` 时 `--name` 参数指定的名字 (例如 `tgf`)。
    * `bot_service`: 填入你刚获取的 `bot_token` 和 `admin_user_ids` (填你自己的数字 ID)。
    * `accounts`: 填入你的 `api_id, api_hash` 和 `session_name` (例如 `account_1`)。
    * `sources` / `targets`: 填入你要监控和转发的频道/群组 ID。

4.  **运行 Docker 容器**:
    * (重要) 你必须使用你自己的 Docker Hub 用户名，或者使用 `docker build` 自行构建。
    * 假设你的镜像是 `dswang2233/tgf:latest`：

    ```bash
    docker run -d \
      -it \
      --name tgf \
      -v ~/tg_forwarder/config.yaml:/app/config.yaml \
      -v ~/tg_forwarder/data:/app/data \
      --restart always \
      dswang2233/tgf:latest
    ```

5.  **首次登录 (交互式)**:
    * 容器启动后，它会在日志中打印 "请在控制台输入手机号..."
    * 运行 `docker attach tgf` (这里的 `tgf` 必须与你 `--name` 和 `config.yaml` 中设置的一致)。
    * 你现在进入了容器的交互模式。
    * 按照提示输入你的**手机号** (例如 `+861234567890`)，按回车。
    * 输入收到的**验证码**，按回车。
    * 如果设置了**两步验证密码**，输入密码，按回车。
    * 登录成功后，你会看到程序开始正常运行。
    * 按 `Ctrl+P` 然后按 `Ctrl+Q` 来分离 (Detach) 终端，**千万不要按 `Ctrl+C`** (这会停止容器)。
    * 你的登录文件 (`.session`) 现已保存在 `~/tg_forwarder/data` 目录中，下次重启容器将自动登录。

# ⚙️ 配置文件陷阱 (必读)

* **`replacements` (内容替换)**:
    * **正确用法** (删除 "#ad"):
        ```yaml
        replacements:
          "#ad": ""
        ```
    * **危险用法** (不要用!):
        ```yaml
        replacements:
          "": "#ad"  # 错误! 这会把 "hi" 变成 "#ah#ai#a"
        ```

* **`ad_filter` (广告过滤)**:
    * `keywords:`: 使用**全词匹配**。`"ad"` 只会匹配单词 "ad"，不会匹配 "download"。
    * `patterns:`: 使用**正则表达式**。

* **`distribution_rules` (分发规则)**:
    * 逻辑是： `(ALL keywords) AND (ANY keywords OR ANY file_types OR ANY file_name_patterns)`
    * 如果你想**仅凭**文件名（如 `*.apk`）就转发，请确保 `all_keywords` 列表为空 `[]` 或直接删除该行。
    * **示例**:
        ```yaml
        distribution_rules:
          # 规则 1: 仅凭 .apk 文件名就匹配
          - name: "APK 文件"
            file_name_patterns: ["*.apk"]
            target_identifier: -1009876543210 
            topic_id: 11
          
          # 规则 2: 必须同时包含 "win" 和 .exe
          - name: "Windows 软件"
            all_keywords: ["win"]
            file_name_patterns: ["*.exe", "*.msi"]
            target_identifier: -1009876543210 
            topic_id: 10
        ```

# ❓ 故障排查 (Troubleshooting)

### Q: 为什么我的某个频道收不到消息？（无日志）

**这是最常见的问题：静默失败。** 你的工具人账号能看到消息，但程序日志里什么都没有。

这通常意味着 Telegram 服务器**没有向你的程序推送更新**。

* **原因 1 (90% 的可能): 频道被静音/归档**
    * 你的工具人账号（`accounts` 里的第一个号）在它的 Telegram 客户端上（手机或电脑）**静音 (Mute)** 或 **归档 (Archive)** 了这个源频道。
    * Telegram 服务器因此认为你“不关心”这个频道的实时更新，于是停止了向 API 推送。
    * **✅ 解决方案:** 登录你的工具人账号，找到那个源频道，**“取消静音” (Unmute)** 并 **“取消归档” (Unarchive)**。

* **原因 2 (10% 的可能): 账户被节流**
    * 你的工具人账号加入了*太多*高流量（刷屏）的频道。
    * Telegram 服务器不堪重负，停止了对你账户的实时推送，降级为“延迟通知”，导致 `events.NewMessage` 处理器静默失败。
    * **✅ 解决方案:** 登录你的工具人账号，退出（Leave）那些你**不需要转发**的、但非常吵闹的频道，减轻服务器负担。

* **如何确认？ (高级排错)**
    * 编辑 `ultimate_forwarder.py` 文件。
    * 将 `logging.getLogger('telethon').setLevel(logging.WARNING)` 改为 `logging.INFO`。
    * 重启容器，查看 `docker logs tgf`。
    * 如果你在源频道发消息时，日志里**没有**看到 `UpdateNewChannelMessage` 或 `Got difference...`，就说明 Telegram 服务器确实没有给你推送更新。

# 鸣谢

本项目的设计和功能灵感来源于以下项目和贡献者：

1.  [fish2018/tgforwarder](https://github.com/fish2018/TGForwarder)
2.  [ccrsrg/tg_zf](https://github.com/CCRSRG/TG_ZF)
3.  [Google Gemini](https://gemini.google.com/) (AI 助手，协助进行代码重构、功能融合和文档编写)