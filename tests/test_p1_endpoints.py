"""新增端点（history / release-events / intel-events）与卡片模板测试。"""

import httpx
import pytest

from core.api_client import GunplaLabClient
from core import render

BASE = "http://test.local/api/v1/data"


def make_client(handler) -> GunplaLabClient:
    return GunplaLabClient(BASE, min_interval=0, transport=httpx.MockTransport(handler))


class TestHistoryEndpoint:
    @pytest.mark.asyncio
    async def test_days_clamped_and_encoded(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = str(request.url)
            seen["days"] = request.url.params.get("days")
            return httpx.Response(200, json={"ok": True, "meta": {}, "data": {"history": []}})

        client = make_client(handler)
        await client.get_item_history("HGUC#21", days=999)
        assert "items/HGUC%2321/history" in seen["path"]
        assert seen["days"] == "365"
        await client.close()


class TestNewsEndpoints:
    @pytest.mark.asyncio
    async def test_release_events_params_and_as_of(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["month"] = request.url.params.get("month")
            seen["limit"] = request.url.params.get("limit")
            return httpx.Response(
                200,
                json={"ok": True, "meta": {"data_as_of": "2026-08-31T00:00:01+08:00"},
                      "data": {"events": [{"title": "x"}], "count": 1}},
            )

        client = make_client(handler)
        data, as_of = await client.get_release_events(month="2026-09", limit=10)
        assert seen["month"] == "2026-09" and seen["limit"] == "10"
        assert data["events"][0]["title"] == "x"
        assert as_of == "2026-08-31T00:00:01+08:00"
        await client.close()

    @pytest.mark.asyncio
    async def test_intel_events(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True, "meta": {}, "data": {"items": [], "count": 0}})

        client = make_client(handler)
        data, as_of = await client.get_intel_events(limit=5)
        assert data["items"] == [] and as_of == ""
        await client.close()


class TestTemplates:
    def test_template_files_exist(self):
        for name in ("detail_card.j2", "price_card.j2", "schedule_card.j2"):
            source = render.load_template(name)
            assert "{%" in source or "{{" in source

    def test_price_card_renders_with_jinja2(self):
        jinja2 = pytest.importorskip("jinja2")
        from jinja2 import Template

        source = render.load_template("price_card.j2")
        html = Template(source).render({
            "display_name": "RG 36 Hi-ν高达",
            "official_line": "4,500 日元",
            "golden_lines": "¥180 / ¥225",
            "spot_line": "¥200",
            "spot_source": "pdd",
            "spot_time": "09-12 18:30",
            "verdict": "处于四算~五算之间，正常水平",
            "tone": "ok",
            "sparkline": "<svg><polyline points='1,2'/></svg>",
            "note": "",
            "data_as_of": "09-13 08:23",
        })
        assert "RG 36 Hi-ν高达" in html
        assert "verdict ok" in html
        assert "<svg><polyline" in html  # |safe 未转义

    def test_detail_card_without_cover(self):
        jinja2 = pytest.importorskip("jinja2")
        from jinja2 import Template

        html = Template(render.load_template("detail_card.j2")).render({
            "display_name": "测试模型", "tags": ["RG", "1/144"], "work": "逆袭的夏亚",
            "official_line": "4,200 日元", "dates": "发售 2019-08", "spot_line": "",
            "rating_line": "", "jan": "", "cover_uri": None,
            "data_as_of": "09-13 08:23", "detail_url": "",
        })
        assert "暂无封面图" in html
        assert "测试模型" in html


class TestCoverCache:
    @pytest.mark.asyncio
    async def test_cache_roundtrip(self, tmp_path):
        cache = render.CoverCache(tmp_path / "covers", ttl_days=7)
        url = "https://cdn.example/cover.jpg"
        payload = b"\xff\xd8\xff" + b"fakejpeg"

        async def fetcher(u: str) -> bytes | None:
            return payload if u == url else None

        # 用 monkey 方式直接替换下载方法，避免真实网络
        cache._download = fetcher  # type: ignore[assignment]
        uri = await cache.get_data_uri(url)
        assert uri and uri.startswith("data:image/jpeg;base64,")
        # 第二次命中磁盘缓存
        uri2 = await cache.get_data_uri(url)
        assert uri2 == uri
        await cache.close()

    @pytest.mark.asyncio
    async def test_download_failure_returns_none(self, tmp_path):
        cache = render.CoverCache(tmp_path / "covers")
        cache._download = lambda url: None  # type: ignore[assignment]
        assert await cache.get_data_uri("") is None
        await cache.close()
