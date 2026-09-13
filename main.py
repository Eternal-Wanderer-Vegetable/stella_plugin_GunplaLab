"""胶情局 · GunplaLab 开放数据插件（AstrBot × Stella 双框架适配）。

指令通路：/胶查 /胶价 /胶史 /胶排 /情报 /胶情 /胶推 /胶助
工具通路：search_gunpla / check_gunpla_price / get_gunpla_history /
          get_release_schedule / get_gunpla_intel / recommend_gunpla
          （配合 capability.toml）
数据来源：GunplaLab 开放数据 API /api/v1/data（全量快照常驻本地内存，行情按需远程）
"""

from __future__ import annotations

import datetime as _dt
import logging
import time
from pathlib import Path
from typing import AsyncGenerator

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .core import advisor, corrections, news, recommender, render, trends, watch
from .core.api_client import GunplaApiError, GunplaLabClient
from .core.scheduler import DailyLoop, IntervalLoop
from .core.service import GunplaService
from .core.snapshot import SnapshotStore, load_store_async, sync_once

logger = logging.getLogger("gunplalab")

PLUGIN_NAME = "gunplalab"
PLUGIN_VERSION = "v0.2.0"

_COMMAND_TOKENS = {
    "胶查", "查胶", "胶档", "胶价", "查价", "行情", "胶助",
    "胶史", "走势", "胶排", "排期", "情报", "胶情", "胶推", "推荐",
    "胶订", "胶纠",
}

_SCHEDULE_BADGES = {
    "release": ("发售", ""),
    "reissue": ("再版", "reissue"),
    "estimated-release": ("预定发售", "estimated"),
}


