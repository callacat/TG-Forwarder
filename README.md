TG Ultimate Forwarder - 终极 Telegram 转发器

本项目融合了 tgforwarder 和 tg_zf 的核心优势，并加入了话题分发、多模式转发等新功能，旨在提供一个稳定、强大且高度可配置的 Telegram 内容聚合工具。

✨ 核心功能

多账号支持: 使用多个账号轮换转发，有效规避 FloodWait 和账号限制。

多模式转发:

Forward 模式: 标准转发，保留消息来源。

Copy 模式: 复制消息内容，作为新消息发送，可突破源频道的转发限制。

高级内容过滤:

白名单: 仅转发包含特定关键词的消息。

黑名单: 过滤广告、无意义内容（支持正则）。

智能内容处理:

内容替换: 自动替换消息中的指定文本，如广告标签、频道链接等。

高级链接提取: (TODO) 解析超链接、机器人回复、评论区中的隐藏链接。

精准分发 (新):

话题分发: (新) 根据关键词将消息精准分发到目标群组的不同话题 (Topics) 中。

多频道分发: (新) 根据关键词将消息分发到不同的目标频道。

健壮性设计:

内容去重: 基于消息哈希防止重复转发。

断点续传: 自动记录每个频道的转发进度，重启后不丢失。

新消息/历史消息: (新) 可配置为只处理新消息 (forward_new_only: true)，或回溯所有历史消息 (false)。

配套工具:

失效链接检测: (新) 附带 link_checker.py，可定时扫描、标记或删除失效的网盘链接。

频道/话题导出: (新) export 模式帮助你获取配置所需的频道和话题 ID。

🚀 部署指南 (Docker)

使用 Docker 是最推荐的部署方式。

准备配置文件:

在你的服务器上创建一个目录，例如 ~/tg_forwarder。

mkdir -p ~/tg_forwarder/data

将 config_template.yaml 复制到该目录，并重命名为 config.yaml。

获取 Session String:

你需要将你的账号转换为 String Session (而不是 session_name 文件)。

在本地克隆 StringSession 项目或使用在线工具，运行 generate.py 生成 Session 字符串。

安全提示: String Session 等同于你的账号密码，请妥善保管。

编辑 config.yaml:

accounts: 填入你的 api_id, api_hash 和 session_string。

sources: 填入你要监控的源频道 ID。运行 python ultimate_forwarder.py export 获取 ID。

targets: 填入默认的目标频道 ID。

targets.distribution_rules: (可选) 参照模板配置你的话题分发规则。

运行 Docker 容器:

docker run -d \
  --name tg-forwarder \
  -v ~/tg_forwarder/config.yaml:/app/config.yaml \
  -v ~/tg_forwarder/data:/app/data \
  --restart always \
  [你的DockerHub用户名]/[镜像仓库名]:latest


(请将 [你的DockerHub用户名]/[镜像仓库名] 替换为你实际的镜像地址，见下方 GitHub Actions 部分)

🛠️ 本地运行 (开发/调试)

克隆仓库: git clone ...

安装依赖: pip install -r requirements.txt

生成 Session: (参见上文)

配置 config.yaml。

运行:

启动转发: python ultimate_forwarder.py run

检测链接: python ultimate_forwarder.py checklinks

导出ID: python ultimate_forwarder.py export

⚙️ 配置文件详解 (config.yaml)

sources (监控源)

id 必须是数字ID (运行 export 模式获取)。

targets (转发目标)

default_target: 必需，未命中任何分发规则时的默认目标。

distribution_rules: (新) 核心功能。

keywords: 列表，消息文本包含 任一 关键词即命中。

target_id: 目标群组/频道 ID。

topic_id: (新) 目标话题 ID。如果目标是普通频道或群组，省略或设为 null。

forwarding (转发行为)

mode: "copy": 推荐。可以突破源频道"禁止转发"的限制。

forward_new_only: true: 推荐。true 表示只处理新消息；false 表示会从头扫描所有源频道的历史消息。

📦 GitHub Actions (自动发布到 Docker Hub)

我已为你提供了 .github/workflows/docker-publish.yml 文件。

你需要做的准备:

在 GitHub 仓库中设置 Secrets:

DOCKERHUB_USERNAME: 你的 Docker Hub 用户名。

DOCKERHUB_TOKEN: 你的 Docker Hub 访问令牌 (Access Token)。

修改 docker-publish.yml:

在 Build and push 步骤中，将 tags: your-username/your-repo:latest 修改为你自己的 Docker Hub 仓库名。

推送代码:

当你将代码 push 到 main 分支时，GitHub Actions 将自动启动。

它会构建 Docker 镜像并将其推送到你的 Docker Hub。

之后你就可以在服务器上 docker pull 你的最新镜像了。

⚠️ 关于 Webhook

你提到了 "Webhook 方式"。在 Telethon (用户账号) 的上下文中，我们不使用 Webhook。

取而代之的是，我们使用事件驱动 (Event-Driven) 的方式 (events.NewMessage)。

效果: 你的客户端会与 Telegram 保持一个持久连接。一旦源频道有新消息，Telegram 会 立即 将该消息推送给你的客户端，客户端会 立即 (毫秒级) 触发 handle_new_message 函数。

结论: 这比 Webhook 更快、更高效，并且完全满足你 "实时获取更新" 的需求。