"""自测工程 conftest —— 与 XApp testCase/conftest.py 的接入方式完全一致，
仅把"仪器 *IDN? 读身份"替换为环境变量，机台编号可用 ATE_TEST_CLIENT 模拟不同物理机。

两种进站模式（由 ATE_TEST_SN 是否置空决定）：
    普通工位：ATE_TEST_SN=仪器/环境读到的 SN   → 收集结束立即进站（全自动）
    写 SN 工位：ATE_TEST_SN=（置空）           → 延迟进站：前置用例先离线自检，
        写 SN 用例通过 sn_burner 夹具扫码触发 check_in（防呆前置：服务端拦截
        发生在烧录硬件之前），进站后前置用例结果批量补报，全程入台账。

环境变量：
    ATE_TEST_SN / ATE_TEST_MODEL / ATE_TEST_FW   被测件身份（写 SN 工位置空 ATE_TEST_SN）
    ATE_TEST_CLIENT                              机台编号（默认电脑名）
    ATE_FAIL=nodeid子串[,子串...]                 命中的用例判 FAIL（验证必测失败不出站）
    ATE_SKIP=nodeid子串[,子串...]                 命中的用例 SKIP（验证漏测拦截）
    ATE_ARTIFACTS=1                              每个用例上传一个演示产物文件（验证产物归档链路；
                                                 真实工程替换为仪器截图/波形数据，见 ate_case_gate）
"""
import os
import time

import pytest

import api as ate

_ate_run = None

SN = os.environ.get("ATE_TEST_SN", "")      # 置空 = 延迟进站（写 SN 工位）
MODEL = os.environ.get("ATE_TEST_MODEL", "DPO4054B")
FW = os.environ.get("ATE_TEST_FW", "V3.20")
ate.CLIENT_ID = os.environ.get("ATE_TEST_CLIENT", ate.CLIENT_ID)  # 模拟不同物理机台

_FAIL = set(filter(None, os.environ.get("ATE_FAIL", "").split(",")))
_SKIP = set(filter(None, os.environ.get("ATE_SKIP", "").split(",")))


def _checked_in(run) -> bool:
    """是否已完成进站：以 client.state 为准。run.sn 在 check_in 之前就已赋值，
    进站被拦截时仍非空，据此出站会反复撞"check_out before check_in"。"""
    return bool(run is not None and run.client is not None and run.client.state is not None)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    """普通工位：收集完成即进站，并给上轮已 PASS 的用例打 skip。

    必须早于任何用例的 setup（会话夹具里打标记会漏掉第一个用例）；
    写 SN 工位（SN 置空）维持延迟进站，由 sn_burner 扫码触发。
    """
    global _ate_run
    if not items or config.option.collectonly:
        return
    _ate_run = ate.AteRun(ate.create_client())
    if not SN.strip():
        return
    try:
        done = _ate_run.start(items, sn=SN, model=MODEL, fw=FW)
    except ate.AteExit as e:
        pytest.exit(e.message, returncode=e.returncode)
    """续测：上次已 PASS 的本轮跳过"""
    for item in items:
        if item.nodeid in done:
            item.add_marker(pytest.mark.skip(reason="ATE 续测：该用例已通过，跳过"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """收集每个用例的结果（XApp 工程里这一步在现有 makereport 钩子内调用 collect）。
    未进站阶段照常收集：进站后由首次 gate() 批量补报。"""
    outcome = yield
    if _ate_run is not None:
        _ate_run.collect(item, outcome.get_result())


@pytest.fixture(autouse=True)
def ate_inject(request):
    """失败/跳过注入：命中 ATE_FAIL 判 FAIL（setup 失败，验证异常用例不消失）；
    命中 ATE_SKIP 跳过（验证出站漏测拦截）。ATE_SLOW=秒，每用例延时（制造
    "跑到一半被杀"的崩溃续测窗口）。"""
    if any(f in request.node.nodeid for f in _FAIL):
        pytest.fail(f"ATE_TEST_FAIL 注入失败: {request.node.nodeid}")
    if any(s in request.node.nodeid for s in _SKIP):
        pytest.skip(f"ATE_TEST_SKIP 注入跳过: {request.node.nodeid}")
    slow = float(os.environ.get("ATE_SLOW", "0") or 0)
    if slow > 0:
        time.sleep(slow)

@pytest.fixture(scope="session", autouse=True)
def project_session_start(request):
    """会话收尾：仅已进站的会话出站（未进站 = 服务器零痕迹，无需出站）。
    进站在 pytest_collection_modifyitems 完成，此处负责 yield 后的出站。"""
    yield
    if _checked_in(_ate_run):
        try:
            _ate_run.finish()
        except ate.AteExit as e:
            pytest.exit(e.message, returncode=e.returncode)


@pytest.fixture(scope="function", autouse=True)
def ate_case_gate(request):
    """每个用例收尾守卫：未进站阶段只累积结果不上报（checkpoint 由进站后首次
    收尾批量补报）；产物上传同样仅进站后生效。"""
    yield
    if _ate_run is not None and _checked_in(_ate_run):
        try:
            if os.environ.get("ATE_ARTIFACTS") == "1":
                short = request.node.nodeid.split("::")[-1]
                _ate_run.upload_artifact(
                    request.node.nodeid,
                    content=f"selftest artifact | {request.node.nodeid}".encode("utf-8"),
                    filename=f"artifact_{short}.log",
                )
            _ate_run.gate()
        except ate.AteExit as e:
            pytest.exit(e.message, returncode=e.returncode)


@pytest.fixture
def sn_burner(request):
    """专供【写 SN 用例】调用的夹具：前置失败检查 → 扫码 → 防呆进站 → 返回 SN。

    用法：
        def test_burn_sn(sn_burner):
            sn = sn_burner()            # 扫码 + check_in（服务端拦截在此处中止，绝不烧录）
            hardware.write(sn)          # 进站成功后才执行硬件烧录
            assert hardware.readback() == sn

    防呆规则：
        1. 此前任一用例 FAIL → 拒绝扫码（前置自检未通过不消耗合法 SN）
        2. check_in 的服务端拦截（重号/跳站/极性/基线/锁）发生在烧录之前
        3. 进站后前置用例结果批量补报，写号动作全程入台账
    """
    def _scan_and_checkin(scan_func=None) -> str:
        bad = [nid for nid, res in getattr(_ate_run, "results", {}).items() if res == "failed"]
        if bad:
            pytest.fail(f"前置自检未通过，禁止扫码写号：{bad}")

        scanned_sn = (scan_func() if scan_func else input("\n[请扫描产品SN条码]: ")).strip()
        if not scanned_sn:
            pytest.fail("扫码失败：SN 不能为空")

        try:
            _ate_run.start(request.session.items, sn=scanned_sn, model=MODEL, fw=FW)
        except ate.AteExit as e:
            pytest.exit(f"防呆拦截（禁止烧录）: {e.message}", returncode=e.returncode)

        return scanned_sn

    return _scan_and_checkin