@register(
    PLUGIN_NAME,
    "Eternal-Wanderer-Vegetable",
    "胶情局开放数据：模型资料、现货行情、走势排期与预算推荐",
    PLUGIN_VERSION,
    "https://github.com/Eternal-Wanderer-Vegetable/stella_plugin_GunplaLab",
)
class GunplaLabPlugin(Star):
    """胶情局群聊助手：查资料、判价格、看走势排期、按预算推荐。"""

    def __init__(self, context: Context, config=None):
        try:
            super().__init__(context, config)
        except TypeError:  # 兼容仅接受 context 的旧版宿主
            super().__init__(context)
        self.config = config

        self._client: GunplaLabClient | None = None
        self._store: SnapshotStore | None = None
        self._service: GunplaService | None = None
        self._sync_loop: DailyLoop | None = None
        self._watch_loop: IntervalLoop | None = None
        self._digest_loop: DailyLoop | None = None
        self._covers: render.CoverCache | None = None
        self._watch_store: watch.WatchStore | None = None
        self._pending_watch: dict[str, tuple[watch.WatchItem, float]] = {}
        self._last_call: dict[tuple[str, str], float] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        base_url = str(self._cfg("base_url", "http://127.0.0.1:3000/api/v1/data"))
        api_key = str(self._cfg("api_key", "") or "")
        use_snapshot = bool(self._cfg("use_snapshot", True))
        sync_hour = int(self._cfg("sync_hour", 4))
        max_results = int(self._cfg("max_search_results", 5))
        website_url = str(self._cfg("website_url", "") or "")

        try:
            data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            data_dir = Path(__file__).resolve().parent / "data"
        self._data_dir = data_dir

        self._client = GunplaLabClient(
            base_url,
            api_key=api_key,
            trust_env=bool(self._cfg("use_system_proxy", False)),
        )
        self._store = SnapshotStore(data_dir)
        self._service = GunplaService(
            self._store, self._client, max_results=max_results, website_url=website_url
        )
        self._covers = render.CoverCache(
            data_dir / "covers", ttl_days=int(self._cfg("cover_cache_days", 7))
        )
        self._watch_store = watch.WatchStore(data_dir / "watch.json")

        # 秒级可用：先吃磁盘缓存，刷新交给后台循环
        loaded = await load_store_async(self._store)
        if loaded:
            stats = self._store.stats()
            logger.info(
                "本地快照就绪：%s 款模型（底账 %s）",
                stats["total_items"], stats["generated_at"] or "未知",
            )
        else:
            logger.info("本地无快照缓存，将在后台首次同步（%s）", base_url)

        if use_snapshot:
            self._sync_loop = DailyLoop(self._sync_job, at=f"{max(0, min(23, sync_hour)):02d}:00")
            if callable(getattr(self.context, "register_task", None)):
                self.context.register_task(self._sync_loop.run(), "GunplaLab 快照同步")
            else:
                # 极旧宿主：无任务登记能力，降级为仅启动时同步一次
                logger.warning("宿主不支持 context.register_task，每日自动同步不可用")
                try:
                    await self._sync_job()
                except Exception as exc:
                    logger.warning("启动同步失败（将在下次启动重试）：%s", exc)

        # 盯盘轮询（P2）
        if bool(self._cfg("watch_enabled", False)):
            interval = float(self._cfg("watch_interval_minutes", 60) or 60)
            self._watch_loop = IntervalLoop(self._watch_job, interval_minutes=interval)
            if callable(getattr(self.context, "register_task", None)):
                self.context.register_task(self._watch_loop.run(), "GunplaLab 盯盘轮询")
            else:
                logger.warning("宿主不支持 register_task，盯盘轮询不可用")

        # 今日胶情定时推送（P2）
        if bool(self._cfg("digest_enabled", False)):
            push_time = str(self._cfg("digest_push_time", "09:00") or "09:00")
            self._digest_loop = DailyLoop(
                self._digest_job, at=push_time, jitter_minutes=5.0
            )
            if callable(getattr(self.context, "register_task", None)):
                self.context.register_task(self._digest_loop.run(), "GunplaLab 今日胶情推送")
            else:
                logger.warning("宿主不支持 register_task，今日胶情定时推送不可用")

    async def terminate(self) -> None:
        for loop in (self._sync_loop, self._watch_loop, self._digest_loop):
            if loop is not None:
                loop.stop()
        for loop in (self._sync_loop, self._watch_loop, self._digest_loop):
            if loop is not None:
                await loop.wait_stopped()
        if self._covers is not None:
            await self._covers.close()
        if self._client is not None:
            await self._client.close()
        logger.info("胶情局插件已停止")

    async def _sync_job(self) -> None:
        if self._client is None or self._store is None:
            return
        result = await sync_once(self._store, self._client)
        logger.info("快照同步完成：%s", result)

    async def _watch_job(self) -> None:
        if self._client is None or self._watch_store is None:
            return
        stats = await watch.poll_once(
            self._watch_store,
            self._store or SnapshotStore(Path(".")),
            self._client,
            push=self._push_text,
            website_url=str(self._cfg("website_url", "") or ""),
        )
        logger.info(
            "盯盘轮询完成：检查 %s 款、推送 %s 条警报 / %s 条雷达（失败 %s）",
            stats["items"], stats["alerts"], stats["radar"], stats["errors"],
        )

    async def _digest_job(self) -> None:
        groups = self._digest_targets()
        if not groups:
            return
        collected = await news.collect_digest(self._client)
        if not (collected.get("intel") or collected.get("events")):
            logger.info("今日胶情无内容，跳过推送")
            return
        text = news.render_digest(collected, str(self._cfg("website_url", "") or ""))
        for session in groups:
            ok = await self._push_text(session, text)
            logger.info("今日胶情推送 %s：%s", session, "成功" if ok else "失败")

    def _digest_targets(self) -> list[str]:
        """digest_groups：完整会话串（platform:GroupMessage:号）或裸群号。"""
        platform = str(self._cfg("digest_platform", "aiocqhttp") or "aiocqhttp")
        targets = []
        for entry in self._cfg("digest_groups", []) or []:
            text = str(entry or "").strip()
            if not text:
                continue
            if text.isdigit():
                text = f"{platform}:GroupMessage:{text}"
            targets.append(text)
        return targets

    async def _push_text(self, session: str, text: str) -> bool:
        """主动推送：MessageChain 双框架通用；失败只记日志，不影响轮询。"""
        try:
            from astrbot.api.event import MessageChain
            chain = MessageChain().message(text)
        except Exception:
            chain = text  # Stella 的 send_message 也接受纯字符串
        try:
            return bool(await self.context.send_message(session, chain))
        except Exception as exc:
            logger.warning("主动推送失败 %s：%s", session, exc)
            return False

    # ------------------------------------------------------------------
    # 指令通路
    # ------------------------------------------------------------------

    @filter.command("胶查")
    async def gunpla_query(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查询模型资料：/胶查 <名称/俗称/编号>"""
        tokens = self._extract_tokens(event)
        if not tokens:
            yield event.plain_result("用法：/胶查 <名称/俗称/编号>，例如：/胶查 海牛")
            return
        keyword = " ".join(tokens)
        scope = self._scope(event)
        # 序号选择是会话延续，不受体闸冷却限制
        if not keyword.isdigit() and not self._cooldown_ok(scope, "胶查"):
            return
        outcome = await self._service.resolve(keyword, scope=scope)
        if outcome.status == "miss":
            yield event.plain_result(self._miss_text(keyword))
            return
        if len(outcome.items) > 1:
            yield event.plain_result(
                self._service.format_candidates(outcome.keyword or keyword, outcome.items)
            )
            return

        item = outcome.items[0]
        detail = await self._service.item_detail(str(item.get("id") or ""))
        text = self._service.format_detail_card(item, detail)
        path = await self._detail_card(item, detail)
        if path:
            yield event.image_result(path)
        else:
            yield event.plain_result(text)

    @filter.command("胶价")
    async def gunpla_price(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查询现货行情与比价结论：/胶价 <名称/俗称>"""
        tokens = self._extract_tokens(event)
        if not tokens:
            yield event.plain_result("用法：/胶价 <名称/俗称>，例如：/胶价 海牛")
            return
        keyword = " ".join(tokens)
        scope = self._scope(event)
        if not keyword.isdigit() and not self._cooldown_ok(scope, "胶价"):
            return
        outcome = await self._service.resolve(keyword, scope=scope)
        if outcome.status == "miss":
            yield event.plain_result(self._miss_text(keyword))
            return
        if len(outcome.items) > 1:
            yield event.plain_result(
                self._service.format_candidates(outcome.keyword or keyword, outcome.items)
            )
            return

        item = outcome.items[0]
        detail = await self._service.item_detail(str(item.get("id") or ""))
        remote_failed = detail is None and self._client is not None
        text = self._service.format_price_card(item, detail, remote_failed=remote_failed)
        path = await self._price_card(item, detail)
        if path:
            yield event.image_result(path)
        else:
            yield event.plain_result(text)

    @filter.command("胶史")
    async def gunpla_history(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查询价格走势：/胶史 <名称/俗称> [天数，默认180]"""
        tokens = self._extract_tokens(event)
        days = 180
        # 单独一个数字 = 消歧序号选择；仅当「关键词 + 数字」时末位数字才是天数
        if len(tokens) >= 2 and tokens[-1].isdigit():
            days = min(max(int(tokens[-1]), 7), 365)
            tokens = tokens[:-1]
        if not tokens:
            yield event.plain_result("用法：/胶史 <名称/俗称> [天数]，例如：/胶史 海牛 90")
            return
        keyword = tokens[0] if len(tokens) == 1 and tokens[0].isdigit() else " ".join(tokens)
        scope = self._scope(event)
        if not keyword.isdigit() and not self._cooldown_ok(scope, "胶史"):
            return
        outcome = await self._service.resolve(keyword, scope=scope)
        if outcome.status == "miss":
            yield event.plain_result(self._miss_text(keyword))
            return
        if len(outcome.items) > 1:
            yield event.plain_result(
                self._service.format_candidates(outcome.keyword or keyword, outcome.items)
            )
            return

        item = outcome.items[0]
        try:
            payload = await self._client.get_item_history(str(item.get("id") or ""), days)
        except GunplaApiError as exc:
            yield event.plain_result(f"胶情局走势服务暂时不可用（{exc.api_message or exc.code}）")
            return
        text = trends.summarize_history(item, payload, days) or "暂无历史观测记录"
        path = await self._history_card(item, payload)
        if path:
            yield event.image_result(path)
        else:
            yield event.plain_result(text)

    @filter.command("胶排")
    async def gunpla_schedule(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查询官方发售排期：/胶排 [年-月，默认本月]"""
        tokens = self._extract_tokens(event)
        month = news.normalize_month(tokens[0]) if tokens else news.current_month()
        scope = self._scope(event)
        if not self._cooldown_ok(scope, "胶排"):
            return
        try:
            data, as_of = await self._client.get_release_events(month=month, limit=100)
        except GunplaApiError as exc:
            yield event.plain_result(f"胶情局排期服务暂时不可用（{exc.api_message or exc.code}）")
            return
        events = data.get("events") or []
        text = news.format_schedule(events, month, as_of, self._service.website_url)
        path = await self._schedule_card(events, month, as_of)
        if path:
            yield event.image_result(path)
        else:
            yield event.plain_result(text)

    @filter.command("情报")
    async def gunpla_intel(self, event: AstrMessageEvent) -> AsyncGenerator:
        """查询官方最新情报：/情报 [条数，默认5]"""
        tokens = self._extract_tokens(event)
        limit = 5
        if tokens and tokens[0].isdigit():
            limit = min(max(int(tokens[0]), 1), 10)
        if not self._cooldown_ok(self._scope(event), "情报"):
            return
        try:
            data, as_of = await self._client.get_intel_events(limit=limit)
        except GunplaApiError as exc:
            yield event.plain_result(f"胶情局情报服务暂时不可用（{exc.api_message or exc.code}）")
            return
        yield event.plain_result(
            news.format_intel(data.get("items") or [], as_of, self._service.website_url)
        )

    @filter.command("胶情")
    async def gunpla_digest(self, event: AstrMessageEvent) -> AsyncGenerator:
        """今日胶情：情报 + 本月排期要点"""
        if not self._cooldown_ok(self._scope(event), "胶情"):
            return
        yield event.plain_result(await news.build_digest(self._client, self._service.website_url))

    @filter.command("胶推")
    async def gunpla_recommend(self, event: AstrMessageEvent) -> AsyncGenerator:
        """按预算推荐：/胶推 <预算> [品类/级别/用途]，例如 /胶推 500 rg 把玩"""
        tokens = self._extract_tokens(event)
        budget = recommender.parse_budget(tokens[0]) if tokens else None
        if budget is None:
            yield event.plain_result(
                "用法：/胶推 <预算> [品类/级别/用途]，例如：/胶推 500 rg 把玩、/胶推 800 假面骑士"
            )
            return
        category = grade = usage = ""
        for token in tokens[1:]:
            category = category or recommender.normalize_category(token)
            grade = grade or recommender.normalize_grade(token)
            usage = usage or recommender.normalize_usage(token)
        if not self._cooldown_ok(self._scope(event), "胶推"):
            return
        if not (self._store and self._store.loaded):
            yield event.plain_result("推荐功能依赖本地底账，快照尚未就绪，请稍后再试")
            return
        result = recommender.recommend(
            self._store, budget, category=category, grade=grade, usage=usage,
            limit=int(self._cfg("max_search_results", 5)),
        )
        yield event.plain_result(recommender.format_recommend(result))

    @filter.command("胶订")
    async def gunpla_watch_cmd(self, event: AstrMessageEvent) -> AsyncGenerator:
        """群级盯盘订阅：/胶订 添加|雷达|移除|列表|确认|取消"""
        if self._watch_store is None:
            yield event.plain_result("订阅功能未就绪")
            return
        tokens = self._extract_tokens(event)
        session = event.unified_msg_origin  # 群级订阅：不含发送者
        sender_scope = self._scope(event)
        if not tokens:
            yield event.plain_result(_WATCH_USAGE)
            return
        action, rest = tokens[0], tokens[1:]

        if action in ("列表", "list"):
            yield event.plain_result(watch.format_list(self._watch_store.list_group(session)))
            return

        if action in ("添加", "盯"):
            async for r in self._watch_add(event, session, sender_scope, rest):
                yield r
            return

        if action == "确认":
            pending = self._pending_watch.pop(sender_scope, None)
            if not pending or time.monotonic() > pending[1]:
                yield event.plain_result("当前没有待确认的订阅（可能已超时），请重新创建")
                return
            self._watch_store.add(pending[0])
            yield event.plain_result(
                f"✅ 盯盘已创建：【{pending[0].keyword}】\n变化时会推送到本群，/胶订 列表 可随时查看"
            )
            return

        if action == "取消":
            pending = self._pending_watch.pop(sender_scope, None)
            yield event.plain_result(
                "已取消。" if pending else "当前没有待确认的订阅。"
            )
            return

        if action in ("雷达",):
            keyword = " ".join(rest).strip()
            if not keyword:
                yield event.plain_result("用法：/胶订 雷达 <关键词>，例如：/胶订 雷达 MGSD")
                return
            radars = [s for s in self._watch_store.list_group(session) if s.radar]
            if len(radars) >= watch.MAX_RADAR_PER_GROUP:
                yield event.plain_result(f"每群最多 {watch.MAX_RADAR_PER_GROUP} 条情报雷达，请先 /胶订 移除")
                return
            self._watch_store.add(watch.WatchItem(
                session=session, keyword=keyword, radar=True,
                created_at=_dt.datetime.now().astimezone().replace(microsecond=0).isoformat(),
            ))
            yield event.plain_result(
                f"✅ 情报雷达已创建：标题含「{keyword}」的新情报将推送到本群\n"
                "（情报来源为官方公告，通常每天更新；/胶订 列表 可查看）"
            )
            return

        if action in ("移除", "删除"):
            index = int(rest[0]) if rest and rest[0].isdigit() else 0
            victim = self._watch_store.remove(session, index)
            if victim is None:
                yield event.plain_result("序号无效，用 /胶订 列表 查看当前订阅")
                return
            yield event.plain_result(f"✅ 已移除：{victim.keyword}")
            return

        yield event.plain_result(_WATCH_USAGE)

    async def _watch_add(self, event: AstrMessageEvent, session: str, sender_scope: str,
                         rest: list[str]) -> AsyncGenerator:
        keyword, target, reissue, restock = _parse_watch_conditions(rest)
        if not keyword or (target is None and not reissue and not restock):
            yield event.plain_result(
                "用法：/胶订 添加 <名称> <目标价|再版|补货>\n例如：/胶订 添加 海牛 220 再版、/胶订 添加 独角兽 1500"
            )
            return
        if not self._cooldown_ok(sender_scope, "胶订添加"):
            return
        outcome = await self._service.resolve(keyword, scope=sender_scope)
        if outcome.status == "miss":
            yield event.plain_result(self._miss_text(keyword))
            return
        if len(outcome.items) > 1:
            yield event.plain_result(
                self._service.format_candidates(outcome.keyword or keyword, outcome.items)
            )
            return
        item = outcome.items[0]
        item_id = str(item.get("id") or "")
        if len(self._watch_store.list_group(session)) >= watch.MAX_PER_GROUP:
            yield event.plain_result(f"每群最多 {watch.MAX_PER_GROUP} 条订阅，请先 /胶订 移除")
            return

        display = str(item.get("displayName") or item.get("name") or item_id)
        conds = []
        if target is not None:
            conds.append(f"现货价 ≤ {advisor.format_cny(target)}")
        if reissue:
            conds.append("再版排期")
        if restock:
            conds.append("补货")
        sub = watch.WatchItem(
            session=session, keyword=display, item_id=item_id,
            target_price=target, watch_reissue=reissue, watch_restock=restock,
            created_at=_dt.datetime.now().astimezone().replace(microsecond=0).isoformat(),
        )
        # 复述确认（Notion 要求：防止盯错版本）
        self._pending_watch[sender_scope] = (sub, time.monotonic() + 120.0)
        current = ""
        detail = await self._service.item_detail(item_id)
        spot = ((detail or {}).get("market_observation") or {}).get("current_price")
        if spot is not None:
            current = f"当前现货价：{advisor.format_cny(spot)}\n"
        yield event.plain_result(
            f"即将盯盘：\n【{display}】\n条件：{' + '.join(conds)}\n{current}"
            "回复「/胶订 确认」创建，「/胶订 取消」放弃（120 秒内有效）"
        )

    @filter.command("胶纠")
    async def gunpla_correct(self, event: AstrMessageEvent) -> AsyncGenerator:
        """提交纠错建议：/胶纠 <内容>"""
        tokens = self._extract_tokens(event)
        text = " ".join(tokens).strip()
        if not text:
            yield event.plain_result("用法：/胶纠 <内容>，例如：/胶纠 「海牛」应该能查到 RG 36 Hi-ν高达")
            return
        path = corrections.save_correction(
            self._data_dir,
            scope=event.unified_msg_origin,
            sender=event.get_sender_id() or "",
            text=text,
        )
        yield event.plain_result(f"✅ 纠错建议已记录，感谢反馈（共 {sum(1 for _ in path.open(encoding='utf-8'))} 条）")

    @filter.command("胶助")
    async def gunpla_help(self, event: AstrMessageEvent) -> AsyncGenerator:
        """显示胶情局功能帮助"""
        stats = self._store.stats() if self._store else {}
        lines = [
            "🧪 胶情局 · 模型资料与行情",
            "/胶查 <名称/俗称/编号>：查模型资料",
            "/胶价 <名称/俗称>：查现货行情、判断贵不贵",
            "/胶史 <名称/俗称> [天数]：价格走势（默认 180 天）",
            "/胶排 [年-月]：官方发售排期（默认本月）",
            "/情报 [条数]：官方最新情报雷达",
            "/胶情：今日胶情（情报 + 排期要点）",
            "/胶推 <预算> [品类/级别/用途]：按预算推荐",
            "/胶订 添加 <名称> <目标价|再版|补货>：盯盘（群级）",
            "/胶订 雷达 <关键词>：新情报推送｜/胶订 列表｜/胶订 移除 <序号>",
            "/胶纠 <内容>：提交数据纠错建议",
            "/胶助：显示本帮助",
            "也可以直接 @我 用自然语言提问，例如「查一下卡牛」「海牛多少钱」",
        ]
        if stats.get("total_items"):
            lines.append(f"—— 已收录 {stats['total_items']} 款｜底账更新 {_short(stats.get('generated_at'))}")
        if self._service and self._service.website_url:
            lines.append(f"完整档案：{self._service.website_url}")
        yield event.plain_result("\n".join(lines))

    # ------------------------------------------------------------------
    # 工具通路（LLM function calling，配合 capability.toml）
    # ------------------------------------------------------------------

    @filter.llm_tool(name="search_gunpla")
    async def search_gunpla(self, event: AstrMessageEvent, keyword: str) -> str:
        """查询胶情局收录的模型资料（高达/假面骑士/奥特曼/国模等），返回名称、级别比例、版本、官方定价与发售时间。当用户想知道某个模型是什么、查型号、看版本比例或发售时间时使用。
        Args:
            keyword(string): 模型的名称、俗称或商品编号，例如：RG海牛、卡牛、HG新安洲
        """
        if not (keyword or "").strip():
            raise RuntimeError("参数 keyword 不能为空：请向用户询问具体模型名称")
        self._ensure_ready()
        outcome = await self._service.resolve(keyword)
        if outcome.status == "miss":
            return self._miss_text(keyword)
        item = outcome.items[0]
        detail = await self._service.item_detail(str(item.get("id") or ""))
        summary = self._service.summarize_detail(item, detail)
        if len(outcome.items) > 1:
            alts = "；".join(_alt_label(i) for i in outcome.items[1:4])
            return f"{summary}。另有相似条目：{alts}。请先向用户确认想要的版本。"
        return summary

    @filter.llm_tool(name="check_gunpla_price")
    async def check_gunpla_price(self, event: AstrMessageEvent, keyword: str) -> str:
        """查询模型现货行情，并结合四算/五算黄金线给出贵不贵的判断。当用户问某模型现在多少钱、贵不贵、值不值得入手时使用。
        Args:
            keyword(string): 模型的名称、俗称或商品编号，例如：海牛、卡牛、RG沙
        """
        if not (keyword or "").strip():
            raise RuntimeError("参数 keyword 不能为空：请向用户询问具体模型名称")
        self._ensure_ready()
        outcome = await self._service.resolve(keyword)
        if outcome.status == "miss":
            return self._miss_text(keyword)
        if len(outcome.items) > 1:
            alts = "；".join(_alt_label(i) for i in outcome.items[:4])
            return f"「{keyword}」匹配到多款：{alts}。请先向用户确认具体版本再查价。"
        item = outcome.items[0]
        detail = await self._service.item_detail(str(item.get("id") or ""))
        remote_failed = detail is None and self._client is not None
        return self._service.summarize_price(item, detail, remote_failed=remote_failed)

    @filter.llm_tool(name="get_gunpla_history")
    async def get_gunpla_history(self, event: AstrMessageEvent, keyword: str, days: int = 180) -> str:
        """查询模型近一段时间（默认180天，最长365天）的现货行情走势：最低/最高/最新价与涨跌。当用户问某模型最近降没降价、价格走势、历史最低价时使用。
        Args:
            keyword(string): 模型的名称、俗称或商品编号，例如：海牛、RG沙
            days(integer, optional): 回看天数，默认 180
        """
        if not (keyword or "").strip():
            raise RuntimeError("参数 keyword 不能为空：请向用户询问具体模型名称")
        self._ensure_ready()
        days = min(max(int(days or 180), 7), 365)
        outcome = await self._service.resolve(keyword)
        if outcome.status == "miss":
            return self._miss_text(keyword)
        if len(outcome.items) > 1:
            alts = "；".join(_alt_label(i) for i in outcome.items[:4])
            return f"「{keyword}」匹配到多款：{alts}。请先向用户确认具体版本再查走势。"
        item = outcome.items[0]
        try:
            payload = await self._client.get_item_history(str(item.get("id") or ""), days)
        except GunplaApiError as exc:
            raise RuntimeError(f"胶情局走势服务暂时不可用：{exc.api_message or exc.code}") from exc
        return trends.summarize_history_compact(item, payload, days)

    @filter.llm_tool(name="get_release_schedule")
    async def get_release_schedule(self, event: AstrMessageEvent, month: str = "") -> str:
        """查询万代官方发售与再版排期日历。当用户问最近/某月有什么新品发售、再版计划、发售表时使用。
        Args:
            month(string, optional): 月份，格式如 2026-09；留空表示本月
        """
        self._ensure_ready()
        target = news.normalize_month(month)
        try:
            data, as_of = await self._client.get_release_events(month=target, limit=100)
        except GunplaApiError as exc:
            raise RuntimeError(f"胶情局排期服务暂时不可用：{exc.api_message or exc.code}") from exc
        events = data.get("events") or []
        head = news.format_schedule_compact(events, limit=5)
        return f"{target}｜{head}" if events else head

    @filter.llm_tool(name="get_gunpla_intel")
    async def get_gunpla_intel(self, event: AstrMessageEvent, limit: int = 5) -> str:
        """查询万代/魂商店最新官方情报（新品、再版、出荷等公告）。当用户问最近有什么新情报、胶圈新闻、官方公告时使用。
        Args:
            limit(integer, optional): 返回条数，默认 5
        """
        self._ensure_ready()
        limit = min(max(int(limit or 5), 1), 10)
        try:
            data, as_of = await self._client.get_intel_events(limit=limit)
        except GunplaApiError as exc:
            raise RuntimeError(f"胶情局情报服务暂时不可用：{exc.api_message or exc.code}") from exc
        items = data.get("items") or []
        head = news.format_intel_compact(items, limit=limit)
        return f"{head}（截至 {_short(as_of)}）" if items and as_of else head

    @filter.llm_tool(name="recommend_gunpla")
    async def recommend_gunpla(
        self,
        event: AstrMessageEvent,
        budget: float,
        category: str = "",
        grade: str = "",
        usage: str = "",
    ) -> str:
        """按预算推荐 GunplaLab 收录的模型（以官方价四算线为基准），可按品类、级别、用途过滤；结果不足时会说明限制条件。当用户问预算内买什么、求推荐模型、送人买什么时使用。
        Args:
            budget(number): 预算金额（人民币元），例如 300、500
            category(string, optional): 品类：gunpla(高达)/rider(假面骑士)/ultraman(奥特曼)/guomo(国模)
            grade(string, optional): 级别：RG/MG/HG/PG/EG/SD/FM 等
            usage(string, optional): 用途：把玩/展示/拍照/送礼/新手/拼装
        """
        if budget is None or float(budget) <= 0:
            raise RuntimeError("参数 budget 不能为空：请先向用户询问预算（人民币）")
        if not (self._store and self._store.loaded):
            raise RuntimeError("本地底账尚未就绪，推荐功能暂不可用，请稍后重试")
        result = recommender.recommend(
            self._store,
            float(budget),
            category=recommender.normalize_category(category),
            grade=recommender.normalize_grade(grade),
            usage=recommender.normalize_usage(usage),
            limit=int(self._cfg("max_search_results", 5)),
        )
        return recommender.summarize_recommend(result)

    # ------------------------------------------------------------------
    # 图片卡（渲染不可用时一律回退纯文本）
    # ------------------------------------------------------------------

    async def _card_path(self, kind: str, data: dict) -> str:
        if not bool(self._cfg("card_enabled", True)):
            return ""
        try:
            tmpl = render.load_template(kind)
            return str(await self.html_render(tmpl, data, return_url=False) or "")
        except Exception as exc:
            logger.debug("卡片渲染不可用，回退文本：%s", exc)
            return ""

    async def _detail_card(self, item: dict, detail: dict | None) -> str:
        try:
            cover_uri = await self._covers.get_data_uri(str(item.get("image") or ""))
        except Exception:
            cover_uri = None
        conv4 = item.get("conv4")
        conv5 = item.get("conv5")
        official_line = (
            f"{advisor.format_jpy(item.get('jpy'))}（四算 {advisor.format_cny(conv4)} / 五算 {advisor.format_cny(conv5)}）"
            if item.get("jpy") else "暂无官方定价资料"
        )
        dates = "｜".join(
            b for b in (
                f"发售 {item.get('releaseDate')}" if item.get("releaseDate") else "",
                f"再版 {item.get('reissueDate')}" if item.get("reissueDate") else "",
            ) if b
        )
        spot, observed_at, avail, source = GunplaService._spot_parts(detail)
        spot_line = (
            f"{advisor.format_cny(spot)}（{advisor.availability_label(avail)}·{service_source(source)}）"
            if spot is not None else ""
        )
        rating_line = (
            f"{item.get('rating')} 分（{item.get('ratingCount') or 0} 票）" if item.get("rating") else ""
        )
        work_zh = str(item.get("work") or "")
        universe = str(item.get("universeName") or "")
        if work_zh and universe:
            work = f"{work_zh}（{universe}）"
        else:
            work = work_zh or universe
        data = {
            "display_name": item.get("displayName") or item.get("name") or "",
            "tags": [b for b in (item.get("grade") or "", item.get("scale") or "", item.get("versionTag") or "") if b],
            "work": work,
            "official_line": official_line,
            "dates": dates,
            "spot_line": spot_line,
            "rating_line": rating_line,
            "jan": (detail or {}).get("identifiers", {}).get("jan_code") or item.get("jan") or "",
            "cover_uri": cover_uri,
            "data_as_of": _short(self._store.meta.get("generated_at")),
            "detail_url": ((detail or {}).get("links") or {}).get("official_detail_url") or "",
        }
        return await self._card_path("detail_card.j2", data)

    async def _price_card(self, item: dict, detail: dict | None, history_payload: dict | None = None) -> str:
        conv4 = item.get("conv4") or 0
        conv5 = item.get("conv5") or 0
        spot, observed_at, avail, source = GunplaService._spot_parts(detail)
        if item.get("jpy"):
            official_line = advisor.format_jpy(item.get("jpy"))
            golden_lines = f"{advisor.format_cny(conv4)} / {advisor.format_cny(conv5)}"
        else:
            official_line = "暂无"
            golden_lines = "—"
        if spot is not None:
            verdict = advisor.price_verdict(spot, conv4, conv5)
            spot_line = advisor.format_cny(spot)
        else:
            verdict = "暂无现货观测数据，无法判断"
            spot_line = "—"
        sparkline = ""
        rows = []
        if history_payload:
            rows = [r for r in (history_payload.get("history") or []) if r.get("price") is not None]
            sparkline = trends.sparkline_svg(rows)
        note_parts = []
        if spot is None and detail is None and self._client is not None:
            note_parts.append("现货观测暂时不可用")
        reissue = _future_reissue(item, detail)
        if reissue:
            note_parts.append(f"官方排期显示近期再版（{reissue}）")
        data = {
            "display_name": item.get("displayName") or item.get("name") or "",
            "official_line": official_line,
            "golden_lines": golden_lines,
            "spot_line": spot_line,
            "spot_source": news._clean(source or "pdd", 8) if spot is not None else "巡检",
            "spot_time": _short(observed_at),
            "verdict": verdict,
            "tone": advisor.verdict_tone(verdict),
            "sparkline": sparkline,
            "note": "；".join(note_parts),
            "data_as_of": _short(self._store.meta.get("generated_at")),
        }
        return await self._card_path("price_card.j2", data)

    async def _history_card(self, item: dict, payload: dict) -> str:
        rows = [r for r in (payload.get("history") or []) if r.get("price") is not None]
        if len(rows) < 2:
            return ""  # 无走势可画，回退文本卡
        prices = [float(r["price"]) for r in rows]
        last = rows[-1]
        conv4 = item.get("conv4") or 0
        conv5 = item.get("conv5") or 0
        data = {
            "display_name": item.get("displayName") or item.get("name") or "",
            "official_line": advisor.format_jpy(item.get("jpy")) if item.get("jpy") else "—",
            "golden_lines": f"{advisor.format_cny(conv4)} / {advisor.format_cny(conv5)}"
            if (conv4 or conv5) else "—",
            "spot_line": advisor.format_cny(last["price"]),
            "spot_source": "最新观测",
            "spot_time": str(last.get("record_date")),
            "verdict": f"近 {payload.get('days', 180)} 天：最低 {min(prices):g} · 最高 {max(prices):g}（{len(rows)} 次观测）",
            "tone": "na",
            "sparkline": trends.sparkline_svg(rows),
            "note": "",
            "data_as_of": _short(self._store.meta.get("generated_at")),
        }
        return await self._card_path("price_card.j2", data)

    async def _schedule_card(self, events: list[dict], month: str, as_of: str) -> str:
        rows = []
        for e in events[: news.MAX_SCHEDULE_SHOW]:
            label, badge = _SCHEDULE_BADGES.get(
                str(e.get("eventType") or ""), (event_type_label(e.get("eventType")), "estimated")
            )
            price = e.get("officialPrice") or {}
            rows.append({
                "label": label,
                "badge": badge,
                "title": e.get("title") or "",
                "price": f"{int(price['amount']):,} 日元" if price.get("amount") else "",
                "date": e.get("dateLabel") or "",
            })
        data = {"month": month, "total": len(events), "rows": rows, "updated_at": _short(as_of)}
        return await self._card_path("schedule_card.j2", data)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default=None):
        if not self.config:
            return default
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _ensure_ready(self) -> None:
        if self._service is None or self._store is None:
            raise RuntimeError("胶情局插件尚未初始化完成，请稍后重试")

    def _scope(self, event: AstrMessageEvent) -> str:
        try:
            sender = event.get_sender_id() or ""
        except Exception:
            sender = ""
        return f"{event.unified_msg_origin}|{sender}"

    def _cooldown_ok(self, scope: str, cmd: str) -> bool:
        seconds = float(self._cfg("group_cooldown_seconds", 10) or 0)
        if seconds <= 0 or not scope:
            return True
        now = time.monotonic()
        key = (scope, cmd)
        if now - self._last_call.get(key, 0.0) < seconds:
            return False
        self._last_call[key] = now
        return True

    @staticmethod
    def _extract_tokens(event: AstrMessageEvent) -> list[str]:
        text = (event.message_str or "").strip()
        if text.startswith("/"):
            text = text[1:].strip()
        parts = text.split()
        if parts and parts[0] in _COMMAND_TOKENS:
            parts = parts[1:]
        return parts

    def _miss_text(self, keyword: str) -> str:
        if not (self._store and self._store.loaded) and self._client is None:
            return "胶情局数据服务尚未就绪（未配置数据源），请联系管理员检查插件配置"
        text = f"胶情局暂未收录「{keyword}」。可以试试正式名称、日文名或商品编号。"
        if self._service and self._service.website_url:
            text += f"也可到网站检索：{self._service.website_url}"
        return text


_WATCH_USAGE = (
    "用法：/胶订 <动作>\n"
    "· 添加 <名称> <目标价|再版|补货>：盯盘，如 /胶订 添加 海牛 220 再版\n"
    "· 雷达 <关键词>：标题命中的新情报推送\n"
    "· 列表｜移除 <序号>｜确认｜取消"
)


def _parse_watch_conditions(tokens: list[str]) -> tuple[str, float | None, bool, bool]:
    """从指令尾部分离关键词与条件（目标价 / 再版 / 补货）。"""
    keyword_tokens: list[str] = []
    target: float | None = None
    watch_reissue = watch_restock = False
    for token in tokens:
        if token == "再版":
            watch_reissue = True
        elif token in ("补货", "现货"):
            watch_restock = True
        else:
            value = recommender.parse_budget(token)
            if value is not None and target is None:
                target = value
            else:
                keyword_tokens.append(token)
    return " ".join(keyword_tokens), target, watch_reissue, watch_restock


def _alt_label(item: dict) -> str:
    name = str(item.get("name") or item.get("displayName") or item.get("id") or "")
    tag = item.get("versionTag") or ""
    grade = item.get("grade") or ""
    bits = "·".join(b for b in (grade, tag) if b)
    return f"{name}（{bits}）" if bits else name


def _short(raw: object) -> str:
    text = str(raw or "")
    try:
        return _dt.datetime.fromisoformat(text).strftime("%m-%d %H:%M")
    except ValueError:
        return text[:16] if text else "—"


def service_source(source: str) -> str:
    return {"pdd": "拼多多"}.get(source, source or "未知来源")


def event_type_label(event_type: object) -> str:
    return news.event_label(str(event_type or ""))


def _future_reissue(item: dict, detail: dict | None) -> str:
    candidates = [
        str(item.get("reissueDate") or ""),
        str(((detail or {}).get("release") or {}).get("reissue_date") or ""),
    ]
    now = _dt.date.today().replace(day=1)
    for text in candidates:
        try:
            y, m = text.split("-")[:2]
            target = _dt.date(int(y), int(m), 1)
        except (ValueError, TypeError):
            continue
        if target >= now:
            return text
    return ""
