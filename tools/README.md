# tools/ 目录索引

运维与验证工具，按业务域分组。每个子目录的详细用法见其内部文件与 doc 文档。

## 目录结构

| 目录 / 文件 | 用途 | 给谁用 |
|---|---|---|
| `client/` | **上位机接入域** | 产线上位机开发者 |
| `atetest/` | **契约自测工程** | SDK / 服务端联调验证 |
| `cases/` | **用例清单域** | 工艺 / 测试项维护 |
| `reports/` | **出厂报告域** | 报告功能维护与插件开发 |

### client/ —— 上位机接入

| 文件 | 说明 |
|---|---|
| `ate_client.py` | SDK 参考实现 + CLI 演示（demo/normal/gate/resume/takeover/sweep/resolve），仅标准库。**产线工程拷贝这份**作为接入模块 |
| `line_simulator.py` | 多机台真实并发模拟器：锁竞争 / 防呆拦截 / 崩溃续测 / 失联接管 / 孤儿锁回收 / 混沌注入（--fail-rate 等） |
| `sim_local.py` | 一键仿真环境：自建临时库 + 临时后端 + 跑模拟器 + 收尾清理（`--attach` 只对运行中后端）。入口：`scripts\sim-local.bat` |

### atetest/ —— 契约自测工程

pytest 全场景仿真（沙箱流程 PROC-SELFTEST-DPO-BASE，与真实工艺配置隔离）：
多机台四站流转、防复测 / 跳站 / 漏测 / 机型未注册 / 固件防呆 / 失败重跑闭环 /
选做 FAIL 不判停 / 进程崩溃续测 / 锁冲突。

- `api.py`：SDK 的**产线形态**（conftest 直接 `import api`；服务端地址可用环境变量 `ATE_BASE_URL` 覆盖）
- `conftest.py`：与真实产线 XApp 工程接入方式完全一致（`start / collect / gate / finish` 四钩子）
- `run_scenarios.py`：场景 runner（pytest 子进程 + 退出码断言 + 服务端对账）
- `setup_line.py`：幂等搭建沙箱（工位 / 流程 / 用例 / 机台）

```powershell
# 本地服务端跑全套场景
$env:ATE_BASE_URL = "http://127.0.0.1:8000"
python setup_line.py
python run_scenarios.py
```

### cases/ —— 用例清单

| 文件 | 说明 |
|---|---|
| `sync_cases.py` | 用例 ID 全量同步到服务端（JSON 输入 + 中止会话 + 沉降等待） |
| `cases.example.json` | 输入模板 |

### reports/ —— 出厂报告

| 条目 | 说明 |
|---|---|
| `report_test.py` | 报告引擎端到端验证（候选入队 → 生成 → PDF → 归档） |
| `pdf_worker/` | PDF 外部转换 Worker（部署在装有 WPS/Office 的 Windows 测试机），打包见其 `build_exe.md` |
| `plugin/` | 插件开发包（对外发放）：tek_mso 参考实现 + 本地台架 harness + 取数引擎镜像，开发指南见其 `插件开发规则.md` |

## SDK 三副本同步约定（重要）

同一接入契约目前存在三份实现，**以 `client/ate_client.py` 为契约参考基准**：

| 副本 | 定位 |
|---|---|
| `client/ate_client.py` | 契约参考实现（含 SCPI `*IDN?` 身份解析、CLI 演示） |
| `atetest/api.py` | 产线形态（纯类接口，pytest conftest 直接 import） |
| 产线 XApp 工程 | 生产使用（从本目录拷贝） |

改动接入逻辑时：先改 `client/ate_client.py`（契约与演示），再同步 `atetest/api.py` 与产线工程，
三处保持行为一致；改完用 `atetest/run_scenarios.py` 全量回归。

## 与各域配套的文档

- 上位机接入契约：[`doc/API.md`](../doc/API.md)
- 插件开发：[`reports/plugin/插件开发规则.md`](reports/plugin/插件开发规则.md)
- Worker 打包部署：[`reports/pdf_worker/build_exe.md`](reports/pdf_worker/build_exe.md)
