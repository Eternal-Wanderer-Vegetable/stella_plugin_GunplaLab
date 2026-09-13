# GunplaLab 群聊插件设计方案（AstrBot × Stella 双框架适配）

> 状态：方案稿 v1.1（未开始编码；已对照 GunplaLab 项目本体 `E:\GunplaLab\GunplaLab` 实测校准，见 §4.5）
> 依据：`docs/open-data-api.md`（胶情局开放数据 API v1）、Notion《胶情局群机器人》产品需求、AstrBot 插件开发指南、Stella《插件接入规范 v1.0》

---

## 1. 一句话定位

一个同时原生适配 **AstrBot** 与 **Stella** 的「胶情局」群聊插件：把 GunplaLab 开放数据 API 的约 1.9 万款模型底账、现货行情与官方排期带进群聊，让群友用**指令**或**自然语言**完成「查资料、判价格、选模型、看情报、盯降价」，并严格遵守 Notion 需求的内容纪律（区分价格类型、不编造、标注数据时间、不凑数、不刷屏）。

## 2. 设计前提

| 输入 | 关键约束 |
|---|---|
| 开放数据 API v1 | 只读；匿名 30 次/分钟、Bearer Token 300~500 次/分钟；ETag/304 协商缓存；`/snapshot` 全量 gzip 快照实测约 1.5MB（19070 条）；统一响应外壳 `{ok, data, meta}` |
| API 最佳实践 | 模式 A：快照常驻本地内存、群聊查询零网络请求（强烈推荐）；模式 B：仅查价/走势等按需远程单查 |
| Notion 产品需求 | 八大场景；官方价/预订/现货/二手必须区分且只陈述数据源覆盖到的价格；无内容明说；防刷屏；分期建设（今日胶情定时推送靠后、订阅靠后、趣味活动最后） |
| AstrBot | Star 插件体系：`@register`、`@filter.command` / `@filter.llm_tool`（docstring `Args` 段声明参数）、`_conf_schema.json`、`metadata.yaml`、`html_render` |
| Stella | 通过 `astrbot_compat` 兼容层直接加载 AstrBot 插件（零改码）；完整发挥需追加 `capability.toml` 能力声明 + `metadata.yaml` 的 `stella.egress`；工具返回值被摘要至 ≤300 字符；失败必须抛异常；后台任务必须 `context.register_task`；`html_render` 返回本地路径、不可用时返回空串 |

**核心结论：只写一份代码库，以 AstrBot 插件规范为主体，按 Stella 规范补齐可选文件与行为契约——「一次开发，双框架完整可用」，不维护两份代码。**

## 3. 总体架构

```
┌────────────────────────── 消息入口（双通路）──────────────────────────┐
│  指令通路 @filter.command（唤醒前缀 + @机器人，确定性触发，可改状态）    │
│  工具通路 @filter.llm_tool + capability.toml（只读语义触发）            │
└──────────────────────────────┬───────────────────────────────────────┘
                               │ 两通路共用同一批内部实现函数
┌──────────────────────────────▼───────────────────────────────────────┐
│ 业务核心层（纯 Python、不 import astrbot.*，便于单测）                  │
│  api_client   限流感知 HTTP 客户端（envelope 解析 / ETag / 错误码→异常） │
│  snapshot     快照下载·gzip 解压·磁盘缓存·每日增量同步                  │
│  search       归一化 + 别名索引 + 排序（别名/JAN/前缀/模糊 + 消歧）      │
│  advisor      四算/五算黄金线比价结论（附数据时间）                     │
│  recommender  预算/品类/用途过滤 + 评分排序（不凑数）                   │
│  digest       今日胶情聚合（情报 + 排期 + 订阅命中，上限 5 条）          │
│  watch        群级订阅（单品盯盘 + 情报雷达两类）与轮询                  │
│  render       Jinja 模板 → 图片卡；纯文本降级（必须保留）               │
├──────────────────────────────────────────────────────────────────────┤
│ 数据层：模式 A 快照常驻内存（查询 0 延迟）＋ 模式 B 按需远程（行情/走势） │
│ 持久层：StarTools.get_data_dir() 下存快照 gz、别名缓存、群订阅 KV；      │
│         会话级消歧状态仅存内存（TTL 120s）                              │
└──────────────────────────────────────────────────────────────────────┘
```

