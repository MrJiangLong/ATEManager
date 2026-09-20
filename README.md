# ATE 防呆测试管理系统

面向 TEK 示波器量产场景的**防呆（Poka-Yoke）测试管理系统**：以「用例ID」为数字凭据，
把**防漏测**与**防跳工位**两条硬约束下沉到服务端，让上位机 pytest 无法绕过。

> **核心思路**
> Web 只建静态规则，以专属工艺路线（Process）解耦硬件构型差异；
> pytest 通过底层指令（SCPI `*IDN?`）直读硬件 SN 与型号，首工位动态建档；
> 以用例ID驱动防漏测与防跳工位。

| 组件 | 版本 | 说明 |
|---|---|---|
| 后端 API | `1.0.0` | `backend/app/config.py::APP_VERSION` |
| 上位机接口文档 | `v1.0` | `doc/API.md` |
| 前端 | `1.0.0` | `frontend/package.json` |

---

## 目录

- [1. 项目简介](#1-项目简介)
- [2. 核心特性](#2-核心特性)
- [3. 系统架构](#3-系统架构)
- [4. 技术栈](#4-技术栈)
- [5. 快速开始](#5-快速开始)
- [6. 目录结构](#6-目录结构)
- [7. 数据模型](#7-数据模型)
- [8. 防呆契约](#8-防呆契约)
- [9. 接口一览](#9-接口一览)
- [10. 上位机接入](#10-上位机接入)
- [11. 配置说明](#11-配置说明)
- [12. 维修处置](#12-维修处置)
- [13. 测试](#13-测试)
- [14. 部署](#14-部署)
- [15. 生产安全建议](#15-生产安全建议)
- [16. 故障排查](#16-故障排查)

---

## 1. 项目简介

### 1.1 两大核心需求

| 需求 | 目标 | 实现要点 |
|---|---|---|
| **需求 1：完成上传闭环** | 出站必须拿到服务端落库回执（ACK），否则上位机亮红灯死锁，严禁拔线流转 | `check_out` 返回 201 + `acknowledged=true`；`checkout_id` 幂等，断网重传不产生重复账目 |
| **需求 2：跳站卡控** | 前置工位未完成，下一站进站立即 `403` 阻断，pytest 终止、治具不开电 | 出站后追加「工位印章」；进站时按拓扑比对 `depends_on` 与已盖章集合 |

### 1.2 三条设计原则

1. **静态规则与运行时彻底分离** —— Web 端只维护工艺/机型/工位/用例ID清单，运行期不做任何 `IF/ELSE` 构型判断。
2. **硬件构型用独立流程物理隔离** —— 带 AWG 与不带 AWG 是两条独立 `process`，不是同一流程的条件分支。
3. **无纸化、免扫码** —— 被测件身份全部由 `*IDN?` 直读，首工位动态建档，无人工投产环节。

---

## 2. 核心特性

- **用例ID 驱动防漏测**：进站比对本次待执行清单，出站校验必测项是否被真实执行（`SKIP` 等同未执行）
- **防跳工位闸门**：`depends_on` 拓扑依赖 + 已盖章工位集合双重判定
- **防复测**：已盖章工位严禁重测（`409 station_already_passed`）
- **固件基线校验**：机型绑定 `target_fw_version`，`fw_match_rule` 决定口径（`exact` 完全一致 / `min` 不低于基线，按数字段比较），不符 `403` 拦截
- **出站 ACK 闭环**：`checkout_id` 幂等，网络重传安全
- **租约锁**：心跳保活 + 双超时（失联 120s / 硬超时按工位配置）
- **崩溃续测**：断点增量上报，重启后 `attempt+1` 并跳过已完成用例
- **锁快速接管 + fencing**：失联锁可被同工位机台接管，旧 token 写入一律拒绝
- **孤儿锁自动回收**：后台 Sweeper 周期扫描，无人抢锁也会释放
- **维修处置**：RETEST / ROLLBACK / RESET / SCRAP，自动作废受影响记录
- **SN 全追溯**：事件台账 + 维修履历 + 会话时间线
- **SPC 良率看板**：产量/良率趋势、工位与流程良率、失效用例排行、机台在线
- **三角色权限**：viewer 只读 / operator 产线操作 / admin 配置与用户管理，后端逐端点收口

---

## 3. 系统架构

```
┌────────────────────────────────────────────────────────────────────┐
│ 1. Web 端：只维护静态工程规则（低频配置，无人工投产扫码）            │
│    · 机型专属工艺流程基线（带 AWG 走 6 站，无 AWG 走 4 站）          │
│    · 工步拓扑 depends_on 依赖（无 AWG 流程从物理拓扑上直接剔除）     │
│    · 每个工位必须执行的用例ID（Case ID）清单                         │
└───────────────────────────────┬────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│ 2. 上位机 Pytest：纯底层硬件驱动（无纸化、免扫码枪）                 │
│    · 示波器接入治具开机，通过 SCPI `*IDN?` 直读厂商/型号/SN/固件      │
│    · 首工位初次通电即触发「动态自动建档」，零手工介入                 │
└───────────────────────────────┬────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│ 3. 用例ID驱动防漏测与防跳工位                                        │
│    · 防漏测：进站前比对待执行用例ID与服务端静态规则                   │
│    · 防跳工位：出站落库后追加「工位印章」，下一站比对印章库            │
└───────────────────────────────┬────────────────────────────────────┘
                                ▼
┌────────────────────────────────────────────────────────────────────┐
│ 4. 落地两大核心需求                                                  │
│    · 需求 1：出站必须拿到 ACK，否则红灯死锁、禁止拔线                 │
│    · 需求 2：前置未完成，进站 403 阻断、pytest 终止、治具不开电        │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 分层依赖

```
routers  ──▶  services  ──▶  models
 参数校验       业务规则        数据模型
 响应封装     （唯一事实来源）
```

路由层只做参数校验与响应封装，**业务规则全部集中在 `services/`**：

| 模块 | 职责 |
|---|---|
| `services/routing.py` | 静态拓扑：流程装载、依赖闸门、用例ID清单、DAG 校验 |
| `services/gate.py` | 运行时状态机：进站 / 保活 / 断点 / 出站 ACK / 锁释放 / 维修处置 |
| `services/firmware.py` | 固件基线匹配（`exact` 精确 / `min` 不低于基线），进站卡控与展示共用 |
| `services/sweeper.py` | 孤儿锁回收后台任务（失联 / 硬超时） |
| `services/metrics.py` | 仪表盘聚合（流程缓存 / 在制品 / 日趋势 / TOP 失效 / 窗口良率） |
| `services/views.py` | ORM 实体 → 展示模型的派生与组装 |
| `services/timeutil.py` | UTC 时间语义统一 + 统计日界（本地自然日切分） |

### 3.2 双通道鉴权与三角色

| 通道 | 前缀 | 鉴权 | 使用者 |
|---|---|---|---|
| 通道一 | `/api/v1/*` | `X-API-Key` | 产线上位机（pytest） |
| 通道二 | `/api/admin/*` | JWT Bearer | Web 管理端 |
| 鉴权 | `/api/auth/*` | 账密换 token | 登录 |

通道二按**三角色**收口（`users.role`，JWT 携带，逐端点校验）：

| 角色 | 权限 |
|---|---|
| `viewer` | 只读全部：运营总览、在制/台账/追溯、工艺配置查看 |
| `operator` | viewer + 机台注册/绑定/注销、维修处置、强制解锁、中止会话 |
| `admin` | operator + 工艺/主数据配置（流程/工位/机型/用例清单）、流程导入导出克隆、用户管理 |

防自锁守卫：系统中永远保证至少一个启用中的 admin——删除/停用/降权最后一个有效 admin 一律 `409 last_admin`，不能删除自己的账号。被停用账号登录拒绝且已有 token 立即失效。

> 服务端不做多语言协商，错误信息一律为英文 `code: message`，前端按 `code` 自行翻译。

### 3.3 硬件构型如何解耦

**不使用运行时 `IF/ELSE`**，而是为每种构型建立**独立的工艺流程**：

| 流程 | 机型 | 工步数 | 说明 |
|---|---|---|---|
| `PROC-SCOPE-MSO-AWG` | `MSO4054B` | 6 | 带 AWG 选件，含 `CAL-AWG` / `TST-AWG` |
| `PROC-SCOPE-DPO-BASE` | `DPO4054B` | 4 | 无 AWG 标准流程，物理剔除所有 AWG 工步 |

机型只需绑定 `process_id`，上位机按用例ID清单执行，服务端按拓扑判定 —— 运行期零分支。

---

## 4. 技术栈

### 后端

| 组件 | 版本 | 用途 |
|---|---|---|
| Python | 3.8+（Docker 镜像 3.11） | 运行时 |
| FastAPI | ≥ 0.110 | Web 框架与 OpenAPI |
| SQLAlchemy | ≥ 2.0 | ORM（PG 数组/JSONB、SQLite 降级） |
| Pydantic | ≥ 2.6 | 请求校验与响应契约 |
| pg8000 | ≥ 1.30 | PostgreSQL 纯 Python 驱动 |
| PyJWT | ≥ 2.6 | 通道二鉴权 |
| Uvicorn | ≥ 0.29 | ASGI 服务器 |

### 前端

| 组件 | 版本 |
|---|---|
| Vue | 3.4 |
| Element Plus | 2.7 |
| ECharts | 5.5 |
| vue-router | 4.3 |
| vue-i18n | 9.14（zh-CN / en-US） |
| Vite | 5.2 |
| Axios | 1.7 |

---

## 5. 快速开始

### 5.1 前置条件

- Python 3.8+（Windows 一键脚本会自动创建 `.venv`）
- Node.js 18+（仅前端开发/构建需要）
- PostgreSQL 13+（生产）；本地开发可直接使用 SQLite

### 5.2 一键脚本（Windows）

```bat
scripts\setup.bat           :: 创建 venv、安装依赖、初始化数据
scripts\dev-backend.bat     :: 后端 http://localhost:8000 （Swagger /docs）
scripts\dev-frontend.bat    :: 前端 http://localhost:5173
scripts\seed.bat            :: 补跑种子数据（幂等）
scripts\test-backend.bat    :: 运行端到端回归
```

默认管理员：`admin` / `admin123`（仅在用户表为空时自动创建，可在 `.env` 中修改）。

### 5.3 手动启动

```bash
# 后端
cd backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env          # 按需修改 DATABASE_URL / V1_API_KEY / JWT_SECRET
python -m app.seed              # 静态规则 + 随机数据（幂等）
uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev                     # http://localhost:5173
npm run build                   # 产物输出到 frontend/dist（Docker 镜像会用到）
```

### 5.4 验证

| 检查项 | 方法 |
|---|---|
| 服务存活 | `GET http://localhost:8000/api/health` → `{"status":"ok"}` |
| 接口文档 | 浏览器打开 `http://localhost:8000/docs` |
| 数据就绪 | 登录 Web 端，运营总览应有在制品与趋势数据 |
| 契约回归 | 执行 `scripts\test-backend.bat`，40 项全部通过 |
| 上位机链路 | `python tools/ate_client.py --api-key <V1_API_KEY> demo` |

### 5.5 数据初始化

```bash
python -m app.seed                        # 幂等：已有规则则跳过
python -m app.seed --reset                # 清空业务表与工艺配置后重建（保留 users）
python -m app.seed --reset --products 80  # 指定随机在制品数量
python -m app.seed --reset --no-repairs   # 不生成维修履历
python -m app.seed --scenarios            # 仅重建租约锁/会话场景数据（C0990xx）
python -m app.seed --clear-business       # 只清业务数据，保留工艺配置
python -m app.seed --backfill-completed   # 回填 is_completed 冗余列
```

预置静态规则：

- 2 条流程（MSO 6 站 / DPO 4 站）、6 个工位、2 个机型、**10 台机台**（含 4 台备用/演示台，部分预置 `app_version`）
- 用例ID测试项 **37 条**（MSO 21 条 + DPO 16 条，如 `tests/test_cal_param.py::TestAmp::test_amp_cal`）
- 随机数据：60 台在制品（含在制/锁定/报废/固件非基线场景）及其完整事件台账 + 5 条维修履历
- **8 组租约锁/会话场景数据**（SN 前缀 `C0990xx`）：正常持锁 / 僵尸锁可接管 / 崩溃续测中（attempt=2）/
  硬超时 / 续测成功 / 被备用机台接管 / 连续失联计失败 / 超时终止

> 失联锁与硬超时锁会被后台回收任务自动释放；若需常驻可见，请以 `SWEEPER_ENABLED=false` 启动后端。

---

## 6. 目录结构

```
ATEManager/
├── backend/
│   ├── app/
│   │   ├── main.py           入口：路由装配、schema 初始化、托管前端产物、SPA 回退
│   │   ├── config.py         集中配置（环境变量 → Settings 单例）
│   │   ├── database.py       引擎 / 会话 / 建表
│   │   ├── models.py         10 张业务表 + users（用例ID：列名 nodeid → 属性 case_id）
│   │   ├── schemas.py        API 契约（In / Out）
│   │   ├── security.py       双通道鉴权：JWT + X-API-Key
│   │   ├── errors.py         业务异常 → 统一响应包
│   │   ├── logging.py        控制台 + 滚动文件日志
│   │   ├── seed.py           静态工艺规则 + 随机在制品 + 租约锁/会话场景数据
│   │   ├── services/
│   │   │   ├── routing.py    静态拓扑：流程装载、依赖闸门、用例ID清单、DAG 校验
│   │   │   ├── gate.py       运行时状态机：进站 / 保活 / 断点 / 出站 ACK / 锁释放 / 维修
│   │   │   ├── firmware.py   固件基线匹配（exact / min 口径）
│   │   │   ├── sweeper.py    孤儿锁回收后台任务（失联 / 硬超时）
│   │   │   ├── metrics.py    仪表盘聚合（在制品 / 日趋势 / 窗口良率 / TOP 失效）
│   │   │   ├── views.py      ORM → 展示模型的派生与组装
│   │   │   └── timeutil.py   UTC 时间语义、"距某时刻多久"、统计日界
│   │   └── routers/
│   │       ├── auth.py       登录 / 当前用户 / 改密
│   │       ├── v1.py         通道一：resolve / check-in / heartbeat / checkpoint
│   │       │                            / check-out / release / ack
│   │       ├── masters.py    主数据：流程 / 机型 / 工位
│   │       ├── routing.py    拓扑编排 / 用例ID测试项 / 校验 / 克隆
│   │       ├── products.py   在制品清单 / 详情 / 维修处置 / 强制解锁
│   │       ├── records.py    事件台账 / SN 全生命周期追溯
│   │       ├── repairs.py    维修处置履历
│   │       ├── sessions.py   测试会话：续测断点 / 僵尸锁 / 强制终止
│   │       ├── clients.py    机台档案与工位绑定
│   │       └── metrics.py    仪表盘统计
│   └── tests/test_backend.py 端到端回归测试（40 项）
├── tools/                    运维与验证脚本（Python，仅标准库）
│   ├── line_simulator.py     多机台并发模拟器（锁竞争 / 崩溃续测 / 失联接管）
│   ├── sim_local.py          本地测试库一键仿真（--attach 只对运行中后端）
│   ├── sync_cases.py         用例ID全量同步（JSON + 中止会话 + 沉降等待）
│   ├── cases.example.json    用例清单 JSON 模板（sync_cases 的输入示例）
│   └── ate_client.py         SDK 参考实现 + 演示脚本（仅标准库，非产线执行器）
├── doc/                      上位机接入文档与参考实现
│   └── API.md                上位机接口文档（唯一契约依据）
│
├── frontend/
│   └── src/
│       ├── api/              Axios 封装 + 按域划分的 API 模块
│       ├── components/       DataCard / PageToolbar / StatTile / EmptyState
│       │                     YieldTable / RepairDialog / ProductDrawer / SessionDrawer
│       │                     TopFailedList / AppLogoMark
│       ├── composables/      useProcesses（工艺字典缓存）/ usePolling（静默轮询）
│       ├── layout/           Layout / AppHeader / AppSidebar / ChangePasswordDialog
│       ├── locales/          zh-CN / en-US（i18n 词条）
│       ├── router/           vue-router 路由表与鉴权守卫
│       ├── stores/auth.js    会话与鉴权状态（localStorage 持久化）
│       ├── styles/           全局样式与 CSS 变量
│       ├── utils/            展示格式化 / 路由标题 / 常量
│       ├── i18n/             vue-i18n 实例与语言探测
│       └── views/
│           ├── Dashboard.vue    运营总览（KPI + 产量与良率趋势）
│           ├── Products.vue     在制管理
│           ├── Trace.vue        SN 全生命周期追溯
│           ├── Records.vue      事件台账
│           ├── Repairs.vue      维修处置
│           ├── Sessions.vue     测试会话（断点 / 僵尸锁）
│           ├── Clients.vue      机台管理
│           ├── Login.vue        登录
│           ├── RouteConfig.vue  工艺配置入口
│           └── route-config/    RouteProcesses / RouteModels / RouteStations
│                                 RouteTopology / RouteItems
├── scripts/                  Windows 启动器（.bat）：setup / dev-backend / dev-frontend
│                             / seed / test-backend / sim-local / sync-cases
├── docker-compose.yml        单容器部署配置（端口 8000）
├── Dockerfile                多阶段构建（前端 Vite → 后端同源托管）
└── README.md
```

---

## 7. 数据模型

**10 张业务表 + 1 张鉴权表**，严格按生产级 DDL 实现。

### 7.1 静态工艺与主数据（Web 低频维护）

| 表 | 主键 | 作用 |
|---|---|---|
| `processes` | `process_id` | 工艺流程主表，一个硬件构型一条；`is_active` 停用后不再接受新机型绑定 |
| `product_models` | `product_model` | 机型 → 专属流程 + 固件基线 `target_fw_version`，`fw_match_rule` 定匹配口径（`exact`/`min`） |
| `stations` | `station_id` | 逻辑工位字典，含 `timeout_sec` 硬超时时长 |
| `process_stations` | `(process_id, station_id)` | 工步拓扑：`step_order` 定序、`depends_on` 定闸门；整体覆盖式保存，保存前校验成环/缺工位等结构问题（不通过整体回滚，409 `topology_invalid`），`force=true` 可跳过 |
| `station_items` | `item_id` | 工位用例ID静态清单，`is_mandatory` 定必测；全量导入（replace）会停用清单外项，重新出现自动复活（`is_active`） |

> **用例ID（Case ID）命名**：DDL 中该列名为 `nodeid`，为保持表结构不变，
> ORM 层用 `mapped_column("nodeid")` 将属性名映射为 `case_id`。
> 即「物理列名 `nodeid` ↔ 代码/API/界面一律使用 `case_id`」。

### 7.2 编号规范（`process_id` / `station_id` / `client_id`）

三者都是**自然主键**，且会被 `test_records` / `test_sessions` / `product_status` 冗余引用。
规范目标：拿到一个编号就能判断「它是什么、属于哪条线、在哪一阶段」，同时保证在
URL、SCPI 指令、日志文件名里都无需转义。

**强制范围**：`process_id` / `station_id` 的格式在写入口强制校验；`client_id` 只要求
非空（长度 ≤ 64），格式规范为建议约定（见下文机台一节）。

#### 通用约束

| 项 | 规则 |
|---|---|
| 字符集 | 建议 `[A-Z0-9-]`，**全大写**；禁用空格、中文、下划线、点号、斜杠 |
| 分隔符 | 段与段之间用 `-`，段内不再分段 |
| 长度 | 建议 ≤ 32 字符（字段上限 64） |
| 稳定性 | **投产即冻结** —— 一旦产出过测试记录便不可改名，只能新建并停用旧的 |
| 当前校验 | `process_id` / `station_id`：写入口强制校验正则（不合规 422）；`client_id`：仅非空 |

#### 工艺流程 `process_id`：`PROC-<产品族>-<系列>-<构型>`

| 段 | 含义 | 取值建议 |
|---|---|---|
| `PROC` | 固定前缀 | — |
| 产品族 | 仪器大类 | `SCOPE` 示波器、`DMM` 万用表、`PSU` 电源、`AFG` 信号源 |
| 系列 | 厂商系列 / 型号族 | `MSO`、`DPO`、`3446` |
| 构型 | 硬件选件差异，**决定流程分支** | `BASE` 标准、`AWG` 带任意波、`16CH` 16 通道 |

`PROC-SCOPE-MSO-AWG`　示波器 MSO 系列 · 带 AWG
`PROC-SCOPE-DPO-BASE`　示波器 DPO 系列 · 标准
`PROC-DMM-3446-BASE`　　万用表 3446 系列 · 标准

- **构型不同必须用不同 `process_id`**：这是本项目「一个硬件构型一条独立流程」的落点，
  运行期不做任何 IF/ELSE 判断（见 [3.3 硬件构型如何解耦](#33-硬件构型如何解耦)）。
- **同一构型只保留一个生效流程**：工艺调整直接编辑该流程的工步拓扑，
  不靠新建 id 来表达版本。仅在新旧工艺过渡期临时使用 `-V2` 这类后缀。

#### 工位 `station_id`：`<阶段>-<测试域>[-<序号>]`

| 段 | 含义 | 取值建议 |
|---|---|---|
| 阶段 | 工艺阶段（2–4 字母） | `CAL` 校准、`TST` 测试、`FCT` 功能、`AGE` 老化、`SAF` 安规、`PACK` 包装、`REP` 维修 |
| 测试域 | 该站位测什么 | `PARAM` 指标、`IFACE` 接口、`AWG` 任意波、`BURNIN` 老化、`SAFETY` 安规、`FINAL` 终检 |
| 序号 | 同类站位有多套时区分（2 位） | `01`、`02`；单套时可省略 |

`CAL-PARAM`　　　　校准 · 指标（单站位，省略序号）
`CAL-PARAM-01`　　校准 · 指标 · 1 号位（并行多套）
`TST-IFACE`　　　 测试 · 接口
`AGE-BURNIN-01`　 老化 · 烧机 · 1 号位

- 工位是**逻辑**概念：**不含产线与物理位置**（那是机台的事），跨产线共享同一份字典。
- 工位**不体现先后顺序**：顺序由 `process_stations.step_order` 决定，调序改拓扑即可。

#### 机台 `client_id`：`<厂区>-<产线>-<阶段>-<序号>[-<用途>]`（建议，不做强制）

系统对 `client_id` **只要求非空**，不做格式校验（现场编号风格各异）。以下规范为
**建议约定**——遵循它可从编号直接读出"哪条线、哪个阶段"，便于肉眼核对绑定关系：

| 段 | 含义 | 取值建议 |
|---|---|---|
| 厂区 | 生产基地代码 | `SZ` 深圳、`CD` 成都 |
| 产线 | 线体编号 | `L1`、`L2` |
| 阶段 | 与所绑工位的阶段保持一致 | `CAL`、`TST`、`AGE` |
| 序号 | 线体内同阶段机台流水号（2 位） | `01`、`02` |
| 用途 | 可选，非标准用途 | `SPARE` 备用、`DEV` 调试 |

`SZ-L1-CAL-01`　　　　深圳 · 1 号线 · 校准 · 1 号台
`SZ-L1-CAL-01-SPARE`　同上，备用台
`CD-L2-TST-03`　　　　成都 · 2 号线 · 测试 · 3 号台

- 阶段段应与绑定的 `station_id` 阶段一致，便于肉眼核对绑定是否正确。
- **机台换工位不改 `client_id`**：改的是 `station_clients.station_id` 绑定关系。
- 机台报废或更换硬件时**注销旧的、注册新的**，不复用编号，保证历史台账可追溯。

#### 机型 `product_model`：不由本项目定义

它来自上位机 `*IDN?` 直读（如 `MSO4054B`），系统只做注册、**不做格式约束**：

- 必须与仪器返回值**逐字符一致**，否则进站判 `model_mismatch`；
- 同型号的不同固件基线仍用同一个 `product_model`，差异由
  `target_fw_version` + `fw_match_rule` 表达。

#### 强制校验

服务端在**写入口**强制校验格式，不合规直接 `422` 并返回期望格式：

| 字段 | 校验时机 | 正则 |
|---|---|---|
| `process_id` | `POST /api/admin/processes` | `^PROC-[A-Z0-9]+(-[A-Z0-9]+){2}$` |
| `station_id` | `POST /api/admin/stations` | `^[A-Z]{2,4}-[A-Z0-9]+(-[0-9]{2})?$` |
| `client_id` | `POST /api/admin/clients` | `^[A-Z]{2}-L[0-9]+-[A-Z]{2,4}-[0-9]{2}(-[A-Z]+)?$` |

**更新接口不做校验**：三者投产即冻结，原地改名会让历史台账失联，
要换编号只能「新建合规编号 → 停用旧的」。

`test_records` / `test_sessions` 里的 `station_id` / `client_id` 是**冗余快照而非外键**，
历史记录始终显示当时的值 —— 这是刻意设计（保证历史可追溯），迁移时**不要**去改写历史。

### 7.3 运行时数据（上位机高频读写）

| 表 | 主键 | 作用 |
|---|---|---|
| `station_clients` | `client_id` | 物理工控机档案，绑定逻辑工位（允许未绑定态存在，显式传空即解绑）；`app_version` 留档上位机程序版本，`created_at` 留档建档时间 |
| `product_status` | `sn` | 在制品状态机，`passed_stations` 为已盖章工位集合 |
| `test_records` | `record_id` | 事件底账，`executed_items` JSONB 为执行快照 |
| `repair_records` | `repair_id` | 维修处置与回滚履历 |
| `test_sessions` | `session_id` | 测试会话，承载续测断点（崩溃续测的核心载体） |

> `users` 表为 Web 管理端鉴权所需的基础设施，不属于业务表。

### 7.4 关键字段语义

| 字段 | 说明 |
|---|---|
| `product_status.passed_stations` | 已盖章工位集合。**唯一写入口是 `gate._write_passed()`**，改它才能同步维护冗余列 |
| `product_status.is_completed` | 冗余派生列：流程全工步是否已盖齐。用于列表页在 SQL 层精确过滤与分页 |
| `test_records.executed_items` | 执行快照 JSONB，内部携带 `checkout_id`（幂等键）、`session_id`、`attempt` |
| `test_records.is_valid` | 维修处置作废标记；作废记录不参与追溯结论 |
| `test_sessions.checkpoint` | 续测断点（按 `case_id` 去重覆盖）与客户端 `cursor` |
| `product_models.fw_match_rule` | 固件基线口径：`exact` 完全一致（默认）；`min` 不低于基线，按数字段比较（`V3.9 < V3.20`） |
| `station_clients.app_version` | 上位机程序版本，身份上报与进站时刷新，用于排查版本漂移 |
| `processes.is_active` | 停用只作用于管理端（不再接受新机型绑定），运行期已绑定机型的在制品照常流转 |

### 7.5 租约锁模型（v1.0）

```
持锁    = current_status == 'TESTING' AND current_client == <client_id> AND lock_token
失联    = now - lock_last_seen_at  > LOCK_HEARTBEAT_GRACE_SEC  → 可被接管（不计失败）
硬超时  = now - lock_acquired_at   > stations.timeout_sec      → 计一次失败
释放    = 出站 / 主动释放 / 强制解锁 / 回收任务
```

关键区分：**心跳只刷新 `lock_last_seen_at`，不篡改 `lock_acquired_at`**，
因此长测试可跑满 30min 不被接管，而崩溃机台 120s 内即可释放锁。

**防脏写（fencing）**：每次进站/接管换发新 `lock_token`；旧持锁方持旧 token
出站/上报断点一律 `403 lock_invalid`，避免已接管会话被僵尸机台覆盖。

出站幂等的 `checkout_id` 存放于 `test_records.executed_items` JSONB 内部，
既不改动表结构，又能保证断网重传不产生重复账目。

---

## 8. 防呆契约

上位机以 **HTTP 状态码** 为第一分流依据，响应体 `exit_code` 供既有产线脚本做数值分支。

| 场景 | HTTP | `code` | `exit_code` | 上位机动作 |
|---|---|---|---|---|
| 进站放行 | 200 | `ok.checkin` | 0 | 治具上电，开始执行 |
| **跳站拦截（需求 2）** | **403** | `missing_prereq` | 10 | 弹告警、pytest 终止、治具不开电 |
| 固件不符 | 403 | `firmware_mismatch` | 10 | 提示刷写基线固件 |
| 机型不符 / 未注册 | 403 | `model_mismatch` / `model_not_registered` | 10 | 检查贴标与工艺配置 |
| 已工程锁定 | 403 | `product_locked` | 16 | 提示送修 |
| 已报废 | 403 | `product_scrapped` | 10 | 禁止流转 |
| 锁失效 / 超时 / 被接管 | 403 | `lock_invalid` / `lock_expired` | 14 | 停机，重新进站 |
| **复测拦截** | **409** | `station_already_passed` | 10 | 提示推错车，禁止复测 |
| 锁冲突 | 409 | `lock_conflict` | 11 | 等待或换机台 |
| 工位不在流程 | 400 | `station_not_in_process` | 10 | 提示工艺不符 |
| 用例ID清单不匹配 | 400 | `case_id_mismatch` | 15 | 提示脚本版本不符 |
| **漏测拦截** | **400** | `missing_mandatory` | 13 | 提示补齐必测用例 |
| 出站成功 | 201 | `ok.checkout` | 0（PASS）/ 1（FAIL） | 收到 ACK 方可拔线 |

统一响应包：

```json
{ "ok": false, "exit_code": 10, "code": "missing_prereq",
  "message": "missing_prereq: C020001 must finish CAL-IFACE before TST-PARAM",
  "data": { "missing": ["CAL-IFACE"] } }
```

> 完整错误码表见 `doc/API.md` 第 6 章。

---

## 9. 接口一览

### 9.1 通道一 `/api/v1/*`（X-API-Key）

| 方法 | 路径 | 说明 | 成功码 |
|---|---|---|---|
| POST | `/client/resolve` | 机台身份上报与工位反查（首次自动注册） | 200 |
| POST | `/client/check-in` | 进站：防跳站/防复测/固件/用例ID 卡控 + 领取工位锁 | 200 |
| POST | `/client/heartbeat` | 心跳保活（只刷新失联时间，不续硬超时） | 200 |
| POST | `/client/checkpoint` | 续测断点：增量上报已完成用例（幂等覆盖） | 200 |
| POST | `/client/check-out` | 出站：断点补齐 + 漏测拦截 + 落库返回 ACK | **201** |
| POST | `/client/release` | 主动放弃工位锁（不计失败） | 200 |
| GET | `/client/ack` | 上传闭环校验：确认服务端已落库 | 200 |

### 9.2 通道二 `/api/admin/*` 与 `/api/auth/*`（JWT）

最低角色列：`view` = 登录即可读（三角色均可），`op` = operator 及以上，`admin` = 仅管理员。写端点角色不足返回 `403 permission_denied`。

| 分组 | 最低角色 | 接口 |
|---|---|---|
| 鉴权 | view | `POST /api/auth/login`、`GET /api/auth/me`、`POST /api/auth/change-password` |
| 工艺流程 | view / admin 写 | `GET/POST /api/admin/processes`、`PUT/DELETE /api/admin/processes/{id}` |
| 机型 | view / admin 写 | `GET/POST /api/admin/product-models`、`PUT/DELETE /api/admin/product-models/{id}` |
| 工位 | view / admin 写 | `GET/POST /api/admin/stations`、`PUT/DELETE /api/admin/stations/{id}` |
| 工艺拓扑 | view / admin 写 | `GET /api/admin/routing/topology`、`GET/PUT/DELETE /api/admin/routing/stations[/{station_id}]`、`GET/POST/PUT/DELETE /api/admin/routing/items[/{item_id}]`、`GET /api/admin/routing/validate`、`POST /api/admin/routing/clone`、`POST /api/admin/routing/processes/import`、`GET /api/admin/routing/processes[/{id}]/export`、`GET /api/admin/routing/item-summary` |
| 在制品 | view / op 写 | `GET /api/admin/products`（支持 `zombie_only`）、`GET /api/admin/products/{sn}`、`POST /api/admin/products/{sn}/force-release`、`GET /api/admin/products/{sn}/sessions` |
| 台账追溯 | view | `GET /api/admin/records`、`GET /api/admin/records/{id}`、`GET /api/admin/records/trace/{sn}` |
| 维修履历 | view / op 写 | `GET /api/admin/repairs`、`POST /api/admin/repairs` |
| 测试会话 | view / op 写 | `GET /api/admin/sessions`、`GET /api/admin/sessions/zombie-locks`、`GET /api/admin/sessions/{id}`、`POST /api/admin/sessions/{id}/abort`、`POST /api/admin/sessions/abort-running` |
| 机台 | view / op 写 | `GET/POST /api/admin/clients`、`PUT/DELETE /api/admin/clients/{client_id}` |
| 统计 | view | `GET /api/admin/metrics/overview` |
| 用户管理 | admin | `GET/POST /api/admin/users`、`PUT/DELETE /api/admin/users/{user_id}` |

完整参数与响应模型见 Swagger（`http://localhost:8000/docs`）。

---

## 10. 上位机接入

```python
from ate_client import AteClient

cli = AteClient("http://127.0.0.1:8000", api_key, client_id="SZ-L1-CAL-01",
                state_file=Path(".ate_session.json"))
state = cli.check_in(sn, model, firmware, case_ids=case_ids)   # 403/409 → 终止
for case_id in case_ids:
    if case_id in state.completed_case_ids:                    # 崩溃续测：跳过
        continue
    cli.checkpoint([run(case_id)])                             # 每用例上报断点
ack = cli.check_out(items)                                     # 201 + acknowledged → 放行
```

四个必须持久化的值：`session_id`、`lock_token`、`checkout_id`、`cursor`。

**完整契约、错误码、时序图、pytest conftest 示例见 [`doc/API.md`](doc/API.md)。**

配套代码：

| 文件 | 定位 |
|---|---|
| `tools/ate_client.py` | 参考实现 SDK + 演示脚本（仅标准库，非产线执行器） |
| `tools/line_simulator.py` | 多机台并发验证（锁竞争 / 崩溃续测 / 失联接管） |

---

## 11. 配置说明

完整清单见 `backend/.env.example`；Docker 环境由 `docker-compose.yml` 的 `environment` 段注入。

### 11.1 应用与网络

| 变量 | 默认 | 说明 |
|---|---|---|
| `APP_DEBUG` | `true` | 调试模式（影响日志级别与错误详情），生产必须 `false` |
| `LOG_LEVEL` | `DEBUG`（跟随 APP_DEBUG）/ `INFO` | 控制台与文件日志级别 |
| `LOG_FILE` | `logs/app.log` | 滚动日志路径（容器内推荐 `/app/data/logs/app.log`） |
| `APP_HOST` / `APP_PORT` | `0.0.0.0` / `8000` | 监听地址（后端不读取此配置，实际由启动脚本 `scripts\dev-backend.bat` 或 docker-compose 决定） |
| `CORS_ORIGINS` | `http://localhost:5173,...` | 跨域白名单，多值用英文逗号分隔 |

### 11.2 数据库

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+pg8000://...` | PG 生产；本地可改 `sqlite:///./data/dev.db` |

> PostgreSQL 使用原生数组 `TEXT[]` 与 `JSONB`；SQLite 自动降级为 JSON，并跳过行锁（`FOR UPDATE`）。

### 11.3 鉴权

| 变量 | 默认 | 说明 |
|---|---|---|
| `JWT_SECRET` | 开发默认值 | 通道二签名密钥，**生产必须更换 ≥32 字节随机串** |
| `JWT_EXPIRE_HOURS` | `168` | Web 登录态有效期 |
| `DEFAULT_ADMIN_USERNAME` | `admin` | 首次启动自动创建的账号（角色=admin，存量库迁移时自动回填 admin） |
| `DEFAULT_ADMIN_PASSWORD` | `admin123` | 首次启动自动创建的口令（仅用户表为空时生效），**首次登录后立即修改** |
| `DEFAULT_ADMIN_NAME` | `系统管理员` | 显示名 |
| `V1_API_KEY` | 空 | 通道一密钥，留空则仅允许网页登录用户调试 |

### 11.4 防呆与租约锁

| 变量 | 默认 | 说明 |
|---|---|---|
| `FAIL_LIMIT` | `3` | 连续失败达阈值 → 工程锁定 |
| `ENFORCE_FW` | `true` | 固件基线校验，`false` 时仅记录不拦截 |
| `ENFORCE_CASE_IDS` | `true` | 进站时比对待执行的用例ID清单 |
| `CLIENT_ONLINE_WINDOW_SECONDS` | `90` | 机台在线判定窗口 |
| `METRICS_WINDOW_DAYS` | `14` | 仪表盘统计窗口 |
| `APP_TIMEZONE` | `Asia/Shanghai` | 统计日界时区（今日良率 / 日趋势按该时区自然日切分） |
| `APP_TZ_OFFSET_HOURS` | `8` | 无 tzdata（Python 3.8 / Windows）时的回退固定偏移 |
| `LOCK_HEARTBEAT_GRACE_SEC` | `120` | 失联判定窗口（新机台最快多久能接管） |
| `LOCK_HEARTBEAT_INTERVAL_SEC` | `30` | 下发给上位机的建议心跳间隔 |
| `SWEEPER_INTERVAL_SEC` | `30` | 孤儿锁回收周期 |
| `SWEEPER_ENABLED` | `true` | 回收总开关（演示场景数据可置 `false`） |
| `LOST_LOCK_FAIL_THRESHOLD` | `3` | 连续失联达 N 次才计一次产品失败 |
| `MERGE_CHECKPOINT_ON_CHECKOUT` | `true` | 出站时用断点补齐未提交用例 |
| `STRICT_LOCK_TOKEN` | `false` | `true` 时"未携带 token"也拒绝；**token 不匹配始终拒绝** |

---

## 12. 维修处置

| 动作 | 语义 | 记录作废 |
|---|---|---|
| `RETEST` | 收回指定工位印章，允许重测 | 该工位旧记录 `is_valid=false` |
| `ROLLBACK` | 回退到目标工位，清除其及后续所有工步印章 | 涉及的记录作废 |
| `RESET` | 清空全部印章与失败计数，重新投产（可复活报废品） | 全部记录作废 |
| `SCRAP` | 判定报废，后续进站一律 403 | — |

处置仅在在制品未被持锁（非 `TESTING`）时允许执行，否则返回 `409 product_holding_lock`。

---

## 13. 测试

```bash
python tests/test_backend.py      # 40 项，覆盖全部卡控场景
pytest tests/test_backend.py      # 亦可用 pytest 收集
```

覆盖范围：进站/出站全链路、跳站与复测拦截、固件与机型校验、用例ID漏测拦截、
ACK 幂等回放、失败锁定、锁冲突与超时接管、心跳不延长硬超时、断点续测合并、
孤儿锁回收、强制解锁、维修处置与守卫、SN 追溯、仪表盘统计、鉴权守卫。

---

## 14. 部署

```bash
docker compose up -d --build     # 单容器同源托管，http://localhost:8000
```

- 多阶段构建：Node 20 构建前端 → Python 3.11 运行时，镜像内自带 `frontend/dist`
- 后端自动挂载前端产物并做 SPA 回退；未检测到产物时纯 API 模式运行
- 端口：宿主机 `8000` → 容器 `8000`
- 健康检查：`GET /api/health`（容器内每 30s 探活）
- 数据卷：`./data` → `/app/data`（持久化 SQLite / 日志）

> 国内网络构建前端可加 `--build-arg NPM_REGISTRY=https://registry.npmmirror.com`。

---

## 15. 生产安全建议

| 项 | 要求 |
|---|---|
| `V1_API_KEY` | 更换为随机长令牌，与产线上位机一一对应下发 |
| `JWT_SECRET` | 更换为 ≥32 字节随机串 |
| `DEFAULT_ADMIN_PASSWORD` | 首次启动后立即修改 |
| `APP_DEBUG` | 必须 `false` |
| `DATABASE_URL` | 使用 PostgreSQL（SQLite 仅供开发/单测） |
| `CORS_ORIGINS` | 收敛到实际前端域名 |
| 网络 | 上位机与服务端置于产线内网，禁止暴露到办公网/公网 |

---

## 16. 故障排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 进站 `403 client_not_bound` | 机台未绑定工位 | Web 端「机台管理」绑定，或用 `POST /api/admin/clients` |
| 进站 `403 missing_prereq` | 前工序未完成 | 按提示补齐前置工位；确认 SN 未被贴错 |
| 进站 `409 station_already_passed` | 该工位已盖章 | 禁止复测；确认是否推错车，必要时走 RETEST 处置 |
| 进站 `403 firmware_mismatch` | 固件非基线 | 刷写基线固件；调试期可临时 `ENFORCE_FW=false` |
| 出站 `403 lock_invalid` | 锁已被其他机台接管 | 停机，重新进站；旧数据作废 |
| 出站 `400 missing_mandatory` | 必测用例未执行或被报为 SKIP | 补齐用例；检查脚本与服务端清单是否同步 |
| 崩溃后从头重测 | 未上报断点或未持久化 session | 确认每用例调用 `checkpoint`，且断点文件写入成功 |
| 锁长时间不释放 | Sweeper 关闭或周期过长 | 检查 `SWEEPER_ENABLED` / `SWEEPER_INTERVAL_SEC`；或用强制解锁 |
| 演示场景数据看不到僵尸锁 | 被后台任务自动回收 | 以 `SWEEPER_ENABLED=false` 启动后端 |
| 前端页面 404（刷新后） | 静态托管未生效 | 确认 `frontend/dist/index.html` 存在，或改用 Hash 路由 |

---

## 17. 相关文档

| 文档 | 内容 |
|---|---|
| [`doc/API.md`](doc/API.md) | 上位机接口完整契约（错误码 / 时序 / 实现规范） |
| `backend/.env.example` | 全部配置项及注释 |
| `/docs`（运行时） | OpenAPI 交互式文档 |
