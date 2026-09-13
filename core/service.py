"""业务服务层：检索消歧、资料/行情文本卡、LLM 工具摘要。

不 import astrbot.*——指令通路与工具通路共用这里的实现。
"""

from __future__ import annotations

import datetime as _dt
import time
from dataclasses import dataclass, field

from .advisor import availability_label, format_cny, format_jpy, price_verdict
from .api_client import GunplaApiError, GunplaLabClient
from .snapshot import SnapshotStore, normalize

SESSION_TTL_SECONDS = 120.0

_SOURCE_LABELS = {"pdd": "拼多多", "official_catalog": "官方目录"}

# 服务端别名里混有过于宽泛的词，学进本地索引会劫持查询
_GENERIC_ALIASES = {"高达", "gundam", "模型", "胶", "拼装", "万代", "bandai", "假面骑士", "奥特曼"}


@dataclass
class ResolveOutcome:
    """一次关键词解析的结果。"""

    status: str                    # "hit" | "candidates" | "miss"
    items: list[dict] = field(default_factory=list)
    keyword: str = ""
    source: str = "local"          # local | remote


class GunplaService:
    def __init__(
        self,
        store: SnapshotStore,
        client: GunplaLabClient | None,
        *,
        max_results: int = 5,
        website_url: str = "",
    ):
        self.store = store
        self.client = client
        self.max_results = max(1, min(10, max_results))
        self.website_url = website_url.strip()
        self._sessions: dict[str, tuple[float, list[dict]]] = {}

    # ------------------------------------------------------------------
    # 消歧会话
    # ------------------------------------------------------------------

    def save_candidates(self, scope: str, items: list[dict]) -> None:
        self._sessions[scope] = (time.monotonic(), items)

    def pick_candidate(self, scope: str, number: int) -> dict | None:
        """按序号选取候选。过期/越界返回 None。"""
        entry = self._sessions.pop(scope, None)
        if not entry:
            return None
        saved_at, items = entry
        if time.monotonic() - saved_at > SESSION_TTL_SECONDS:
            return None
        if 1 <= number <= len(items):
            return items[number - 1]
        return None

    # ------------------------------------------------------------------
    # 关键词解析
    # ------------------------------------------------------------------

    async def resolve(self, keyword: str, *, scope: str = "") -> ResolveOutcome:
        """本地精确命中直接返回；前缀/模糊命中与未命中均尝试远程别名兜底。"""
        keyword = (keyword or "").strip()
        if not keyword:
            return ResolveOutcome("miss", keyword=keyword)

        # 序号消歧优先
        if scope and keyword.isdigit():
            picked = self.pick_candidate(scope, int(keyword))
            if picked is not None:
                return ResolveOutcome("hit", items=[picked], keyword=keyword)
            return ResolveOutcome("miss", keyword=keyword)

        ranked = self.store.search_ranked(keyword, limit=self.max_results)
        # 存在精确命中（别名/JAN/编号/名称完全一致）时只返回精确层结果，
        # 前缀/子串等模糊结果不与之混排（如「RG33」不应混入 HG33、SD330）；
        # 仅精确命中也不唯一（同编号多款/多俗称同名）时才进入消歧。
        strong = [(item, tier) for item, tier in ranked if tier <= 1]
        if strong:
            items = [item for item, _ in strong]
            if len(items) > 1 and scope:
                self.save_candidates(scope, items)
            return ResolveOutcome("hit", items=items, keyword=keyword)

        # 弱命中（纯子串）或未命中：远程别名兜底（服务端 LIKE 覆盖官方别名与日文名）
        remote_items = await self._remote_search(keyword)
        if remote_items:
            if len(remote_items) > 1 and scope:
                self.save_candidates(scope, remote_items)
            return ResolveOutcome("hit", items=remote_items, keyword=keyword, source="remote")

        if ranked:
            items = [item for item, _ in ranked]
            if len(items) > 1 and scope:
                self.save_candidates(scope, items)
            return ResolveOutcome("hit", items=items, keyword=keyword)

        return ResolveOutcome("miss", keyword=keyword)

    async def _remote_search(self, keyword: str) -> list[dict]:
        """远程检索 + 首条详情别名学习。失败返回空列表（由调用方降级）。"""
        if self.client is None:
            return []
        try:
            rows = await self.client.search_items(keyword, page_size=self.max_results)
        except GunplaApiError:
            return []
        if not rows:
            return []

        items: list[dict] = []
        alias_pairs: list[tuple[str, str]] = []
        for row in rows:
            item_id = str(row.get("item_id") or "")
            local = self.store.get(item_id)
            items.append(local if local is not None else _remote_row_to_snapshot_schema(row))
        # 仅取首条做详情别名学习，控制请求量
        first_id = str(rows[0].get("item_id") or "")
        if first_id and self.store.get(first_id) is not None:
            try:
                detail = await self.client.get_item(first_id)
                for alias in (detail.get("identifiers") or {}).get("aliases") or []:
                    if normalize(alias) in _GENERIC_ALIASES:
                        continue
                    alias_pairs.append((alias, first_id))
                name_ja = (detail.get("names") or {}).get("ja") or ""
                if name_ja and normalize(name_ja) not in _GENERIC_ALIASES:
                    alias_pairs.append((name_ja, first_id))
            except GunplaApiError:
                pass
        if alias_pairs:
            self.store.learn_aliases(alias_pairs)
        return items

    async def item_detail(self, item_id: str) -> dict | None:
        """拉取全景档案并学习别名；失败返回 None（调用方决定降级话术）。"""
        if self.client is None:
            return None
        try:
            detail = await self.client.get_item(item_id)
        except GunplaApiError:
            return None
        aliases = (detail.get("identifiers") or {}).get("aliases") or []
        pairs = [
            (a, item_id)
            for a in aliases
            if normalize(a) not in _GENERIC_ALIASES
        ]
        name_ja = (detail.get("names") or {}).get("ja") or ""
        if name_ja and normalize(name_ja) not in _GENERIC_ALIASES:
            pairs.append((name_ja, item_id))
        if pairs:
            self.store.learn_aliases(pairs)
        return detail

    # ------------------------------------------------------------------
    # 文本卡（指令通路）
    # ------------------------------------------------------------------

    def format_detail_card(self, item: dict, detail: dict | None) -> str:
        lines = [f"【{_display_name(item)}】"]
        meta_bits = "｜".join(
            b for b in (
                _grade_label(item),
                _scale_label(item),
                item.get("versionTag") or "",
            ) if b
        )
        if meta_bits:
            lines.append(meta_bits)
        work = item.get("work") or ""
        universe = item.get("universeName") or ""
        if work and universe:
            lines.append(f"作品：{work}（{universe}）")
        elif work or universe:
            lines.append(f"作品：{work or universe}")

        conv4 = item.get("conv4")
        conv5 = item.get("conv5")
        if item.get("jpy"):
            lines.append(
                f"官方定价：{format_jpy(item.get('jpy'))}"
                f"（四算 {format_cny(conv4)} / 五算 {format_cny(conv5)}）"
            )
        else:
            lines.append("官方定价：暂无官方定价资料")
        dates = "｜".join(
            b for b in (
                f"发售：{item.get('releaseDate')}" if item.get("releaseDate") else "",
                f"再版：{item.get('reissueDate')}" if item.get("reissueDate") else "",
            ) if b
        )
        if dates:
            lines.append(dates)

        spot_line = self._spot_line(detail)
        if spot_line:
            lines.append(spot_line)

        rating = item.get("rating")
        votes = item.get("ratingCount")
        if rating:
            lines.append(f"评分：{rating} 分（{votes} 人评价）")

        jan = item.get("jan") or ""
        if detail:
            jan = (detail.get("identifiers") or {}).get("jan_code") or jan
        if jan:
            lines.append(f"JAN：{jan}")

        lines.append(f"数据截至：底账 {_short_time(self.store.meta.get('generated_at'))}")
        url = ((detail or {}).get("links") or {}).get("official_detail_url") or ""
        if url:
            lines.append(f"详情：{url}")
        elif self.website_url:
            lines.append(f"完整档案：{self.website_url}")
        return "\n".join(lines)

    def format_candidates(self, keyword: str, items: list[dict]) -> str:
        head = f"「{keyword}」匹配到 {len(items)} 款，请回复“胶查 序号”选择（{int(SESSION_TTL_SECONDS)} 秒内有效）："
        rows = []
        for idx, item in enumerate(items, 1):
            rows.append(
                f"{idx}. {_short_name(item)}｜{_grade_label(item)}｜{item.get('versionTag') or '普通版'}"
                f"｜{format_cny(item.get('conv4'))} 起"
            )
        return "\n".join([head, *rows])

    def format_price_card(self, item: dict, detail: dict | None, *, remote_failed: bool = False) -> str:
        conv4 = item.get("conv4") or 0
        conv5 = item.get("conv5") or 0
        lines = [f"【{_display_name(item)}】行情"]
        if item.get("jpy"):
            lines.append(
                f"官方定价：{format_jpy(item.get('jpy'))}"
                f"（四算 {format_cny(conv4)} / 五算 {format_cny(conv5)}）"
            )
        else:
            lines.append("官方定价：暂无官方定价资料，无法给出参考线")
        spot, observed_at, avail, source = self._spot_parts(detail)
        if spot is not None:
            lines.append(
                f"现货价：{format_cny(spot)}（{availability_label(avail)}｜{_source_label(source)}"
                f"｜{_short_time(observed_at)} 观测）"
            )
            lines.append(f"判断：{price_verdict(spot, conv4, conv5)}")
        elif remote_failed:
            lines.append("判断：现货观测暂不可用，无法判断当前入手时机")
        else:
            lines.append("判断：暂无现货观测数据，无法判断")

        reissue = _future_reissue(item, detail)
        if reissue:
            lines.append(f"提示：官方排期显示近期再版（{reissue}），可等等看")
        lines.append(
            f"数据时间：行情 {_short_time(observed_at) or '—'}｜底账 {_short_time(self.store.meta.get('generated_at'))}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # LLM 工具摘要（≤300 字符人话短句）
    # ------------------------------------------------------------------

    def summarize_detail(self, item: dict, detail: dict | None) -> str:
        parts = [
            _display_name(item),
            f"{_grade_label(item) or '—'}·{_scale_label(item) or '—'}·{item.get('versionTag') or '普通版'}",
        ]
        if item.get("jpy"):
            parts.append(
                f"官方 {format_jpy(item.get('jpy'))}"
                f"（四算{format_cny(item.get('conv4'))}/五算{format_cny(item.get('conv5'))}）"
            )
        if item.get("releaseDate"):
            parts.append(f"{item['releaseDate']} 发售")
        spot, observed_at, avail, _ = self._spot_parts(detail)
        if spot is not None:
            parts.append(f"现货 {format_cny(spot)}（{availability_label(avail)}，{_short_time(observed_at)}观测）")
        if item.get("rating"):
            parts.append(f"评分 {item['rating']}")
        return "｜".join(parts)

    def summarize_price(self, item: dict, detail: dict | None, *, remote_failed: bool = False) -> str:
        conv4 = item.get("conv4") or 0
        conv5 = item.get("conv5") or 0
        if item.get("jpy"):
            head = (
                f"{_display_name(item)}行情：官方 {format_jpy(item.get('jpy'))}"
                f"（四算{format_cny(conv4)}/五算{format_cny(conv5)}）"
            )
        else:
            head = f"{_display_name(item)}行情：暂无官方定价参考线"
        spot, observed_at, avail, source = self._spot_parts(detail)
        if spot is not None:
            verdict = price_verdict(spot, conv4, conv5)
            return (
                f"{head}｜{_source_label(source)}现货 {format_cny(spot)}"
                f"（{availability_label(avail)}，{_short_time(observed_at)}观测）→ {verdict}"
            )
        if remote_failed:
            return f"{head}｜现货观测暂不可用，无法判断当前入手时机"
        return f"{head}｜暂无现货观测数据"

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _spot_parts(detail: dict | None) -> tuple[object, str, str, str]:
        obs = (detail or {}).get("market_observation") or {}
        return (
            obs.get("current_price"),
            str(obs.get("observed_at") or ""),
            str(obs.get("availability") or ""),
            str(obs.get("source") or ""),
        )

    def _spot_line(self, detail: dict | None) -> str:
        spot, observed_at, avail, source = self._spot_parts(detail)
        if spot is None:
            return ""
        return (
            f"现货：{format_cny(spot)}｜{availability_label(avail)}"
            f"｜来源 {_source_label(source)}｜观测 {_short_time(observed_at)}"
        )


# ----------------------------------------------------------------------
# 纯函数
# ----------------------------------------------------------------------

def _remote_row_to_snapshot_schema(row: dict) -> dict:
    """/items 列表模式 -> 快照紧凑模式（本地缺失该条时的降级展示）。"""
    price = row.get("official_price") or {}
    return {
        "id": row.get("item_id", ""),
        "name": row.get("name_zh", ""),
        "displayName": row.get("name_zh", ""),
        "jpy": price.get("amount"),
        "conv4": price.get("conv4"),
        "conv5": price.get("conv5"),
        "scale": row.get("scale", ""),
        "releaseDate": row.get("release_date", ""),
        "reissueDate": row.get("reissue_date", ""),
        "grade": row.get("series", ""),
        "versionTag": "",
        "rating": (row.get("rating") or {}).get("score"),
        "ratingCount": (row.get("rating") or {}).get("vote_count"),
        "jan": row.get("jan_code", ""),
    }


def _display_name(item: dict) -> str:
    return str(item.get("displayName") or item.get("name") or item.get("id") or "未知模型")


def _short_name(item: dict) -> str:
    name = str(item.get("name") or item.get("displayName") or item.get("id") or "")
    return name


def _grade_label(item: dict) -> str:
    return str(item.get("grade") or item.get("series") or "").strip()


def _scale_label(item: dict) -> str:
    return str(item.get("scale") or "").strip()


def _source_label(source: str) -> str:
    return _SOURCE_LABELS.get(source, source or "未知来源")


def _short_time(raw: object) -> str:
    """'2026-09-12 18:30:00' / ISO -> '09-12 18:30'。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        if "T" in text:
            parsed = _dt.datetime.fromisoformat(text)
            return parsed.strftime("%m-%d %H:%M")
        parsed = _dt.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        return parsed.strftime("%m-%d %H:%M")
    except ValueError:
        return text[:16]


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
