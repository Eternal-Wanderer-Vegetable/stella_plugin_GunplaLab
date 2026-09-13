"""纠错暂存与胶情采集闸门测试。"""

import httpx
import pytest

from core import corrections, news
from core.api_client import GunplaLabClient


class TestCorrections:
    def test_append_and_read(self, tmp_path):
        p1 = corrections.save_correction(tmp_path, scope="g1", sender="u1", text="海牛 应该是 RG 36")
        p2 = corrections.save_correction(tmp_path, scope="g1", sender="u2", text="第二条")
        assert p1 == p2
        lines = [_.strip() for _ in p1.read_text(encoding="utf-8").splitlines() if _.strip()]
        assert len(lines) == 2
        import json
        rec = json.loads(lines[0])
        assert rec["text"] == "海牛 应该是 RG 36" and rec["scope"] == "g1" and rec["sender"] == "u1"
        assert rec["time"].startswith("20")


class TestDigestGating:
    @pytest.mark.asyncio
    async def test_collect_flags_content(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/intel-events"):
                return httpx.Response(200, json={"ok": True, "meta": {}, "data": {
                    "items": [{"id": "a", "title": "MGSD X", "category": "new"}]}})
            return httpx.Response(200, json={"ok": True, "meta": {}, "data": {"events": []}})

        client = GunplaLabClient("http://t.local/api/v1/data", min_interval=0,
                                 transport=httpx.MockTransport(handler))
        collected = await news.collect_digest(client)
        assert collected["intel"] and not collected["events"]
        text = news.render_digest(collected, "https://example.com")
        assert "◆ 情报" in text and "今天没有重要更新" not in text

    @pytest.mark.asyncio
    async def test_collect_empty(self):
        def handler(request: httpx.Request) -> httpx.Response:
            payload = {"items": []} if request.url.path.endswith("/intel-events") else {"events": []}
            return httpx.Response(200, json={"ok": True, "meta": {}, "data": payload})

        client = GunplaLabClient("http://t.local/api/v1/data", min_interval=0,
                                 transport=httpx.MockTransport(handler))
        collected = await news.collect_digest(client)
        assert not (collected["intel"] or collected["events"])
        assert "今天没有重要更新" in news.render_digest(collected)
