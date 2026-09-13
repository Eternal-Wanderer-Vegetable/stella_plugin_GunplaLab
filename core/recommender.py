"""预算推荐（胶推）：快照本地过滤 + 透明理由，不凑数。"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from .advisor import format_cny
from .snapshot import SnapshotStore, normalize

logger = logging.getLogger("gunplalab.recommender")

MIN_SATISFIED = 3  # 少于 3 款视为「不凑数」，要说明限制条件

# 品类别名 → snapshot.category
CATEGORY_ALIASES: dict[str, str] = {}
for _canon, _names in {
    "gunpla": ("gunpla", "高达", "敢达", "钢普拉", "gundam"),
    "rider": ("rider", "假面骑士", "骑士", "kamen", "rider"),
    "ultraman": ("ultraman", "奥特曼", "奥特", "ultra"),
    "guomo": ("guomo", "国模", "国创", "模玩"),
}.items():
    for _n in _names:
        CATEGORY_ALIASES[normalize(_n)] = _canon

# 级别/形态别名 → snapshot.grade（实测值集：FINISHED/HG/SD/MG/OTHER/RG/FM_RE/MEGA/PG/EG/MGSD）
GRADE_ALIASES: dict[str, str] = {}
for _canon, _names in {
    "RG": ("rg",),
    "MG": ("mg",),
    "HG": ("hg",),
    "PG": ("pg",),
    "EG": ("eg",),
    "SD": ("sd", "bb战士", "sd高达bb战士"),
    "FM_RE": ("fm", "fmre", "fullmechanics"),
    "MEGA": ("mega", "megaspize", "megaspize"),
    "MGSD": ("mgsd",),
    "FINISHED": ("finished", "成品", "完成品"),
}.items():
    for _n in _names:
        GRADE_ALIASES[normalize(_n)] = _canon

# 用途偏好 → 过滤谓词（只施加用户明说的条件，不脑补）
USAGE_FILTERS = {
    "拼装": lambda it: it.get("grade") != "FINISHED",
    "把玩": lambda it: it.get("grade") in ("HG", "RG", "EG", "SD"),
    "展示": lambda it: it.get("grade") in ("MG", "PG", "FM_RE", "MEGA", "MGSD")
    or str(it.get("scale") or "") in ("1/100", "1/60", "1/48"),
    "拍照": lambda it: float(it.get("rating") or 0) >= 8.5,
    "送礼": lambda it: float(it.get("rating") or 0) >= 8.8,
    "新手": lambda it: it.get("grade") in ("EG", "HG", "ENTRY GRADE") or "entry" in str(it.get("series") or "").lower(),
}

_BUDGET_RE = re.compile(r"[^\d.]")

USAGE_ALIASES = {
    "把玩": ("把玩", "可动", "把玩党"),
    "展示": ("展示", "体量", "摆柜", "陈列"),
    "拍照": ("拍照", "摄影"),
    "送礼": ("送礼", "礼物", "送人"),
    "新手": ("新手", "入坑", "入门"),
    "拼装": ("拼装",),
}


@dataclass
class RecommendResult:
    items: list[dict] = field(default_factory=list)
    budget: float = 0.0
    steps: list[tuple[str, int]] = field(default_factory=list)  # (条件标签, 剩余款数)
    suggestion: str = ""


def parse_budget(text: str) -> float | None:
    """「500」「500块」「¥500.5」→ 500.0。解析失败返回 None。"""
    cleaned = _BUDGET_RE.sub("", text or "").strip()
    if not cleaned:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    return value if 1 <= value <= 100000 else None


def normalize_category(text: str) -> str:
    return CATEGORY_ALIASES.get(normalize(text), "")


def normalize_grade(text: str) -> str:
    return GRADE_ALIASES.get(normalize(text), "")


def normalize_usage(text: str) -> str:
    key = normalize(text)
    for usage, names in USAGE_ALIASES.items():
        for n in names:
            if key and (key in normalize(n) or normalize(n) in key):
                return usage
    return ""


def recommend(
    store: SnapshotStore,
    budget: float,
    *,
    category: str = "",
    grade: str = "",
    usage: str = "",
    limit: int = 5,
) -> RecommendResult:
    """四算线（官方价换算）为价格基准做预算内推荐；全程只施加用户给定的条件。"""
    result = RecommendResult(budget=budget)

    pool = [
        it
        for it in store.items.values()
        if it.get("conv4") and float(it["conv4"]) > 0 and float(it["conv4"]) <= budget
    ]
    result.steps.append((f"预算 ≤ {format_cny(budget)}", len(pool)))

    canon_category = normalize_category(category) if category else ""
    if canon_category:
        pool = [it for it in pool if it.get("category") == canon_category]
        result.steps.append((f"品类 {canon_category}", len(pool)))

    canon_grade = normalize_grade(grade) if grade else ""
    if canon_grade:
        pool = [it for it in pool if str(it.get("grade") or "").upper() == canon_grade]
        result.steps.append((f"级别 {canon_grade}", len(pool)))

    usage_key = normalize_usage(usage) if usage else ""
    pred = USAGE_FILTERS.get(usage_key)
    if pred:
        before = len(pool)
        pool = [it for it in pool if pred(it)]
        if len(pool) != before:
            result.steps.append((f"用途 {usage_key}", len(pool)))

    pool.sort(
        key=lambda it: float(it.get("rating") or 0) * (1.0 + math.log10(float(it.get("ratingCount") or 0) + 1)),
        reverse=True,
    )
    result.items = pool[: max(1, limit)]

    if len(result.items) < MIN_SATISFIED:
        result.suggestion = _limited_advice(store, result, canon_category, canon_grade)
    return result


def _limited_advice(store: SnapshotStore, result: RecommendResult, category: str, grade: str) -> str:
    """不凑数：说明是哪个条件把结果卡少的，并给出最近的放宽方向。"""
    steps = result.steps
    bottleneck = next((label for label, count in steps if count < MIN_SATISFIED), "")
    advice = f"当前条件只找到 {len(result.items)} 款"
    if bottleneck:
        advice += f"（卡在「{bottleneck}」）"
    # 放宽预算：同条件往上看一档最近的价格
    relax = None
    if result.items:
        max_conv4 = max(float(it["conv4"]) for it in result.items)
        candidates = [
            float(it["conv4"])
            for it in store.items.values()
            if it.get("conv4")
            and float(it["conv4"]) > max_conv4
            and (not category or it.get("category") == category)
            and (not grade or str(it.get("grade") or "").upper() == grade)
        ]
        if candidates:
            relax = min(candidates)
    if relax:
        advice += f"，可把预算放宽到 {format_cny(relax)} 试试"
    else:
        advice += "，可试着放宽预算或减少条件"
    return advice


def _reason(item: dict) -> str:
    bits = [f"四算 {format_cny(item.get('conv4'))}"]
    if item.get("rating"):
        bits.append(f"{item['rating']} 分（{item.get('ratingCount') or 0} 票）")
    meta = "·".join(b for b in (item.get("grade") or "", item.get("scale") or "") if b)
    if meta:
        bits.append(meta)
    if item.get("releaseDate"):
        bits.append(f"{item['releaseDate']} 发售")
    return "｜".join(bits)


def format_recommend(result: RecommendResult) -> str:
    lines = [f"【预算 {format_cny(result.budget)} 推荐】"]
    if not result.items:
        lines.append("预算内没有找到符合条件的模型")
        if result.suggestion:
            lines.append(result.suggestion)
        return "\n".join(lines)
    for idx, item in enumerate(result.items, 1):
        lines.append(f"{idx}. {item.get('displayName')}｜{_reason(item)}")
    if result.suggestion:
        lines.append(f"—— {result.suggestion}")
    return "\n".join(lines)


def summarize_recommend(result: RecommendResult) -> str:
    """LLM 工具摘要：紧凑候选 + 不足时说明限制（≤300 字符）。"""
    if not result.items:
        return f"预算 {format_cny(result.budget)} 内没有符合条件的模型。{result.suggestion}"
    parts = [f"{i['displayName']}（{format_cny(i['conv4'])}，{i.get('rating') or '?'} 分）" for i in result.items[:3]]
    text = f"预算 {format_cny(result.budget)} 推荐：{'；'.join(parts)}"
    if result.suggestion:
        text += f"。{result.suggestion}"
    return text
