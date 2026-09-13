"""盯盘订阅测试：存储持久化、变化检测、雷达匹配、轮询任务。"""

import httpx
import pytest

from core import watch
from core.api_client import GunplaLabClient
from core.snapshot import SnapshotStore

DETAIL = {
    "item_id": "78dm_ct_10",
    "names": {"zh": "PG 独角兽高达[最终决战]"},
    "market_observation": {
        "current_price": 1642.0, "availability": "A", "source": "pdd",
        "observed_at": "2026-09-06T23:44:25+08:00",
    },
    "release": {"reissue_date": ""},
    "links": {"official_detail_url": "https://example.com/10"},
}
SNAP_ITEM = {"id": "78dm_ct_10", "displayName": "PG 独角兽高达[最终决战]", "reissueDate": ""}


def make_store(tmp_path) -> tuple[watch.WatchStore, SnapshotStore]:
    snap = SnapshotStore(tmp_path / "snap")
    snap._install([dict(SNAP_ITEM)], {})
    snap._load_alias_cache()
    return watch.WatchStore(tmp_path / "watch.json"), snap


def make_sub(**kwargs) -> watch.WatchItem:
    base = dict(session="g1", keyword="独角兽", item_id="78dm_ct_10",
                target_price=None, watch_reissue=False, watch_restock=False)
    base.update(kwargs)
    return watch.WatchItem(**base)


class TestStore:
    def test_add_remove_persist(self, tmp_path):
        store, _ = make_store(tmp_path)
        store.add(make_sub())
        store.add(make_sub(session="g2", radar=True, keyword="MGSD"))
        assert len(store.list_group("g1")) == 1
        assert len(store.list_group("g2")) == 1

        store2 = watch.WatchStore(store.path)
        assert len(store2.subscriptions) == 2
        assert store2.remove("g1", 1).item_id == "78dm_ct_10"
        assert store2.remove("g1", 1) is None


class TestEvaluate:
    def test_first_round_records_baseline_no_alert(self, tmp_path):
        sub = make_sub(watch_restock=True, watch_reissue=True)
        assert watch.evaluate_item(sub, SNAP_ITEM, DETAIL) == []
        assert sub.last_seen["price"] == 1642.0

    def test_price_target_trigger_and_rearm(self):
        sub = make_sub(target_price=1700.0)
        sub.last_seen = {"price": 1750.0, "avail": "A", "reissue": "", "observed_at": "t"}
        alerts = watch.evaluate_item(sub, SNAP_ITEM, DETAIL)  # 1642 ≤ 1700 → 触发
        assert len(alerts) == 1 and "盯盘触发" in alerts[0] and sub.triggered
        # 已触发状态下继续低于目标不再重复推送
        assert watch.evaluate_item(sub, SNAP_ITEM, DETAIL) == []
        # 价格回升 → 重新武装（无告警）
        detail2 = {**DETAIL, "market_observation": {**DETAIL["market_observation"], "current_price": 1800.0}}
        assert watch.evaluate_item(sub, SNAP_ITEM, detail2) == []
        assert not sub.triggered

    def test_restock_transition(self):
        sub = make_sub(watch_restock=True)
        sub.last_seen = {"price": 1642.0, "avail": "C", "reissue": "", "observed_at": "t"}
        alerts = watch.evaluate_item(sub, SNAP_ITEM, DETAIL)  # C → A
        assert len(alerts) == 1 and "补货" in alerts[0]

    def test_reissue_change(self):
        sub = make_sub(watch_reissue=True)
        sub.last_seen = {"price": 1642.0, "avail": "A", "reissue": "2026-01", "observed_at": "t"}
        detail = {**DETAIL, "release": {"reissue_date": "2026-12"}}
        alerts = watch.evaluate_item(sub, SNAP_ITEM, detail)
        assert len(alerts) == 1 and "再版" in alerts[0]


class TestRadar:
    def test_matches(self):
        sub = make_sub(radar=True, keyword="MGSD")
        items = [
            {"id": "i1", "title": "MGSD 刹帝利", "subcategory": "MGSD"},
            {"id": "i2", "title": "＜SIDE MS＞ νガンダム", "subcategory": "METAL ROBOT魂"},
        ]
        hits = watch.radar_matches(sub, items)
        assert [h["id"] for h in hits] == ["i1"]

    def test_store_dedupes_intel(self, tmp_path):
        store, _ = make_store(tmp_path)
        items = [{"id": "a"}, {"id": "b"}]
        assert len(store.filter_new_intel(items)) == 2
        assert store.filter_new_intel(items) == []  # 第二轮全部已见


class TestPollOnce:
    @pytest.mark.asyncio
    async def test_poll_triggers_and_pushes(self, tmp_path):
        store, snap = make_store(tmp_path)
        store.add(make_sub(target_price=1700.0))
        store.add(make_sub(session="g1", radar=True, keyword="MGSD"))

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/items/78dm_ct_10"):
                return httpx.Response(200, json={"ok": True, "meta": {}, "data": DETAIL})
            if path.endswith("/intel-events"):
                return httpx.Response(200, json={"ok": True, "meta": {}, "data": {
                    "items": [{"id": "n1", "title": "MGSD 新品X", "category": "new"}]}})
            return httpx.Response(404, json={"ok": False, "error": {}})

        client = GunplaLabClient("http://t.local/api/v1/data", min_interval=0,
                                 transport=httpx.MockTransport(handler))
        pushed: list[tuple[str, str]] = []

        async def push(session: str, text: str) -> bool:
            pushed.append((session, text))
            return True

        stats = await watch.poll_once(store, snap, client, push=push)
        assert stats["items"] == 1 and stats["alerts"] == 1 and stats["radar"] == 1
        targets = [s for s, _ in pushed]
        assert targets.count("g1") == 2  # 盯盘触发 + 雷达各一条
        assert any("盯盘触发" in t for _, t in pushed)
        assert any("情报雷达" in t for _, t in pushed)
        # 第二轮：价格未变（已触发不再推）、情报已去重
        pushed.clear()
        stats2 = await watch.poll_once(store, snap, client, push=push)
        assert stats2["alerts"] == 0 and stats2["radar"] == 0 and pushed == []


class TestFormatList:
    def test_empty_and_filled(self, tmp_path):
        store, _ = make_store(tmp_path)
        assert "还没有盯盘订阅" in watch.format_list(store.list_group("g1"))
        store.add(make_sub(target_price=1700.0))
        store.add(make_sub(radar=True, keyword="MGSD"))
        text = watch.format_list(store.list_group("g1"))
        assert "共 2 条" in text and "盯盘中" in text and "[雷达]" in text
