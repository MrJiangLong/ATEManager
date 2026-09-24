"""自测工程 conftest —— 与 XApp testCase/conftest.py 的接入方式完全一致，
仅把"仪器 *IDN? 读身份"替换为环境变量，机台编号可用 ATE_TEST_CLIENT 模拟不同物理机。

环境变量：
    ATE_TEST_SN / ATE_TEST_MODEL / ATE_TEST_FW   被测件身份（默认 CSELFTEST01 / DPO4054B / V3.20）
    ATE_TEST_CLIENT                              机台编号（默认电脑名）
    ATE_FAIL=nodeid子串[,子串...]                 命中的用例判 FAIL（验证必测失败不出站）
    ATE_SKIP=nodeid子串[,子串...]                 命中的用例 SKIP（验证漏测拦截）
"""
import os
import time

import pytest

import api as ate

_ate_run = None

SN = os.environ.get("ATE_TEST_SN", "CSELFTEST01")
MODEL = os.environ.get("ATE_TEST_MODEL", "DPO4054B")
FW = os.environ.get("ATE_TEST_FW", "V3.20")
ate.CLIENT_ID = os.environ.get("ATE_TEST_CLIENT", ate.CLIENT_ID)  # 模拟不同物理机台

_FAIL = set(filter(None, os.environ.get("ATE_FAIL", "").split(",")))
_SKIP = set(filter(None, os.environ.get("ATE_SKIP", "").split(",")))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """收集每个用例的结果（XApp 工程里这一步在现有 makereport 钩子内调用 collect）。"""
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
def ate_session(request):
    global _ate_run
    run = ate.AteRun(ate.create_client())   # ATE_URL 留空时 client=None，全部方法自动旁路
    try:
        done = run.start(request.session.items, sn=SN, model=MODEL, fw=FW)
    except ate.AteExit as e:
        pytest.exit(e.message, returncode=e.returncode)
    for item in request.session.items:      # 续测：上次已 PASS 的本轮跳过
        if item.nodeid in done:
            item.add_marker(pytest.mark.skip(reason="ATE 续测：该用例已通过，跳过"))
    _ate_run = run
    yield
    try:
        run.finish()
    except ate.AteExit as e:
        pytest.exit(e.message, returncode=e.returncode)


@pytest.fixture(scope="function", autouse=True)
def ate_case_gate():
    """每个用例收尾：上报断点 + 失锁自检。"""
    yield
    if _ate_run is not None:
        try:
            _ate_run.gate()
        except ate.AteExit as e:
            pytest.exit(e.message, returncode=e.returncode)
