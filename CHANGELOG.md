# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与[语义化版本](https://semver.org/lang/zh-CN/)（SemVer）。

## [Unreleased]

### Added

- **AI 密钥可在面板填写（F11/F14，只写不回读）**：新增 `GET/POST /api/ai-secret` 与 `config.set_yaml_secret`。面板「实验功能 / AI 处理」tab 出现「API 密钥」密码框 + 「✓ 已配置 / 未配置」布尔状态位。**为什么不并进 `SystemSettings`**：`/api/settings` 原样返回它、`/api/settings/update` 会把它 `model_dump` 落进面板 sqlite——密钥进去即明文入库且登录者可 GET 回读。故独立成只写通道：POST 落 `config.yaml`（已被 `.gitignore` 忽略），GET 只回布尔，**任何响应都不含明文**。写文件走**行级定点改写**而非 yaml 往返——config.yaml 是带注释的手工维护文件，`safe_dump` 回写一次注释全没；改写后先在内存校验能解析且值对得上，再落 `.bak` 备份 + 同目录临时文件 `os.replace` 原子换入。段名走 `AI_SECRET_SECTIONS` 白名单（防越权写任意顶层键），值含换行直接拒（换行会破坏行级结构）。面板输入框不绑进 `settings`，避免被 `saveSettings` 一并 POST 进 sqlite（`TestPanelUi` 钉这条）。

- **F11/F14 支持在 config.yaml 直填 API key（`api_key`）**：此前 F11/F14 只有 `api_key_env` 一条通道，需靠环境变量注入，而容器重建后要重新 `-e` 注入、很容易漏。现新增 `translate.api_key` / `ai_content.api_key` 直填字段，写在 `config.yaml`（已被 `.gitignore` 忽略），**不进仓库、不进面板 sqlite、不会出现在 `/api/settings` 响应里**——刻意不镜像进 `SystemSettings`（面板只镜像 6 个非敏感键）。取值优先级：`api_key` 非 `None` 时直接用（**显式填 `""` = 明确不带鉴权，此时 `api_key_env` 会被忽略**）；不填/留 `None` 才回退读 `api_key_env` 环境变量。取不到 key 一律 fail-open 原文放行，不阻塞转发。
- **F14 AI 结构化内容处理**：转发链路新增一次模型调用同时产出「广告判定 + 清洗正文」，输出为结构化 JSON（`is_ad` / `ad_type` / `confidence` / `cleaned_text` / `reason`）。位置在 F13 判别之后、去重之前；**是否丢弃由服务端按 `confidence >= threshold` 决定**，不采信模型自行拍板或返回哨兵串——同类项目（如 Heavrnl/TelegramForwarder）让模型回 `#不转发` 即丢弃的方案，会在模型误吐该串时静默丢消息，撞本项目 fail-open 铁律。请求失败/超时/解析失败/字段缺失/无 key 一律原文放行。判据措辞沿用 F13 于 2026-09-23 实测标定的版本（锚定「商业广告/引流牟利」，资源分享放行、卖号硬广/群推广拦截），不重新试错。端点走 OpenAI 兼容 `/chat/completions`（默认 axonhub glm-5.3-flash，与 F10/F11 同源），结果复用 `TranslationCache`（LRU+TTL）避免重复计费。
- **F14 清洗护栏（防丢数据）**：模型返回的 `cleaned_text` 需通过双重长度界（相对原文 < `min_ratio` = 内容截失、> `max_ratio` = 模型跑偏）才被采用，不通过则用原文。**正文超过 `max_text_chars` 时只判广告不改文**——模型只看得到前缀，若用其结果整体替换长文本会把模型没看见的尾巴一并抹掉（长度护栏拦不住：5000/10000 = 50% 在界内）。清洗可独立关掉（`clean_enabled: false`）退化为「只判广告不改文」，作为改正文前的小流量闸门。运行期每条判定的 `is_ad`/`confidence`/`type`/`reason` 落 INFO 日志，沿用 F13 的误杀排查观测。
- **F14 配置与面板入口**：yaml `ai_content:` 段（含 `sources` 源白名单、`json_mode`、清洗护栏、缓存等进阶项）+ Web 面板「AI 处理」tab（开关/端点/模型/阈值/清洗开关/超时），两者改后均即时热重载生效，无需重启容器。面板只镜像 6 个键，进阶项不落表——面板保存不会冲掉 yaml 里的 `sources`/`json_mode`/护栏配置。`min_ratio >= max_ratio` 的非法组合由配置校验直接 422 拒绝，不放到热重载期才失败。**默认关=现网行为零变化**（未装配时转发路径为绝对零路径）。
- **F13/F14 双开告警**：两者都开启时装配期打 WARN——每条消息会发两次广告判定请求，且两套判据可能给出不一致结论，建议只开其一。不自动关掉任一方。

