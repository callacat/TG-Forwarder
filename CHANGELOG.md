# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 与[语义化版本](https://semver.org/lang/zh-CN/)（SemVer）。
## [Unreleased]

### Added

- **渠道抽象（CMP-284，东哥定调）**：AI 端点不再写死在代码常量里。新增 `Channel`
  `{name, protocol(systemone|openai), base_url, models[], api_key_ref}`，落
  `app_config` 的 `channels` 键（复用既有 `ConfigRepository`，零 schema 迁移）。
  `ad_judge` / `ai_content` / `digest` 三个消费者各有 `settings.<consumer>_channel`，
  选中后由渠道接管该消费者的 `base_url` + `model` + `api_key`；阈值/模糊区/超时等
  非端点字段仍在各自段里。面板新增「渠道」tab：增/改/删 + 一键探测（`GET /models`，
  端点不支持时退化为根路径存活探针——现网 laya 只有 `/v1/systemone` 与
  `/health/ready`，实测其余全 404）。
  - **密钥仍在 config.yaml**：`api_key_ref` 存的是**段名**不是密钥，面板默认生成
    `channel_<渠道名>.api_key`，也可指向既有段复用同一个 key。`_is_secret_section`
    用「固定白名单 + `channel_` 前缀 + 受限字符集」放行，`/api/ai-secret` 只回布尔位。
    渠道名限制在 `[A-Za-z0-9_.-]{1,64}`（它同时是 yaml 段名后缀）。
  - **回退能力**：`*_channel` 为空 → 完全走旧 `base_url/model`（老库升级行为零变化）；
    渠道被删/未选 → 同样回落且**不**静默换用别的渠道；被消费者引用的渠道拒绝删除
    （静默删会让该消费者悄悄打回旧端点）。
  - `AdJudge` 新增 `api_key`（laya 要求 `Authorization: Bearer`，jev 这类本机端点
    不带 key 时行为与升级前完全一致）；`api_key` 已纳入 `_reconcile_ai_features`
    的重建键，否则面板换渠道密钥后 `/reload` 沿用旧实例 → 401（与 F10 同款教训）。
  - **渠道协议真正生效**：`AdJudge` 新增 `protocol`，`openai` 渠道走
    `/chat/completions` + 判据提示词、要模型回 `{"is_ad": true|false}` 再映射成
    0/1 分（复用同一套阈值语义；解析不出明确布尔 → None 放行，绝不当广告）；
    `systemone` 渠道行为不变。此前 `protocol` 只做了校验、消费者未消费，等于
    「面板能选 openai 渠道但 ad_judge 照样打 `/v1/systemone`」。面板 ad_judge
    渠道下拉也不再只列 `systemone`（换模型不必先改代码）。`protocol` 已纳入
    热重载重建键。

- **laya 接入准备：协议兼容，但默认阈值在真实样本上不可用（阻塞项）**。
  `systemone` 协议与现网 `noul` 直接兼容，无需适配层——laya 的 `noul` 分支靠
  `re.search(r"ad|advert|广告", instructions)` 判定「这题是不是广告题」，现网
  `_INSTRUCTIONS` 命中，故返回真实概率而非中性 0.5。**但**：
  - 它是个 ModernBERT 二分类器（`id2label={0:ad,1:normal}`），**只读 `state` 里的
    正文，完全忽略 `instructions`/`criteria`**；改判据措辞对分数无效（实测四种
    截然不同的 instructions 对同一条消息给出同一分数 0.9985）。
  - `seq_len` 硬上限 512 且 `state` 必须是 `{"message":{"text":…}}`（裸字符串会被
    判 unsupported 返回 0.5）。
  - **用 09-23 现网真实样本集（LDAPK 3755-3761，4 条 App 资源 / 3 条硬广）回放，
    0.85/0.60 阈值会把 3/4 的 App 资源误杀为广告**：3755 电视影院 0.9966、
    3756 全能工具箱 0.9101、3759 口袋猫漫画 0.9765 全部 >= 0.85；3757 繁华漫画
    0.8443 落在模糊区。而真广告 3758 是 0.8875——**广告最低分(0.8875) < 资源最高
    分(0.9966)，任何单一阈值都无法分开**（gap = -0.109）。卡内那张 0.5497 的表
    对应的是一条被截短的样本（"电视影院TV_9.1_会员版 免费观看全网影视"），
    同一资源的完整现网文案是 0.9966，加一句"一款手机影视App"就跳到 0.9737，
    说明分数对**措辞极其敏感**、不是稳定的内容信号。
  ⇒ 结论：渠道/协议层已就绪且可一键回退 jev，但 **laya 不能按 0.85/0.60 直接切
  现网**；需东哥拍板是「重新标定（可能需要双阈值+白名单/规则兜底）」还是
  「换用其它模型/继续 jev」。数据表见 CMP-284 卡内评论。