**为什么采用模式 A 常驻内存**：API 文档强烈推荐；群内高频查询完全不受 30 次/分钟匿名限流约束、零网络延迟；每日仅 1~2 次全量同步（ETag 304 命中则零下载）；约 1.9 万条紧凑字典内存占用数十 MB 量级，可接受。

**依赖策略**：仅声明 `httpx`（Jinja2 优先复用宿主环境，缺失则退化为纯文本输出）。**不用** APScheduler——统一用一条经 `context.register_task` 登记的常驻 asyncio 协程循环（休眠+到期检查），两个框架行为完全一致，热重载可正确回收，且规避 `plugin-check` 第 ⑮ 项。

## 4. 数据接入设计

### 4.1 快照同步流程
1. `initialize()`：优先加载磁盘缓存 `snapshot.json.gz`，**秒级可用**（重启不依赖网络）；
2. 后台请求 `/manifest`（带 `If-None-Match`）：304 或 sha256 一致 → 直接用本地数据；有变化 → 流式下载 `/snapshot`、解压、重建索引、原子替换磁盘缓存（临时文件 + rename）；
3. 每日定时重同步（默认凌晨 4:30 + 30 分钟内随机抖动，可配），由常驻循环驱动；
4. 变更判定双保险：`manifest.json` 静态清册中的 `snapshot.sha256`（对 gz 文件字节计算）为主、snapshot 响应 ETag（实测实现为 `"<mtime>-<size>"` 指纹）为辅；其余 JSON 端点的 ETag 为响应体内容 md5。

### 4.2 内存索引与别名解析
- 主索引 `item_id` → 紧凑档案；本地可索引字段（快照实测全量具备）：`name` / `displayName` / `num`（如 RG32）/ `series` / `grade` / `jan` / `work` / `universe`；
- **快照不含 aliases 与日文名**（已实测）→ 别名解析采用「**本地优先 + 远程兜底**」：本地未命中时调用 `/items?q=关键词&page_size=5`（服务端 LIKE 覆盖 `aliases_json` 与日文名 `name_ja`），命中后把该条详情的 `identifiers.aliases` 回写本地别名缓存并持久化，越用越准；
- 归一化：全半角、大小写、比例词（1/144 等）剥离、去空白；
- 检索排序：别名精确 > 名称前缀 > 名称包含 > JAN 精确 > 模糊；评分/票数作次级排序；默认最多 5 条；
- 消歧标签直接使用快照的 `versionTag`（实测分布：普通版 16040 / 限定版 1805 / 特殊限定版 936 / 特别版 289）+ `grade`，天然满足 Notion「版本不能混答」要求。

### 4.3 限流与容错（面向 429/401/断网）
- 远程请求统一经 `api_client`：最小请求间隔 + 读取 `X-RateLimit-Remaining` 做预算感知；
- 429 → 按 `X-RateLimit-Reset` 退避；401 → 明确提示「API Key 无效，请检查配置」；非 `ok:true` 响应一律映射为带错误码的异常；
- **降级链**：远端不可用时，快照内的资料/官方价查询不受影响；现货价、走势、排期、情报四类回复「胶情局行情服务暂时不可用」；首次启动即断网且无本地快照 → 进入模式 B（每次查询远程单查）并在帮助中提示。

### 4.4 数据新鲜度与诚实边界（Notion 硬要求）
- 所有行情回复带 `observed_at` / `data_as_of`；`/胶助` 与资料卡展示「底账更新时间」；数据陈旧或异常时直说；
- 只陈述数据源覆盖的价格类型：**官方日元定价（含四算/五算换算）+ 拼多多巡检现货价**；被问二手价、淘宝价时明确说明「暂无该渠道数据」，不编造、不用现货价冒充。

### 4.5 真实环境实测结论（2026-09-13，依据 `E:\GunplaLab\GunplaLab\backend`）

设计前已对 GunplaLab 项目本体做实测核对（读 `data_api.py` 实现与真实 `snapshot.json.gz`），结论如下，方案已按此校准：

