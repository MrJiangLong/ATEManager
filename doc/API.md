# ATE Manager 上位机接口

> **通道一（`/api/v1/*`）** —— 面向产线上位机（pytest 测试工程）的机器接口。
> Web 管理端接口（`/api/admin/*`）见 [第 11 章](#11-管理端运维接口jwt)，仅供运维台调用。

| 项 | 值 |
|---|---|
| 文档版本 | v1.2 |
| 适用服务端 | ATE Manager API ≥ 2.1.0 |
| 接口前缀 | `/api/v1` |
| 鉴权方式 | 请求头 `X-API-Key` |
| 传输协议 | HTTP/1.1 + JSON（UTF-8） |
| 配套代码 | `tools/atetest/api.py`（产线形态 SDK：`AteRun` 会话状态机）、`tools/client/ate_client.py`（契约参考 + CLI 演示）、`tools/client/line_simulator.py`（多机台并发验证） |

---

## 目录

1. [读者与范围](#1-读者与范围)
2. [接入前准备](#2-接入前准备)
3. [通用约定](#3-通用约定)
4. [生命周期与状态机](#4-生命周期与状态机)
5. [接口详解](#5-接口详解)
6. [错误码与处理决策表](#6-错误码与处理决策表)
7. [租约锁模型：双超时](#7-租约锁模型双超时)
8. [崩溃续测与锁接管](#8-崩溃续测与锁接管)
9. [幂等与上传闭环](#9-幂等与上传闭环)
10. [客户端实现规范](#10-客户端实现规范)
11. [管理端运维接口（JWT）](#11-管理端运维接口jwt)
12. [服务端配置项](#12-服务端配置项)
13. [联调与验收](#13-联调与验收)
14. [附录](#14-附录)

---

## 1. 读者与范围

本文档是上位机接入的**唯一契约依据**，适用于：

- 产线 pytest 测试工程（conftest / 插件）的开发者
- 测试工位工控机上运行的自动化脚本
- 需要做联调、压测或问题定位的测试工程师

**不在本文范围**：Web 管理端页面的使用说明、工艺流程配置方法（见仓库 `README.md`）。

### 1.1 上位机只需做对 5 件事

| # | 要求 | 不做会怎样 |
|---|---|---|
| 1 | 进站后保存 `session_id` / `lock_token` 并**持久化到本地文件** | 崩溃后无法续测，退化为"从头重测" |
| 2 | 按 `heartbeat_interval_sec`（默认 30s）**后台心跳** | 超过宽限期锁被判定失联，可被同工位其他机台接管 |
| 3 | 每跑完一个用例即 `POST /client/checkpoint` **增量上报断点** | 崩溃后已跑完的用例丢失，需重测 |
| 4 | 出站携带 `lock_token`，收到 `403 lock_invalid` **立即停机** | 锁已被接管，写入的结果作废甚至污染台账 |
| 5 | `checkout_id` 由客户端生成并持久化，**网络重试复用同一值** | 断网重传产生重复账目 |

> 第 1、5 条是"必须"，第 2、3、4 条决定崩溃与并发场景下的正确性与效率。

### 1.2 被测件身份从哪来

`sn` / `product_model` / `firmware` 一律由底层 SCPI `*IDN?` **直读解析**，不存在人工扫码或手工建档环节：

```
*IDN?  →  TEKTRONIX,DPO4054B,C020001,CF:91.1CT,FV:V3.20
           └── 型号 ──┘ └─ SN ─┘         └─ 固件 ─┘
```

首工位首次进站时，服务端用该结果**动态建档**（`product_status` 新增一行）。

> **写 SN 工位是唯一例外**：被测件进站前还没有 SN。该工位的前置用例先离线自检
> （结果只在本地累积），写 SN 用例扫码后再 `check-in`；服务端对重号/跳站/已锁/
> 已报废/机型固件基线的拦截发生在烧录动作之前，进站成功后前置结果批量补报。
> 接入方式见附录 A 的 `sn_burner`。

### 1.3 用例ID（case_id）

与服务端 `station_items` 静态清单严格一致，取 pytest nodeid：

```
tests/test_cal_param.py::TestAmp::test_amp_cal
```

> 数据库物理列名为 `nodeid`（沿用既有 DDL），但**接口、代码、界面一律使用 `case_id`**。

---

## 2. 接入前准备

### 2.1 获取密钥

从服务端配置 `V1_API_KEY` 取值（产线部署时由运维下发）。所有请求必须携带：

```http
X-API-Key: <V1_API_KEY>
Content-Type: application/json
```

> - 若服务端未配置 `V1_API_KEY`，可退化为网页 JWT（`Authorization: Bearer <token>`），
>   仅供开发调试，**产线禁止**。
> - 上位机与 Web 端的**四角色权限体系无关**：通道一只认 API Key，不存在角色概念。
>   Web 端用户管理（viewer/operator/manager/admin）仅约束管理端接口；
>   `manager` = 业务/配置管理（工艺、SN 登记、报告规则与标准器），不含用户管理。

### 2.2 注册机台并绑定工位

机台（物理工控机）必须先与逻辑工位绑定，否则进站返回 `403 client_not_bound`。
由运维在 Web 端「机台管理」完成（需 operator 及以上角色），或用管理端接口注册：

```http
POST /api/admin/clients
{"client_id": "SZ-L1-CAL-01", "bound_stations": ["CAL-PARAM"], "ip_address": "10.1.60.11"}
```

`bound_stations` 为绑定集合（一机可绑多个工位，但同一流程内只能绑定一个，否则保存即 `400 station_ambiguous`）。
同一流程内的多个工位请分别登记为独立机台档案，档案编号按「电脑名-工位」命名并由上位机工程赋给 `CLIENT_ID`，不要共用一个跨工位档案。
也可用 `POST /api/v1/client/resolve` 自动注册（此时绑定为空，仍需 Web 端补录绑定）。

### 2.3 确认识别用例清单

工位必须已在工艺流程中配置用例ID清单（Web 端「工艺配置 → 测试项」），
否则进站会通过但出站会因"无必测项"而不产生拦截语义。

---

## 3. 通用约定

### 3.1 统一响应包

所有响应（含错误）均为 `EnvelopeOut`：

```jsonc
{
  "ok": true,              // exit_code == 0
  "exit_code": 0,          // 供产线脚本做数值分支（见 3.3）
  "code": "ok.checkin",    // 机器可读的结果码
  "message": "Check-in accepted",
  "data": { ... }          // 业务数据；错误时为补充信息（如 missing 清单）
}
```

成功时业务数据在 `data` 中；各接口的 `data` 结构见[第 5 章](#5-接口详解)。

### 3.2 HTTP 状态码是第一分流依据

| 状态码 | 语义 | 上位机动作 |
|---|---|---|
| `200` | 放行 / 查询成功 | 继续 |
| `201` | 出站落库成功（**即 ACK**） | 可拔线流转 |
| `400` | 请求或工艺配置错误 | **不重试**，报障人工介入 |
| `401` | 密钥缺失或无效 | 检查 `X-API-Key` |
| `403` | 防呆拦截 / 锁失效 | **不重试**；按 `code` 分支处理 |
| `404` | 实体不存在（机台/SN/会话/ACK） | 检查参数 |
| `409` | 状态冲突（复测拦截 / 锁冲突） | 锁冲突可等待后重试 |
| `422` | 请求体校验失败（字段格式不符，如编号规则） | **不重试**，修正请求 |
| `5xx` | 服务端故障 | 可重试（指数退避） |

### 3.3 exit_code 数值表

供既有产线脚本做数值分支（与 HTTP 状态并存）：

| exit_code | 常量名 | 含义 |
|---|---|---|
| `0` | `EXIT_OK` | 成功（PASS） |
| `1` | `EXIT_FAIL` | 出站成功但结论为 FAIL |
| `10` | `EXIT_GATE_BLOCKED` | 防呆拦截（跳站 / 复测 / 固件 / 报废 / 工艺不符） |
| `11` | `EXIT_LOCK_CONFLICT` | 锁冲突 |
| `13` | `EXIT_MISSING_MANDATORY` | 漏测拦截 |
| `14` | `EXIT_LOCK_EXPIRED` | 锁失效 / 超时 / 被接管 |
| `15` | `EXIT_CASE_ID_MISMATCH` | 用例ID清单不匹配 |
| `16` | `EXIT_PRODUCT_LOCKED` | 已工程锁定（连续失败达上限） |

### 3.4 重试策略

| 错误类型 | 是否重试 | 建议 |
|---|---|---|
| 连接失败 / 超时 / `5xx` | **是** | 指数退避，最多 2~3 次 |
| `409 lock_conflict` | 条件重试 | 等 `grace_sec - lock_idle_sec` 后重试，或换机台 |
| `4xx` 其他 | **否** | 报障，人工介入 |

> 网络重试必须坚持"幂等键不变"原则：`checkout_id` 复用同一值，服务端回放既有回执。

---

## 4. 生命周期与状态机

### 4.1 在制品状态（`product_status.current_status`）

```
       首站进站                    出站 PASS（盖章）
NULL ─────────────→ IDLE ─────────────────────────→ IDLE（下一站待测）
                     │  ↑                            全部工步盖齐 → 完工
                     │  │ 出站 FAIL / 锁超时 / 漏测
                     ↓  │
                  TESTING（持锁中）
                     │
                     │ 连续失败 ≥ FAIL_LIMIT
                     ↓
                  LOCKED ──(维修处置)──→ IDLE
                     │
                     └──(SCRAP)──→ SCRAPPED（终态，进站一律 403）
```

### 4.2 测试会话状态（`test_sessions.status`）

| 状态 | 含义 | 触发 |
|---|---|---|
| `RUNNING` | 进行中，持锁 | 进站 |
| `COMPLETED` | 正常出站 | 出站成功 |
| `ABORTED` | 机台失联 / 主动释放 / 漏测终止 | 心跳断流、`release`、漏测拦截 |
| `EXPIRED` | 超过工位硬超时 | 持锁超过 `timeout_sec` |
| `TAKEN_OVER` | 被其他机台接管 | 失联或超时后他人进站 |

---

## 5. 接口详解

调用顺序：`resolve` → `check-in` → (`heartbeat` ∥ `checkpoint` ∥ `artifact`) → `check-out` → 必要时 `ack` / `release`。

---

### 5.1 `POST /api/v1/client/resolve` — 机台身份上报

启动时调用一次，用于上报 IP、反查绑定的工位。首次调用自动注册。

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `client_id` | string(64) | 是 | 机台唯一标识 |
| `ip_address` | string(45) | 否 | 本机 IP |
| `app_version` | string(50) | 否 | 上位机版本，便于追溯 |

```jsonc
{"client_id": "SZ-L1-CAL-01", "ip_address": "10.1.60.11", "app_version": "ate-client/1.0"}
```

**响应 `data`**

```jsonc
{
  "client_id": "SZ-L1-CAL-01",
  "station_id": "CAL-PARAM",     // 未绑定时为 null
  "ip_address": "10.1.60.11",
  "bound": true,                 // false 时进站会 403 client_not_bound
  "last_seen_at": "2026-09-08T13:58:01.386992+00:00",
  "server_time": "..."
}
```

---

### 5.2 `POST /api/v1/client/case-list` — 用例清单上报（录制态草稿，check-in 前）

把机台本次会话的**执行序列**（有序 `case_ids` 全集）上报为该工位的基线草稿。
上位机应在 `check-in` **之前**调用：即使进站被拒（必测缺失/顺序不符/固件不符等），
草稿也已留下机台真实序列，供工艺员在管理端审阅并锁定基线。

- 录制态（`baseline_locked=false`）：内容变化才写库，返回 `recorded=true/false`
- 锁定态：**不覆盖**草稿，返回 `recorded=false, locked=true, baseline_count`
- 失败不影响测试：SDK 在 `start()` 内静默调用（异常吞掉），进站会给出真正的错误

**请求**

```json
{
  "client_id": "SZ-L9-CAL-99-PARAM",
  "product_model": "DPO4054B",
  "case_ids": ["tests/test_cal_param.py::TestAmp::test_amp_cal", "..."]
}
```

**响应 data**

```json
{ "recorded": true, "locked": false, "station_id": "CAL-PARAM", "baseline_count": 5 }
```

---

### 5.3 `POST /api/v1/client/check-in` — 进站（领取工位锁）

进站是**全部防呆卡控的入口**：跳站、复测、固件基线、机型一致性、用例ID清单、锁竞争都在此判定。

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `client_id` | string(64) | 是 | 机台 ID |
| `sn` | string(64) | 是 | `*IDN?` 直读序列号 |
| `product_model` | string(64) | 是 | `*IDN?` 直读机型（须已注册） |
| `firmware` | string(32) | 是 | `*IDN?` 直读固件版本 |
| `case_ids` | string[] | 否 | 本次待测的用例ID清单，用于防漏测前置校验 |
| `resume_session_id` | string(36) | 否 | 续测会话ID：崩溃重启后携带 |
| `app_version` | string(50) | 否 | 上位机版本 |

```jsonc
{
  "client_id": "SZ-L1-CAL-01",
  "sn": "C020001",
  "product_model": "DPO4054B",
  "firmware": "V3.20",
  "case_ids": ["tests/test_cal_param.py::TestAmp::test_amp_cal"],
  "resume_session_id": null
}
```

**响应 `data`（200）**

```jsonc
{
  "sn": "C020001",
  "product_model": "DPO4054B",
  "process_id": "PROC-SCOPE-DPO-BASE",
  "station_id": "CAL-PARAM",
  "station_name": "校准-指标测试站位",
  "timeout_sec": 1800,                 // 硬超时上限（秒）
  "is_first_station": true,
  "created_on_checkin": true,          // 本次是否动态建档
  "passthrough": false,                // true = 受控直通：station_id/rules/session_id 为空，
                                       // 直接开始测试，跳过心跳/断点/出站（零写入）
  "rules": [                           // 服务端下发的执行规则，按 station_items.sort_order
                                       // 排序；上位机必须按此顺序执行用例（服务端契约，
                                       // 未配置序号的用例由服务端按添加/导入顺序编号）。
                                       // 进站上报的 case_ids 顺序不符 → case_order_mismatch
    {"case_id": "tests/test_cal_param.py::TestAmp::test_amp_cal",
     "item_name": "CHn幅度校准", "is_mandatory": true}
  ],
  "passed_stations": [],               // 已盖章工位
  "next_stations": [{"station_id": "CAL-IFACE", "station_name": "...", "step_order": 20}],
  "server_time": "...",

  "session_id": "3f2a...",             // 会话ID，务必持久化
  "lock_token": "b1c9...",             // fencing 凭证，出站/上报必须带回
  "attempt": 1,                        // 第几次尝试（>1 表示崩溃后续测）
  "heartbeat_interval_sec": 30,        // 建议心跳间隔
  "heartbeat_grace_sec": 120,          // 超过即判定失联，锁可被接管
  "resume": {                          // 续测上下文
    "resumed": false,
    "attempt": 1,
    "completed_case_ids": [],          // 崩溃前已完成的用例，应跳过
    "cursor": {}                       // 上次存的断点上下文，原样回传
  },
  "takeover": false,                   // 本次是否接管了别人的锁
  "takeover_from": null                // 被接管的原机台
}
```

**自动续测**：即使不传 `resume_session_id`，只要**同一台机台**重新进站同一 SN + 同一工位，
服务端也会自动复用会话（`attempt + 1`）并返回已完成用例清单。换新机台则默认新建会话（不继承断点，更安全）。

**状态码**

| 码 | `code` | 含义 | 处理 |
|---|---|---|---|
| 200 | — | 放行（可能含 `takeover=true`） | 开始测试 |
| 400 | `station_not_in_process` | 该工位不属于此机型的流程 | 工艺配置错误，报障 |
| 400 | `case_id_mismatch` | 待执行清单缺少必测用例ID | 脚本版本与服务端规则不符 |
| 400 | `case_order_mismatch` | 用例执行顺序与工位配置（`sort_order`）不符 | 调整工程/用例定义顺序，使收集顺序与规则一致 |
| 403 | `client_not_bound` | 机台未绑定工位 | 联系运维绑定 |
| 403 | `model_not_registered` | 机型未注册 | 联系工艺工程师 |
| 403 | `sn_is_test_device` | SN 已登记为试验设备，生产服（`SERVER_ROLE=production`）禁止进站 | 生产件走产线，试验设备送测试服 |
| 403 | `sn_not_registered_test_device` | SN 未登记，研发服（`SERVER_ROLE=development`）白名单拒收 | 联系管理员在「SN 登记」登记后重试 |
| 403 | `missing_prereq` | **跳站拦截**：前置工位未完成（声明依赖 `depends_on` ∪ 严格顺序下的前序必跑工位；流程 `is_sequential` 关闭时仅按依赖判定） | 弹告警、pytest 终止、治具不开电 |
| 403 | `model_mismatch` | SN 已登记为其他机型 | 贴错机型/换线未清线 |
| 403 | `firmware_mismatch` | 固件不满足基线（口径见机型 `fw_match_rule`） | 提示刷写基线固件 |
| 403 | `app_version_mismatch` | 测试软件版本不满足基线（口径见机型 `app_match_rule`；版本缺失/unknown 视为不满足） | 升级上位机测试程序 |
| 403 | `product_scrapped` | 已报废 | 禁止流转 |
| 403 | `product_locked` | 连续失败达上限，已工程锁定 | 提示送修 |
| 404 | `process_not_found` / `session_not_found` | 流程/会话不存在 | 检查参数 |
| 409 | `station_already_passed` | **复测拦截**：该工位已盖章 | 提示推错车，禁止复测 |
| 409 | `lock_conflict` | 锁被其他机台持有且未失联 | 等待或换机台 |
| 409 | `session_completed` | 指定的 `resume_session_id` 已出站 | 不要续测，重新进站 |
| 400 | `session_mismatch` | `resume_session_id` 与 SN/工位不符 | 不要续测，重新进站 |
| 404 | `client_not_registered` | 机台未注册 | 先调 resolve 自动注册 |

> `lock_conflict` 的 `data` 含 `lock_idle_sec` 与 `grace_sec`，可用于估算"还要等多久才能接管"。

**固件基线口径（`fw_match_rule`）**

由机型的 `fw_match_rule` 决定，运维在 Web 管理端「工艺配置 → 机型」中维护：

| 取值 | 判定 | 适用场景 |
|---|---|---|
| `exact`（默认） | `firmware == target_fw_version` | 严格锁定版本，任何偏差都拦截 |
| `min` | `firmware >= target_fw_version` | 允许小版本升级，只要不低于基线 |

`min` 按**数字段**比较，不是字符串比较 —— 因此 `V3.9 < V3.20` 判定正确（字符串比较会误判成 `V3.9 > V3.20` 而放行旧固件）。
版本含日期/哈希等无法解析的内容时退回字符串比较。

`firmware_mismatch` 的 `data` 回传 `expected` / `actual` / `rule`，客户端可直接提示"需刷写到 V3.20 或更高"。
若产线尚未强制固件，服务端可用 `ENFORCE_FW=false` 关闭该拦截（仅记录不阻断）。

**测试软件基线口径（`app_match_rule`）**

与固件基线同构：由机型的 `app_match_rule` 决定，在 Web 管理端「工艺配置 → 机型」中维护（`target_app_version`，留空 = 不卡控）。

| 取值 | 判定 | 适用场景 |
|---|---|---|
| `exact` | `app_version == target_app_version` | 严格锁定测试程序版本，任何偏差都拦截 |
| `min` | `app_version >= target_app_version`（数字段比较） | 允许升级，只要不低于基线 |

`app_version` 缺失或为 `unknown` 且基线已配置时视为不满足（版本不明不得上线）。
`app_version_mismatch` 的 `data` 回传 `expected` / `actual` / `rule`，客户端可直接提示"需升级测试程序到指定版本或更高"。
若暂不强制，服务端可用 `ENFORCE_APP_SW=false` 关闭该拦截（仅记录不阻断）。

---

### 5.4 `POST /api/v1/client/heartbeat` — 保活

**请求**

```jsonc
{"client_id": "SZ-L1-CAL-01", "sn": "C020001", "lock_token": "b1c9..."}
```

**响应 `data`**

```jsonc
{
  "sn": "C020001",
  "holding_lock": true,       // false = 锁已被接管，必须立即停机
  "remaining_sec": 95,        // 距失联判定还剩多久
  "lease_remaining_sec": 1680,// 硬超时剩余（心跳不续期，单调递减）
  "heartbeat_count": 12,      // 诊断用
  "session_id": "3f2a...",
  "server_time": "..."
}
```

> `holding_lock=false` 时必须停机：说明别的机台已接管，继续测的结果**不会被接受**。

---

### 5.5 `POST /api/v1/client/checkpoint` — 续测断点上报

崩溃续测的核心。按 `case_id` **去重覆盖**，重复上报幂等。

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `client_id` | string(64) | 是 | 机台 ID |
| `sn` | string(64) | 是 | 序列号 |
| `session_id` | string(36) | 是 | 本次会话 ID |
| `lock_token` | string(64) | 否 | fencing 凭证 |
| `items` | object[] | 是 | 本次完成的用例结果（见下表） |
| `cursor` | object | 否 | 任意断点上下文，原样回传 |

**`items[]` 结构**

| 字段 | 类型 | 说明 |
|---|---|---|
| `case_id` | string(256) | 用例ID（必填） |
| `result` | enum | `PASS` / `FAIL` / `SKIP` |
| `values` | object | 测量值快照，任意 JSON |
| `duration_ms` | int | 单项耗时 |
| `message` | string | 失败信息/备注 |

```jsonc
{
  "client_id": "SZ-L1-CAL-01",
  "sn": "C020001",
  "session_id": "3f2a...",
  "lock_token": "b1c9...",
  "items": [
    {"case_id": "tests/test_cal_param.py::TestAmp::test_amp_cal", "result": "PASS",
     "values": {"voltage_v": 3.301}, "duration_ms": 1500, "message": null}
  ],
  "cursor": {"step": 1, "instrument": {"afg": "CH1"}}
}
```

**响应 `data`**

```jsonc
{"sn": "...", "session_id": "...", "accepted_count": 1, "merged_count": 3,
 "completed_case_ids": ["...", "...", "..."], "server_time": "..."}
```

语义与建议：

- `merged_count` 为合并后总数；`completed_case_ids` 为服务端记录的已完成清单
- 上报失败**不应阻断测试**：本地落盘，恢复后把积压的 items 一次性批量补传
- 建议粒度：每跑完一个用例即上报；长用例也可分段上报（同一 `case_id` 后写覆盖）

---

### 5.6 `POST /api/v1/client/check-out` — 出站落库（返回 ACK）

**需求 1 的落点**：必须拿到本接口的 201 回执，才允许拔线流转。

**请求**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `client_id` | string(64) | 是 | 机台 ID |
| `sn` | string(64) | 是 | 序列号 |
| `checkout_id` | string(64) | 是 | **幂等键**，客户端生成并持久化 |
| `items` | object[] | 是 | 本次实际执行的用例结果 |
| `duration_ms` | int | 否 | 总耗时 |
| `reason` | string | 否 | 备注（如失败原因） |
| `lock_token` | string(64) | 否 | **fencing 凭证** |

```jsonc
{
  "client_id": "SZ-L1-CAL-01",
  "sn": "C020001",
  "checkout_id": "<客户端生成并持久化的幂等键>",
  "lock_token": "b1c9...",
  "items": [{"case_id": "...", "result": "PASS", "values": {}, "duration_ms": 1500}],
  "duration_ms": 45000,
  "reason": ""
}
```

**响应 `data`（201）**

```jsonc
{
  "acknowledged": true,          // true 即落库成功
  "record_id": 12345,
  "checkout_id": "...",
  "session_id": "3f2a...",
  "checkpoint_merged_count": 2,  // 由断点补齐的用例数
  "sn": "C020001",
  "station_id": "CAL-PARAM",
  "overall_result": "PASS",      // PASS / FAIL
  "idempotent_replay": false,    // true = 命中重试回放
  "fail_count": 0,
  "fail_limit": 3,
  "product_locked": false,       // 连续失败达上限已锁定
  "is_completed": false,         // 全部工步是否已盖齐
  "passed_stations": [],
  "next_stations": [],
  "server_time": "..."
}
```

要点：

- **只提交本次实际跑的用例即可**；崩溃前已上报但未提交的会由 checkpoint 自动补齐，不会误判漏测
- **必测用例上报 `SKIP` 等同于未执行** → 触发漏测拦截
- **overall 判定只看必测用例**：必测 FAIL → 工位 FAIL；非必测（选做/加测）用例的
  结果仅记录在 `executed_items`（台账与 TopFailed 可见），不影响放行、不计失败
- `403 lock_invalid` = 锁已被接管 → **结果作废，停止测试，重新进站**
- `403 lock_expired` = 超过工位硬超时 → 计一次失败，需重新进站
- `400 missing_mandatory` = 漏测拦截 → 计一次失败

**状态码**

| 码 | `code` | 处理 |
|---|---|---|
| 201 | `ok.checkout` | 落库成功，可放行 |
| 400 | `missing_mandatory` | 漏测拦截（已计一次失败），人工介入 |
| 400 | `station_not_in_process` | 工艺配置错误 |
| 403 | `lock_invalid` | 锁被接管，停机重新进站 |
| 403 | `lock_expired` | 锁超时，停机重新进站 |
| 403 | `client_not_bound` | 机台未绑定工位，联系运维绑定 |
| 404 | `product_not_found` | SN 不存在 |

---

### 5.7 `POST /api/v1/client/artifact` — 测试产物上传（结果文件/日志等）

**通用产物归档机制**：任何机台客户端（pytest 上位机、独立工控机程序等）均按此上传产物。
文件落 MinIO（与出厂报告共用同一桶配置，仅前缀不同；MinIO 未配置时上传直接拒绝），
引用暂存进会话断点，出站时自动并入对应 `case_id` 的 `executed_items.items[].files` 落库。

**请求（multipart/form-data）**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `client_id` | string(64) | 是 | 机台 ID |
| `sn` | string(64) | 是 | 序列号 |
| `session_id` | string(36) | 是 | 当前会话 ID |
| `case_id` | string(256) | 是 | 产物归属的用例 ID |
| `lock_token` | string(64) | 否 | **fencing 凭证**；锁被接管后旧 token 一律拒绝 |
| `file` | binary | 是 | 文件内容；空文件拒绝，默认上限 200MB（`ARTIFACT_MAX_BYTES`） |

**响应 `data`（200）**

```jsonc
{"object_key": "artifacts/DPO4054B/C020001/3f9a1c2b7e40/amp.cal", "filename": "amp.cal", "size": 1024}
```

要点：

- 对象键布局 `artifacts/{model}/{sn}/{case_hash}/{filename}`，其中
  `case_hash` = `sha1(case_id)` 前 12 位十六进制——case_id（如 pytest nodeid
  含 `/` 与 `::`）不进路径；case_id 本身在 `executed_items.items[].case_id` 留档
- **同 case_id + 文件名重复上传 = 覆盖**（同 case_id 恒定同哈希）
- 时序约定：每完成一个流程 → 上传产物 → checkpoint 上报该流程结果；
  引用由服务端自动并入出站记录（客户端无需在 checkpoint/check-out 里回传 files）
- 文件永久保留；追溯入口 = 台账/追溯页记录详情的产物文件列（登录用户下载，见 §11）

**状态码**：200 `ok.artifact`；400 `artifact_empty` / `artifact_too_large` / `session_mismatch`；
403 `lock_invalid`；404 `session_not_found` / `product_not_found`；409 `session_completed` / `session_aborted`；
503 `artifact_storage_unavailable`（服务端 MinIO 未配置）。

---

### 5.8 `POST /api/v1/client/release` — 主动放弃锁

```jsonc
{"client_id": "SZ-L1-CAL-01", "sn": "C020001", "lock_token": "b1c9...", "reason": "操作员取消"}
```

**不计产品失败**，锁立即释放，会话置为 `ABORTED`。
用于优雅退出、放弃本次测试、或崩溃前主动让位。响应 `data`：`{sn, released, session_id, server_time}`。

---

### 5.9 `GET /api/v1/client/ack` — 上传闭环校验

```
GET /api/v1/client/ack?sn=C020001&checkout_id=<幂等键>
```

命中（200）即证明服务端已落库，可放行流转；未命中返回 `404 ack_not_found`。
用于超时/断网重传后确认状态，**不得**作为唯一的放行依据（正常路径应直接取 `check-out` 的 201）。

---

## 6. 错误码与处理决策表

### 6.1 全量错误码

**错误消费契约**：上位机应以 `code` + `data` 进行逻辑判断与本地化渲染（`code` 为稳定英文机器码，永不改变；`data` 携带 `sn`/`station_id`/`station_name` 等结构化参数）。`message` 为**中文**人类可读文本（工位/机台/流程以"代码（名称）"成对出现），仅供直接展示或日志，服务端不提供 message 的多语言版本。

| HTTP | `code` | `exit_code` | 触发接口 | 含义 |
|---|---|---|---|---|
| 401 | `invalid_credentials` | 10 | 全部 | 缺少或无效的 `X-API-Key` |
| 400 | `station_not_in_process` | 10 | check-in / check-out | 工位不属于该机型流程 |
| 400 | `case_id_mismatch` | 15 | check-in | 待执行清单缺少必测用例ID |
| 400 | `case_order_mismatch` | 15 | check-in | 未锁定工位：上报用例相对顺序与工位配置 `sort_order` 不符；**锁定工位**：上报用例全列表与受控基线不一致（含顺序/增删，`data` 携带 `missing`/`extra`/`first_diff_index` 分诊），需工艺员在 Web 解锁重录 |
| 400 | `station_ambiguous` | 10 | 管理端保存绑定 / check-in | 同一流程内命中多个绑定工位（配置层拒绝或拓扑漂移兜底） |
| 400 | `missing_mandatory` | 13 | check-out | 漏测拦截（未执行或 SKIP） |
| 400 | `session_mismatch` | 10 | check-in / checkpoint / artifact | 会话与 SN/机台不匹配 |
| 400 | `station_mismatch` | 10 | checkpoint | 会话属于其他工位 |
| 400 | `artifact_empty` | 10 | artifact | 产物文件内容为空 |
| 400 | `artifact_too_large` | 10 | artifact | 产物文件超过 `ARTIFACT_MAX_BYTES` 上限 |
| 409 | `process_switch_wip_confirm_required` | 10 | 管理端机型改绑流程 | 存在在制品，需显式确认（`data.wip`/`from_process`/`to_process`）——切换后印章按新流程判定（交集工位继承、新增必跑补测） |
| 409 | `baseline_locked` | 10 | 管理端 items 增删改/导入 | 工位基线已锁定：用例清单由基线固化，需先解锁再编辑 |
| 409 | `baseline_already_locked` | 10 | 管理端 baseline/lock | 工位基线已处于锁定态 |
| 409 | `baseline_not_locked` | 10 | 管理端 baseline/unlock | 工位基线未锁定，无需解锁 |
| 400 | `baseline_empty` | 10 | 管理端 baseline/lock | 尚未捕获到用例序列草稿（该工位无进站记录），无法锁定 |
| 403 | `client_not_bound` | 10 | 全部 | 机台未绑定工位 |
| 403 | `model_not_registered` | 10 | check-in | 机型未注册 |
| 403 | `sn_is_test_device` | 10 | check-in | 生产服黑名单：登记的试验设备禁止进生产流程（建档前拦截，零痕迹） |
| 403 | `sn_not_registered_test_device` | 10 | check-in | 测试服白名单：未登记 SN 拒收（服务端硬卡控，客户端无法绕过） |
| 409 | `model_topology_missing` | 10 | 管理端 | 关直通被拒：机型流程无拓扑，切严格卡控会进站 404 |
| 409 | `model_has_wip` | 10 | 管理端 | 开直通被拒：机型存在在制件，不允许旁路件与卡控件混合态 |
| 409 | `drain_confirm_required` | 10 | 管理端 | 关直通缺清线确认位（`confirm_drain`），防止直连 API 绕过清线弹窗 |
| 403 | `missing_prereq` | 10 | check-in | 跳站拦截（`depends_on` + 严格顺序前序必跑工位；`data.missing` 按工步序列出） |
| 403 | `model_mismatch` | 10 | check-in | 机型与建档不符 |
| 403 | `firmware_mismatch` | 10 | check-in | 固件不满足基线（口径见机型 `fw_match_rule`） |
| 403 | `app_version_mismatch` | 10 | check-in | 测试软件版本不满足基线（口径见机型 `app_match_rule`） |
| 403 | `product_scrapped` | 10 | check-in | 已报废 |
| 403 | `product_locked` | 16 | check-in | 已工程锁定 |
| 403 | `lock_invalid` | 14 | check-out / checkpoint / artifact / release | 锁被接管或失效 |
| 403 | `lock_expired` | 14 | check-out | 锁超过硬超时 |
| 404 | `client_not_registered` | 10 | 全部 | 机台未注册 |
| 404 | `product_not_found` | 10 | check-out / artifact / 管理端 | SN 不存在 |
| 404 | `process_not_found` | 10 | check-in | 流程不存在 |
| 404 | `session_not_found` | 10 | check-in / checkpoint / artifact | 会话不存在 |
| 404 | `ack_not_found` | 10 | ack | 无对应落库记录 |
| 503 | `artifact_storage_unavailable` | 10 | artifact / 管理端产物下载 | 服务端 MinIO 未配置 |
| 409 | `station_already_passed` | 10 | check-in | 复测拦截 |
| 409 | `lock_conflict` | 11 | check-in / 管理端 | 锁被他人持有 |
| 409 | `session_completed` | 10 | check-in / checkpoint / artifact | 会话已出站 |
| 409 | `session_aborted` | 14 | checkpoint / artifact | 会话已被运维中止（换测试用例清单前），须立即停机 |
| 409 | `product_holding_lock` | 11 | 管理端 | 在制品被持锁，禁止处置 |
| 409 | `product_scrapped` | 10 | 管理端维修处置 | 报废件禁非报废处置（封死复活通道；check-in 侧为 403 同名码） |
| 400 | `action_not_batchable` | 10 | 管理端批量处置 | RESET/SCRAP 破坏面大，仅允许单条 + 二次确认 |
| 400 | `items_required` / `batch_too_large` | 10 | 管理端批量处置 | 批量清单为空或超过 50 条上限 |
| 400 | `duplicate_sn` / `field_required` | 10 | 管理端批量处置（单项） | 批量清单中的重复/空 SN，按失败项返回不中断整批 |
| 409 | `already_undone` | 10 | 管理端撤销处置 | 该处置已被撤销（含撤销人与时间） |
| 409 | `action_not_undoable` | 10 | 管理端撤销处置 | SCRAP 不可撤销（永久拦截语义） |
| 409 | `not_latest_disposition` | 10 | 管理端撤销处置 | 仅允许撤销该 SN 最新一条处置 |
| 409 | `repair_state_changed` | 10 | 管理端撤销处置 | 处置后产线已有动作（新会话/印章变化），只能走物理重测 |

### 6.2 处理决策

```python
try:
    ...
except ApiError as e:
    if e.code in ("lock_invalid", "lock_expired"):
        stop_testing()          # 停机，结果作废，重新进站
    elif e.code == "lock_conflict":
        wait_and_retry()        # 等 grace 后重试，或换机台 / 运维强制解锁
    elif e.status == 400:
        abort_with_alarm()      # 工艺/用例配置错误，人工介入
    elif e.is_retryable:        # 网络故障 / 5xx
        retry_with_backoff()
```

| 场景 | 服务端行为 | 客户端行为 |
|---|---|---|
| 上位机崩溃 | 断点保留在 `test_sessions.checkpoint`；超宽限期后锁释放（不计失败） | 重启后带着 `session_id` 重新进站，跳过已完成用例 |
| 断网 | checkout/checkpoint 失败 | 本地暂存，恢复后按同一 `checkout_id` 重传；用 `/client/ack` 确认 |
| 锁被接管 | 旧 token 写入一律 403 | 停机，提示操作员，**不要**继续测 |
| 工位硬超时 | 锁释放并计一次失败 | 停机，重新进站 |

---

## 7. 租约锁模型：双超时

| 类型 | 判据 | 默认 | 谁会触发 | 后果 |
|---|---|---|---|---|
| **失联** | `now - lock_last_seen_at > LOCK_HEARTBEAT_GRACE_SEC` | **120s** | 心跳断流（崩溃/断电/断网） | 锁可被同工位任意机台接管；**不计产品失败** |
| **硬超时** | `now - lock_acquired_at > stations.timeout_sec` | 1800s（按工位配置） | 测试真跑了 30min | 锁释放 + **计一次失败** |

关键：**心跳只刷新 `lock_last_seen_at`，不延长 `lock_acquired_at`**。
因此长测试可以跑满 30 分钟不被接管；崩溃机台 2 分钟内锁即可被回收（不再是"卡 30 分钟"）。

服务端另有后台任务（Sweeper）每 `SWEEPER_INTERVAL_SEC`（默认 30s）扫描一次，
即使无人抢锁，也会把失联锁自动置回 `IDLE`。

**防脏写（fencing）**：每次进站/接管都会换发新的 `lock_token`。
旧持锁方持旧 token 出站或上报断点，一律 `403 lock_invalid`。

---

## 8. 崩溃续测与锁接管

### 8.1 崩溃 → 原机台续测（推荐路径）

```
上位机                                       服务端
  │ 进站 (case_ids=[A,B,C,D,E])
  │──────────────────────────────────────────▶ session=S1, token=T1, attempt=1
  │ checkpoint(A)                              checkpoint={A}
  │──────────────────────────────────────────▶
  │ ✗ 进程崩溃（锁与断点遗留在服务端）
  │
  │ 重启后进站，携带 resume_session_id=S1
  │──────────────────────────────────────────▶ session=S1, token=T2(新), attempt=2
  │                                            resume.completed_case_ids=[A]
  │ checkpoint(B..E)  ← 跳过 A
  │──────────────────────────────────────────▶ checkpoint={A,B,C,D,E}
  │ 出站 items=[B,C,D,E]
  │──────────────────────────────────────────▶ 201，checkpoint 补齐 A
                                               → checkpoint_merged_count=1
```

### 8.2 崩溃 → 备用机台接管

```
机台 A 持锁崩溃
  │
  ├─ 已超过失联宽限(120s) ──▶ 机台 B 直接进站成功，takeover=true, takeover_from=A
  │                            （新会话，不继承断点——更安全）
  └─ 未超过宽限 ────────────▶ 机台 B 进站 409 lock_conflict
                               → 运维强制解锁 → 机台 B 进站接管

机台 A 恢复后用旧 token 出站 ──▶ 403 lock_invalid（结果作废，数据未被污染）
```

> 换机台接管时服务端**不继承断点**：新机台无法确认旧机台的进度是否可信，
> 宁可重测也不接受可疑数据。

---

## 9. 幂等与上传闭环

### 9.1 checkout_id

- 由**客户端**在进站时生成（UUID hex 即可），写入本地断点文件
- 网络重试、断网补传一律复用同一值
- 服务端按 `test_records.executed_items.checkout_id` 回溯近 200 条记录：
  命中即回放既有回执，`idempotent_replay: true`，**不产生第二条账目**

### 9.2 checkpoint 幂等

按 `case_id` 去重覆盖：同一用例重复上报以最后一次为准，`merged_count` 为合并后总数。
因此"重复补传"是安全操作，客户端无需做去重。

### 9.3 放行准则

```
出站返回 201 且 acknowledged=true   → 放行
出站超时/网络失败                   → 复用同一 checkout_id 重试，或 GET /client/ack 确认
两者都失败                          → 红灯死锁，禁止拔线，转人工处理
```

---

## 10. 客户端实现规范

### 10.1 心跳线程

```python
while not stop.wait(interval):          # interval = heartbeat_interval_sec / 2
    try:
        data = cli.heartbeat()
        if not data.get("holding_lock"):
            stop_testing(); break       # 锁已被接管
    except ApiError as e:
        if e.is_lock_invalid:
            stop_testing(); break
        # 网络抖动：继续重试，不要退出
```

- 心跳**不能**替代 checkpoint：心跳只证明"活着"，不保存进度
- 心跳失败不要立刻放弃：连续失败超过 `heartbeat_grace_sec` 才会被判定失联
- 正常出站/释放前**先停心跳线程**，避免与服务端状态竞争
- 线程停止事件不可命名为 `_stop`（会覆盖 `threading.Thread` 内部同名方法导致 `join()` 崩溃）

### 10.2 会话被运维中止时的停机约定（必须实现）

运维更换测试用例清单前会调用 `POST /api/admin/sessions/abort-running` 批量中止会话。
**服务端无法杀掉上位机进程**，只能靠协作式停机：

1. 会话被中止 → 工位锁释放、在制品回 `IDLE`（**不计失败**，这点与 `missing_mandatory` 的安全卡控不同）
2. 上位机下一次心跳收到 `holding_lock=false`（≤ `heartbeat_interval_sec`）
3. SDK 心跳线程置位 `lost_lock`，并触发构造参数 `on_lost_lock` 回调
4. 此后 `checkpoint` 会收到 `409 session_aborted`，`check-out` 会被 `lock_invalid` 拒绝
5. **上位机必须自己停**：跑完当前用例后停止剩余用例，不要再尝试出站

直接用 `AteClient`（低层参考实现）时，pytest 工程需自行实现如下停机判断；用 `AteRun`
时 `gate` / `finish` 已内置同样判断（抛 `AteExit(3)`），宿主只需转 `pytest.exit`。

pytest 工程接入示例：

```python
import logging
import threading

import pytest

LOGGER = logging.getLogger(__name__)

def run_all(cli, cases):
    for case in cases:
        if cli.lost_lock:                    # 心跳线程已发现锁失效
            pytest.exit("锁已失效：会话被运维中止", returncode=3)
        cli.checkpoint([run(case)])          # 也可能直接抛 409 session_aborted

# 或回调式（无需轮询，但注意线程边界）
# on_lost_lock 由心跳线程调用：直接 pytest.exit() 抛出的 SystemExit 只会杀掉
# 心跳线程，主线程的 pytest 继续跑。必须用 interrupt_main 把 KeyboardInterrupt
# 注入主线程——pytest 原生处理 KeyboardInterrupt，且能打断正在跑的长用例。
def _on_lost_lock(reason: str):
    LOGGER.error("lock lost: %s", reason)
    threading.interrupt_main()

cli = AteClient(url, key, client_id="SZ-L1-CAL-01", on_lost_lock=_on_lost_lock)
```

被中止的件回到 `IDLE` 且未盖章，**重新进站跑一遍即可**，不计失败、不会工程锁定。

### 10.3 断点文件

- 进站后立即写入 `session_id` / `lock_token` / `checkout_id` / `cursor`
- **`state_file` 传目录时按 SN 分文件**（`.ate_state_{sn}.json`）：一台设备中途离站、
  另一台顶上测试时旧断点不被覆盖，设备拿回工位仍可续测；传具体 `.json` 文件则保持
  旧的单文件语义（同一时刻只测一台件的场景）
- 采用"临时文件 + `os.replace`"原子写入，避免崩溃时写坏文件
- **写入失败绝不能中断测试**（Windows 上杀毒/索引占用文件很常见），续测只是优化
- 成功出站后删除该文件
- 出站/释放**失败时必须保留**该文件：`checkout_id` / 待补传断点都在里面，凭同一
  `checkout_id` 重试，服务端按 `(sn, checkout_id)` 幂等回放；文件一丢就可能换新 ID
  重发产生双账

### 10.4 必做与禁做

| 必做 | 禁做 |
|---|---|
| 进站失败（非 `lock_conflict`）立即终止，治具不开电 | 忽略 `403/409` 继续测试 |
| 每用例跑完立即上报 checkpoint | 把所有用例攒到出站才上报（崩溃即丢） |
| 出站后校验 `acknowledged` | 未拿到 ACK 就放行流转 |
| 收到 `lock_invalid` 立即停机 | 用旧 token 反复重试 |
| `checkout_id` 持久化并复用 | 每次重试生成新的 `checkout_id` |

### 10.5 宿主职责分工与接入指引

SDK（`tools/atetest/api.py`，单文件零第三方依赖的产线形态）已覆盖传输层全部职责（重试退避、
断点队列、幂等重传、失锁感知、产物上传），并把会话流程收敛为 `AteRun` 状态机
（`start` / `collect` / `gate` / `finish`）。宿主只写 pytest 钩子与夹具：收集期进站
（写 SN 工位由 `sn_burner` 扫码后延迟进站）、
`makereport` 里调 `collect`、每用例 `gate`、会话结束 `finish`；`AteExit` 一律转
`pytest.exit(message, returncode)`。`tools/client/ate_client.py` 是同一契约的最低层参考实现
（无 `AteRun`，另有 CLI 演示），首次接入可对照两者。完整 conftest.py 骨架见**附录 A**。

**断网重传各场景的分工**（宿主无需自己实现队列）：

| 场景 | 处理者 |
|---|---|
| 秒级抖动 | SDK `HttpClient` 指数退避重试，对调用方透明 |
| 断网数分钟继续测 | `gate()` 内 `checkpoint` 仅对断网/5xx 自动进 `pending_items` 落盘；会话已终止/锁失效/其他 4xx 立即抛出停机 |
| 网络恢复后补传 | 下次 `gate` / `finish` 自动合并队列，服务端按 `case_id` 去重 |
| 出站瞬间断网 | `finish()` 内置 6 次退避重试，复用同一 `checkout_id` |
| 服务端已落库但响应丢失 | 同 `checkout_id` 重试 → 幂等回放既有回执；重试仍失败则 `ack()` 凭 `checkout_id` 查回执，命中即闭环并清断点，不会双账 |
| 断网 + 进程崩溃 | 断点文件恢复会话；服务端拒绝旧会话（`session_completed/session_not_found/session_mismatch`）时丢弃断点，按全新进站重试一次，不再永久卡死 |
| 断网太久锁被回收/接管 | `on_lost_lock` 注入 `KeyboardInterrupt`，`gate`/`finish` 抛 `AteExit(3)` 停机；会话被终止/不属于本机台（`session_aborted/completed/not_found`、`station_mismatch`）同按死会话停机 |

---

### 10.6 非 Python 程序接入（`ate-cli`）

`api.py` 只是省事的客户端封装，**接入点是 HTTP 契约**。对不希望实现网络协议/心跳的独立程序（LabVIEW、C#/Delphi 上位机、bat、厂商自带软件），用 `ate-cli`
（`tools/atetest/cli.py`，可用 PyInstaller 打成单文件 exe）以"子进程 + stdout JSON + 退出码"的方式接入。

#### 10.6.1 三种形态与选型

| 形态 | 适用 | 开销 |
|---|---|---|
| **直连 HTTP** | 程序自带 HTTP 库（C#/Delphi/LabVIEW/C++）、调用频繁 | 最优（无子进程，连接复用） |
| **`ate-cli <子命令>`** | 脚本/运维/低频调用（每工位 3~5 次） | ~170ms/次（进程启动） |
| **`ate-cli serve`** | 能起子进程但不想写 HTTP、调用较频繁 | 仅首次启动，后续每条 ~30ms |

#### 10.6.2 启动与就绪

```bat
set ATE_URL=http://10.1.1.4:8000
set ATE_API_KEY=<V1 API Key>
set ATE_CLIENT_ID=SZ-L9-CAL-99-PARAM
set ATE_STATE_FILE=D:\ate\cal-state.json

ate-cli serve
```

进程启动即输出一行就绪信号（宿主据此判定可发命令）：

```json
{"ok": true, "command": "serve",
 "data": {"ready": true, "state": "D:\\ate\\cal-state.json", "commands": ["..."],
          "resumable": true, "sn": "C020001", "station_id": "CAL-PARAM"}}
```

- `resumable: true` = 上次有未完成会话，直接 `check-in` 即按续测处理（服务端 attempt+1、回传已完成用例）
- 全局参数也可走命令行：`--url/--key/--client/--app/--state/--timeout`；密钥建议只走环境变量（命令行会暴露在进程列表）

#### 10.6.3 协议：一行请求 → 一行响应

**请求**（stdin，每行一个 JSON 对象）：

| 字段 | 说明 |
|---|---|
| `cmd` | `resolve` / `case-list` / `check-in` / `heartbeat` / `checkpoint` / `artifact` / `check-out` / `ack` / `release` / `status` / `sn-status` / `state` / `ping` / `quit` |
| `id` | 任意标识，响应原样回显（用于请求/响应配对） |
| 业务参数 | `sn` `model` `fw` `case`（字符串或数组）`result` `values`（对象或 JSON 串）`duration` `file` `items`（数组或 JSON 串）`reason` `release` `auto_heartbeat` `cases_file` `items_file` |

**响应**（stdout，每行一个 JSON 对象）：

```json
{"ok": true,  "id": 1, "command": "check-in", "exit": 0, "data": {"station_id": "CAL-PARAM", "session_id": "..."}}
{"ok": false, "id": 2, "code": "missing_prereq", "message": "防跳站拦截：...", "data": {"missing": ["CAL-IFACE"]}, "exit": 10}
```

**事件行**（无 `id`，带 `event` 字段，由后台线程主动推送——宿主必须先判 `event`）：

```json
{"event": "lock_lost", "code": "lock_invalid", "exit": 11}
{"event": "released_on_eof", "sn": "C020001", "exit": 0}
```

**退出码语义**（响应里的 `exit` 与一次性子命令的进程退出码一致）：

| 码 | 含义 | 宿主动作 |
|---|---|---|
| `0` | 放行 | 继续 |
| `10` | 业务拦截（重号/跳站/固件/漏测/序列不符…） | 停机、禁止烧录/校准，按 `code` 分支提示 |
| `11` | 锁失效（被接管/运维中止） | **立即停机**，结果作废 |
| `12` | 网络不可达 | 可重试 |
| `1`/`2` | 其它错误 / 参数错误 | 检查调用 |

#### 10.6.4 完整示例（DC/AC 校准工位）

一个工位 = 一次会话；DC/AC 是**同一会话的两个用例**（各自进站会被 `station_already_passed` 复测拦截）。

```
→ {"id":1,"cmd":"check-in","sn":"C020001","model":"DPO4054B","fw":"V3.20","case":["cal_dc","cal_ac"]}
← {"ok":true,"id":1,"exit":0,"data":{"station_id":"CAL-PARAM","session_id":"...","completed_case_ids":[]}}

（DC 校准完成后）
→ {"id":2,"cmd":"checkpoint","case":"cal_dc","result":"PASS","duration":1200,"values":{"gain":1.02}}
← {"ok":true,"id":2,"exit":0,"data":{"merged_count":1,"reported":1}}
→ {"id":3,"cmd":"artifact","case":"cal_dc","file":"D:\\logs\\dc.csv"}
← {"ok":true,"id":3,"exit":0,"data":{"object_key":"...","size":1024}}

（AC 校准完成后）
→ {"id":4,"cmd":"checkpoint","case":"cal_ac","result":"PASS"}
← {"ok":true,"id":4,"exit":0}

→ {"id":5,"cmd":"status"}
← {"ok":true,"id":5,"exit":0,"data":{"active":true,"holding_lock":true,"remaining_sec":120,"completed_count":2}}

→ {"id":6,"cmd":"check-out","duration":120000}
← {"ok":true,"id":6,"exit":0,"data":{"acknowledged":true,"record_id":411,"overall_result":"PASS"}}

→ {"cmd":"quit","release":true}
← {"ok":true,"exit":0,"data":{"released":false,"active_session":false}}
```

任一响应 `exit != 0` → 宿主立即中断（尤其是 `check-in` 被拦时，**绝不能**再写 SN/EEPROM）。

#### 10.6.5 宿主代码（C#）

```csharp
var psi = new ProcessStartInfo("ate-cli.exe", "serve") {
    RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true,
    UseShellExecute = false, StandardOutputEncoding = Encoding.UTF8 };
var p = Process.Start(psi);
p.OutputDataReceived += (s, e) => { if (e.Data != null) Handle(e.Data); };
p.BeginOutputReadLine();

void Send(string json) { p.StandardInput.WriteLine(json); p.StandardInput.Flush(); }

void Handle(string line) {
    var doc = JsonDocument.Parse(line);
    if (doc.RootElement.TryGetProperty("event", out _)) { OnEvent(doc); return; }   // 事件行
    var id   = doc.RootElement.GetProperty("id").GetInt32();
    var exit = doc.RootElement.GetProperty("exit").GetInt32();
    if (exit != 0) Abort(line);                                   // 拦截即停机
    OnResponse(id, doc);
}
```

一次性形态（脚本/低频）直接看进程退出码：

```csharp
var r = Run("ate-cli", "check-in --sn S1 --model DPO4054B --fw V3.20");
if (r.ExitCode != 0) Abort(r.Stdout);     // 10 = 业务拦截，12 = 网络不可达
```

#### 10.6.6 心跳生命周期（serve 自动保活，宿主无需干预）

| 阶段 | 行为 |
|---|---|
| 启动 | `check-in` 成功后**自动**启动后台心跳线程（daemon），间隔 = 服务端 `heartbeat_interval_sec × 0.5`（默认 15s）；无需再发 `heartbeat` |
| 运行 | 线程持续 `POST /heartbeat`；网络抖动自动重试；**不写 state 文件**（与主线程无写冲突） |
| 失锁 | `holding_lock=false` 或 403 `lock_invalid` → 线程退出并**主动推事件** `{"event":"lock_lost","exit":11}`；此后每条响应附 `"lock_lost": true` |
| 停止 | `check-out`/`release` 内部先停线程再出站；`quit` 显式停线程；进程退出时 daemon 线程随之结束（服务端由租约兜底判失联） |
| 换件 | 再次 `check-in` 先回收旧线程，避免双心跳 |
| 直通机型 | 不建会话、不启动心跳（零写入） |
| 自管心跳 | `check-in` 带 `"auto_heartbeat": false`（命令行为 `--no-heartbeat`），由宿主按需发 `heartbeat` |

实测：`check-in` 后 15s 服务端 `lock_heartbeat_count` 0→1；`quit` 后再等 15s 计数与 `lock_last_seen_at` 不变（心跳已停）；`--no-heartbeat` 进站 17s 后计数保持 0。

#### 10.6.7 退出语义（三种情况必须区分）

| 场景 | 行为 | 服务端结果 |
|---|---|---|
| `{"cmd":"quit"}`（默认） | 停心跳、**保留会话与 `--state`**，响应带 `active_session:true` + 提示 | 会话仍 TESTING（可续测），锁超时后失效 |
| `{"cmd":"quit","release":true}` | 先 `release` 再退出 | 锁立即释放，state 文件清理 |
| stdin 关闭（宿主崩溃/被杀） | **自动 `release`** 并推 `{"event":"released_on_eof"}` | 锁立即释放，避免僵尸锁 |

实测：`quit` 默认后 DB 仍 `TESTING` 且 state 保留，重启 serve 的 ready 行返回 `resumable:true`；`quit(release)` 后状态回 `IDLE` 并清理 state；stdin 关闭触发 `released_on_eof`。

#### 10.6.8 命令与参数总表

| 命令 | 关键参数 | 说明 |
|---|---|---|
| `resolve` | — | 机台身份上报与工位反查（首次自动注册） |
| `case-list` | `--model`、`--case`/`--cases-file` | 录制态草稿上报（check-in 之前） |
| `check-in` | `--sn --model --fw`、`--case`/`--cases-file`、`--no-resume`、`--no-heartbeat` | 进站领锁；返回 `session_id/lock_token/station_id` |
| `heartbeat` | `--loop --interval` | 单次或循环保活（失锁退出码 11） |
| `checkpoint` | `--case --result --duration --values`，或 `--items`/`--items-file` | 增量上报已完成用例（可逐段落断点） |
| `artifact` | `--case --file` | 上传产物（日志/波形/截图） |
| `check-out` | `--items`/`--items-file`、`--duration`、`--reason` | 出站：补齐断点 + 漏测拦截 + 盖章 |
| `ack` | `--sn --checkout-id` | 出站闭环校验（网络抖动补确认） |
| `release` | `--reason` | 主动放弃锁 |
| `status` | — | 聚合：本地会话 + 服务端 `holding_lock`/剩余租约 |
| `sn-status` | `--sn` | 登记状态（测试服/生产服路由判定） |
| `state` | `--clear` | 查看/清除本地会话状态 |
| `serve` | 全局参数同上 | 常驻 JSON-lines 模式 |
| `--version` | — | 打印 CLI 版本 |

#### 10.6.9 实用要点

1. **一个进程一个会话**：多工位并行 = 启动多个 `ate-cli serve`，各用不同 `--state`（不做进程内多会话）
2. **崩溃恢复**：重启 serve → ready `resumable:true` → 再发 `check-in` 即续测（已完成用例回传跳过）
3. **编码**：CLI 启动时把 stdin/stdout/stderr 重配为 UTF-8；宿主按 UTF-8 读取即可
4. **复测约束**：同一工位一次会话只能 `check-in` 一次，子流程（DC/AC…）必须作为同会话的多个用例
5. **防呆前置**：`check-in` 的 `exit != 0` 必须视为"禁止烧录/校准"，服务端拦截在写硬件之前
6. **参数体积**：nodeid 多或 items 大时用 `--cases-file` / `--items-file`，规避 Windows 命令行 8191 字符上限

打单文件 exe：`cd tools\atetest; pyinstaller -F -n ate-cli cli.py`（生成 `dist/ate-cli.exe`）；
若嫌 onefile 每次解包慢，改用 `-D`（目录模式）分发。

## 11. 管理端运维接口（JWT）

前置：`POST /api/auth/login {"username","password"}` → `access_token`，
后续请求带 `Authorization: Bearer <token>`。

| 接口 | 说明 |
|---|---|
| `GET /api/admin/sessions` | 会话清单，支持 `sn / station_id / client_id / status / abnormal_only / date_from / date_to / page / page_size` |
| `GET /api/admin/sessions/zombie-locks` | 失联僵尸锁清单（可被接管/强制解锁） |
| `GET /api/admin/sessions/{id}` | 会话详情，含 checkpoint 明细（"跑到哪崩的"） |
| `POST /api/admin/sessions/{id}/abort` | 强制终止会话并解锁（body: `{"reason": "..."}`） |
| `POST /api/admin/sessions/abort-running` | 批量终止运行中的会话；body 至少给一个过滤条件 `station_id / process_id / sn / client_id`（否则 400 `scope_required`），支持 `dry_run` 预演 |
| `POST /api/admin/products/{sn}/force-release` | 强制解锁（body: `{"reason": "..."}`），不动印章与失败计数 |
| `POST /api/admin/repairs` | 单条处置：`{"sn", "repair_action"(RETEST/ROLLBACK/RESET/SCRAP), "target_station"?, "reason"?, "confirm_downstream"?}`。守卫：报废件 403 `product_scrapped`、持锁 409 `product_holding_lock`、RETEST 未盖章工位 400 `station_not_passed`；RETEST 命中依赖重测工位（`invalidates_downstream`）且下游有已盖章工位时，未确认 409 `downstream_invalidation_required`（`data.stations` 回显依赖重测工位清单），`confirm_downstream=true` 确认后下游印章与记录一并作废。处置破坏完工态时自动作废该 SN 报告（MinIO/本地副本清除，已上传 MES 的副本按规则 `mes_void_url` 联动作废，未配置转人工） |
| `POST /api/admin/repairs/batch` | 批量处置（仅 RETEST/ROLLBACK）：`{"items": [sn...], "repair_action", "target_station", "reason", "dry_run", "confirm_downstream"?}`；dry_run=true 只守卫不落库并回显破坏面（机型/流程/当前状态/将撤销印章/将作废记录数）；RETEST 命中依赖重测工位时未确认 409 `downstream_invalidation_required`，确认后级联生效；逐条独立生效，失败项不影响已成功项；上限 50；非 dry_run 响应携带 `batch_id` 供批量撤销 |
| `POST /api/admin/repairs/batch-undo` | 批量撤销：`{"batch_id"}`，按快照逐条独立还原整批处置（每条走单条撤销全部守卫：SCRAP/最新一条/零后续动作/未撤销），失败项回显原因不影响已成功项 |
| `POST /api/admin/repairs/{repair_id}/undo` | 撤销维修处置（窄窗口）：按处置前快照精确还原印章/失败计数/状态，被作废的台账记录按快照 ID 精确恢复；完工态恢复后自动重出报告。边界（409）：仅 RETEST/ROLLBACK/RESET（`action_not_undoable`，SCRAP 永不可撤）、仅该 SN 最新一条（`not_latest_disposition`）、处置后无新会话且印章未变（`repair_state_changed`）、不可重复撤销（`already_undone`） |
| `GET /api/admin/repairs` | 履历筛选扩展：`technician`（操作人模糊）、`undone`（撤销状态 true/false）、`date_from`/`date_to`（日期区间，YYYY-MM-DD，按本地日界） |
| `GET /api/admin/products/{sn}/sessions` | 该 SN 的会话时间线 |
| `GET /api/admin/products` | 在制品清单，支持 `zombie_only=true` 过滤失联锁、`date_from`/`date_to`（按最近活动时间） |
| `GET /api/admin/metrics/overview` | 运行概况，含 `locks: {active, zombie, sessions_running, sessions_abnormal}` |
| `GET /api/admin/clients` | 机台清单（在线状态 + 持锁 SN），支持 `date_from`/`date_to`（按注册时间） |
| `POST /api/admin/clients` | 注册机台并绑定工位 |
| `GET /api/admin/records/{record_id}/files/{index}` | 测试产物下载（结果文件/日志等；返回 MinIO 预签名 URL JSON；MinIO 未配置时 503） |
| `GET /api/admin/sn-registry` | 试验设备登记清单（按登记时间倒序） |
| `POST /api/admin/sn-registry` | 登记试验设备（body: `{"sn", "note"?}`；重复登记 409 `sn_already_registered`；`registered_by` 记操作人） |
| `DELETE /api/admin/sn-registry/{sn}` | 注销SN 登记：仅删除登记行、恢复进站资格，**不触碰任何生产/测试记录**。"是否曾运行研发代码"只有实验室知道，重流线由工艺员显式走维修处置 RESET（清印章/作废报告/履历留痕）；被动过的设备回产线会被固件基线 403 与用例判定自动拦下，无静默出货路径 |

---

## 12. 服务端配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `V1_API_KEY` | 空 | 通道一密钥；留空则仅允许网页 JWT 调试 |
| `LOCK_HEARTBEAT_GRACE_SEC` | 120 | 失联判定窗口；**新机台最快多久能接管** |
| `LOCK_HEARTBEAT_INTERVAL_SEC` | 30 | 下发给上位机的建议心跳间隔 |
| `SWEEPER_INTERVAL_SEC` | 30 | 孤儿锁回收周期 |
| `SWEEPER_ENABLED` | true | 回收总开关（演示场景数据可置 false） |
| `LOST_LOCK_FAIL_THRESHOLD` | 3 | 连续失联达 N 次才计一次产品失败 |
| `MERGE_CHECKPOINT_ON_CHECKOUT` | true | 出站时用断点补齐未提交用例 |
| `STRICT_LOCK_TOKEN` | false | true 时"未携带 token"也拒绝（老上位机需先升级）；**token 不匹配始终拒绝** |
| `FAIL_LIMIT` | 3 | 连续失败达阈值 → 工程锁定 |
| `SERVER_ROLE` | `production` | SN 准入极性：`production`（黑名单：登记的试验设备进站 403 `sn_is_test_device`）/ `development`（白名单：未登记 SN 进站 403 `sn_not_registered_test_device`）。归一化小写，除 `development` 外的一切值均按生产黑名单语义卡控（Fail-Safe） |
| `ENFORCE_FW` | true | 固件基线校验 |
| `ENFORCE_APP_SW` | true | 测试软件版本基线校验（机型基线留空则不卡控） |
| `ENFORCE_CASE_IDS` | true | 进站时比对待执行的用例ID清单 |
| `CLIENT_ONLINE_WINDOW_SECONDS` | 90 | 机台在线判定窗口 |

### 12.1 SN 准入极性与机型双态（SERVER_ROLE / passthrough）

同一套代码部署生产/测试两台服务器，准入语义由 `.env` 的 `SERVER_ROLE` 决定，全部卡控收敛在服务端，SDK 零改动仅透传 403：

| 角色 | 登记表（`sn_registry`）语义 | 进站行为 |
|---|---|---|
| `production`（默认，Fail-Safe） | **黑名单**：登记的试验设备 | 进站 403 `sn_is_test_device`，建档前拦截零痕迹 |
| `development` | **白名单**：仅登记设备可测 | 未登记 SN 进站 403 `sn_not_registered_test_device` |

- 极性校验在**建档之前**且**先于机型直通**（身份不受流程豁免）；登记/注销走 `/api/admin/sn-registry`（写操作 require_admin）。
- **机型双态（passthrough）**：直通机型进站直接 ACK 零写入（响应 `passthrough=true`，`station_id`/`rules`/`session_id` 为空），SDK 跳过心跳/断点/出站；身份卡控不豁免。
- **模式切换**：关直通需拓扑齐备（409 `model_topology_missing`）+ 清线确认位 `confirm_drain=true`（409 `drain_confirm_required`）；开直通需无在制（409 `model_has_wip`）。
- **注销义务链**：注销仅解除登记，不触碰任何记录；若设备曾运行研发测试代码，重新入库前由工艺员走维修处置 RESET 重新流线（被动过的设备回产线会被固件基线 403 与用例判定自动拦下）。
- **上位机路由判定（V1 只读查询）**：`GET /api/v1/client/sn-registry/status?sn=XXX`（X-API-Key）→ `data.registered`（true = 试验设备，应走研发服；false = 产线件，走生产服）。只返回布尔值，不暴露登记清单与备注；两端的进站守卫（黑名单/白名单）仍是最终卡控，误路由只会得到 403。登记动作仍在管理端「SN 登记」（require_manager），本端点不提供任何设置能力。

---

## 13. 联调与验收

### 13.1 单线程演示（`ate_client.py`）

```powershell
# 1) 准备数据
scripts\seed.bat --products 60

# 2) 启动后端（观察僵尸锁时可关闭自动回收）
$env:SWEEPER_ENABLED="false"
scripts\dev-backend.bat

# 3) 另开终端（api-key 取 backend/.env 的 V1_API_KEY）
python tools/client/ate_client.py --base-url http://127.0.0.1:8000 --api-key <KEY> demo
```

| 场景 | 参数 | 验收点 |
|---|---|---|
| 正常全流程 | `normal` | 5 个断点上报，出站 201，`checkpoint_merged_count=0` |
| 防跳站拦截 | `gate` | 未做首站直接进第二站 → `403 missing_prereq`；补做后放行 |
| 崩溃续测 | `resume` | `attempt=2`，跳过 1 个用例，`checkpoint_merged_count=1` |
| 备用机台接管 | `takeover` | 强制解锁后接管成功；旧 token 出站 → `403 lock_invalid` |
| 孤儿锁回收 | `sweep` | 留一把锁在服务端，Web 端「测试会话 → 僵尸锁」可见 |
| 测试产物归档 | `artifact` | 逐用例 multipart 上传产物（`/client/artifact`），出站并入 `executed_items.items[].files`；Web 台账展开该记录点击产物 chip 经预签名 URL 下载（需服务端已配置 MinIO） |

单场景调试可加 `-v` 输出 SDK 调试日志。

### 13.2 多机台并发验证（`line_simulator.py`）

```powershell
python tools\client\line_simulator.py --api-key <KEY> --mode normal --units 15
python tools\client\line_simulator.py --api-key <KEY> --mode chaos --crash-rate 0.12
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--clients` | 2 | 每工位机台数量（自动注册为 `SZ-L9-<工段>-<NN>[-<工位>]`，符合服务端编号规则） |
| `--units` | 12 | 投产被测件数量 |
| `--model` | DPO4054B | 机型（自动取其流程与基线固件） |
| `--station` | 空 | 不传 = 全流程逐站推进；指定工位则进入单工位模式 |
| `--items` | 0 | 每件的测试项数量（0 = 全量必测项） |
| `--mode` | chaos | `normal` 仅关闭崩溃注入；`chaos` 随机注入崩溃 |
| `--crash-rate` | 0.12 | 每个测试项执行后的崩溃概率 |
| `--fail-rate` | 0.08 | 不良件比例 → 判 FAIL / 连败工程锁定 |
| `--missing-rate` | 0.05 | 漏报必测项比例 → 400 missing_mandatory |
| `--bad-fw-rate` | 0.05 | 非基线固件比例 → 403 firmware_mismatch |
| `--jump-rate` | 0.05 | 跳站比例 → 403 missing_prereq |
| `--abandon-rate` | 0.05 | 中途主动放弃比例 → 停在半途 |
| `--stop-rate` | 0.25 | 过站后停线比例 → 停在半途（下班/待处理） |
| `--artifact-rate` | 0 | 每个测试项后按概率 multipart 上传一个模拟产物（`/client/artifact` 真连 MinIO 归档，出站自动并入 `executed_items.items[].files`，Web 台账/追溯可下载）；上传失败不阻断测试。`sim_local.py` 检测到 `.env` 配置了 MinIO 时默认 0.3（`SIM_ARTIFACT_RATE` 可调） |
| `--crash-handoff` | 关 | 崩溃后由备用机台接管（而不是原机台续测） |
| `--repair-locked` | 关 | 结束后对工程锁定件自动送修回流（有印章 RETEST 重测该工位 / 一件未过 RESET 重来）并重新投产复测 |
| `--no-chaos-resume` | 关 | 崩溃后不续测，直接换件 |
| `--seed` | 随机 | 固定随机种子（复现某一轮结果） |

`--fail-rate` 等注入参数在 `normal` 模式下依然生效（normal 只关崩溃注入）。

输出含每台机台的 `出库 / 崩溃 / 续测 / 接管 / 失败` 统计与服务端侧校验
（活跃锁、僵尸锁、运行会话、异常会话）。实测 5 台 × 5 项 × 15 件约 5 秒跑完。

### 13.3 场景数据集（seed 内置，SN 前缀 `C0990xx`）

| SN | 场景 | 页面表现 |
|---|---|---|
| C099001 | 正常持锁，心跳 20s 前，断点 3/5 | 锁状态绿色"持锁" |
| C099002 | 僵尸锁，心跳 300s 前 | 红色"失联·可接管"，可强制解锁 |
| C099003 | 崩溃过一次，正在续测 attempt=2 | 会话页 `attempt>1` 标记 |
| C099004 | 硬超时：持锁 35min、心跳仍在 | 出站时 `403 lock_expired` |
| C099005 | 历史：崩溃后续测成功出库 | 1 条 ABORTED + 1 条 COMPLETED |
| C099006 | 历史：被备用机台 SZ-L1-CAL-09 接管 | TAKEN_OVER + RUNNING |
| C099007 | 连续失联 3 次 → 已计一次失败 | 3 条 ABORTED |
| C099008 | 历史：硬超时终止（计失败） | EXPIRED |

> 只重建场景数据（不动其余）：`python -m app.seed --scenarios`
> 失联锁与硬超时锁会被后台任务自动释放，需常驻可见请以 `SWEEPER_ENABLED=false` 启动。

---

## 14. 附录

### 附录 A：完整接入示例（conftest.py）

产线 pytest 工程把 `tools/atetest/api.py`（产线形态：传输层 + `AteRun` 会话状态机，零第三方
依赖）拷为工程内模块，再按本附录写一个 conftest；`tools/client/ate_client.py` 是同一契约的
最低层参考实现（无 `AteRun`，另有 CLI 演示），`tools/atetest/` 是可直接运行的自测工程。

```python
# conftest.py
import os

import pytest

import api as ate                          # tools/atetest/api.py

# ---------- 0. 连接参数与身份 ----------
ate.ATE_URL = "http://10.0.0.10:8000"      # 留空 = 不对接，AteRun 全部旁路
ate.ATE_KEY = os.environ["ATE_API_KEY"]    # 与服务端 V1_API_KEY 一致
ate.CLIENT_ID = "SZ-L1-CAL-01"             # 与 Web 端「机台管理」的工位绑定一致

WRITE_SN = os.environ.get("ATE_WRITE_SN") == "1"   # 写 SN 工位：延迟进站


def read_identity():
    """返回 (sn, model, fw)。普通工位 *IDN? 直读；写 SN 工位 SN 留空，扫码后再进站。

    收集钩子早于所有会话夹具，身份必须在收集期可读。仪器只能在夹具里初始化的工程，
    请改用 sn_burner 式延迟进站（由夹具返回 SN 后再调用 run.start）。
    """
    if WRITE_SN:
        return "", "DPO4054B", "V3.20"     # 机型/固件来自工装配置
    idn = inst.query("*IDN?")              # inst = 工程仪器实例
    # TEKTRONIX,DPO4054B,C020001,CF:91.1CT,FV:V3.20
    _vendor, model, serial, fw = [p.strip() for p in idn.split(",")]
    return serial, model, fw


SN, MODEL, FW = read_identity()
_ate_run = None


def _checked_in(run) -> bool:
    """是否已进站，以 client.state 为准。run.sn 在 check_in 之前就已赋值，进站被
    拦截时仍非空，据此出站会反复撞 check_out before check_in。"""
    return bool(run is not None and run.client is not None and run.client.state is not None)


# ---------- 1. 结果收集：setup/teardown 失败也要收 ----------
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    """异常用例若"凭空消失"，出站时 missing_mandatory 会把整个工位卡死。"""
    outcome = yield
    if _ate_run is not None:
        _ate_run.collect(item, outcome.get_result())


# ---------- 2. 普通工位：收集结束即进站 + 续测跳过 ----------
@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    """必须早于任何用例的 setup：会话夹具里打 skip 会漏掉第一个用例。"""
    global _ate_run
    if not items or config.option.collectonly:
        return
    _ate_run = ate.AteRun(ate.create_client())
    if not SN:
        return                             # 写 SN 工位：等 sn_burner 扫码
    try:
        # 必须携带完整 collection；-k 部分运行会被 case_id_mismatch 拦截（防漏测属预期）
        done = _ate_run.start(items, sn=SN, model=MODEL, fw=FW)
    except ate.AteExit as e:
        pytest.exit(e.message, returncode=e.returncode)
    for item in items:
        if item.nodeid in done:            # 只跳过上轮已 PASS 的，FAIL 的照常重跑
            item.add_marker(pytest.mark.skip(reason="ATE 续测：该用例已通过，跳过"))


# ---------- 3. 会话收尾：判必测 + 出站 ----------
@pytest.fixture(scope="session", autouse=True)
def project_session_start():
    yield
    if _checked_in(_ate_run):              # 未进站 = 没有会话，无需出站
        try:
            _ate_run.finish()              # 必测 FAIL → 保留会话不出站（退出码 2）
        except ate.AteExit as e:
            pytest.exit(e.message, returncode=e.returncode)


# ---------- 4. 每用例收尾：checkpoint + 失锁自检 ----------
@pytest.fixture(autouse=True)
def ate_case_gate(request):
    yield
    if not _checked_in(_ate_run):
        return                             # 未进站阶段只本地累积，进站后首次 gate 补报
    try:
        # 可选产物：换成仪器截图/波形；上传失败只记日志，不阻断测试
        # _ate_run.upload_artifact(request.node.nodeid, path="D:/ate/shots/shot.png")
        _ate_run.gate()
    except ate.AteExit as e:
        pytest.exit(e.message, returncode=e.returncode)


# ---------- 5. 写 SN 工位专用：前置检查 → 扫码 → 进站 → 才允许烧录 ----------
@pytest.fixture
def sn_burner(request):
    def _scan_and_checkin(scan_func=None) -> str:
        bad = [nid for nid, res in getattr(_ate_run, "results", {}).items() if res == "failed"]
        if bad:
            pytest.fail(f"前置自检未通过，禁止扫码写号：{bad}")
        sn = (scan_func() if scan_func else input("\n[请扫描产品 SN]: ")).strip()
        if not sn:
            pytest.fail("扫码失败：SN 不能为空")
        try:
            _ate_run.start(request.session.items, sn=sn, model=MODEL, fw=FW)
        except ate.AteExit as e:
            pytest.exit(f"防呆拦截（禁止烧录）: {e.message}", returncode=e.returncode)
        return sn
    return _scan_and_checkin
```

写 SN 用例：

```python
def test_burn_sn(sn_burner):
    sn = sn_burner()                       # 服务端拦截在此中止，绝不烧录
    hardware.write(sn)                     # 进站成功后才允许动烧录硬件
    assert hardware.readback() == sn
```

关键点：

1. **进站时机**：普通工位在 `pytest_collection_modifyitems`；写 SN 工位在前置用例跑完、
   扫码取得 SN 后（`sn_burner`）。续测 skip 必须在任何用例 setup 之前打上。
2. **身份时序**：收集钩子早于会话夹具，`*IDN?` 必须此时可读；否则改用延迟进站。
3. **全量 collection**：`-k` / `--deselect` 部分运行会被 `case_id_mismatch` 拦截（防漏测）；
   pytest 收集顺序即上报顺序，须与 `rules`（`sort_order`）一致，否则 `case_order_mismatch`。
   工位基线锁定后升级为全列表严格比对（含顺序与增删），与受控基线的任何差异均阻断，
   `data` 携带缺失/多余/首个差异下标；解锁重录走管理端基线端点（延迟进站模式配合 `sn_burner`）。
4. **`AteExit` → `pytest.exit`**：SDK 只返回"干净停机决策"，结束进程由宿主执行。

`AteExit.returncode` 约定（仅 ATE 主动终止时）：

| returncode | 含义 | 服务端 |
|---|---|---|
| 1 | 进站/出站防呆拦截、未拿到 ACK、身份为空 | 进站被拦 = 不产生本轮会话；出站被拦 = 会话与断点保留 |
| 2 | 必测项 FAIL，不出站 | 会话保留，`.retry_<SN>.json` 让重跑只执行未通过项 |
| 3 | 会话被运维中止 / 锁失效 | 本轮结果作废，重新进站 |
| 4 | 网络不可达、锁长期不释放、出站重试耗尽 | 断点文件保留，恢复后重新进站续测 |

> pytest 自身的用例失败也返回 1，需区分时看退出消息或本地日志。

### 附录 B：字段映射表

| 现有上位机工程 | ATE 服务端 |
|---|---|
| `report["serialNumber"]` / `["model"]` / `["version"]`（`*IDN?`） | check-in 的 `sn` / `product_model` / `firmware` |
| `report["data"][*]["nodeid"]` | `case_id` |
| `rep.outcome`（passed / failed / skipped） | `PASS` / `FAIL` / `SKIP` |
| `pytest_runtest_makereport` | `AteRun.collect` 本地累积结果 |
| `ate_case_gate`（function/autouse） | `AteRun.gate()`：批量 `checkpoint` + 失锁自检 |
| `pytest_collection_modifyitems`（`trylast`） | 普通工位：`AteRun.start` 进站，并按返回的 `done` 打续测 skip |
| `sn_burner`（写 SN 用例） | 写 SN 工位：前置检查 → 扫码 → `AteRun.start` 延迟进站 |
| `project_session_start`（session/autouse） | 出站 `AteRun.finish()`（必测判定 + check-out） |
| `action()` 线程收到 `abort` | `release` 主动放弃锁 |

### 附录 C：完整调用序列

普通工位（SN 在收集期已知）：

```
resolve ─▶ check-in ─┬─▶ heartbeat ─┐
                     │              │  （后台周期调用，直到出站/释放）
                     └─▶ checkpoint ┘  （每用例一次，可批量补传）
                              │
                              ├─▶ check-out ─▶ 201 ACK ─▶ 拔线流转
                              ├─▶ release    ─▶ 放弃本次测试（不计失败）
                              └─▶ ack        ─▶ 断网后确认落库
```

写 SN 工位（进站前没有 SN）：

```
离线前置用例 ─▶ 本地累积结果（不发请求、无会话）
                     │  任一 FAIL → 禁止扫码
                     ▼
               扫码取得 SN ─▶ resolve + check-in
                     │              │
                     │              └─ 拦截（重号/跳站/极性/基线/锁）→ 退出码 1，不烧录
                     ▼
               写出硬件 SN ─▶ 首次 checkpoint 批量补报前置结果
                     │
                     └─▶ 后续用例与普通工位相同（heartbeat / gate / finish）
```
