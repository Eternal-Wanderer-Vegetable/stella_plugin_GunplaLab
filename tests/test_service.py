"""业务服务层测试：检索兜底、消歧会话、文本卡与工具摘要。"""

import httpx
import pytest

from core.api_client import GunplaLabClient
from core.service import GunplaService
from core.snapshot import normalize
from tests.test_search import ITEMS, make_store

DETAIL = {
    "item_id": "78dm_ct_2",
    "identifiers": {"jan_code": "", "aliases": ["RG海牛", "海牛高达", "Hi-Nu"]},
    "names": {"zh": "33 RX-93ν2 Hi-ν高达", "ja": "Hi-νガンダム", "en": "Hi-Nu Gundam"},
    "classification": {"category": "gunpla", "series": "RG", "scale": "1/144"},
    "release": {"release_date": "2021-09", "reissue_date": "2026-10"},
    "official_price": {"currency": "JPY", "amount": 4950, "conv4": 198.0, "conv5": 247.5},
    "market_observation": {
        "current_price": 220.0,
        "currency": "CNY",
        "availability": "A",
        "source": "pdd",
        "source_url": "https://mobile.yangkeduo.com/goods.html?goods_id=1",
        "observed_at": "2026-09-12 18:30:00",
    },
    "links": {"cover_url": "", "official_detail_url": "https://example.com/item/2"},
    "rating": {"score": 9.4, "vote_count": 412},
}


def make_service(tmp_path, client=None) -> GunplaService:
    store = make_store(tmp_path)
    return GunplaService(store, client, max_results=5, website_url="https://example.com")


