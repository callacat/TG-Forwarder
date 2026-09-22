# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与[语义化版本](https://semver.org/lang/zh-CN/)（SemVer）。

## [Unreleased]

### Added

- **AI 广告判别器（jev-1.13）**：转发前用 System One 决策模型判断广告并过滤（默认关=现网行为零变化）。位置在 `should_filter` 之后、去重之前；`verdict=True` 拦截、`False` 放行、`None`（模糊区 0.60-0.85 / 超时 / 异常）fail-open 放行。仅 yaml 配置（`ad_judge:` 段），经 `/reload` 或面板保存热重载生效。

### Fixed

- **ad_judge 热重载字段覆盖**：`/reload` 现按 `(base_url, model, threshold, fuzzy_low, timeout)` 全字段变化重建判别器；原先只比 `threshold`，改端点/模型/模糊下界/超时后不重建、静默沿用旧配置（ad_judge 不经面板表，`/reload` 是唯一生效通道）。

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