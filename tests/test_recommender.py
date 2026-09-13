"""预算推荐测试。"""

from core import recommender
from core.snapshot import SnapshotStore

ITEMS = [
    {"id": "a", "displayName": "RG A", "category": "gunpla", "grade": "RG", "scale": "1/144",
     "conv4": 168.0, "conv5": 210.0, "rating": 9.3, "ratingCount": 1696, "releaseDate": "2019-08"},
    {"id": "b", "displayName": "MG B", "category": "gunpla", "grade": "MG", "scale": "1/100",
     "conv4": 220.0, "conv5": 275.0, "rating": 8.8, "ratingCount": 300, "releaseDate": "2012-07"},
    {"id": "c", "displayName": "SHF C", "category": "rider", "grade": "", "scale": "NON",
     "conv4": 300.0, "conv5": 375.0, "rating": 9.0, "ratingCount": 88, "releaseDate": "2020-01"},
    {"id": "d", "displayName": "RG D", "category": "gunpla", "grade": "RG", "scale": "1/144",
     "conv4": 96.0, "conv5": 120.0, "rating": 8.0, "ratingCount": 30, "releaseDate": "2015-05"},
    {"id": "e", "displayName": "成品 E", "category": "gunpla", "grade": "FINISHED", "scale": "NON",
     "conv4": 180.0, "conv5": 225.0, "rating": 9.5, "ratingCount": 50, "releaseDate": "2024-01"},
]


def make_store(tmp_path) -> SnapshotStore:
    store = SnapshotStore(tmp_path / "g")
    store._install([dict(i) for i in ITEMS], {})
    store._load_alias_cache()
    return store


class TestParsers:
    def test_parse_budget(self):
        assert recommender.parse_budget("500") == 500.0
        assert recommender.parse_budget("500块") == 500.0
        assert recommender.parse_budget("¥500.5") == 500.5
        assert recommender.parse_budget("abc") is None
        assert recommender.parse_budget("") is None

    def test_normalize_category(self):
        assert recommender.normalize_category("假面骑士") == "rider"
        assert recommender.normalize_category("高达") == "gunpla"
        assert recommender.normalize_category("zzz") == ""

    def test_normalize_grade(self):
        assert recommender.normalize_grade("rg") == "RG"
        assert recommender.normalize_grade("FM") == "FM_RE"
        assert recommender.normalize_grade("成品") == "FINISHED"

    def test_normalize_usage(self):
        assert recommender.normalize_usage("把玩") == "把玩"
        assert recommender.normalize_usage("送人") == "送礼"
        assert recommender.normalize_usage("入坑") == "新手"


class TestRecommend:
    def test_budget_only(self, tmp_path):
        result = recommender.recommend(make_store(tmp_path), 250)
        ids = [i["id"] for i in result.items]
        assert set(ids) == {"a", "b", "d", "e"}
        # 同层内按评分×热度排序：A(9.3/1696) 第一
        assert result.items[0]["id"] == "a"

    def test_category_and_grade(self, tmp_path):
        result = recommender.recommend(make_store(tmp_path), 500, category="gunpla", grade="RG")
        assert [i["id"] for i in result.items] == ["a", "d"]

    def test_usage_filter(self, tmp_path):
        result = recommender.recommend(make_store(tmp_path), 500, usage="拼装")
        assert "e" not in [i["id"] for i in result.items]  # 成品被排除

    def test_no_padding_advice(self, tmp_path):
        """不足 3 款必须给限制说明与放宽建议。"""
        result = recommender.recommend(make_store(tmp_path), 100)
        assert len(result.items) == 1
        assert result.suggestion and "放宽" in result.suggestion

    def test_zero_budget(self, tmp_path):
        result = recommender.recommend(make_store(tmp_path), 50)
        assert result.items == []
        assert result.suggestion


class TestFormats:
    def test_format(self, tmp_path):
        result = recommender.recommend(make_store(tmp_path), 250)
        text = recommender.format_recommend(result)
        assert text.startswith("【预算 ¥250 推荐】")
        assert "1. " in text

    def test_tool_summary_compact(self, tmp_path):
        result = recommender.recommend(make_store(tmp_path), 250)
        text = recommender.summarize_recommend(result)
        assert len(text) <= 300
        assert "预算 ¥250" in text