### Fixed

- **F14 解析器加固版自身的回退：落单引号致合法 JSON 漏检（Codex 审查 t_15e9ed46 驳回项）**：加固版用 `_brace_positions` 的 `in_str` 状态机预判字符串边界、跳过「字符串内」的 `{`，但模型回复是**散文 + JSON 的混合体**，散文里的 ASCII 双引号常不成对（`说明 "这是广告 如下`）。一个落单的 `"` 就把状态永久卡在 `True`，后面合法的 `{…}` **整体漏扫** → 返回 `None` → fail-open 放行广告，**比修复前的切片法还弱**（实测 `说明 "这是广告 如下\n{"is_ad": true, "confidence": 0.99}` 旧法得字典、新法得 `None`）。修法：**删掉这层预判，每个 `{` 都直接试 `raw_decode`**——落在字符串内的 `{` 起点本就解码失败并顺移，过滤多余且会误杀。补 2 条回归（单元 + 端到端真拦），并实测旧实现在同一输入上失败、新实现通过，确认守卫会红不是恒绿。

- **F14 加固后 A3 验收监测串丢失（Codex 审查 minor）**：非 JSON 日志改版后终态行只打「按 fail-open 放行 处理」，不再含 `原文放行` 字面量，而 10-11 基线正是靠 `docker logs | grep '原文放行'` 数出来的——验收期该 grep 会**归零**，让人误判 fail-open 已清零。终态处置行补回与 `fail_open` 取值绑定的稳定字面量：默认打 `原文放行`、严格模式打 `原文丢弃`。中间每次重试的 WARNING 刻意不含这两个串，保证一条消息只被计一次（否则 3 次尝试会被数成 3 次）。补 2 条用例钉住两种模式各自的字面量。

- **catchup 去重表无容量上限（Codex 审查 minor）**：`_catchup_err_counts` 按 `(源, 错误文本)` 累加，错误文本若含变化串（时间戳 / 请求 ID / traceback 行号）键会持续新增，长运行下无界增长。满 512 键即整表清空——正常场景键数 = 源数 × 错误种类，恒定远小于此；清空只让该源的下一次异常重新打第 1 次 ERROR，不丢日志。

- **取证报告自相矛盾与重复小节（Codex 审查 minor）**：`docs/observations/t_15e9ed46/report-t_15e9ed46-f14-failopen.md` 结论先行仍写「根因是解析器缺陷」，与 §4「envelope 仅约 14%、prose 才是大头」直接矛盾，且存在两个 `## 4`、§4/§5 内容重复。结论改为「**存在一个确定的解析器缺陷，但不是全部成因**」并点明 prose 才是残余大头；删掉重复小节、小节序号归位。