1. **服务形态**：`tracker_server.py`（默认 3000 端口）同时托管前端与开放数据 API（`/api/v1/data/*` 由 `data_api.dispatch_request` 挂载）；`python run_local.py --public` 可绑 `0.0.0.0` 供局域网内机器人访问；管理端 `admin_server.py` 跑在 8788。**项目暂未上线公网**，因此 P0 阶段 `base_url` 默认指向本机/局域网地址，上线后仅改配置即可；
2. **快照 schema（`gunplalab_snapshot_v1`，19070 条，字段 100% 统一）**：`id / family / category / series / num / name / displayName / jpy / conv4 / conv5 / price / scale / releaseDate / reissueDate / status / recommend / image / source / grade / subGrade / subGradeName / versionTag / universe / universeName / work / workSeries / channelType / brand / rating / ratingCount / jan`；**不含 aliases、不含日文名、不含现货价**——由此确定 4.2 的「本地优先 + 远程兜底」策略与「查价走远程」边界；
3. **item_id 含特殊字符**：除 `78dm_*` 主体外还有 `HGUC#21`、`SD#SD-xx`、`国模*`、`ROBOT魂*` 等形态 → 远程路径参数必须 URL 编码，本地索引原样存储；
4. **详情接口已内嵌行情**：`/items/{id}` 的 `market_observation`（现价/可货状态/来源/观测时间）随详情一次返回，查价无需二连请求；
5. **鉴权实现确认**：Token 白名单在 `backend/data/api_tokens.json`（热加载），Token 级默认 300 次/分钟，匿名 30 次/分钟按 IP 滑动窗口；`api_key` 亦兼容 URL Query 传参（不建议插件使用，避免 key 进日志）；
6. **实测中发现并已修复的服务端问题**（改动在 GunplaLab 仓库 `backend/data_api.py`，其 13 项接口测试全过）：
   - 路径参数未做 URL 解码，带 `#` 的 item_id（如 `HGUC#21`、`PG#PGU-02`）远程详情必 404 → 已加 `urllib.parse.unquote`；
   - `/items` 按 item_id 排序、无相关度，俗称查询首条常是错误条目 → 已改为「别名命中优先 → 票数热度 → ID 稳定排序」；
7. **Windows 宿主系统代理风险**：注册表代理（如 Clash）会被 httpx 自动继承，把发往本机/局域网的请求送进代理返回 502 → 插件客户端默认 `trust_env=False`，新增配置 `use_system_proxy` 供公网部署时开启；
8. **P1 实测补充（2026-09-13）**：
   - Stella 兼容层的工具 schema 把 docstring Args 段所有参数记为必填（不支持 optional 标记）→ 带参数的工具一律不声明 keywords（§⑪ 的取乙方）；
   - `html_render` 视口高度固定 800，宽度从模板 `<meta viewport>` 读取 → 三张模板均写 `width=640`，html 底色与卡片底色统一以消化留白；
   - 图片卡渲染需要 `pip install playwright && playwright install chromium`，未装时 Stella 返回空串、插件自动回退纯文本（已实测两条路径）；
   - 消歧序号选择（「胶查 1」）是对话延续，不受群级冷却限制；`/胶史` 单独数字参数优先按序号选择解释，天数需与关键词同时给出；
   - 服务端 `/items` 现按「别名命中 → 票数热度」排序（P0 时改的），远程兜底的首条即最高相关条目，别名学习取首条详情。

## 5. 功能设计（Notion 场景 → 插件能力逐条映射）

| # | Notion 场景 | 指令 | LLM 工具 | 数据来源 | 阶段 |
|---|---|---|---|---|---|
| 1 | 看到模型不认识，查是什么 | `/胶查 <关键词>` | `search_gunpla(keyword)` | 快照 + `/items/{id}` | P0 |
| 2 | 这个价格贵不贵 | `/胶价 <关键词>` | `check_gunpla_price(keyword)` | `/items/{id}/prices` + 快照官方价 | P0 |
| 3 | 预算/喜好推荐买什么 | `/胶推 <预算> [条件]` | `recommend_gunpla(budget, ...)` | 快照本地过滤 | P1 |
| 4 | 今日胶情 | `/胶情` | （不注册为工具，见 6.3） | `/intel-events` + `/release-events` + 订阅命中 | P1 点播 / P2 定时 |
| 5 | 只关心某 IP/品类，自定义订阅 | `/胶订 添加/移除/列表` | （**不做工具**，写操作） | 本地订阅 + `/prices` `/intel-events` 轮询 | P2 |
| 6 | 降价/再版提醒 | 同上（盯盘） | 同上 | 同上 | P2 |
| 7 | 首次介绍 / 会什么 | `/胶助` | — | — | P0 |
| 8 | 纠错 / 提交来源 | `/胶纠 <内容>` | — | 本地暂存待管理员导出 | P2 |

