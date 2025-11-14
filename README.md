TG Ultimate Forwarder - 终极 Telegram 转发器
本项目融合了 tgforwarder 和 tg_zf 的核心优势，并加入了话题分发、多模式转发等新功能，旨在提供一个稳定、强大且高度可配置的 Telegram 内容聚合工具。
✨ 核心功能
多账号支持: 使用多个账号轮换转发，有效规避 FloodWait 和账号限制。
多源监控 (已支持): 可在 config.yaml 中配置任意多个源频道。
多模式转发:
Forward 模式: 标准转发，保留消息来源。
Copy 模式: 复制消息内容，作为新消息发送，可突破源频道的转发限制。
高级内容过滤:
过滤逻辑 (新): 清晰的过滤优先级： 白名单 (高) > 黑名单 (中) > 默认通过 (低)。
白名单: 仅转发包含特定关键词的消息。
黑名单: 过滤广告、无意义内容（支持正则）。
智能内容处理:
内容替换: 自动替换消息中的指定文本，如广告标签、频道链接等。
高级链接提取: (TODO) 解析超链接、机器人回复、评论区中的隐藏链接。
精准分发 (新):
话题分发: (新) 根据关键词将消息精准分发到目标群组的不同话题 (Topics) 中。
多频道/群组分发: (新) 根据关键词将消息分发到不同的目标频道或群组。
按文件类型分发 (新): (新) 根据文件名 (*.mkv) 或文件类型 (video/mp4) 分发。
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
获取 Session String (重要!):
这是什么? Session String 是你的登录凭证，它不是你的 api_id 或手机号。
为什么需要? 在 Docker 中，程序无法像在电脑上那样“弹窗让你输入验证码”。String Session 是一种将你的“登录状态”复制粘贴到配置文件中的方法，让服务器程序可以免密登录。
如何获取?
(推荐) 在你的本地电脑上克隆 StringSession 项目。
pip install telethon
python generate.py
按照提示输入你的 api_id 和 api_hash，然后输入手机号和验证码。
它会打印出一长串文本（AQ...），这就是你的 session_string。
安全提示: 这个字符串等同于你的账号密码，绝对不要泄露给任何人。
编辑 config.yaml:
accounts: 填入你的 api_id, api_hash 和刚生成的 session_string。
sources: 填入你要监控的源频道/群组 ID。运行 python ultimate_forwarder.py export 获取 ID。
targets: 填入默认的目标频道/群组 ID。
targets.distribution_rules: (可选) 参照模板配置你的话题和文件分发规则。
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
id 必须是数字ID (运行 export 模式获取)。sources 是一个列表，你可以添加任意多个。
targets (转发目标)
目标可以是频道或群组。
default_target: 必需，未命中任何分发规则时的默认目标。
distribution_rules: (新) 核心功能。
规则按顺序匹配，第一个命中的规则生效。
规则内的条件 (keywords, file_types, file_name_patterns) 是 "OR" (或) 关系，满足任意一个即命中。
keywords: 匹配消息文本。
file_types: (新) 匹配文件的 MIME Type (例如: "video/mp4", "image/jpeg")。
file_name_patterns: (新) 匹配文件名 (例如: ".mkv", "1080p", "S01E")。
topic_id: (新) 目标话题 ID。如果目标是普通频道或群组，省略或设为 null。
forwarding (转发行为)
mode: "copy": 推荐。可以突破源频道"禁止转发"的限制。
forward_new_only: true: 推荐。true 表示只处理新消息；false 表示会从头扫描所有源频道的历史消息。
过滤器逻辑 (重要)
白名单 (Whitelist) 模式:
如果 whitelist: enable: true:
只有 命中 白名单的消息会 [通过]，并跳过所有黑名单。
未命中白名单的消息会被 [过滤]。
黑名单 (Blacklist) 模式:
如果 whitelist: enable: false:
消息默认 [通过]。
但如果 命中 ad_filter 或 content_filter，消息会被 [过滤]。
默认模式:
如果所有过滤器都 enable: false，所有消息 [通过]。
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
