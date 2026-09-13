"""胶史摘要与走势图测试。"""

from core import trends
from core.advisor import verdict_tone

HISTORY = {
    "item_id": "x",
    "days": 180,
    "history": [
        {"record_date": "2026-06-15", "price": 265.0, "status": "ok"},
        {"record_date": "2026-07-20", "price": 252.0, "status": "ok"},
        {"record_date": "2026-09-12", "price": 248.0, "status": "fresh"},
    ],
}
ITEM = {"displayName": "RG 36 Hi-ν高达", "conv4": 180.0, "conv5": 225.0}


class TestSummarizeHistory:
    def test_normal(self):
        text = trends.summarize_history(ITEM, HISTORY, 180)
        assert "观测 3 次" in text
        assert "最低 ¥248" in text and "最高 ¥265" in text
        assert "较上次 ↓" in text
        assert "参考线：四算 ¥180｜五算 ¥225" in text

    def test_empty(self):
        text = trends.summarize_history(ITEM, {"days": 180, "history": []}, 180)
        assert "暂无历史观测记录" in text
        assert "参考线" in text

    def test_single_point(self):
        payload = {"days": 90, "history": [{"record_date": "2026-09-12", "price": 250.0, "status": "fresh"}]}
        text = trends.summarize_history(ITEM, payload, 90)
        assert "观测 1 次" in text
        assert "趋势" not in text

    def test_compact_under_300(self):
        text = trends.summarize_history_compact(ITEM, HISTORY, 180)
        assert len(text) <= 300
        assert "下降" in text and "最低 ¥248" in text


class TestSparkline:
    def test_polyline(self):
        svg = trends.sparkline_svg(HISTORY["history"])
        assert svg.startswith("<svg")
        assert "<polyline" in svg and 'class="dot"' in svg
        assert "¥265" in svg and "¥248" in svg

    def test_fewer_than_two_points(self):
        assert trends.sparkline_svg([]) == ""
        assert trends.sparkline_svg([{"record_date": "d", "price": 1.0}]) == ""


class TestVerdictTone:
    def test_tones(self):
        assert verdict_tone("低于四算黄金线，性价比区间") == "good"
        assert verdict_tone("处于四算~五算之间，正常水平") == "ok"
        assert verdict_tone("明显高于五算线，存在溢价，可等再版/补货") == "warn"
        assert verdict_tone("暂无现货观测数据，无法判断") == "na"