### Fixed

- **空洞对账历史倒灌（rc.9 事故止血，rc.10）**：rc.9 的 catchup 空洞段按「progress 前 50 条 id」开窗，对低频源 50 条 = 数月跨度，叠加 LRU 重启清零 + dedup hash TTL 已清 → 三层防线同时失效，现网 2026-09-23 23:33-23:37 向目标群重发 254 条历史（东哥实锤）。修复：空洞段加 `_CATCHUP_HOLE_MAX_AGE=6h` 年龄闸——事件间隙漏收是分钟级，超 6h 一律不补。rc.10 上线首轮 catchup 验证：「命中分发规则」=0、重复拦 40，零倒灌（对照 rc.9 同期命中 185）。
- **同一张图并发去重失效，转发两次到不同话题（现网 2026-09-26 报，`.../27/18082` 与 `.../1/18085` 同图）**：`process_message` 的去重是 `check_hash → send → add_hash` 三段，中间隔着一次网络发送；Telethon 每个事件 handler 是独立 task 并发跑，首条尚未 `add_hash` 时第二条必然查不到 → 两条都放行。`_seen_recently` LRU 按 `chat_id/message_id` 键，两个不同消息 id 不命中，挡不住。修复：新增 `_inflight_hashes` 占位集，通过检查后立即占位、`finally` 无条件释放，只有真正发出的那条才落库（单源指纹与 F1 跨源指纹均占位）；发送失败/catchup 重试语义不变。**实现要点**：判重必须「先 `await check_hash` → 再同步 `in` 判断 → 再同步占位」，三者之间不能有 await——写成 `h in inflight or await check_hash(h)` 会让 `in` 在 await 前求值，两个 task 同时看到未占位照样双双放行。回归用例 `test_concurrent_same_photo_forwards_once`，未修复版与「短路顺序写错」版均必红。

## [v3.0.0-rc.9] - 2026-09-23

### Added

- **catchup 源 id 空洞对账（C 方案）**：根治「事件间隙漏收 + 后续消息推过 progress → 永久跳过」（实锤 QTFXS0/3119）。`_catchup_source` 增量段前反扫 `[progress-50, progress]` 窗口，交 `process_message` 由 LRU 分辨已处理/空洞补处理；`set_progress` 单调不倒退（防空洞 finally 回拉水位）；`_CATCHUP_LRU_SIZE 200→2000`（15 源×50 窗口全量覆盖防互挤）。测试：空洞补收/单调保护/短源守卫 3 例。

## [Unreleased-历史]

### Added

- **AI 广告判别器（jev-1.13）**：转发前用 System One 决策模型判断广告并过滤（默认关=现网行为零变化）。位置在 `should_filter` 之后、去重之前；`verdict=True` 拦截、`False` 放行、`None`（模糊区 0.60-0.85 / 超时 / 异常）fail-open 放行。配置入口：yaml `ad_judge:` 段或 Web 面板「AI 判别」tab，两者均即时热重载生效。
- **F13 AI 广告判别面板入口**：sidebar 新增「AI 判别」tab，可开关并按 `AdJudgeConfig` 全字段配置（`base_url`/`model`/`threshold`/`fuzzy_low`/`timeout`），保存走既有 `/api/settings/update` → 热重载生效。字段以 `SystemSettings` 镜像键（`ad_judge_*`）落表，沿用 F10-F12 的「存在性逐键覆盖 yaml」机制；`fuzzy_low > threshold` 由模型校验直接 422 拒绝，不放到热重载期才失败。默认关=现网行为零变化。
- **面板版本号（单一事实源）**：新增 `tg_forwarder/version.py` 统一解析版本（镜像注入 `TG_FORWARDER_VERSION` → `git describe` → dev 占位），新增 `GET /api/version`，FastAPI 元信息同步取该值（原先硬编码 `version="3.0"`）；系统设置页展示「当前版本」。Dockerfile 与两个 workflow 经 `build-args` 注入 `git describe --tags --always` 结果，前端不硬编码第二份版本号。

### Fixed

