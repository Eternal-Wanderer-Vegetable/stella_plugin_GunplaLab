# astrbot_plugin_GunplaLab · 胶情局群聊助手

把 GunplaLab 胶情局开放数据 API 的 1.9 万款模型底账、现货行情与官方排期带进群聊：**查资料、判价格、看走势、盯排期、按预算推荐**，同时原生适配 **AstrBot** 与 **Stella** 双框架（一份代码，两边完整可用）。

- **AstrBot**：标准 Star 插件，指令 + LLM function calling 双通路；
- **Stella**：经 `astrbot_compat` 兼容层直接加载，附 `capability.toml` 能力声明（6 条）与 `stella.egress` 出网披露，自然语言即可触发。

## 功能

| 指令 | 说明 | LLM 工具 |
|---|---|---|
| `/胶查 <名称/俗称/编号>` | 模型资料卡（级别比例、版本、官方价与四算/五算、档期、评分、封面） | `search_gunpla` |
| `/胶价 <名称/俗称>` | 现货行情 + 四算/五算比价结论（附数据时间与来源） | `check_gunpla_price` |
| `/胶史 <名称> [天数]` | 价格走势（默认 180 天，图片卡含迷你折线） | `get_gunpla_history` |
| `/胶排 [年-月]` | 官方发售/再版排期（默认本月，图片卡） | `get_release_schedule` |
| `/情报 [条数]` | 万代/魂商店最新情报雷达 | `get_gunpla_intel` |
| `/胶情` | 今日胶情（情报 + 本月排期要点；无更新明说） | — |
| `/胶推 <预算> [品类/级别/用途]` | 按预算推荐（四算线为基准，不凑数、说明限制条件） | `recommend_gunpla` |
| `/胶订 添加 <名称> <目标价\|再版\|补货>` | 群级盯盘：复述确认后创建，达标/再版/补货推送到本群 | — |
| `/胶订 雷达 <关键词>` | 情报雷达：标题命中关键词的新情报推送到本群 | — |
| `/胶订 列表` / `移除 <序号>` / `确认` / `取消` | 订阅管理 | — |
| `/胶纠 <内容>` | 提交数据纠错建议（本地暂存，管理员可导出） | — |
| `/胶助` | 帮助 | — |

也可以直接 @机器人 用自然语言提问（Stella / AstrBot LLM 开启时），例如「海牛现在多少钱」「500块推荐个胶」「最近有什么新品发售」。

**行为准则**（来自胶情局产品需求）：多候选不混答（列出候选让用户选版本，序号选择不受冷却限制）；只陈述数据源覆盖的价格；所有行情必附观测时间；推荐不凑数（不足 3 款说明限制条件并给放宽建议）；防刷屏（群级冷却、候选限量）。

## 图片卡片

`card_enabled` 开启后，资料卡/行情卡/排期卡以图片发送（封面图磁盘缓存 7 天）；渲染失败自动回退纯文本。Stella 环境需要渲染依赖：

```bash
pip install playwright
python -m playwright install chromium
```

## 数据接入

- **模式 A（默认）**：每日定时（默认凌晨 4 点 + 抖动，启动后 3 秒先做首轮校验）经 `/manifest` ETag/sha256 增量检查，必要时下载 `/snapshot` 全量快照（约 1.5MB gzip）常驻本地内存——群聊查询零网络请求、不受限流约束；
- **模式 B（按需）**：现货价、走势、排期、情报按需远程单查；
- **别名学习**：本地未精确命中时走远程检索（服务端别名/日文名 LIKE，别名命中优先排序），命中后把官方别名回写本地缓存，俗称越用越准；
- **限流防护**：默认关闭系统代理（防 Windows 回环请求被代理拦截）、最小请求间隔、429 按 `X-RateLimit-Reset` 退避。

## 安装

```bash
# AstrBot：复制到 data/plugins/ 后在 WebUI 重载
# Stella：复制到 data/plugins/ 后运行检查
python -m deploy plugin-check <插件目录>

# 依赖（部分宿主不自动安装）：
pip install httpx
```

**配置 `base_url`**：默认 `http://127.0.0.1:3000/api/v1/data`（同机部署）。跨机部署用 `python run_local.py --public` 让 GunplaLab 监听 `0.0.0.0`，然后填局域网地址；上线公网后替换并配置 Bearer Token（`api_key`，可扩容限流至 300 次/分钟）。

## 配置项

| 键 | 默认 | 说明 |
|---|---|---|
| `base_url` | `http://127.0.0.1:3000/api/v1/data` | API 根路径 |
| `api_key` | 空 | Bearer Token（可选） |
| `sync_hour` | 4 | 每日快照同步时刻 |
| `use_snapshot` | true | 模式 A 开关 |
| `card_enabled` | true | 图片卡开关（失败自动回退文本） |
| `cover_cache_days` | 7 | 封面缓存天数 |
| `max_search_results` | 5 | 单次最多候选数 |
| `group_cooldown_seconds` | 10 | 群级指令冷却（消歧序号选择不受限） |
| `use_system_proxy` | false | 是否走系统代理 |
| `watch_enabled` | false | 盯盘轮询开关（`/胶订` 功能需要开启） |
| `watch_interval_minutes` | 60 | 盯盘轮询间隔（每被盯条目每轮 1 次远程请求） |
| `digest_enabled` | false | 今日胶情定时推送开关（无内容不推送） |
| `digest_push_time` / `digest_groups` / `digest_platform` | 09:00 / [] / aiocqhttp | 推送时刻与目标群（群号或完整会话串） |
| `website_url` | 空 | 胶情局网站唯一入口链接 |

## 本地开发与测试

```bash
python -m pytest          # 83 项测试：单元 + 真实快照集成（自动探测本机 GunplaLab）
```

Stella 侧验收（在 Stella 项目根目录）：

```bash
python -m deploy plugin-check data/plugins/astrbot_plugin_gunplalab    # 16 项全零
python -m deploy plugin-scaffold data/plugins/astrbot_plugin_gunplalab --measure --dry-run
```

## 数据来源与致谢

数据来自 GunplaLab 胶情局开放数据 API（万代官方、78动漫等权威源清洗），仅供群聊查询辅助，请以官方信息为准。

## License

MIT