### 5.1 查资料（P0）
- **多候选消歧**：一个俗称命中多款（普通版/限定版/电镀版/套装版）时绝不混答——列出编号候选（默认 ≤5 条），用户回复 `/胶查 2` 从缓存（群 × 发送者维度，TTL 120 秒）中选中；不做全消息正则监听，避免误触发；
- 回复内容：中/日文名、品类/系列/比例、官方日元价与四算/五算换算、发售/再版时间、评分、封面（图片卡内嵌）、「查看完整档案」网站链接——**全插件唯一入口链接**，不堆二维码；
- 未收录：明确回复「暂未收录」+ 相近候选或建议关键词，并提示可用 `/胶纠` 提交。

### 5.2 查价与判断（P0）
结论矩阵（客户端本地计算，不经 LLM）：

| 现货价 vs 参考线 | 结论话术 |
|---|---|
| ≤ 四算线 conv4 | 「低于四算黄金线，性价比区间」 |
| 四算线 ~ 五算线之间 | 「处于四算~五算之间，正常水平」 |
| > 五算线 conv5 | 「明显高于五算线，存在溢价，可等再版/补货」 |

按 Notion 要求**必须附理由与数据时间**：官方日元价、现货价（来源 pdd、可货状态、观测时间）分行呈现；官方价与现货价严格分开，绝不合并成「全网最低价」；有可靠再版排期时一并提示。

实现取舍：`/items/{id}` 详情接口的 `market_observation` 已内嵌当前现货价与观测时间（实测确认），**查价默认走详情接口一次调用**即可同时拿到官方价与现价，无需再请求 `/prices`；仅在需要来源商品链接 `source_url` 时才补一次 `/prices`。快照内的 `price` 字段（实测 98% 等于 conv4）只作展示，不参与判断。

### 5.3 价格走势（P1）
`/胶史 <关键词> [天数]`：输出近 90/180 天摘要（最低/最高/当前、较上次观测涨跌）；图片卡内嵌纯 SVG 迷你折线（零外部图表依赖）。

### 5.4 预算推荐（P1）
- 输入：预算（CNY）、品类/系列/比例偏好、用途（拼装/收藏/把玩/送礼/拍照/桌面展示）；
- 交互纪律（Notion 硬要求）：信息不足时**最多追问 1~2 个最影响结果的问题**，不发问卷；不足 3 款时**不凑数**，说明是哪个条件限制了结果并询问是否放宽；结论带条件（「重视可动选 A，重视体量展示选 B」）；
- 实现：工具返回紧凑候选列表（≤5 条、每条一句理由），由框架 LLM 组织自然语言；指令通路直接输出结构化文本。

### 5.5 今日胶情（P1 点播 / P2 定时）
- 聚合：最新情报（新品/再版/补货类）+ 本月/次月重点发售排期 + 本群订阅命中变化；
- 上限 5 条，每条含**来源与时间**；群内只给摘要，完整内容指向网站（唯一入口）；
- 没有值得推的内容时明确说「今天没有重要更新」，不凑数；
- Notion 指出网站端「今日胶情」尚未上线，故先做点播 `/胶情`，定时推送做成可配置项放 P2。

### 5.6 群级订阅（P2）
两类订阅，均**群级**（Notion：只提供群级汇总，不公开个人数据；个人提醒需个人通道，推迟到 P3）：
1. **单品盯盘**：绑定 item_id + 条件（目标价 ≤X / 再版 / 补货）；创建前机器人**复述条件请用户确认**，防止盯错版本；
2. **情报雷达**：按关键词/IP/系列过滤 `/intel-events` 与 `/release-events` 的增量。

轮询：默认 60 分钟 + 随机抖动，纳入限流预算（20 条订阅 × 每小时 ≈ 0.3 次/分钟，余量充足）；仅状态变化时推送；订阅增删走指令通路。

## 6. 双框架适配设计（本方案核心）

### 6.1 兼容策略
- 主体代码 100% 按 AstrBot 插件规范编写；Stella 经 `astrbot_compat` 直接加载，零分叉；
- 为 Stella 补齐：`capability.toml`（能力声明，人审后 `reviewed = true`）、`metadata.yaml` 的 `stella.egress`（出网披露）；
- 规避兼容矩阵 ③ 类 API（会抛 `StellaCompatNotSupported`）：不用 `get_db`、`register_web_api`、`kb_manager`、合并转发消息段 `Node/Nodes` 等；主动推送用双方都已实现的 `Context.send_message` / `StarTools.send_message`（需拿 `unified_msg_origin`，群聊场景在事件发生时缓存群会话串）。

