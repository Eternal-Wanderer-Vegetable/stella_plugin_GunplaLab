"""SnapshotStore 检索与别名学习测试（合成数据）。"""

from core.snapshot import SnapshotStore, normalize

ITEMS = [
    {
        "id": "78dm_ct_1",
        "name": "32 RX-93 ν高达（牛高达）",
        "displayName": "[RG系列拼装模型] 32 RX-93 ν高达（牛高达）",
        "num": "RG32",
        "series": "RG系列拼装模型",
        "grade": "RG",
        "versionTag": "普通版",
        "scale": "1/144",
        "jpy": 4200,
        "conv4": 168.0,
        "conv5": 210.0,
        "rating": 9.3,
        "ratingCount": 1696,
        "jan": "4573102612345",
        "work": "逆袭的夏亚",
        "universeName": "UC｜宇宙世纪",
        "releaseDate": "2019-08",
        "reissueDate": "2020-09",
        "image": "https://cdn.example/1.jpg",
    },
    {
        "id": "78dm_ct_2",
        "name": "33 RX-93ν2 Hi-ν高达",
        "displayName": "[RG系列拼装模型] 33 RX-93ν2 Hi-ν高达",
        "num": "RG33",
        "series": "RG系列拼装模型",
        "grade": "RG",
        "versionTag": "限定版",
        "scale": "1/144",
        "jpy": 4950,
        "conv4": 198.0,
        "conv5": 247.5,
        "rating": 9.4,
        "ratingCount": 412,
        "jan": "",
        "work": "贝托蒂嘉的子嗣",
        "universeName": "UC｜宇宙世纪",
        "releaseDate": "2021-09",
        "reissueDate": "",
        "image": "",
    },
    {
        "id": "78dm_ct_3",
        "name": "新安洲",
        "displayName": "[MG系列拼装模型] 新安洲",
        "num": "MG01",
        "series": "MG系列拼装模型",
        "grade": "MG",
        "versionTag": "普通版",
        "scale": "1/100",
        "jpy": 5500,
        "conv4": 220.0,
        "conv5": 275.0,
        "rating": 8.8,
        "ratingCount": 300,
        "jan": "",
        "work": "UC",
        "universeName": "",
        "releaseDate": "2012-07",
        "reissueDate": "",
        "image": "",
    },
]


def make_store(tmp_path) -> SnapshotStore:
    store = SnapshotStore(tmp_path / "gunplalab")
    store._install([dict(i) for i in ITEMS], {"generated_at": "2026-09-13T08:00:00+08:00"})
    store._load_alias_cache()
    return store


class TestNormalize:
    def test_fullwidth_and_case(self):
        assert normalize("ＲＧ海牛") == "rg海牛"

    def test_scale_stripped(self):
        assert normalize("RG 1/144 海牛") == "rg海牛"

    def test_punct_stripped(self):
        assert normalize("ν高达（牛高达）") == "ν高达牛高达"


class TestSearch:
    def test_exact_name(self, tmp_path):
        store = make_store(tmp_path)
        hits = store.search("新安洲")
        assert hits and hits[0]["id"] == "78dm_ct_3"

    def test_num_lookup(self, tmp_path):
        store = make_store(tmp_path)
        hits = store.search("RG33")
        assert hits and hits[0]["id"] == "78dm_ct_2"

    def test_prefix(self, tmp_path):
        store = make_store(tmp_path)
        hits = store.search("新安")
        assert hits and hits[0]["id"] == "78dm_ct_3"

    def test_grade_stripped_alias(self, tmp_path):
        store = make_store(tmp_path)
        store.learn_aliases([("海牛", "78dm_ct_2")])
        hits = store.search("RG海牛")
        assert hits and hits[0]["id"] == "78dm_ct_2"

    def test_learned_alias_direct_hit(self, tmp_path):
        store = make_store(tmp_path)
        store.learn_aliases([("卡牛", "78dm_ct_1")])
        assert store.search("卡牛")[0]["id"] == "78dm_ct_1"
        # 别名缓存应持久化并在重载后生效
        store2 = SnapshotStore(store.data_dir)
        store2._install([dict(i) for i in ITEMS], {})
        store2._load_alias_cache()
        assert store2.search("卡牛")[0]["id"] == "78dm_ct_1"

    def test_jan_lookup(self, tmp_path):
        store = make_store(tmp_path)
        hits = store.search("4573102612345")
        assert hits and hits[0]["id"] == "78dm_ct_1"

    def test_substring_fallback(self, tmp_path):
        store = make_store(tmp_path)
        hits = store.search("高达")
        assert hits, "子串兜底应有结果"
        ids = [h["id"] for h in hits]
        assert "78dm_ct_1" in ids

    def test_limit(self, tmp_path):
        store = make_store(tmp_path)
        assert len(store.search("高达", limit=1)) == 1

    def test_no_hit(self, tmp_path):
        store = make_store(tmp_path)
        assert store.search("独角兽") == []

    def test_ranked_tiers(self, tmp_path):
        store = make_store(tmp_path)
        store.learn_aliases([("卡牛", "78dm_ct_1")])
        ranked = store.search_ranked("卡牛")
        assert ranked[0][1] == 0, "别名精确应为 tier 0"
        ranked = store.search_ranked("高达")
        assert all(tier == 3 for _, tier in ranked), "子串命中应为 tier 3"

    def test_popularity_beats_low_vote_perfect_rating(self, tmp_path):
        """同层内，高票热门件应排在低票满分件之前。"""
        store = make_store(tmp_path)
        # 「牛」同时子串命中两者：ct_1 9.3 分/1696 票，ct_2 9.4 分/412 票
        ranked = store.search_ranked("高达")
        ids = [item["id"] for item, _ in ranked]
        assert ids.index("78dm_ct_1") < ids.index("78dm_ct_2")


class TestDiskCache:
    def test_roundtrip(self, tmp_path):
        import gzip as _gzip
        import json

        store = make_store(tmp_path)
        payload = {"meta": {"generated_at": "2026-09-13T08:00:00+08:00"}, "items": ITEMS}
        gz = _gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"), mtime=0)
        store.snapshot_etag = '"123-456"'
        store._save_to_disk(gz)

        store2 = SnapshotStore(store.data_dir)
        assert store2.load_from_disk() is True
        assert len(store2.items) == 3
        assert store2.snapshot_etag == '"123-456"'
        assert store2.meta.get("generated_at", "").startswith("2026-09-13")