- **F14 AI 广告判别大面积 fail-open 放行，广告 apk 漏过（现网 2026-10-11 晚间，东哥 10-12 报障）**：10-11 12:00→10-12 01:00 窗口内 `F14 AI 内容处理响应不是可解析的 JSON（原文放行）` **37 次** vs 正常判定 15 次、拦截 5 次——**AI 判别 fail-open 占比约 71%**，而关键词黑名单环同时段正常拦 30 条，说明只有 AI 环失守，漏掉的正是关键词拦不住的广告 apk 文案。**根因（2026-10-12 直连 `100.64.0.2:8091` 用现网 `gpt-5.6-luna` 实测复现，非推测）**：同一提示词下模型有三种输出形态——①纯散文（「你发来的是一则手游资源推广帖…告诉我具体需求即可」）；②**合法 JSON 被包进 `:::writing{variant="standard" …} … :::` 信封**；③裸 JSON。旧解析器用「首个 `{` 到末个 `}`」切片，在形态 ② 上截出 `{variant="standard"…}\n{…json…}` 这种双对象串，`json.loads` 必炸 → 整条 fail-open 放行。修法：`_extract_json` 改为**逐个候选起点用 `raw_decode` 扫描**，起点失败即顺移，天然跳过信封且兼容原有的代码围栏/前后解说容错——**根因修复（不是重试掩盖），但不是 71% 的全部成因**。另加三层加固：①**解析失败/网络失败指数退避重试**（`max_retries` 默认 2、退避 `retry_backoff` 0.5s 基数），实测模型跑偏（返回散文）概率不低——广告文案下 21 次调用里 9 次，重试能捞回一部分（判定率 4/12 → 10/12）但不是全部，胜过直接放行；②**失败响应落截断原文**（≤500 字符、URL 经既有 `_url_hint` 脱敏成 `t.me/***`、换行压成 `\n` 保一条 WARNING 一行）——本次事故无法归因正是因为日志只有结论没有原文，而脱敏不可省：模型回复会原样带回正文的私有邀请链接；③**失败率打点**（`warn_every`/`warn_ratio`，满 50 条且失败占比 ≥20% 打 WARN 汇总并清零），静默的大面积 fail-open 靠逐条 WARNING 看不出趋势。新增 `fail_open` 开关（**默认 true=放行，铁律不变，绝不因 AI 不可用丢消息**；`false` 为严格模式，AI 不可用时丢弃该条，宁可漏放不漏过广告），且 `build_ai_content` 刻意不用 `or` 兜底以免显式 `False` 被吞成默认 `True`。五项新配置同时入 `_reconcile_ai_features` 的重建键——否则 yaml 改 `fail_open: false` 后 `/reload` 会静默沿用旧实例，配置看着生效实际没变。**未改模型名与 base_url**（`gpt-5.6-luna` / `http://100.64.0.2:8091/v1` 现网在用，处置待东哥拍板）。**逐条独立实测**（每组 12 条内容唯一的广告/普通消息，按消息级 `confidence>0` 计判定，避开缓存命中）：修复前等价行为（`max_retries=0`）广告判定 **4/12**，与现网 71% fail-open 基线同量级；修复后默认（`max_retries=2`）广告 **10/12**、普通 **12/12**。**根因权重如实修正**：逐条分类显示 envelope 形态仅占约 14%（21 次调用 3 次），**prose（模型对广告文案改写输出安全 advisories 散文）才是大头**，且集中在广告文案上——越像广告越易解析失败，与「广告漏放」方向叠加，这解释了现网漏的偏偏是 apk 广告。故本修复解决的是「合法 JSON 被误杀」这个确定缺陷 + 重试兜住部分跑偏，**不保证 100%**，残余缺口需换更听话的模型或收紧 `fail_open` 才能进一步压降（均待东哥拍板）。完整取证、复现脚本与原始输出见 `docs/observations/t_15e9ed46/`。

- **catchup 无权限源每 5 分钟刷一条 ERROR（现网 `-1002926374261` 10-11 全天 288 条纯噪音）**：源失效（私有频道无权限/被踢）后每轮 cron 各打一条 ERROR，还会把同窗口的真事故日志冲走。改为按 `(源, 错误文本)` 去重限频：连刷只打第 1 次与每 12 次 1 次 ERROR，其余降 DEBUG；错误内容变化（如换成 FloodWait）重新计数，不被旧去重吃掉。