### 6.2 两条通路的选择（Stella §3 决策树）
- **只读查询**（查资料/查价/走势/推荐/排期/情报）：同时实现 `@filter.command` 与 `@filter.llm_tool`，共用内部函数——指令保证确定性，自然语言保证易用性；
- **改状态操作**（订阅增删、纠错提交、胶情聚合推送）：**只走指令通路**，绝不注册为可路由工具——Stella 的 Comes 调工具无人工确认环节。

### 6.3 `capability.toml` 能力声明设计

| capability id | providers | 必填参数 | keywords | examples 方向（写群友口语问句，4~6 条/能力） |
|---|---|---|---|---|
| `gunpla.info` | `search_gunpla` | keyword | 不写 | 「RG海牛是什么」「帮我查下卡牛的资料」「这款是哪年发售的」 |
| `gunpla.price` | `check_gunpla_price` | keyword | 不写 | 「海牛现在多少钱」「卡牛贵不贵」「现在入手划算吗」 |
| `gunpla.price_history` | `get_gunpla_history` | keyword | 不写 | 「海牛最近降价了吗」「这胶价格走势怎么样」 |
| `gunpla.recommend` | `recommend_gunpla` | budget | 不写 | 「500 块有什么值得买的」「预算 300 推荐个胶」「送人买什么好」 |
| `gunpla.release_schedule` | `get_release_schedule` | 无 | 可选：「发售排期」等 ≥3 字名词 | 「最近有什么新品发售」「下个月出什么」「有再版计划吗」 |
| `gunpla.intel` | `get_gunpla_intel` | 无 | 可选：「新品情报」 | 「万代最近有什么新情报」「胶圈有什么新闻」 |

- 域统一用自定义 `domain = "gunpla"`：与天气/番剧等 `information` 域天然隔离，同域 6 条能力之间用 `plugin-scaffold --measure` 实测原型分离度（目标 ≥0.06、负样本余量为正）；
- 有必填参数的能力**一律不写 keywords**（Level 0 不抽参数）；无必填参数的排期/情报可享无参直调（`COMES_DIRECT_CALL_NO_ARGS`）；
- `今日胶情` 不注册为工具——其问句与 intel/schedule 高度重叠会拉低同域分离度，且它是聚合产物，仅指令点播；
- 工具 `description`（docstring 首段）面向 AstrBot 决策器写指令句；`examples` 面向 Stella embedding 路由写问句——**两处文本分开维护**，禁用「当用户」「时调用」「本工具」等指令句词汇出现在 examples 中。

### 6.4 工具返回值与失败契约
- 返回人话短句（摘要后 ≤300 字符内可完整呈现），例：「RG 33 海牛高达｜官方 4950 日元（四算 198 / 五算 247.5）｜拼多多现货 ¥248（A 货，09-12 观测）→ 处于四算~五算之间」；**绝不返回几千字 JSON**；
- 查无结果返回「未收录 + 相近候选」短句——这是正常业务结论，不算失败；
- 服务不可用、参数非法：**抛异常**并写清原因（如 `RuntimeError("胶情局 API 429，退避 37s")`），禁止 `return "查询失败：…"`（否则 Stella 无法记账 provider 健康度，还会把失败文案当事实转述）；
- 长耗时操作（如快照首次下载）不阻塞工具：先回「正在同步胶情局数据库」，由后台任务完成后主动 `send_message`。

### 6.5 渲染契约
- 统一 `await self.html_render(tmpl, data)`：Stella 返回**本地文件路径**（`return_url` 被忽略），用 `event.image_result(path)` 发送；**禁用 `url_image(`**（`plugin-check` 第 ⑭ 项）；
- 渲染不可用时返回空串 → **必须**有降级分支：`if img: image_result else: plain_result(文本卡)`——同时覆盖 Stella 首次下载 Chromium 内核期间、AstrBot 渲染失败等场景；
- 封面图下载后 base64 内嵌模板 + 磁盘 LRU 缓存（默认 7 天），避免热链与超时（工具体感预算 5 秒内）。

