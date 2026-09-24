"""身份与失败/跳过注入统一由 conftest 控制（环境变量），测试体保持纯净。"""


class TestNoise:
    def test_noise(self):
        assert True


class TestTouch:
    def test_touch(self):
        assert True


class TestAux:
    def test_aux(self):
        assert True