class TestResolve:
    @pytest.mark.asyncio
    async def test_local_hit(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("新安洲", scope="s1")
        assert outcome.status == "hit"
        assert len(outcome.items) == 1

    @pytest.mark.asyncio
    async def test_remote_alias_fallback_and_learning(self, tmp_path):
        # 本地只有合成 3 条，「海牛」不在本地 → 走远程
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/items"):
                rows = [{"item_id": "78dm_ct_2", "name_zh": "33 RX-93ν2 Hi-ν高达"}]
                return httpx.Response(200, json={"ok": True, "meta": {}, "data": {"items": rows}})
            if request.url.path.endswith("/items/78dm_ct_2"):
                return httpx.Response(200, json={"ok": True, "meta": {}, "data": DETAIL})
            return httpx.Response(404, json={"ok": False, "error": {"code": "ENDPOINT_NOT_FOUND"}})

        client = GunplaLabClient("http://t.local/api/v1/data", min_interval=0,
                                 transport=httpx.MockTransport(handler))
        svc = make_service(tmp_path, client)
        outcome = await svc.resolve("海牛")
        assert outcome.status == "hit"
        assert outcome.source == "remote"
        assert outcome.items[0]["id"] == "78dm_ct_2"
        # 别名应已学习：第二次查询本地直接命中
        outcome2 = await svc.resolve("海牛")
        assert outcome2.status == "hit" and outcome2.source == "local"

    @pytest.mark.asyncio
    async def test_candidates_session(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("高达", scope="g1|u1")
        assert outcome.status == "hit" and len(outcome.items) >= 2
        # 序号选取
        picked = await svc.resolve("2", scope="g1|u1")
        assert picked.status == "hit" and len(picked.items) == 1
        # 一次性会话：再次选取应失败
        again = await svc.resolve("1", scope="g1|u1")
        assert again.status == "miss"

    @pytest.mark.asyncio
    async def test_miss(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("不存在的模型xyz")
        assert outcome.status == "miss"

    @pytest.mark.asyncio
    async def test_weak_local_hit_falls_back_to_remote(self, tmp_path):
        """本地只有模糊子串命中时，应尝试远程别名兜底并采用远程结果。"""
        from core.snapshot import SnapshotStore

        items = [
            {
                # 名称以「海牛」开头（前缀命中，tier 2）——也不应跳过远程兜底
                "id": "gk_001", "name": "海牛高达", "displayName": "[GK] 海牛高达",
                "num": "GK01", "jpy": 0, "conv4": 0, "conv5": 0,
                "grade": "OTHER", "versionTag": "普通版", "rating": 10.0, "ratingCount": 5,
            },
            {
                # 本地存在但名称不含「海牛」——别名学习的前提是条目在本地快照中
                "id": "78dm_ct_2", "name": "33 RX-93ν2 Hi-ν高达", "displayName": "[RG] 33 RX-93ν2 Hi-ν高达",
                "num": "RG33", "jpy": 4950, "conv4": 198.0, "conv5": 247.5,
                "grade": "RG", "versionTag": "普通版", "rating": 9.4, "ratingCount": 412,
            },
        ]
        store = SnapshotStore(tmp_path / "g")
        store._install(items, {})
        store._load_alias_cache()

        remote_row = {"item_id": "78dm_ct_2", "name_zh": "RG 33 海牛高达"}
        detail = {
            "item_id": "78dm_ct_2",
            "identifiers": {"aliases": ["海牛", "高达"]},  # 「高达」是泛化别名，不应被学习
            "names": {"ja": "Hi-νガンダム"},
        }

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/items"):
                return httpx.Response(
                    200, json={"ok": True, "meta": {}, "data": {"items": [remote_row]}}
                )
            if request.url.path.endswith("/items/78dm_ct_2"):
                return httpx.Response(200, json={"ok": True, "meta": {}, "data": detail})
            return httpx.Response(404, json={"ok": False})

        client = GunplaLabClient("http://t.local/api/v1/data", min_interval=0,
                                 transport=httpx.MockTransport(handler))
        svc = GunplaService(store, client)
        outcome = await svc.resolve("海牛")
        assert outcome.status == "hit"
        assert outcome.source == "remote"
        assert outcome.items[0]["id"] == "78dm_ct_2"
        # 「海牛」别名已学习；泛化别名「高达」未学习
        assert store._alias.get(normalize("海牛")) == ["78dm_ct_2"]
        assert normalize("高达") not in store._alias
        # 第二次查询直接本地精确命中
        outcome2 = await svc.resolve("海牛")
        assert outcome2.source == "local" and outcome2.items[0]["id"] == "78dm_ct_2"


class TestFormatting:
    @pytest.mark.asyncio
    async def test_detail_card_contains_key_fields(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("新安洲")
        card = svc.format_detail_card(outcome.items[0], None)
        assert "新安洲" in card
        assert "四算" in card and "¥220" in card
        assert "数据截至" in card
        assert "完整档案：https://example.com" in card

    @pytest.mark.asyncio
    async def test_price_card_verdict(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("RG33")
        card = svc.format_price_card(outcome.items[0], DETAIL)
        assert "4,950 日元" in card
        assert "¥220" in card
        assert "四算~五算" in card
        assert "近期再版" in card  # reissue 2026-10 是未来月份
        assert "09-12 18:30" in card

    @pytest.mark.asyncio
    async def test_candidates_text(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("高达")
        text = svc.format_candidates("高达", outcome.items)
        assert text.startswith("「高达」匹配到")
        assert "1." in text and "120 秒内有效" in text


class TestToolSummary:
    @pytest.mark.asyncio
    async def test_summarize_price_compact(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("RG33")
        summary = svc.summarize_price(outcome.items[0], DETAIL)
        assert len(summary) <= 300
        assert "→" in summary and "四算~五算" in summary
        assert "¥220" in summary

    @pytest.mark.asyncio
    async def test_summarize_detail_compact(self, tmp_path):
        svc = make_service(tmp_path)
        outcome = await svc.resolve("新安洲")
        summary = svc.summarize_detail(outcome.items[0], None)
        assert len(summary) <= 300
        assert "MG" in summary or "MG系列" in summary