- **同名同大小 APK 靠 `file_id` 逃过去重，被转发两次（现网 2026-10-13 20:21/20:25 实锤）**：去重键 `message_hash` 的 document 分支只用 `doc:{file_id}:{size}`，而**同一份文件每次重传 `file_id` 必变**，于是每批重发都是全新键，同内容被转发两次——目标群 `.../1/19057-19062`（姨太坊/推特.中国/91短视频/奶糖Baby/色色AI/萝莉快递，`caption` 全空）与 `.../1/19065-19070` 文件名+大小逐条一致即此。改为文件名优先：`doc:{文件名或file_id}:{size}`，无文件名的裸 `Document`（无 `DocumentAttributeFilename`）仍退回 `file_id`，老行为零变化。**刻意不并入既有 F1 跨源指纹**：F1 的 `file:{name}:{size}` 能力早已存在（`content_fingerprints`），但闸在 `deduplication.cross_source_enable` 上，该开关现网为默认 `False`、且 `app_config` 无 `deduplication` 行，实测 `dedup_hashes` 390 条里 `file:` 前缀 **0** 条——它是「另一个开关 + 另一套跨源 URL 语义」，打开它等于顺带开启全源 URL 指纹去重，远超本次止血范围，属另一项决策。单源 `message_hash` 这条是本次最小闭环。回归 `test_doc_hash_same_name_different_file_id` / `test_doc_hash_different_content_differs`，另有「无文件名仍走 file_id」「photo 键不变」的老用例兜底。

> **验收状态**：A1 非 JSON 日志含截断原文 ✅、A2 重试/兜底有可跑单测 ✅（全套 546 passed，
> CI run 38164398573 pytest + docker 双 job 绿）。本卡闭环判据为**代码修复过审 + tag rc.15 +
> CI 绿 + oraarm 部署 + `/api/version` 三验一致**；**24h 现网复测是部署后的异步观察项**，
> 结果由运维在卡内回 comment 收口，不在本卡循环（老马 2026-10-12 裁决，防审查死循环）。
> 复测口径 `docker logs tgf | grep '原文放行'`——该原子字面量已钉回终态处置行
> （放行分支打「原文放行」、fail-closed 分支打「原文丢弃」），一条消息只出现一次，
> 可直接对 10-11 基线（37 次/13h）。离线逐条测量的判定率 10/12 换算相对降幅约 76.5%，
> 略低于 ≥80%，达标与否以 24h 现网统计为准。
> 模型名与 `fail_open` 默认值待东哥拍板，未回前按现默认（fail-open）执行，代码未擅改。

### Changed

- **`命中分发规则` 从 debug 提到 INFO，行内带 `<源chat>/<消息id> → chat=/topic=`**：现网日志级别 INFO，原行是 `logger.debug`，**成功转发在日志里一行都没有**——漏放排查时无法区分「已转发」与「没进管线」，也无法把目标群消息号关联回源消息（`grep 命中分发规则` 在 30 分钟窗口返回 0 条即此故，2026-10-13 排查受阻的直接原因）。`find_target` 加两个仅进日志的可选参 `src`/`mid`（关键字参数，不传即旧行为，已有调用方与用例不受影响），行内补规则名、源 chat、源消息 id、目标 chat 与 topic。**这是后续所有漏放/误杀排查的前置条件**，本次未改任何过滤/转发语义。


## [v3.0.0-rc.14] - 2026-09-29

> rc.14：**F14 清洗丢下载链接修复**（东哥 2026-09-29 现网报障）。prompt 判定/清洗两段规则对齐 + URL 守恒护栏 + 裸链/尾标点/日志脱敏收尾，来自审查卡 t_ee732883 与 t_9f1fe7d8（均 verdict=pass）。

### Fixed

- **F14 清洗把下载链接整行删掉（现网 2026-09-29 报，`t.me/LCGFX/10145` → 目标话题消息 URL 数 1→0）**：F14 一次调用同时出「广告判定 + 清洗正文」，系统提示词**两段规则打架**——判定段写着「频道内正常分享软件…（含下载链接）属于内容本身，不算广告」，清洗段的删除清单却只列了「频道尾巴/引流尾巴」「无关注价值的自推链接行」「纯广告行、装饰符号」，**没有一条要求保留链接**。于是 `➡️➡️➡️ [**『点击下载』**](url)⬅️⬅️⬅️⬅️` 被模型归入「引流尾巴/自推链接行」删掉，而同一响应里的判定段还判着 `is_ad=False conf=0.99 reason='属于频道软件资源分享，下载链接是内容本身'`。两层修复：①提示词清洗段前置两条硬规则（下载/资源链接必须逐字保留，含「点击下载」「下载地址」「网盘链接」及任何 http(s) URL；「引流尾巴」限定为与内容无关的推广即关注公众号/进群/加微，**不含下载链接**，拿不准一律保留），把判定段的链接豁免在清洗段复述一遍使两段自洽。②`_acceptable_clean` 增加 **URL 守恒护栏**：清洗结果丢了原文任一 URL 即整条弃用回原文，与既有双重长度护栏同位置同风格——提示词是概率保证，这条是确定保证（宁可少清洗，也不静默丢链接）。URL 提取不复用 `link_checker.URL_PATTERN`（`https://?[^\s]+` 会把链接后的装饰 emoji 一并吞掉，两边比对必然不等而误伤正常清洗），另起一条截到右括号/空格的窄正则。**广告判定行为零变化**（`is_ad`/`confidence`/`threshold` 逻辑未动），只收紧清洗侧采纳条件。