- **热重载后规则目标丢失（rc.6 回归，现网 2026-09-23 面板保存致转发全停）**：面板保存/规则写操作触发的热重载只整体替换配置快照，`config.snapshot()` 返回的新规则对象 `resolved_target_id` 默认为 None，而重解析目标的 `resolve_targets` 原先只在启动路径调用 → `find_target` 命中规则仍返回 `(None, ...)` → 「无有效目标」丢弃全部消息（现网 08:33/08:35 两次面板保存后丢 9 条）。修复：热重载链路补调 `resolve_targets(healthy[0])` 重解析默认目标与全部分发规则；无健康账号或解析失败时按 `target_identifier` 沿用上一份快照的解析值并告警（标识符改过的规则不沿用），不因一次重载清空可用目标。
- **热重载链路接线回归测试（rc.7 补强）**：rc.7 的用例只覆盖辅助函数 `_resolve_targets_on_reload`，未覆盖「回调实际调用它」这一接线点——而 rc.6 事故正是接线缺失。现将热重载落盘后动作抽为模块级 `_apply_hot_reload(forwarder, accounts, new_cfg)`（`update_settings_cb` 与 `/reload` 共用），测试直接打真实链路；故障注入验证：移除重解析调用后接线用例必红（2 failed），恢复即 373 passed。
- **ad_judge 热重载字段覆盖**：`/reload` 现按 `(base_url, model, threshold, fuzzy_low, timeout)` 全字段变化重建判别器；原先只比 `threshold`，改端点/模型/模糊下界/超时后不重建、静默沿用旧配置（当时 ad_judge 只能由 yaml 段配置，`/reload` 是唯一生效通道；F13 面板入口上线后，yaml 与面板两条路径均可触发热重载，且都走同一套全字段重建逻辑）。
- **ad_judge 误杀资源分享（现网 2026-09-23 LDAPK 3755-3761 七条全拦，其中 4 条正常资源消息）**：旧判据措辞「这条消息是广告/推广内容吗？」把资源分享（软件/工具/App 推荐含下载链接）判成「推广」——实测合成样本 0.96（阈值 0.85 拦截）。判据重锚定为「商业广告/引流推广」（卖号/接单/招代理/群推广/付费推销），明示资源分享不算广告；实测：资源分享 0.23-0.38 放行、卖号硬广 0.98 拦、群推广 0.86 拦、闲聊 0.14 放行。另补观测：判别分数落 INFO 日志、被拦日志带源频道与文本摘要 80 字符（原先无分数无文本，误杀无从排查）。

## [v3.0.0-rc.1] - 2026-09-20

v3 全量重写（方案 B）首个候选版本。默认配置与现网 v2.5 行为零变化，所有新能力默认关闭、需显式启用。

### Added

- **v3 骨架**：Web/Bot 层 + storage 层迁移，CI（pytest + docker buildx 多架构）+ 镜像基建
- **配置单一来源**：`config.py`（R4）+ 仓储 dict 构造修复
- **core 三件套**：accounts / supervision / forwarder + link_checker 迁移
- **catchup 兜底扫描**迁移到 v3（老马复验 P1 回归）
- **F1 跨源内容级去重**：链接指纹 + 文件名/大小指纹维度（默认关）
- **F2 编辑/删除实时同步**：message_map 映射 + 事件级联（per 源默认关）
- **F3 死链安全策略**：分域风控 + 删除前二次复核 + delete_marked 档
- **F4-F9 第二梯队**：年龄截断 / 媒体过滤 / 源标注 / 回复抓取 / parse_mode / delivery
- **M3 Web 面板**：真数据仪表盘（uptime + 消息处理统计）、热重载链路（api/reload + 写操作自动热重载）、加载/空/错误态
- **F10 AI digest**：滚动窗口聚合摘要（axonhub 端点，默认关，现网行为零变化）
- **F11 AI 翻译**：axonhub 端点 + 缓存 + fail-open（默认关）
- **F12 语义去重**：fastembed 向量相似度去重（默认关，模型缺失/加载失败优雅降级）

### Fixed

- **P0**：accounts 生产 factory 未 connect 导致降级链全假性失败（老马验收）
- **T1/T2**：BotService 未接线（Critical）+ maintain_once 运行期假活（P2）+ C1-C5 清理
- **R1**：结构化告警被轮询异常旁路（老马断网复验抓到 JSON 缺失）
- **黑洞式断连检测**：探活改主动 RPC + 超时，不再信任 `is_connected` 布尔
- **T4**：长 caption 丢图——发送前按 1024 UTF-16 单位截断保图，MediaCaptionTooLongError 去 caption 重发兜底
- **Web 面板**（Codex 审查 4 项）：热重载失败如实上抛 / session 鉴权 / 离线横幅 / 防双计
- **Web 并行 401**：同时登出提示仅发一次
- **Web 热重载**：过滤类写端点落表后触发快照刷新
- **F10 落库**：源级 `digest_enabled`（db v6 迁移 + 仓储持久化，幂等）
- **Codex REJECT 三项**：删死代码 / 补 await 复用 embed / 修空洞断言

### Docs

- F10 digest 配置命名偏差显式说明（功能契约 `digest_enabled`/`digest_interval` ↔ 此处 `enabled`/`interval_seconds`）