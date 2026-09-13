"""情报/排期/今日胶情测试。"""

import httpx
import pytest

from core import news
from core.api_client import GunplaLabClient, GunplaApiError

INTEL_ITEMS = [
    {"title": "MGSD 刹帝利", "category": "new", "subcategory": "MGSD",
     "releaseDate": "2026年09月发售", "price": "日本地区建议零售价：<br>7,700日元(含税)",
     "source": "bandaihobbysite.cn"},
    {"title": "＜SIDE MS＞ νガンダム", "category": "reissue", "subcategory": "METAL ROBOT魂",
     "releaseDate": "2026年11月发售", "source": "tamashiiweb.com"},
]
EVENTS = [
    {"title": "HG 1/144 斑豹高达", "eventType": "release", "dateLabel": "2026年8月",
     "officialPrice": {"amount": 2420, "currency": "JPY"}},
    {"title": "RG 1/144 沙扎比", "eventType": "reissue", "dateLabel": "2026年9月", "officialPrice": {}},
]


class TestPure:
    def test_clean_strips_html(self):
        assert news._clean("7,700日元") == "7,700日元"
        assert "<br>" not in news._clean("日本地区建议零售价：<br>7,700日元(含税)")

    def test_labels(self):
        assert news.intel_label("new") == "新品"
        assert news.intel_label("reissue") == "再版"
        assert news.event_label("release") == "发售"
        assert news.event_label("estimated-release") == "预定发售"

    def test_normalize_month(self):
        assert news.normalize_month("2026-10") == "2026-10"
        assert news.normalize_month("2026年9月") == "2026-09"
        assert news.normalize_month("9月").endswith("-09")
        assert news.normalize_month("") == news.current_month()

    def test_format_intel(self):
        text = news.format_intel(INTEL_ITEMS, "2026-09-12T03:30:00+08:00")
        assert "[新品] MGSD 刹帝利" in text
        assert "7,700日元" in text and "<br>" not in text
        assert "[再版]" in text

    def test_format_schedule(self):
        text = news.format_schedule(EVENTS, "2026-09", "2026-08-31T00:00:01+08:00")
        assert "【发售排期 · 2026-09】共 2 条" in text
        assert "2,420 日元" in text
        assert "[再版] RG 1/144 沙扎比" in text

    def test_compact_under_300(self):
        assert len(news.format_intel_compact(INTEL_ITEMS)) <= 300
        assert len(news.format_schedule_compact(EVENTS)) <= 300


def make_client(handler) -> GunplaLabClient:
    return GunplaLabClient("http://t.local/api/v1/data", min_interval=0,
                           transport=httpx.MockTransport(handler))


class TestBuildDigest:
    @pytest.mark.asyncio
    async def test_digest_with_content(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/intel-events"):
                return httpx.Response(200, json={"ok": True, "meta": {}, "data": {"items": INTEL_ITEMS}})
            assert request.url.params.get("month"), "胶情排期必须带当月过滤"
            return httpx.Response(200, json={"ok": True, "meta": {}, "data": {"events": EVENTS}})

        text = await news.build_digest(make_client(handler), "https://example.com")
        assert "今日胶情" in text
        assert "◆ 情报" in text and "◆ 本月排期" in text
        assert "完整内容：https://example.com" in text

    @pytest.mark.asyncio
    async def test_digest_empty_says_so(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = {"items": []} if request.url.path.endswith("/intel-events") else {"events": []}
            return httpx.Response(200, json={"ok": True, "meta": {}, "data": payload})

        text = await news.build_digest(make_client(handler))
        assert "今天没有重要更新" in text

    @pytest.mark.asyncio
    async def test_digest_api_down_still_answers(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom")

        text = await news.build_digest(make_client(handler))
        assert "今天没有重要更新" in text
