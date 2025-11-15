# TG Ultimate Forwarder - 终极 Telegram 转发器

本项目融合了 `tgforwarder` 和 `tg_zf` 的核心优势，并加入了话题分发、多模式转发等新功能，旨在提供一个稳定、强大且高度可配置的 Telegram 内容聚合工具。

# ✨ 核心功能

* **多账号支持**: 使用多个账号轮换转发，有效规避 FloodWait 和账号限制。

* **多源监控 (已支持)**: 可在 `config.yaml` 中配置任意多个源频道。
* 
* (新) 灵活的标识符: 源和目标均支持数字ID (-100...)、用户名 (@username) 和链接 (https://t.me/...)。

* (新) Bot 交互控制:

  *  通过私聊 Bot 实时管理转发器。

  *  /status: 查看服务运行状态和账号健康度。

  *  /reload: 热重载 config.yaml，无需重启 Docker 容器即可应用新规则。

  *  /run_checklinks: 手动触发一次失效链接检测。

* **多模式转发**:

  * **Forward 模式**: 标准转发，保留消息来源。

  * **Copy 模式**: 复制消息内容，作为新消息发送，可突破源频道的转发限制。

* **高级内容过滤**:

  * **过滤逻辑 (新)**: 清晰的过滤优先级： 白名单 (高) > 黑名单 (中) > 默认通过 (低)。

  * **白名单**: 仅转发包含特定关键词的消息。

  * **黑名单**: 过滤广告、无意义内容（支持正则）。

* **智能内容处理**:

  * **内容替换**: 自动替换消息中的指定文本，如广告标签、频道链接等。

  * **高级链接提取**: (TODO) 解析超链接、机器人回复、评论区中的隐藏链接。

* **精准分发 (新)**:

  * **话题分发**: (新) 根据关键词将消息精准分发到目标群组的 **不同话题 (Topics)** 中。

  * **多频道/群组分发**: (新) 根据关键词将消息分发到不同的目标频道或群组。

  * **按文件类型分发 (新)**: (新) 根据文件名 (`*.mkv`) 或文件类型 (`video/mp4`) 分发。

* **健壮性设计**:

  * **内容去重**: 基于消息哈希防止重复转发。

  * **断点续传**: 自动记录每个频道的转发进度，重启后不丢失。

  * **新消息/历史消息**: (新) 可配置为只处理新消息 (`forward_new_only: true`)，或回溯所有历史消息 (`false`)。

* **配套工具**:

  * **失效链接检测**: (新) 附带 `link_checker.py`，可定时扫描、标记或删除失效的网盘链接。

  * **频道/话题导出**: (新) `export` 模式帮助你获取配置所需的频道和话题 ID。

# 🚀 部署指南 (Docker / 本地)

这是最简单、最推荐的部署方式，支持 docker attach 交互式登录。

1. **准备配置文件**:

   * 在你的服务器上创建一个目录，例如` ~/tg_forwarder`。

   * `mkdir -p ~/tg_forwarder/data` (此目录用于存放 `.session` 登录文件和数据库)

   * 将 `config_template.yaml` 复制到该目录，并重命名为 `config.yaml`。`

2. **(新) 创建你的 Bot**:

  *  私聊 @BotFather。

  *  发送 `/newbot`，按提示创建你的 Bot，获取 `bot_token`。

  *  私聊 @userinfobot。

  *  查看回复，获取你自己的 `Id` (一串数字)。

3. **编辑** `config.yaml`:

     * `docker_container_name`: (新) 填入你下一步 `docker run` 时 `--name` 参数指定的名字 (例如 `tgf`)。
     * (新) `bot_service`: 填入你刚获取的 `bot_token` 和 `admin_user_ids` (填你自己的数字 ID，支持多个)。

     * `accounts`: 填入你的 `api_id, api_hash` 和 `session_name` (例如 `account_1`)。

     * `sources`: 填入你要监控的源频道/群组 ID。

     * `targets`: 填入默认的目标频道/群组 ID。

3. **运行 Docker 容器**:

    * 运行以下命令启动容器。

    * **(重要)**首次运行时，程序会卡住并等待你登录。请看第 4 步。

  ```bash
  docker run -d \
    -it \
    --name tgf \
    -v ~/tg_forwarder/config.yaml:/app/config.yaml \
    -v ~/tg_forwarder/data:/app/data \
    --restart always \
    dswang2233/tgf:latest
  ```

* **首次登录 (交互式)**:

  * 容器启动后，它会在日志中打印 "请在控制台输入手机号..."

  * 运行 `docker attach tgf` (这里的 `tgf` 必须与你 `--name` 和 `config.yaml` 中设置的一致)。

  * 你现在进入了容器的交互模式。

  * 按照提示输入你的**手机号** (例如 `+861234567890`)，按回车。

  * 输入收到的**验证码**，按回车。

  * 如果设置了**两步验证密码**，输入密码，按回车。

  * 登录成功后，你会看到程序开始正常运行。

  * **按 `Ctrl+P` 然后按 `Ctrl+Q` 来分离 (Detach)**终端，千万不要按 `Ctrl+C` (这会停止容器)。

  * 你的登录文件 (`.session`) 现已保存在 `~/tg_forwarder/data` 目录中，下次重启容器将自动登录。

# 🛠️ 本地运行 (开发/调试)

1. 克隆仓库: `git clone ...`

2. 安装依赖: `pip install -r requirements.txt`

3. 创建 `data` 目录: `mkdir data`

4. 复制 `config_template.yaml` 为 `config.yaml`。

5. 配置 `config.yaml` (使用 `session_name` 方式)。

6. 运行 (首次运行会要求在终端登录):

  * 启动转发: `python ultimate_forwarder.py run`

  * 检测链接: `python ultimate_forwarder.py checklinks`

  * 导出ID: `python ultimate_forwarder.py export`

# ⚙️ 配置文件详解 (config.yaml)

`accounts`**(登录账号)**
你必须提供 `session_name`：

  * `session_name`: (用于 Docker / 本地) 指定一个会话文件名，程序启动时会要求你交互式登录。

`sources` **(监控源)**
`id` 必须是数字ID (运行 `export` 模式获取)`。sources` 是一个列表，你可以添加任意多个。

`targets` **(转发目标)**
* 目标可以是频道或群组。

* `default_target`: 必需，未命中任何分发规则时的默认目标，支持数字ID、@username 或 https://t.me/link。

  *  (新) 匹配逻辑: `(满足所有 all_keywords) AND (满足任一 any_keywords OR 满足任一 file_types OR 满足任一 file_name_patterns)`

  *  `all_keywords`: [AND] 消息必须同时包含这里的所有词。

  *  `any_keywords`: [OR] 消息包含这里任意一个词即可。

  *  `file_types`: (新) [OR] 匹配文件的 MIME Type (例如: "video/mp4")。

  *  `file_name_patterns`: (新) [OR] 匹配文件名 (例如: "*.mkv", "1080p")。

  *  `topic_id`: (新) 目标话题 ID。如果目标是普通频道或群组，省略或设为 null。

`forwarding` **(转发行为)**
  * `mode: "copy"`: 推荐。可以突破源频道"禁止转发"的限制。

  * `forward_new_only: true`: 推荐。`true` 表示只处理新消息；`false` 表示会从头扫描所有源频道的历史消息。

## 过滤器逻辑 (重要)

1. **白名单 (Whitelist) 模式**:

    * 如果 `whitelist: enable: true`:

    * 只有 命中 白名单的消息会 ***[通过]**，并跳过所有黑名单。

    * 未命中白名单的消息会被 **[过滤]**。

2. **黑名单 (Blacklist) 模式**:

    * 如果 `whitelist: enable: false`:

    * 消息默认 **[通过]**。

    * 但如果 命中 `ad_filter` 或 `content_filter`，消息会被 **[过滤]**。

3. **默认模式**:

     * 如果所有过滤器都 `enable: false`，所有消息 **[通过]**。

# 📦 GitHub Actions (自动发布到 Docker Hub)

我已为你提供了 `.github/workflows/docker-publish.yml` 文件。

**你需要做的准备**:

1. **在 GitHub 仓库中设置 Secrets**:

     * `DOCKER_USERNAME`: 你的 Docker Hub 用户名。

     * `DOCKERHUB_TOKEN`: 你的 Docker Hub 访问令牌 (Access Token)。

2. **检查** `docker-publish.yml`:

       * `tags` 已设置为 `${{ secrets.DOCKER_USERNAME }}/tgf:latest` 。

3. **推送代码**:

     * 当你将代码 `push` 到 `main` 分支时，GitHub Actions 将自动启动。

     * 它会构建 Docker 镜像并将其推送到 `${{ secrets.DOCKER_USERNAME }}/tgf`。

     * 之后你就可以在服务器上 `docker pull ${{ secrets.DOCKER_USERNAME }}/tgf:latest` 你的最新镜像了。

# 鸣谢

本项目的设计和功能灵感来源于以下项目和贡献者：

1. [fish2018/tgforwarder](https://github.com/fish2018/TGForwarder)(提供了链接提取、内容替换和失效链接检测的思路)

2. [ccrsrg/tg_zf](https://github.com/CCRSRG/TG_ZF) (提供了多账号管理、内容过滤和去重的健壮框架)

3. [Google Gemini](https://gemini.google.com/) (AI 助手，协助进行代码重构、功能融合和文档编写)