- **F14 URL 守恒护栏收尾三项（rc.14 前，审查 `t_ee732883` verdict=pass 留的 2 minor + 2 nit）**：①**裸链漏网**——原正则只认带 scheme 的链接，而 TG 原文里不带 `https://` 直接写 `t.me/xxx` 极常见，模型删掉裸链行时两侧差集恒为空、护栏形同虚设，同型故障换个链接写法即复发。正则补两条形态：`www.` 开头、以及「裸域名 + TLD + 路径」。**匹配刻意收窄**：字集只取 RFC 3986 的 URL 合法字符（句读/括号/引号/CJK/emoji 天然截断，不会像 `link_checker.URL_PATTERN` 那样把装饰吞进链接）；裸链只收「TLD + 路径」而非光域名——宽了会把 `v2.1`、`2024.09`、`192.168.1.1/24`、`setup.exe` 这类正常文本当成链接，正常清洗反被误判成丢链。②**尾随标点误伤**——模型把 `https://a.com/1` 规范化成 `https://a.com/1,`（或反向）时字符串差集非空，会误判丢链接、整条合格清洗被丢弃并打出误导性 WARNING「链接被误删」，等于清洗静默失效；改为比对前两侧都剥尾随标点（`。。，、；：）】]」` 等）再比。③**WARNING 落盘私有链接**——原实现在日志里打进丢失链接的前 80 字符原文，私有邀请链接（`https://t.me/+xxxx`）会被 `docker logs` 读走，改为只打 host + 路径掩码（`t.me/***`），userinfo（`https://user:token@host/x`）同样剥掉——那截也是凭据不是 host。三项均只动清洗侧采纳条件与日志脱敏，**广告判定行为依旧零变化**（`is_ad`/`confidence`/`threshold` 未动）。自查时另发现并修掉裸链正则自身的一个隐患：裸链分支可从正文任意位置起配，缺了前置否定环视就会在每个位置重扫，正文里一串 `a.a.a…` 足以把它拖成平方级（实测 4096 字符 **2.9s**，堵在转发事件循环里），加环视后同一输入 3ms，并补 `test_pathological_input_does_not_blow_up` 钉住。

## [v3.0.0-rc.13] - 2026-09-29

> 本节为 rc.9 归位之后的累积变更：含 rc.10/rc.11/rc.12 期间已部署但当时漏归位的条目（rc.12 发版未做 CHANGELOG 归位），随 rc.13 一并归位。
### Added

