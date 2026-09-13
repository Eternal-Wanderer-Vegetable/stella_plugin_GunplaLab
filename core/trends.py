"""价格走势（胶史）：时序摘要 + 纯 SVG 迷你折线（零外部图表依赖）。"""

from __future__ import annotations

import math
from typing import Any

from .advisor import format_cny

# 单点状态标记：fresh/ok 为有效观测，其余（缺货等）仍带价格但注明
_STATUS_LABELS = {"fresh": "最新", "ok": "", "oos": "缺货价"}


def summarize_history(item: dict, payload: dict, days: int = 180) -> str | None:
    """时序摘要文本卡。无有效观测点返回 None（调用方给「暂无记录」话术）。"""
    rows = [r for r in (payload.get("history") or []) if r.get("price") is not None]
    lines = [f"【{item.get('displayName') or item.get('name')}】近 {payload.get('days', days)} 天行情走势"]
    if not rows:
        lines.append("暂无历史观测记录（现货巡检数据自近期开始积累）")
        conv4 = item.get("conv4")
        conv5 = item.get("conv5")
        if conv4 or conv5:
            lines.append(f"参考线：四算 {format_cny(conv4)}｜五算 {format_cny(conv5)}")
        return "\n".join(lines)

    prices = [float(r["price"]) for r in rows]
    lo, hi = min(prices), max(prices)
    first, last = rows[0], rows[-1]
    last_status = _STATUS_LABELS.get(str(last.get("status") or "ok"), "")
    lines.append(
        f"观测 {len(rows)} 次｜{first.get('record_date', '?')} → {last.get('record_date', '?')}"
    )
    lo_str = format_cny(lo) + (f"（{lo_date}）" if (lo_date := _date_of(rows, lo)) else "")
    hi_str = format_cny(hi) + (f"（{hi_date}）" if (hi_date := _date_of(rows, hi)) else "")
    lines.append(f"最低 {lo_str}｜最高 {hi_str}｜最新 {format_cny(last['price'])}{_suffix(last_status)}")

    if len(prices) >= 2:
        delta = prices[-1] - prices[-2]
        arrow = "↓" if delta < 0 else ("↑" if delta > 0 else "→")
        if abs(delta) < 0.5:
            lines.append("趋势：与上次观测持平")
        else:
            lines.append(f"趋势：较上次 {arrow} {format_cny(abs(delta))}（{_short(last.get('record_date'))} 观测）")

    conv4 = item.get("conv4")
    conv5 = item.get("conv5")
    if conv4 or conv5:
        lines.append(f"参考线：四算 {format_cny(conv4)}｜五算 {format_cny(conv5)}")
    return "\n".join(lines)


def summarize_history_compact(item: dict, payload: dict, days: int = 180) -> str:
    """LLM 工具摘要（≤300 字符）。"""
    rows = [r for r in (payload.get("history") or []) if r.get("price") is not None]
    name = str(item.get("displayName") or item.get("name") or item.get("id") or "")
    if not rows:
        return f"{name}近 {days} 天暂无历史观测记录"
    prices = [float(r["price"]) for r in rows]
    lo, hi, last = min(prices), max(prices), prices[-1]
    parts = [
        f"{name}近 {payload.get('days', days)} 天走势",
        f"观测 {len(rows)} 次",
        f"最低 {format_cny(lo)}/最高 {format_cny(hi)}/最新 {format_cny(last)}",
    ]
    if len(prices) >= 2:
        delta = prices[-1] - prices[-2]
        word = "下降" if delta < -0.5 else ("上涨" if delta > 0.5 else "持平")
        parts.append(f"较上次{word}")
    conv4 = item.get("conv4")
    conv5 = item.get("conv5")
    if conv4 or conv5:
        parts.append(f"参考线 四算{format_cny(conv4)}/五算{format_cny(conv5)}")
    return "｜".join(parts)


def sparkline_svg(rows: list[dict], width: int = 640, height: int = 150) -> str:
    """观测点 → SVG 折线（含最低/最高标注）。少于 2 点返回空串。"""
    points = [(str(r.get("record_date")), float(r["price"])) for r in rows if r.get("price") is not None]
    if len(points) < 2:
        return ""

    values = [v for _, v in points]
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    pad_x, pad_y = 14.0, 22.0
    step = (width - pad_x * 2) / (len(points) - 1)

    def xy(i: int, v: float) -> tuple[float, float]:
        x = pad_x + i * step
        y = pad_y + (height - pad_y * 2) * (1.0 - (v - lo) / span)
        return round(x, 1), round(y, 1)

    coords = [xy(i, v) for i, (_, v) in enumerate(points)]
    poly = " ".join(f"{x},{y}" for x, y in coords)
    dots = "".join(
        f'<circle cx="{x}" cy="{y}" r="3.5" class="dot"/>' for x, y in coords
    )
    labels = ""
    lo_i = values.index(lo)
    hi_i = values.index(hi)
    for idx, cls in ((lo_i, "lab-lo"), (hi_i, "lab-hi")):
        x, y = coords[idx]
        anchor = "start" if idx == 0 else ("end" if idx == len(points) - 1 else "middle")
        dy = -8 if cls == "lab-hi" else 16
        labels += (
            f'<text x="{x}" y="{y + dy}" text-anchor="{anchor}" class="{cls}">¥{values[idx]:g}</text>'
        )
    first_d, last_d = points[0][0], points[-1][0]
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" role="img">'
        f'<polyline points="{poly}" class="line"/>{dots}{labels}'
        f'<text x="{pad_x}" y="{height - 4}" class="axis">{first_d}</text>'
        f'<text x="{width - pad_x}" y="{height - 4}" text-anchor="end" class="axis">{last_d}</text>'
        f"</svg>"
    )


def _date_of(rows: list[dict], value: float) -> str:
    for r in rows:
        if r.get("price") is not None and math.isclose(float(r["price"]), value):
            return str(r.get("record_date") or "")
    return ""


def _short(date: Any) -> str:
    return str(date or "")[5:]


def _suffix(status: str) -> str:
    return f"（{status}）" if status else ""