### 6.6 生命周期与后台任务
- 所有后台任务（快照同步循环、订阅轮询循环）经 `context.register_task(coro, desc)` 登记，**绝不裸调 `asyncio.create_task`**（第 ⑮ 项 warn，且热重载后残留）；
- `terminate()` 内 5 秒内完成：停循环标志位、关闭 httpx 客户端、不等待网络；
- 配置一律 `self.config.get(key, 默认值)` 并防御 `config is None`（单测与无 schema 场景）；
- `requirements.txt` 默认不会被 Stella 自动安装，启动文案与 README 明确提示依赖。

### 6.7 隐私与出网披露（`metadata.yaml` → `stella.egress`）
```yaml
stella:
  egress:
    - host: <GunplaLab 服务地址>   # 未上线阶段为本机/局域网地址，上线后替换为公网域名
      purpose: 查询模型资料与行情，只发送用户输入的模型关键词与模型编号
    - host: <封面图 CDN 域名>
      purpose: 仅下载模型封面图片，不上传任何数据
```
- `purpose` 写明**发送什么**；**绝不发送**群聊原文、用户 ID、消息 ID 至第三方（订阅轮询只发 item_id）；
- import httpx 必须有此声明（否则 `plugin-check` 第 ⑯ 项 warn）。

## 7. 回复呈现与防刷屏

| 层 | 规则 |
|---|---|
| 文本卡（永远可用） | 紧凑 ≤10 行；单次候选 ≤5 条；行情必带时间戳与来源 |
| 图片卡（P1，可配开关） | 资料卡 / 行情卡（含迷你折线）/ 排期卡，统一样式；渲染失败自动回退文本 |
| 防刷屏 | 群级指令冷却（默认 10s，可配）；不自动插话；仅两类主动消息（订阅命中推送、定时胶情）且均可关；进群自我介绍简化为管理员可手动触发的欢迎语（P2，视框架事件支持情况） |

## 8. 配置项设计（`_conf_schema.json`）

| 键 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `base_url` | string | `http://127.0.0.1:3000/api/v1/data` | API 根路径；未上线阶段指向本机/局域网部署（`run_local.py --public`），上线后改公网地址 |
| `api_key` | string | 空 | Bearer Token，建议生产配置以扩容限流 |
| `sync_hour` | int | 4 | 每日快照同步小时（自动 +30 分钟内抖动） |
| `use_snapshot` | bool | true | 关闭则退化为模式 B 纯远程查询 |
| `card_enabled` | bool | true | 图片卡开关（渲染失败自动回退文本） |
| `max_search_results` | int | 5 | 查询/推荐单次最多条数（≤10） |
| `group_cooldown_seconds` | int | 10 | 指令群级冷却 |
| `watch_enabled` / `watch_interval_minutes` | bool / int | false / 60 | 订阅轮询开关与间隔 |
| `digest_enabled` / `digest_push_time` / `digest_groups` | bool / string / list | false / "09:00" / [] | 今日胶情定时推送（P2） |
| `website_url` | string | 空 | 胶情局网站唯一入口链接 |
| `cover_cache_days` | int | 7 | 封面缓存天数 |

## 9. 交付物清单（目录结构，非代码）

```
astrbot_plugin_gunplalab/
├── main.py                 # Star 入口：指令 + llm_tool 注册（薄壳）
├── core/
│   ├── api_client.py       # 限流感知客户端、envelope/ETag/错误码→异常
│   ├── snapshot.py         # 下载/解压/磁盘缓存/索引构建/每日同步
│   ├── search.py           # 归一化、别名索引、排序、消歧会话
│   ├── advisor.py          # 比价结论矩阵
│   ├── recommender.py      # 预算过滤与候选理由
│   ├── digest.py           # 今日胶情聚合
│   ├── watch.py            # 两类群级订阅与轮询
│   ├── scheduler.py        # register_task 合规的常驻循环
│   └── render.py           # Jinja 模板渲染 + 文本降级
├── templates/              # 资料卡 / 行情卡 / 排期卡 HTML
├── capability.toml         # Stella 能力声明（人审后 reviewed = true）
├── metadata.yaml           # 含 stella.egress 出网披露
├── _conf_schema.json       # §8 配置项
├── requirements.txt        # httpx
└── README.md               # 安装、配置、指令一览（也是 scaffold 语料来源）
```