- **F10 AI 摘要补齐面板配置入口（对齐 F14 模式）**：F10 此前只有「开关 + 间隔」两个字段，模型/端点/密钥只能手改 `config.yaml`，且改完不重启容器不生效、per 源开关只能 `sqlite UPDATE`——现四项缺口一次补齐。①`SystemSettings` 新增 `digest_base_url` / `digest_model` 镜像键（`"digest_"` + `DigestConfig` 字段名约定，与 F13/F14 同机制：落表值按存在性逐键覆盖 yaml `digest` 段，面板没管过的键保持 yaml 值），面板「系统设置 → 实验功能（M4 · AI）」出现端点/模型输入框。`digest_api_key` **刻意不进 `SystemSettings`**——密钥进去即明文落 sqlite 且可被 `GET /api/settings` 回读，与 F14 同纪律。②`AI_SECRET_SECTIONS` 并入 `digest` 段，F10 密钥走既有 `/api/ai-secret` 只写通道落 `config.yaml` 的 `digest.api_key`（行级定点改写、保留注释、`.bak` + 临时文件 `os.replace` 原子换入），读侧只回布尔；此前 F10 未接该通道，axonhub 8091 端点校验 401 时 key 无处可填。③F10 摘要管线改为按 `(base_url, model, api_key)` 全字段变化热重载重建（原先只判「管线为 None」，非空管线会一直沿用启动时的 `LLMClient`，改配置等于没改）；**间隔刻意不入重建键**——窗口到期判据本就由快照驱动（`DigestPipeline._window_for` 读 `snapshot.digest.interval_seconds`），改间隔后新开的窗口即用新间隔、已开窗口跑完才生效（`RollingWindow.interval` 构造时冻结），而此时重建会丢已缓冲的滚动窗口消息。④新增 `POST /api/sources/update`（按 `identifier` 匹配，`source_repo.save` 走 upsert）打通 per 源 `digest_enabled` 面板入口，「监控源」页每个源卡片加摘要开关，写后照 add 端点惯例热重载（落表却没刷新快照不得声称已生效）。默认关=现网行为零变化。**顺带修正一处升级语义**：旧实现里 `digest.enabled`/`interval_seconds` 是「任一键落表则两键一起以落表为准」，与本段注释声明的「存在性逐键覆盖」自相矛盾——只存过 `digest_enabled` 的早期库会把 yaml 里的间隔冲成默认值 1800；现统一为逐键覆盖，与 F13/F14 一致，yaml 值不再被面板未管过的键清掉。

- **AI 密钥可在面板填写（F11/F14，只写不回读）**：新增 `GET/POST /api/ai-secret` 与 `config.set_yaml_secret`。面板「实验功能 / AI 处理」tab 出现「API 密钥」密码框 + 「✓ 已配置 / 未配置」布尔状态位。**为什么不并进 `SystemSettings`**：`/api/settings` 原样返回它、`/api/settings/update` 会把它 `model_dump` 落进面板 sqlite——密钥进去即明文入库且登录者可 GET 回读。故独立成只写通道：POST 落 `config.yaml`（已被 `.gitignore` 忽略），GET 只回布尔，**任何响应都不含明文**。写文件走**行级定点改写**而非 yaml 往返——config.yaml 是带注释的手工维护文件，`safe_dump` 回写一次注释全没；改写后先在内存校验能解析且值对得上，再落 `.bak` 备份 + 同目录临时文件 `os.replace` 原子换入。段名走 `AI_SECRET_SECTIONS` 白名单（防越权写任意顶层键），值含换行直接拒（换行会破坏行级结构）。面板输入框不绑进 `settings`，避免被 `saveSettings` 一并 POST 进 sqlite（`TestPanelUi` 钉这条）。

