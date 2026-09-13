"""群级盯盘订阅：单品盯盘（目标价/再版/补货）+ 情报雷达，状态持久化与轮询评估。"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

from .advisor import availability_label, format_cny, format_jpy
from .api_client import GunplaApiError, GunplaLabClient
from .snapshot import SnapshotStore, normalize

logger = logging.getLogger("gunplalab.watch")

MAX_PER_GROUP = 10
MAX_RADAR_PER_GROUP = 5
INTEL_SEEN_LIMIT = 500
RADAR_MATCH_CAP = 3  # 单雷达单轮最多推送条数，防刷屏


@dataclass
class WatchItem:
    session: str                 # 创建时所在会话（unified_msg_origin），推送目标
    keyword: str                 # 展示名（创建时的关键词）
    item_id: str = ""            # 单品盯盘
    target_price: float | None = None
    watch_reissue: bool = False
    watch_restock: bool = False
    radar: bool = False          # 情报雷达（keyword 即过滤词）
    created_at: str = ""
    triggered: bool = False      # 目标价已达成并推送过；价格回升后重新武装
    last_seen: dict = field(default_factory=dict)  # {price, avail, reissue, observed_at}

    def to_json(self) -> dict:
        return {
            "session": self.session, "keyword": self.keyword, "item_id": self.item_id,
            "target_price": self.target_price, "watch_reissue": self.watch_reissue,
            "watch_restock": self.watch_restock, "radar": self.radar,
            "created_at": self.created_at, "triggered": self.triggered,
            "last_seen": self.last_seen,
        }

    @classmethod
    def from_json(cls, data: dict) -> "WatchItem":
        return cls(
            session=str(data.get("session") or ""),
            keyword=str(data.get("keyword") or ""),
            item_id=str(data.get("item_id") or ""),
            target_price=data.get("target_price"),
            watch_reissue=bool(data.get("watch_reissue")),
            watch_restock=bool(data.get("watch_restock")),
            radar=bool(data.get("radar")),
            created_at=str(data.get("created_at") or ""),
            triggered=bool(data.get("triggered")),
            last_seen=dict(data.get("last_seen") or {}),
        )


class WatchStore:
    """订阅持久化（单文件 JSON）。key = 会话 unified_msg_origin（群级）。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.subscriptions: list[WatchItem] = []
        self.intel_seen: list[str] = []
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            self.subscriptions = [WatchItem.from_json(d) for d in raw.get("subscriptions") or []]
            self.intel_seen = [str(x) for x in raw.get("intel_seen") or []]
        except Exception as exc:
            logger.warning("订阅文件加载失败，已重置：%s", exc)
            self.subscriptions, self.intel_seen = [], []

    def save(self) -> None:
        payload = {
            "subscriptions": [s.to_json() for s in self.subscriptions],
            "intel_seen": self.intel_seen[-INTEL_SEEN_LIMIT:],
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), "utf-8")
        os.replace(tmp, self.path)

    # -- 单品订阅 -------------------------------------------------------

    def add(self, item: WatchItem) -> None:
        self.subscriptions.append(item)
        self.save()

    def list_group(self, session: str) -> list[WatchItem]:
        return [s for s in self.subscriptions if s.session == session]

    def remove(self, session: str, index: int) -> WatchItem | None:
        group = self.list_group(session)
        if 1 <= index <= len(group):
            victim = group[index - 1]
            self.subscriptions.remove(victim)
            self.save()
            return victim
        return None

    # -- 情报去重 -------------------------------------------------------

    def filter_new_intel(self, items: list[dict]) -> list[dict]:
        seen = set(self.intel_seen)
        fresh = [it for it in items if str(it.get("id") or "") not in seen and it.get("id")]
        known = {str(it.get("id")) for it in items if it.get("id")}
        self.intel_seen = (self.intel_seen + [str(i) for i in known])[-INTEL_SEEN_LIMIT:]
        return fresh


# ----------------------------------------------------------------------
# 变化检测
# ----------------------------------------------------------------------

def evaluate_item(sub: WatchItem, item: dict | None, detail: dict | None) -> list[str]:
    """对一条单品订阅做变化评估，返回告警文本列表（可能为空）。

    首轮观测只记录基线；但目标价条件在首轮即生效（创建时已复述确认）。
    """
    alerts: list[dict] = []
    obs = (detail or {}).get("market_observation") or {}
    price = obs.get("current_price")
    avail = str(obs.get("availability") or "")
    observed_at = str(obs.get("observed_at") or "")
    reissue = str(((detail or {}).get("release") or {}).get("reissue_date")
                  or (item or {}).get("reissueDate") or "")
    last = sub.last_seen

    if sub.target_price is not None and price is not None:
        try:
            price_f = float(price)
            if not sub.triggered and price_f <= float(sub.target_price):
                alerts.append({
                    "icon": "📉", "head": "盯盘触发：价格达到目标",
                    "lines": [
                        f"现价 {format_cny(price_f)} ≤ 目标 {format_cny(sub.target_price)}",
                        f"观测 {short_time(observed_at) or '—'}｜来源 {source_label(obs)}",
                    ],
                })
                sub.triggered = True
            elif sub.triggered and price_f > float(sub.target_price):
                sub.triggered = False  # 价格回升，重新武装
        except (TypeError, ValueError):
            pass

    if sub.watch_restock and last and str(last.get("avail") or "") not in ("A",) and avail == "A":
        alerts.append({
            "icon": "🔔", "head": "补货提醒：现货恢复",
            "lines": [f"状态 {availability_label(avail)}｜观测 {short_time(observed_at) or '—'}"],
        })

    if sub.watch_reissue and last and reissue and str(last.get("reissue") or "") != reissue:
        alerts.append({
            "icon": "🔔", "head": f"再版情报：排期更新为 {reissue}",
            "lines": [f"原排期 {last.get('reissue') or '无'}"],
        })

    sub.last_seen = {"price": price, "avail": avail, "reissue": reissue, "observed_at": observed_at}

    name = display_name(item, detail)
    out = []
    for a in alerts:
        body = "\n".join(a["lines"])
        out.append(f"{a['icon']} {a['head']}\n【{name}】\n{body}")
    return out


