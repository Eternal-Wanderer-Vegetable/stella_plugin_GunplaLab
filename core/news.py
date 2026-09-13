"""官方情报与发售排期（胶排 / 情报 / 今日胶情）。"""

from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from .api_client import GunplaApiError, GunplaLabClient

_INTEL_CATEGORY = {
    "new": "新品",
    "release": "出荷",
    "reissue": "再版",
    "restock": "补货",
    "delay": "延期",
    "cancel": "中止",
}
_EVENT_TYPE = {
    "release": "发售",
    "reissue": "再版",
    "estimated-release": "预定发售",
}
_TAG_RE = re.compile(r"<[^>]+>")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
MAX_SCHEDULE_SHOW = 8
MAX_INTEL_SHOW = 8


def _clean(text: Any, limit: int = 40) -> str:
    """情报字段可能带 <br> 等标记，清洗并截断。"""
    text = _TAG_RE.sub(" ", str(text or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def intel_label(category: str) -> str:
    return _INTEL_CATEGORY.get(str(category or "").strip(), "情报")


def event_label(event_type: str) -> str:
    return _EVENT_TYPE.get(str(event_type or "").strip(), "排期")


def current_month() -> str:
    return _dt.date.today().strftime("%Y-%m")


def normalize_month(text: str) -> str:
    """「2026-9」「202609」「今年9月」以外的输入回落到当月。"""
    text = str(text or "").strip()
    if _MONTH_RE.match(text):
        return text
    m = re.match(r"^(\d{4})[年./-](\d{1,2})月?$", text)
    if m:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.match(r"^(\d{1,2})月?$", text)
    if m:
        return f"{_dt.date.today().year}-{int(m.group(1)):02d}"
    return current_month()


def format_intel(items: list[dict], updated_at: str = "", website_url: str = "") -> str:
    if not items:
        return "情报雷达暂无内容"
    lines = [f"【官方情报雷达】截至 {_short_time(updated_at) or '—'}"]
    for idx, it in enumerate(items, 1):
        bits = [
            f"{idx}.[{intel_label(it.get('category'))}] {_clean(it.get('title'), 36)}",
        ]
        sub = _clean(it.get("subcategory"), 16)
        if sub:
            bits.append(sub)
        if it.get("releaseDate"):
            bits.append(_clean(it["releaseDate"], 16))
        if it.get("price"):
            bits.append(_clean(it["price"], 28))
        lines.append("｜".join(bits))
    if website_url:
        lines.append(f"详情与来源见网站：{website_url}")
    return "\n".join(lines)


def format_intel_compact(items: list[dict], limit: int = 5) -> str:
    if not items:
        return "情报雷达暂无内容"
    parts = []
    for it in items[:limit]:
        parts.append(
            f"[{intel_label(it.get('category'))}]{_clean(it.get('title'), 24)}"
            f"（{_clean(it.get('releaseDate'), 12) or '日期待定'}）"
        )
    return "最新情报：" + "；".join(parts)


def format_schedule(events: list[dict], month: str, updated_at: str = "", website_url: str = "") -> str:
    if not events:
        return f"{month} 暂无官方排期记录"
    total = len(events)
    shown = events[:MAX_SCHEDULE_SHOW]
    lines = [f"【发售排期 · {month}】共 {total} 条，显示前 {len(shown)} 条"]
    for idx, e in enumerate(shown, 1):
        bits = [f"{idx}.[{event_label(e.get('eventType'))}] {_clean(e.get('title'), 40)}"]
        price = e.get("officialPrice") or {}
        if price.get("amount"):
            bits.append(f"{int(price['amount']):,} 日元")
        if e.get("dateLabel"):
            bits.append(_clean(e["dateLabel"], 14))
        lines.append("｜".join(bits))
    if updated_at:
        lines.append(f"—— 数据更新于 {_short_time(updated_at)}")
    if website_url:
        lines.append(f"完整排期：{website_url}")
    return "\n".join(lines)


def format_schedule_compact(events: list[dict], limit: int = 5) -> str:
    if not events:
        return "该月份暂无官方排期记录"
    parts = []
    for e in events[:limit]:
        price = e.get("officialPrice") or {}
        price_str = f"{int(price['amount']):,}日元" if price.get("amount") else ""
        parts.append(
            f"[{event_label(e.get('eventType'))}]{_clean(e.get('title'), 26)}"
            + (f"（{price_str}）" if price_str else "")
        )
    return "近期排期：" + "；".join(parts)


async def collect_digest(client: GunplaLabClient) -> dict:
    """采集今日胶情素材：情报 + 本月排期。服务不可用 respective 列表为空。"""
    today = _dt.date.today().strftime("%Y-%m")
    intel: list[dict] = []
    events: list[dict] = []
    intel_at = ""
    try:
        data, intel_at = await client.get_intel_events(limit=5)
        intel = data.get("items") or []
    except GunplaApiError:
        pass
    try:
        data, _ = await client.get_release_events(month=today, limit=3)
        events = data.get("events") or []
    except GunplaApiError:
        pass
    return {"intel": intel, "events": events, "intel_at": intel_at, "month": today}


def render_digest(collected: dict, website_url: str = "") -> str:
    intel = collected.get("intel") or []
    events = collected.get("events") or []
    today = collected.get("month") or _dt.date.today().strftime("%Y-%m")
    if not intel and not events:
        return "今天没有重要更新：情报雷达和本月排期都没有新内容。"

    lines = [f"🧪 今日胶情 · {_dt.date.today().strftime('%m-%d')}"]
    if intel:
        lines.append("◆ 情报")
        for idx, it in enumerate(intel[:5], 1):
            lines.append(
                f"{idx}.[{intel_label(it.get('category'))}] {_clean(it.get('title'), 30)}"
                + (f"｜{_clean(it.get('releaseDate'), 14)}" if it.get("releaseDate") else "")
                + (f"｜来源 {_clean(it.get('source'), 20)}" if it.get("source") else "")
            )
    if events:
        lines.append(f"◆ 本月排期（{today}）")
        for idx, e in enumerate(events[:3], 1):
            price = e.get("officialPrice") or {}
            price_str = f"{int(price['amount']):,} 日元" if price.get("amount") else ""
            lines.append(
                f"{idx}.[{event_label(e.get('eventType'))}] {_clean(e.get('title'), 30)}"
                + (f"｜{price_str}" if price_str else "")
            )
    if website_url:
        lines.append(f"完整内容：{website_url}")
    return "\n".join(lines)


async def build_digest(client: GunplaLabClient, website_url: str = "") -> str:
    """今日胶情（点播）。"""
    return render_digest(await collect_digest(client), website_url)


def _short_time(raw: Any) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        return _dt.datetime.fromisoformat(text).strftime("%m-%d %H:%M")
    except ValueError:
        return text[:16]
