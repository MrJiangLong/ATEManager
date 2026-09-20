
const pad = (n) => String(n).padStart(2, '0')

/** ISO 时间串 → 本地时区 "YYYY-MM-DD HH:mm:ss"；无效输入原样返回兜底 */
export function fmtDateTime(iso) {
  if (!iso) return '-'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return String(iso)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 秒 → "1m 23s" / "42s"（锁持有时长 / 心跳断流时长） */
export function fmtDurationSec(sec) {
  const s = Math.max(0, Number(sec) || 0)
  if (s >= 60) return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`
  return `${Math.round(s)}s`
}

/** 毫秒 → "1m 23s" / "42.0s" */
export function fmtDurationMs(ms) {
  const seconds = (Number(ms) || 0) / 1000
  if (seconds >= 60) {
    return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`
  }
  return `${seconds.toFixed(1)}s`
}

/**
 * ISO 时间 → 相对时间（"刚刚" / "3 分钟前"）。
 * 列表里比绝对时间更快判断"多久之前"，完整时间交给 tooltip。
 * @param {string} iso
 * @param {Function} t vue-i18n 的 t（文案在 common.* 下，中英共用）
 */
export function fmtRelative(iso, t) {
  if (!iso) return '-'
  const ts = new Date(iso).getTime()
  if (Number.isNaN(ts)) return String(iso)
  const diff = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (diff < 10) return t('common.justNow')
  if (diff < 60) return t('common.agoSec', { n: diff })
  if (diff < 3600) return t('common.agoMin', { n: Math.floor(diff / 60) })
  if (diff < 86400) return t('common.agoHour', { n: Math.floor(diff / 3600) })
  return t('common.agoDay', { n: Math.floor(diff / 86400) })
}

/** pytest nodeid → 用例方法名：`tests/a.py::TestB::test_c` → `test_c` */
export function shortCaseId(caseId) {
  if (!caseId) return '-'
  const parts = String(caseId).split('::')
  return parts.length > 1 ? parts[parts.length - 1] : String(caseId)
}

/** pytest nodeid → 文件 + 类：`tests/a.py::TestB::test_c` → `TestB` */
export function caseOwner(caseId) {
  if (!caseId) return ''
  const parts = String(caseId).split('::')
  return parts.length > 2 ? parts[parts.length - 2] : ''
}

/** 在制品状态 → el-tag type */
export function statusTagType(status) {
  switch (status) {
    case 'IDLE':
      return 'info'
    case 'TESTING':
      return 'primary'
    case 'LOCKED':
      return 'danger'
    case 'SCRAPPED':
      return 'warning'
    case 'COMPLETED':
      return 'success'
    default:
      return 'info'
  }
}

/** 测试会话状态 → el-tag type（会话列表 / 追溯 / 会话抽屉共用） */
export function sessionTagType(status) {
  switch (status) {
    case 'RUNNING':
      return 'success'
    case 'COMPLETED':
      return 'primary'
    case 'EXPIRED':
      return 'danger'
    default:
      return 'warning'
  }
}

/**
 * 尝试次数 → 说明文案（>1 次意味着崩溃后自动续测）。
 * 会话列表 / 追溯 / 会话抽屉共用，文案在 sessions.* 下。
 */
export function attemptTipText(attempt, t) {
  const n = Number(attempt) || 0
  return n > 1 ? t('sessions.attemptTipMany', { n }) : t('sessions.attemptTipFirst')
}

/** 测试结论 → el-tag type */
export function resultTagType(result) {
  switch (result) {
    case 'PASS':
      return 'success'
    case 'FAIL':
      return 'danger'
    case 'SKIP':
      return 'info'
    default:
      return 'info'
  }
}

/** 维修处置动作 → el-tag type */
export function repairTagType(action) {
  switch (action) {
    case 'RETEST':
      return 'warning'
    case 'ROLLBACK':
      return 'primary'
    case 'RESET':
      return 'info'
    case 'SCRAP':
      return 'danger'
    default:
      return 'info'
  }
}

