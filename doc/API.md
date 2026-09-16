# ATE Manager 上位机接口

> **通道一（`/api/v1/*`）** —— 面向产线上位机（pytest 测试工程）的机器接口。
> Web 管理端接口（`/api/admin/*`）见 [第 11 章](#11-管理端运维接口jwt)，仅供运维台调用。

| 项 | 值 |
|---|---|
| 文档版本 | v1.0 |
| 适用服务端 | ATE Manager API ≥ 1.0.0 |
| 接口前缀 | `/api/v1` |
| 鉴权方式 | 请求头 `X-API-Key` |
| 传输协议 | HTTP/1.1 + JSON（UTF-8） |
| 配套代码 | `tools/ate_client.py`（SDK + 演示）、`tools/line_simulator.py`（多机台并发验证） |

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

> 若服务端未配置 `V1_API_KEY`，可退化为网页 JWT（`Authorization: Bearer <token>`），
> 仅供开发调试，**产线禁止**。

### 2.2 注册机台并绑定工位

机台（物理工控机）必须先与逻辑工位绑定，否则进站返回 `403 client_not_bound`。
由运维在 Web 端「机台管理」完成，或用管理端接口注册：

```http
POST /api/admin/clients
{"client_id": "SZ-L1-CAL-01", "station_id": "CAL-PARAM", "ip_address": "10.1.60.11"}
```

也可用 `POST /api/v1/client/resolve` 自动注册（此时 `station_id` 为空，仍需 Web 端补录绑定）。

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

调用顺序：`resolve` → `check-in` → (`heartbeat` ∥ `checkpoint`) → `check-out` → 必要时 `ack` / `release`。

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

### 5.2 `POST /api/v1/client/check-in` — 进站（领取工位锁）

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
  "rules": [                           // 服务端下发的执行规则
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
| 403 | `client_not_bound` | 机台未绑定工位 | 联系运维绑定 |
| 403 | `model_not_registered` | 机型未注册 | 联系工艺工程师 |
| 403 | `missing_prereq` | **跳站拦截**：前置工位未完成 | 弹告警、pytest 终止、治具不开电 |
| 403 | `model_mismatch` | SN 已登记为其他机型 | 贴错机型/换线未清线 |
| 403 | `firmware_mismatch` | 固件不满足基线（口径见机型 `fw_match_rule`） | 提示刷写基线固件 |
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

---

### 5.3 `POST /api/v1/client/heartbeat` — 保活

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

### 5.4 `POST /api/v1/client/checkpoint` — 续测断点上报

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

### 5.5 `POST /api/v1/client/check-out` — 出站落库（返回 ACK）

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

### 5.6 `POST /api/v1/client/release` — 主动放弃锁

```jsonc
{"client_id": "SZ-L1-CAL-01", "sn": "C020001", "lock_token": "b1c9...", "reason": "操作员取消"}
```

**不计产品失败**，锁立即释放，会话置为 `ABORTED`。
用于优雅退出、放弃本次测试、或崩溃前主动让位。响应 `data`：`{sn, released, session_id, server_time}`。

---

### 5.7 `GET /api/v1/client/ack` — 上传闭环校验

```
GET /api/v1/client/ack?sn=C020001&checkout_id=<幂等键>
```

命中（200）即证明服务端已落库，可放行流转；未命中返回 `404 ack_not_found`。
用于超时/断网重传后确认状态，**不得**作为唯一的放行依据（正常路径应直接取 `check-out` 的 201）。

---

## 6. 错误码与处理决策表

### 6.1 全量错误码

| HTTP | `code` | `exit_code` | 触发接口 | 含义 |
|---|---|---|---|---|
| 401 | `invalid_credentials` | 10 | 全部 | 缺少或无效的 `X-API-Key` |
| 400 | `station_not_in_process` | 10 | check-in / check-out | 工位不属于该机型流程 |
| 400 | `case_id_mismatch` | 15 | check-in | 待执行清单缺少必测用例ID |
| 400 | `missing_mandatory` | 13 | check-out | 漏测拦截（未执行或 SKIP） |
| 400 | `session_mismatch` | 10 | check-in / checkpoint | 会话与 SN/工位不匹配 |
| 400 | `station_mismatch` | 10 | checkpoint | 会话属于其他工位 |
| 403 | `client_not_bound` | 10 | 全部 | 机台未绑定工位 |
| 403 | `model_not_registered` | 10 | check-in | 机型未注册 |
| 403 | `missing_prereq` | 10 | check-in | 跳站拦截 |
| 403 | `model_mismatch` | 10 | check-in | 机型与建档不符 |
| 403 | `firmware_mismatch` | 10 | check-in | 固件不满足基线（口径见机型 `fw_match_rule`） |
| 403 | `product_scrapped` | 10 | check-in | 已报废 |
| 403 | `product_locked` | 16 | check-in | 已工程锁定 |
| 403 | `lock_invalid` | 14 | check-out / checkpoint / release | 锁被接管或失效 |
| 403 | `lock_expired` | 14 | check-out | 锁超过硬超时 |
| 404 | `client_not_registered` | 10 | 全部 | 机台未注册 |
| 404 | `product_not_found` | 10 | check-out / 管理端 | SN 不存在 |
| 404 | `process_not_found` | 10 | check-in | 流程不存在 |
| 404 | `session_not_found` | 10 | check-in / checkpoint | 会话不存在 |
| 404 | `ack_not_found` | 10 | ack | 无对应落库记录 |
| 409 | `station_already_passed` | 10 | check-in | 复测拦截 |
| 409 | `lock_conflict` | 11 | check-in / 管理端 | 锁被他人持有 |
| 409 | `session_completed` | 10 | check-in / checkpoint | 会话已出站 |
| 409 | `session_aborted` | 14 | checkpoint | 会话已被运维中止（换测试用例清单前），须立即停机 |
| 409 | `product_holding_lock` | 11 | 管理端 | 在制品被持锁，禁止处置 |

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
3. SDK 置 `cli.lost_lock = True`，并触发构造参数 `on_lost_lock` 回调
4. 此后 `checkpoint` 会收到 `409 session_aborted`，`check-out` 会被 `lock_invalid` 拒绝
5. **上位机必须自己停**：跑完当前用例后停止剩余用例，不要再尝试出站

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

SDK（`tools/ate_client.py`，单文件零第三方依赖）已覆盖传输层全部职责（重试退避、
断点队列、幂等重传、失锁感知），宿主只负责四件事：执行测试、调 `checkpoint`、
失锁停机、出站重试。完整 conftest.py 骨架见**附录 A**。

**断网重传各场景的分工**（宿主无需自己实现队列）：

| 场景 | 处理者 |
|---|---|
| 秒级抖动 | SDK `HttpClient` 指数退避重试，对调用方透明 |
| 断网数分钟继续测 | `checkpoint()` 失败自动进 `pending_items` 落盘，不抛异常 |
| 网络恢复后补传 | 下次 `checkpoint` / `check_out` 自动合并队列，服务端按 `case_id` 去重 |
| 出站瞬间断网 | 宿主按上例退避重试，复用同一 `checkout_id` |
| 服务端已落库但响应丢失 | 同 `checkout_id` 重试 → 幂等回放既有回执，不会双账 |
| 断网 + 进程崩溃 | 断点文件恢复会话（同 `state_file` 路径重新进站） |
| 断网太久锁被回收/接管 | `lost_lock` 置位 → `pytest.exit` 停机，重新进站 |

---

## 11. 管理端运维接口（JWT）

前置：`POST /api/auth/login {"username","password"}` → `access_token`，
后续请求带 `Authorization: Bearer <token>`。

| 接口 | 说明 |
|---|---|
| `GET /api/admin/sessions` | 会话清单，支持 `sn / station_id / client_id / status / abnormal_only / page / page_size` |
| `GET /api/admin/sessions/zombie-locks` | 失联僵尸锁清单（可被接管/强制解锁） |
| `GET /api/admin/sessions/{id}` | 会话详情，含 checkpoint 明细（"跑到哪崩的"） |
| `POST /api/admin/sessions/{id}/abort` | 强制终止会话并解锁（body: `{"reason": "..."}`） |
| `POST /api/admin/sessions/abort-running` | 批量终止运行中的会话；body 至少给一个过滤条件 `station_id / process_id / sn / client_id`（否则 400 `scope_required`），支持 `dry_run` 预演 |
| `POST /api/admin/products/{sn}/force-release` | 强制解锁（body: `{"reason": "..."}`），不动印章与失败计数 |
| `GET /api/admin/products/{sn}/sessions` | 该 SN 的会话时间线 |
| `GET /api/admin/products` | 在制品清单，支持 `zombie_only=true` 过滤失联锁 |
| `GET /api/admin/metrics/overview` | 运行概况，含 `locks: {active, zombie, sessions_running, sessions_abnormal}` |
| `GET /api/admin/clients` | 机台清单（在线状态 + 持锁 SN） |
| `POST /api/admin/clients` | 注册机台并绑定工位 |

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
| `ENFORCE_FW` | true | 固件基线校验 |
| `ENFORCE_CASE_IDS` | true | 进站时比对待执行的用例ID清单 |
| `CLIENT_ONLINE_WINDOW_SECONDS` | 90 | 机台在线判定窗口 |

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
python tools/ate_client.py --base-url http://127.0.0.1:8000 --api-key <KEY> demo
```

| 场景 | 参数 | 验收点 |
|---|---|---|
| 正常全流程 | `normal` | 5 个断点上报，出站 201，`checkpoint_merged_count=0` |
| 防跳站拦截 | `gate` | 未做首站直接进第二站 → `403 missing_prereq`；补做后放行 |
| 崩溃续测 | `resume` | `attempt=2`，跳过 1 个用例，`checkpoint_merged_count=1` |
| 备用机台接管 | `takeover` | 强制解锁后接管成功；旧 token 出站 → `403 lock_invalid` |
| 孤儿锁回收 | `sweep` | 留一把锁在服务端，Web 端「测试会话 → 僵尸锁」可见 |

单场景调试可加 `-v` 输出 SDK 调试日志。

### 13.2 多机台并发验证（`line_simulator.py`）

```powershell
python tools\line_simulator.py --api-key <KEY> --mode normal --units 15
python tools\line_simulator.py --api-key <KEY> --mode chaos --crash-rate 0.12
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

产线 pytest 工程只需把 `tools/ate_client.py` 拷为工程内模块，再写一个 conftest。
职责分工（谁管断网重传、谁管停机）见 **10.5**，本附录给出完整骨架：

```python
# conftest.py
import json
import threading
import time
from pathlib import Path

import pytest

from ate_client import AteClient, ApiError, identity_from_report, to_ate_items

STATE_FILE = Path("D:/atedata")   # 断点目录：SDK 按 SN 分文件，多台件交替测试互不覆盖


def pytest_addoption(parser):
    parser.addoption("--ate-url", default="http://127.0.0.1:8000")
    parser.addoption("--ate-key", default="")                  # 与服务端 V1_API_KEY 一致
    parser.addoption("--ate-client", default="SZ-L1-CAL-01")   # 机台编号，Web 端绑定工位


# ---------- 1. 收集每个用例的结果（pytest 标准钩子） ----------
# setup/teardown 失败也必须收集为 FAIL：若只收 call，异常用例会"凭空消失"，
# 出站时触发 missing_mandatory 把整个工位卡死。
_report_rows = []
_round_results = {}   # 本轮已执行用例的结果表（nodeid → outcome），重跑判定用
_t0 = 0.0

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()
    if rep.when == "call" or (rep.when in ("setup", "teardown") and rep.failed):
        _report_rows.append({
            "nodeid": item.nodeid,
            "duration": rep.duration,
            "result": rep.outcome,   # setup 异常 outcome 为 error，SDK 映射为 FAIL
            "exception": str(rep.longrepr) if rep.failed else "",
        })
        _round_results[item.nodeid] = rep.outcome


def _retry_failed(sn: str) -> list:
    """上一轮失败清单（不出站留下的）。无文件 = 无需重跑。"""
    rp = _retry_path(sn)
    if not rp.exists():
        return []
    try:
        return json.loads(rp.read_text(encoding="utf-8"))
    except Exception:
        return []

# ---------- 2. 仪器身份与计时（按你的仪器层实现） ----------
def read_idn():
    """仪器 *IDN? 直读 → identity_from_report 所需字典。pyvisa 示例：
        idn = inst.query("*IDN?")     # "Keysight,DSOX4104A,CN1234,03.00"
        vendor, model, serial, fw = [p.strip() for p in idn.split(",")]
        return {"model": model, "serialNumber": serial, "version": fw}
    model / firmware 必须与 Web 端机型注册一致，否则 model_mismatch / firmware_mismatch 403。
    """
    raise NotImplementedError("read_idn(): 由工程仪器层实现")


def elapsed_ms():
    return int((time.monotonic() - _t0) * 1000)


# ---------- 3. 客户端夹具 ----------
@pytest.fixture(scope="session")
def ate(request):
    opt = request.config.getoption

    def on_lost_lock(reason):
        # 心跳线程调用：不能在回调里 pytest.exit（SystemExit 只会杀掉心跳线程），
        # 必须 interrupt_main 把 KeyboardInterrupt 注入主线程，长用例也能被打断
        threading.interrupt_main()

    cli = AteClient(opt("--ate-url"), opt("--ate-key"),
                    client_id=opt("--ate-client"),
                    state_file=STATE_FILE, on_lost_lock=on_lost_lock)
    yield cli
    cli.stop_heartbeat()

# ---------- 4. 会话级：进站 → 续测跳过 → 测试 → 出站/重跑 ----------
def _retry_path(sn: str) -> Path:
    """本轮失败清单（conftest 自管，与 SDK 断点文件同目录）。"""
    if STATE_FILE.suffix == "":
        return STATE_FILE / f".retry_{sn}.json"
    return STATE_FILE.with_name(STATE_FILE.stem + ".retry.json")


@pytest.fixture(scope="session", autouse=True)
def ate_session(ate, request):
    global _t0
    sn, model, fw = identity_from_report(read_idn())   # 仪器 *IDN? 直读，工程内实现

    # 进站必须携带完整 collection（含已完成用例），否则 case_id_mismatch 拦截。
    # 注意：pytest -k 的部分运行同样被拦（防漏测属预期），调试请跑全量，
    # 或由运维临时关闭 ENFORCE_CASE_IDS。
    case_ids = [item.nodeid for item in request.session.items]

    # lock_conflict 按持有状态自适应处理，不做盲目长等待：
    #   心跳新鲜（idle < 30s）= 该件正被其他机台正常测试 → 等到它测完遥遥无期，立即退出；
    #   僵尸锁（idle 逼近 120s 宽限）= 只等"Sweeper 回收"所需的精确时间，再重试。
    # 其余进站错误（防呆拦截等）直接失败。
    state = None
    for attempt in range(3):
        try:
            state = ate.check_in(sn, model, fw, case_ids=case_ids)
            break
        except ApiError as exc:
            if exc.is_lock_conflict:
                idle = exc.lock_idle_sec()
                if idle is not None and idle < 30:
                    pytest.exit("进站失败：该件正在其他机台测试中，请等待其完成或走维修处置", returncode=4)
                wait = 15 if idle is None else min(max(120 - idle + 10, 10), 130)
                time.sleep(wait)
                continue
            if exc.is_gate_blocked:
                # 防呆拦截：件已盖章（重测需先走 RETEST 处置）/ 已锁定 / 已报废 / 机型固件不符
                pytest.exit(f"进站被防呆拦截: {exc}", returncode=1)
            if exc.is_network:
                # 网络重试已在 HttpClient 内耗尽：干净退出而不是裸异常栈
                pytest.exit(f"进站失败：服务端不可达（{exc}），请检查网络", returncode=4)
            raise
    if state is None:
        pytest.exit("进站失败：锁一直未释放，请先做重测处置或强制解锁", returncode=4)
    _t0 = time.monotonic()
    _round_results.clear()   # 新一轮：清空结果表（重跑清单由 _retry_failed 提供）

    # 续测跳过：只跳过"上次已 PASS"的用例；上次 FAIL 的（在重跑清单里）本轮重跑，
    # 通过后 checkpoint 按 case_id 覆盖，FAIL 被新结果顶掉。
    # 策略：必测没过 → 不出站，保留会话与断点，重跑时只执行未通过项（见收尾判定）。
    mandatory = set(state.mandatory_case_ids)
    done_pass = set(state.completed_case_ids) - set(_retry_failed(sn))
    for item in request.session.items:
        if item.nodeid in done_pass:
            item.add_marker(pytest.mark.skip(reason=f"ATE 续测：第 {state.attempt} 轮已通过，跳过"))

    yield

    # 收尾判定：本轮必测是否有 FAIL。有 → 不出站（会话与断点原样保留），
    # 记录失败清单退出；处置/修复后重跑将只执行未通过项。全绿 → 正常出站。
    failed_now = {nid for nid, res in _round_results.items() if res != "passed"}
    if failed_now & mandatory:
        rp = _retry_path(sn)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps(sorted(failed_now)), encoding="utf-8")
        pytest.exit(
            f"必测未全过: {sorted(failed_now & mandatory)}；已保留断点不出站，"
            f"重跑将只执行未通过项",
            returncode=2,
        )
    _retry_path(sn).unlink(missing_ok=True)
    items = to_ate_items(_report_rows)
    for attempt in range(6):
        try:
            ack = ate.check_out(items, duration_ms=elapsed_ms())
            if not ack.get("acknowledged"):
                pytest.exit("未拿到落库回执，禁止流转", returncode=1)
            return
        except ApiError as exc:
            # 防呆拦截（漏测/跳站/清单不匹配等）：重试无意义，立即退出人工介入
            if exc.is_gate_blocked:
                pytest.exit(f"出站被防呆拦截: {exc}", returncode=1)
            if ate.lost_lock or exc.is_lock_invalid:
                pytest.exit("会话已被运维中止，停止测试", returncode=3)
            time.sleep(min(2 ** attempt, 30))
        except Exception:
            if ate.lost_lock:
                pytest.exit("会话已被运维中止，停止测试", returncode=3)
            time.sleep(min(2 ** attempt, 30))
    pytest.exit("出站重试后仍未成功，请检查网络或联系运维", returncode=4)

# ---------- 5. 每个用例之间：上报断点 + 失锁自检 ----------
@pytest.fixture(autouse=True)
def ate_case_gate(ate):
    yield
    if _report_rows:
        try:
            # 断网时自动进待补传队列，勿自行包重试；但 lock_invalid 必须抛出
            # （锁已被接管/中止，结果作废）——接住后干净停机，否则后续每个用例
            # 都会带着原始异常栈报 ERROR
            ate.checkpoint(to_ate_items(_report_rows))
        except ApiError as exc:
            if exc.is_lock_invalid:
                pytest.exit("锁已失效：会话被运维中止", returncode=3)
            raise
        _report_rows.clear()
    if ate.lost_lock:
        pytest.exit("锁已失效：会话被运维中止", returncode=3)
```

既有产线工程接入时只需对齐三点：

1. **参数注入**：URL / API-Key / 机台编号改走 `--ate-*` 命令行参数（见上方 `pytest_addoption`）；
2. **身份来源**：SN / 机型 / 固件由仪器 `*IDN?` 直读，经 `identity_from_report()` 解析三要素；
3. **结果来源**：既有报告字典（`report["data"]` 的 nodeid / duration / result / exception 列表）
   经 `to_ate_items()` 转换后即可喂给 `checkpoint` / `check_out`，字段映射见附录 B。

### 附录 B：字段映射表

| 现有上位机工程 | ATE 服务端 |
|---|---|
| `report["serialNumber"]` / `["model"]` / `["version"]`（`*IDN?`） | check-in 的 `sn` / `product_model` / `firmware` |
| `report["data"][*]["nodeid"]` | `case_id` |
| `rep.outcome`（passed / failed / skipped） | `PASS` / `FAIL` / `SKIP` |
| `pytest_runtest_makereport` | 逐用例 `checkpoint` 上报 |
| `project_session_start`（session/autouse，依赖 `dst_instr`） | 进站（yield 前）/ 出站（yield 后） |
| `action()` 线程收到 `abort` | `release` 主动放弃锁 |

### 附录 C：完整调用序列

```
resolve ─▶ check-in ─┬─▶ heartbeat ─┐
                     │              │  （后台周期调用，直到出站/释放）
                     └─▶ checkpoint ┘  （每用例一次，可批量补传）
                              │
                              ├─▶ check-out ─▶ 201 ACK ─▶ 拔线流转
                              ├─▶ release    ─▶ 放弃本次测试（不计失败）
                              └─▶ ack        ─▶ 断网后确认落库
```
