"""比价结论与格式化测试。"""

from core.advisor import availability_label, format_cny, format_jpy, price_verdict


class TestPriceVerdict:
    def test_below_conv4(self):
        assert "低于四算" in price_verdict(150, 168, 210)

    def test_between(self):
        assert "四算~五算之间" in price_verdict(200, 168, 210)

    def test_above_conv5(self):
        assert "溢价" in price_verdict(260, 168, 210)

    def test_boundary_conv4(self):
        assert "低于四算" in price_verdict(168, 168, 210)

    def test_boundary_conv5(self):
        assert "四算~五算" in price_verdict(210, 168, 210)

    def test_missing_reference(self):
        assert "参考线" in price_verdict(100, 0, 0)

    def test_no_spot(self):
        assert "暂无现货" in price_verdict(None, 168, 210)


class TestFormats:
    def test_format_cny(self):
        assert format_cny(198.0) == "¥198"
        assert format_cny(None) == "未知"

    def test_format_jpy(self):
        assert format_jpy(4950) == "4,950 日元"
        assert format_jpy("") == "未知"

    def test_availability(self):
        assert availability_label("A") == "有现货"
        assert availability_label("X") == "现货状态未知"
        assert availability_label(None) == "现货状态未知"
