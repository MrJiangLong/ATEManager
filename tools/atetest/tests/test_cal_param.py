"""5 个用例的 nodeid 与服务端 station_items 精确一致：
    tests/test_cal_param.py::TestAmp::test_amp_cal 等
环境变量 ATE_FAIL=逗号分隔用例尾名 → 对应用例判 FAIL（验证必测失败不出站/续测重跑）。"""
import os

FAIL = set(filter(None, os.environ.get("ATE_FAIL", "").split(",")))


class TestAmp:
    def test_amp_cal(self):
        assert "test_amp_cal" not in FAIL


class TestPhase:
    def test_phase_cal(self):
        assert "test_phase_cal" not in FAIL


class TestAmpDc:
    def test_amp_dc_1m(self):
        assert "test_amp_dc_1m" not in FAIL


class TestBandwidth:
    def test_bw_hi_z(self):
        assert "test_bw_hi_z" not in FAIL


class TestFastEdge:
    def test_fast_edge_1m(self):
        assert "test_fast_edge_1m" not in FAIL


class TestOptional:
    """选做用例（服务端 is_mandatory=False）：FAIL 不判停，只记录。"""
    def test_optional(self):
        assert True
