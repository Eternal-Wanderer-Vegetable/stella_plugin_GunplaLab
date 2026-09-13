"""比价结论：现货价 vs 四算/五算黄金线（Notion 要求：附理由与数据时间）。"""

from __future__ import annotations

from typing import Any

# 巡检可货状态标签（当前库内实测仅出现 A）
AVAILABILITY_LABELS = {
    "A": "有现货",
    "B": "部分现货",
    "C": "预订",
    "D": "缺货",
}


def format_cny(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "未知"
    # 保留 .5 参考线的半元精度，避免四舍五入后与现货价「对不上」
    return f"¥{number:.1f}" if number % 1 else f"¥{number:.0f}"


def format_jpy(value: Any) -> str:
    try:
        return f"{int(value):,} 日元"
    except (TypeError, ValueError):
        return "未知"


def price_verdict(spot: float, conv4: float, conv5: float) -> str:
    """返回一句结论话术。参考线缺失时给出中性说明。"""
    try:
        spot = float(spot)
    except (TypeError, ValueError):
        return "暂无现货观测数据，无法判断"
    try:
        conv4 = float(conv4) if conv4 else 0.0
        conv5 = float(conv5) if conv5 else 0.0
    except (TypeError, ValueError):
        conv4 = conv5 = 0.0

    if conv4 <= 0 and conv5 <= 0:
        return "该款暂无官方定价参考线，仅作参考"
    if conv4 > 0 and spot <= conv4:
        return "低于四算黄金线，性价比区间"
    if conv5 > 0 and spot <= conv5:
        return "处于四算~五算之间，正常水平"
    return "明显高于五算线，存在溢价，可等再版/补货"


def verdict_tone(verdict: str) -> str:
    """结论 → 卡片配色档位：good / ok / warn / na。"""
    if "低于四算" in verdict:
        return "good"
    if "四算~五算" in verdict:
        return "ok"
    if "溢价" in verdict:
        return "warn"
    return "na"


def availability_label(status: Any) -> str:
    return AVAILABILITY_LABELS.get(str(status or "").strip(), "现货状态未知")