def radar_matches(sub: WatchItem, items: list[dict]) -> list[dict]:
    """情报雷达：标题+子类包含关键词（归一化子串）。"""
    pattern = normalize(sub.keyword)
    if not pattern:
        return []
    hits = []
    for it in items:
        hay = normalize(f"{it.get('title') or ''}{it.get('subcategory') or ''}")
        if pattern in hay:
            hits.append(it)
    return hits[:RADAR_MATCH_CAP]


# ----------------------------------------------------------------------
# 轮询任务
# ----------------------------------------------------------------------

async def poll_once(
    watch_store: WatchStore,
    snap_store: SnapshotStore,
    client: GunplaLabClient,
    *,
    push: Callable[[str, str], Awaitable[bool]],
    website_url: str = "",
) -> dict:
    """执行一轮盯盘评估。push(session, text) 由调用方注入（Context.send_message）。"""
    stats = {"items": 0, "errors": 0, "alerts": 0, "radar": 0}

    item_subs = [s for s in watch_store.subscriptions if not s.radar and s.item_id]
    unique_ids = list(dict.fromkeys(s.item_id for s in item_subs))
    for item_id in unique_ids:
        try:
            detail = await client.get_item(item_id)
        except GunplaApiError as exc:
            logger.debug("盯盘详情获取失败 %s：%s", item_id, exc)
            stats["errors"] += 1
            continue
        stats["items"] += 1
        item = snap_store.get(item_id)
        subs = [s for s in item_subs if s.item_id == item_id]
        for sub in subs:
            for text in evaluate_item(sub, item, detail):
                stats["alerts"] += 1
                if detail.get("links", {}).get("official_detail_url"):
                    text += f"\n详情：{detail['links']['official_detail_url']}"
                elif website_url:
                    text += f"\n完整档案：{website_url}"
                await push(sub.session, text)

    radar_subs = [s for s in watch_store.subscriptions if s.radar]
    if radar_subs:
        try:
            data, _ = await client.get_intel_events(limit=50)
            fresh = watch_store.filter_new_intel(data.get("items") or [])
        except GunplaApiError:
            fresh = []
        for sub in radar_subs:
            for it in radar_matches(sub, fresh):
                stats["radar"] += 1
                text = (
                    f"📡 情报雷达「{sub.keyword}」\n"
                    f"[{it.get('category') or '情报'}] {it.get('title') or ''}"
                    + (f"｜{it.get('releaseDate')}" if it.get("releaseDate") else "")
                    + (f"\n来源：{it.get('source')}" if it.get("source") else "")
                    + (f"\n{it.get('url')}" if it.get("url") else "")
                )
                await push(sub.session, text)
        watch_store.save()

    return stats


# ----------------------------------------------------------------------
# 展示
# ----------------------------------------------------------------------

def format_list(subs: list[WatchItem], stats: dict | None = None) -> str:
    if not subs:
        return "本群还没有盯盘订阅。用「/胶订 添加 <名称> [目标价/再版/补货]」或「/胶订 雷达 <关键词>」创建。"
    lines = [f"【本群盯盘订阅】共 {len(subs)} 条"]
    for idx, s in enumerate(subs, 1):
        if s.radar:
            lines.append(f"{idx}. [雷达] 关键词「{s.keyword}」")
            continue
        conds = []
        if s.target_price is not None:
            mark = "✅已达成" if s.triggered else "盯盘中"
            conds.append(f"价格≤{format_cny(s.target_price)}（{mark}）")
        if s.watch_reissue:
            conds.append("再版")
        if s.watch_restock:
            conds.append("补货")
        seen = s.last_seen or {}
        price_part = f"｜现价 {format_cny(seen.get('price'))}" if seen.get("price") is not None else ""
        lines.append(
            f"{idx}. {s.keyword}｜{'/'.join(conds) or '观测中'}{price_part}"
        )
    if stats:
        lines.append(f"—— 上次检查：{stats['items']} 款｜失败 {stats['errors']}")
    return "\n".join(lines)


def display_name(item: dict | None, detail: dict | None) -> str:
    if detail:
        zh = (detail.get("names") or {}).get("zh")
        if zh:
            return str(zh)
    if item:
        return str(item.get("displayName") or item.get("name") or item.get("id"))
    return "未知模型"


def source_label(obs: dict) -> str:
    return {"pdd": "拼多多"}.get(str(obs.get("source") or ""), str(obs.get("source") or "未知来源"))


def short_time(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return _dt.datetime.fromisoformat(text).strftime("%m-%d %H:%M")
    except ValueError:
        try:
            return _dt.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").strftime("%m-%d %H:%M")
        except ValueError:
            return text[:16]