## 10. 分期路线

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **P0 MVP**（实用查询） | ✅ 已交付（2026-09-13）：快照同步 + `/胶查` + `/胶价` + `/胶助` + 文本输出 + capability.toml | Stella `plugin-check` 16 项零 error；AstrBot 正常装载；双框架指令与语义触发双通 |
| **P1**（选购决策） | ✅ 已交付（2026-09-13）：`/胶史` + `/胶排` + `/情报` + `/胶情`（点播）+ `/胶推` + 消歧会话 + 图片卡（资料/行情含折线/排期，纯文本降级） | 路由基准达标（6 能力同域间距 0.083、负样本余量 +0.069）；渲染降级路径实测可用（无 playwright → 纯文本；装后三卡均出图） |
| **P2**（情报沉淀） | ✅ 已交付（2026-09-13）：今日胶情定时推送（无内容不推）+ 群级订阅盯盘（`/胶订` 添加复述确认、目标价/再版/补货、情报雷达、移除列表）+ `/胶纠` 暂存 | 98 项测试全绿；E2E 实测订阅→轮询→触发→去重→推送全链路；24h 限流长测需在真实机器人上跑（轮询请求量：每条订阅每小时约 1 次远程请求，远低于匿名 30 次/分钟配额） |
| **P3 探索** | 猜胶/本命机体等胶圈向趣味玩法、个人提醒通道、账号绑定引导 | 另立方案评审 |

## 11. 测试与验收要点

**Stella 真实环境验收结果（2026-09-13，Stella 开发目录实测）**：
- `python -m deploy plugin-check`：16 项检查 **0 错误 / 0 警告 / 0 提示**（检查过程真实 import 并实例化了插件）；
- `plugin-scaffold --measure`：同域原型最近间距 **0.204**（下限 0.06）；gunpla.info 语料均值 0.717、gunpla.price 均值 0.794；负样本余量 **+0.150**，⑫ 项无告警；
- 在 `astrbot_compat` 真实兼容层中执行功能链路（连真实 GunplaLab 服务）：俗称「海牛」首查远程兜底正确命中 RG 36 Hi-ν（1.5s）并学习别名，二查本地毫秒级返回；`/胶查 高达` 多候选编号消歧正常；未收录话术、群级冷却、`terminate` 干净退出均符合预期；快照经 `context.register_task` 后台循环真实装载（19070 条）；
- 注意：Stella 运行时 Home 在 `D:\Stella_working_space\StellaData`（插件数据落盘到其 `data/plugin_data/gunplalab/`）；正式启用需把插件复制到该 Home 的 `data/plugins/`。

其余验收要求（开发期执行）：

- **Stella 侧**：`python -m deploy plugin-check`（16 项零 error）；`plugin-scaffold --measure` 复算 examples 分离度与负样本余量；`python -m deploy capabilities` 确认 6 条能力全部可路由；
- **AstrBot 侧**：开发模式装载；指令回归清单（中文指令、多候选序号选择、config 为 None 时默认值行为）；
- **数据链路**：mock 304 / 200 / 429 / 401 / 断网五类场景；对生产环境只允许 `/health` `/manifest` 级轻量探活；
- **限流守约**：盯盘 + 同步叠加的 ≥24h 压测，请求速率始终低于配额。

## 12. 风险与待确认问题

已解决（见 §4.5 实测结论）：
- ~~生产接入地址~~：项目暂未上线公网 → P0 面向本机/局域网部署（`run_local.py --public`），`base_url` 作配置项，上线后只改配置；Token 已确认由 `api_tokens.json` 白名单管理，上线时配发即可；
- ~~快照字段完备性~~：已实测核对真实快照——无 aliases/日文名/现货价，索引与查价策略已按此调整。

仍需关注：
1. **封面外链稳定性**：快照 `image` 多为 78动漫 CDN 外链，防盗链策略可能影响图片卡渲染，需实测，必要时文本 + 链接兜底；
2. **主动推送的平台差异**：AstrBot 多平台（QQ 之外）下 `send_message` 会话标识与频控需在 P2 前验证；
3. **今日胶情口径**：与未来网站端「今日胶情」的选取标准对齐（网站端未上线，群内先行、标准可调）；
4. **上线检查项**：公网部署时需同步完成——插件 `base_url` 切换、Bearer Token 配发与 `stella.egress` 域名更新、HTTPS 与反代（当前实现为纯 HTTP 明文）。