- **F11/F14 支持在 config.yaml 直填 API key（`api_key`）**：此前 F11/F14 只有 `api_key_env` 一条通道，需靠环境变量注入，而容器重建后要重新 `-e` 注入、很容易漏。现新增 `translate.api_key` / `ai_content.api_key` 直填字段，写在 `config.yaml`（已被 `.gitignore` 忽略），**不进仓库、不进面板 sqlite、不会出现在 `/api/settings` 响应里**——刻意不镜像进 `SystemSettings`（面板只镜像 6 个非敏感键）。取值优先级：`api_key` 非 `None` 时直接用（**显式填 `""` = 明确不带鉴权，此时 `api_key_env` 会被忽略**）；不填/留 `None` 才回退读 `api_key_env` 环境变量。取不到 key 一律 fail-open 原文放行，不阻塞转发。
- **F14 AI 结构化内容处理**：转发链路新增一次模型调用同时产出「广告判定 + 清洗正文」，输出为结构化 JSON（`is_ad` / `ad_type` / `confidence` / `cleaned_text` / `reason`）。位置在 F13 判别之后、去重之前；**是否丢弃由服务端按 `confidence >= threshold` 决定**，不采信模型自行拍板或返回哨兵串——同类项目（如 Heavrnl/TelegramForwarder）让模型回 `#不转发` 即丢弃的方案，会在模型误吐该串时静默丢消息，撞本项目 fail-open 铁律。请求失败/超时/解析失败/字段缺失/无 key 一律原文放行。判据措辞沿用 F13 于 2026-09-23 实测标定的版本（锚定「商业广告/引流牟利」，资源分享放行、卖号硬广/群推广拦截），不重新试错。端点走 OpenAI 兼容 `/chat/completions`（默认 axonhub glm-5.3-flash，与 F10/F11 同源），结果复用 `TranslationCache`（LRU+TTL）避免重复计费。
- **F14 清洗护栏（防丢数据）**：模型返回的 `cleaned_text` 需通过双重长度界（相对原文 < `min_ratio` = 内容截失、> `max_ratio` = 模型跑偏）才被采用，不通过则用原文。**正文超过 `max_text_chars` 时只判广告不改文**——模型只看得到前缀，若用其结果整体替换长文本会把模型没看见的尾巴一并抹掉（长度护栏拦不住：5000/10000 = 50% 在界内）。清洗可独立关掉（`clean_enabled: false`）退化为「只判广告不改文」，作为改正文前的小流量闸门。运行期每条判定的 `is_ad`/`confidence`/`type`/`reason` 落 INFO 日志，沿用 F13 的误杀排查观测。
- **F14 配置与面板入口**：yaml `ai_content:` 段（含 `sources` 源白名单、`json_mode`、清洗护栏、缓存等进阶项）+ Web 面板「AI 处理」tab（开关/端点/模型/阈值/清洗开关/超时），两者改后均即时热重载生效，无需重启容器。面板只镜像 6 个键，进阶项不落表——面板保存不会冲掉 yaml 里的 `sources`/`json_mode`/护栏配置。`min_ratio >= max_ratio` 的非法组合由配置校验直接 422 拒绝，不放到热重载期才失败。**默认关=现网行为零变化**（未装配时转发路径为绝对零路径）。
- **F13/F14 双开告警**：两者都开启时装配期打 WARN——每条消息会发两次广告判定请求，且两套判据可能给出不一致结论，建议只开其一。不自动关掉任一方。

### Fixed

- **`/api/sources/update` 部分体会静默洗掉无关 per 源配置（真数据丢失，200 静默通过）**：端点原为整条替换语义——pydantic 对请求体未传的字段填默认值后整条 `model_dump` 落库，`POST {"identifier":-100777,"digest_enabled":true}` 会把该源的 `sync_edits`/`sync_deletes` 洗成 `False`、`age_cutoff_hours`/`header_template` 洗成 `None`、`replies_limit` 洗成 `10`，并返回 200。面板目前回传全字段所以线上没炸，但任何只带部分字段的调用方（脚本、curl、未来面板改动）都会静默丢配置。现改为 **merge 语义**：只覆盖请求体里**显式出现**的字段（pydantic `model_fields_set`），未传字段沿用库里原值。`identifier` 是匹配键而非待改字段，显式排除——否则请求体用字符串 id 时会把库里的 int id 改写掉。面板回传全字段的行为不受影响（`exclude_unset` 对全字段请求等价于原语义），F14/F10 通道零变更。
- **面板填密钥会写出重复顶层键，静默丢弃用户已配的 `enabled`/`model`**：`config_template.yaml` 里 `digest:`/`ai_content:`/`translate:` 三段**出厂即整段注释**。原 `set_yaml_secret` 只按「顶格非注释」的段头定位段，注释态的段一律判定为不存在 → 在文件尾追加**第二个**同名顶层 `digest:`。当下 yaml 仍能解析（后一个覆盖前一个，行为碰巧正确），但日后用户手工取消注释即得到重复键，`yaml.safe_load` 只认最后一个，用户自己配的 `enabled`/`model` 被静默丢弃。现改为：整段不存在时先就地取消同名注释段的**段头**再定点写入，段体保持注释态（那些键值是用户意图，替他取消注释等于替他改配置）。日后手工取消段体注释即并入本段，不会产生第二个顶层键——重复键的根因断掉。相邻注释块（模板里 `digest:` 之后全是注释态的 `translate`/`ad_judge`/`ai_content`）不被吞进改写范围。
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