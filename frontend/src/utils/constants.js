// Shared domain constants used across views/components.
// Keep this file pure (no DOM, no Vue) so it can be imported anywhere.

/**
 * 在制品列表可筛选的状态。
 * IDLE / TESTING / LOCKED / SCRAPPED 对应后端 current_status；
 * COMPLETED 为派生状态（拓扑盖章齐全），后端会单独处理：
 *   选"待测试"时排除已完工，选"已完工"时只返回已完工，避免两种状态混淆。
 */
export const WIP_FILTER_STATUSES = ['IDLE', 'TESTING', 'LOCKED', 'SCRAPPED', 'COMPLETED']

/** 维修处置动作（与后端枚举保持一致）。 */
export const REPAIR_ACTIONS = ['RETEST', 'ROLLBACK', 'RESET', 'SCRAP']

/** Repair actions that require a target station. */
export const REPAIR_ACTIONS_WITH_TARGET = ['RETEST', 'ROLLBACK']

/** Test result codes. */
export const TEST_RESULTS = ['PASS', 'FAIL', 'SKIP']

/** 测试会话状态（与后端 test_sessions.status 枚举保持一致）。 */
export const SESSION_STATUS_LIST = ['RUNNING', 'COMPLETED', 'ABORTED', 'EXPIRED', 'TAKEN_OVER']

/** 失联判定阈值（秒）：与后端 LOCK_HEARTBEAT_GRACE_SEC 默认值一致，仅用于前端标红提示 */
export const LOCK_GRACE_SEC = 120
