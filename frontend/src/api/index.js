import axios from 'axios'

const BASE_URL = import.meta.env.VITE_API_BASE || '/api'
export const TOKEN_KEY = 'ate-token'

const api = axios.create({
  baseURL: BASE_URL,
  timeout: 60000,
})

api.interceptors.request.use((config) => {
  const t = localStorage.getItem(TOKEN_KEY)
  if (t) config.headers.Authorization = `Bearer ${t}`
  return config
})

let unauthorizedHandler = null
export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler
}

/** 把后端统一响应包（EnvelopeOut）规范化为 Error，附带 status / code / data */
function normalizeError(error) {
  const status = error.response?.status
  const body = error.response?.data
  let message = body?.message
  if (!message) {
    let detail = body?.detail
    if (Array.isArray(detail)) {
      // 422 参数校验错误
      detail = detail
        .map((i) => (Array.isArray(i.loc) ? `${i.loc.slice(1).join('.')}: ${i.msg}` : i.msg))
        .join('; ')
    }
    message = detail || error.message || 'Request failed'
  }
  const normalized = new Error(message)
  normalized.status = status
  normalized.code = body?.code
  normalized.exitCode = body?.exit_code
  normalized.data = body?.data
  return normalized
}

api.interceptors.response.use(
  (response) => response,
  (error) => {
    if (error.response?.status === 401 && unauthorizedHandler) {
      // 仅当请求携带的 token 仍是本地当前 token 时才判定会话失效，
      // 避免并发旧请求的 401 误清新登录态
      const reqToken = (error.config?.headers?.Authorization || '').replace(/^Bearer\s+/i, '')
      if (reqToken && reqToken === localStorage.getItem(TOKEN_KEY)) unauthorizedHandler()
    }
    return Promise.reject(normalizeError(error))
  }
)

const enc = encodeURIComponent

/** 通道二：认证 */
export const authApi = {
  login: (data) => api.post('/auth/login', data),
  me: () => api.get('/auth/me'),
  changePassword: (data) => api.post('/auth/change-password', data),
}

/** 主数据：工艺流程 */
export const processApi = {
  list: () => api.get('/admin/processes'),
  create: (data) => api.post('/admin/processes', data),
  update: (processId, data) => api.put(`/admin/processes/${enc(processId)}`, data),
  remove: (processId) => api.delete(`/admin/processes/${enc(processId)}`),
}

/** 主数据：机型（绑定工艺 + 固件基线） */
export const modelApi = {
  list: (params) => api.get('/admin/product-models', { params }),
  create: (data) => api.post('/admin/product-models', data),
  update: (productModel, data) => api.put(`/admin/product-models/${enc(productModel)}`, data),
  remove: (productModel) => api.delete(`/admin/product-models/${enc(productModel)}`),
}

/** 主数据：逻辑工位 */
export const stationApi = {
  list: () => api.get('/admin/stations'),
  create: (data) => api.post('/admin/stations', data),
  update: (stationId, data) => api.put(`/admin/stations/${enc(stationId)}`, data),
  remove: (stationId) => api.delete(`/admin/stations/${enc(stationId)}`),
}

/** 主数据：物理机台 */
export const clientApi = {
  list: (params) => api.get('/admin/clients', { params }),
  create: (data) => api.post('/admin/clients', data),
  update: (clientId, data) => api.put(`/admin/clients/${enc(clientId)}`, data),
  remove: (clientId) => api.delete(`/admin/clients/${enc(clientId)}`),
}

/** 工艺拓扑与用例ID */
export const routingApi = {
  listSteps: (processId) => api.get('/admin/routing/stations', { params: { process_id: processId } }),
  saveSteps: (processId, steps) =>
    api.put('/admin/routing/stations', steps, { params: { process_id: processId } }),
  listItems: (processId, stationId) =>
    api.get('/admin/routing/items', { params: { process_id: processId, station_id: stationId || undefined } }),

  createItem: (data) => api.post('/admin/routing/items', data),
  updateItem: (itemId, data) => api.put(`/admin/routing/items/${itemId}`, data),
  removeItem: (itemId) => api.delete(`/admin/routing/items/${itemId}`),
  validate: (processId) => api.get('/admin/routing/validate', { params: { process_id: processId } }),
  clone: (data) => api.post('/admin/routing/clone', data),
}

/** 在制品 */
export const productApi = {
  list: (params) => api.get('/admin/products', { params }),
  /** 强制解锁：机台失联/卡死时人工介入，需填原因 */
  forceRelease: (sn, reason) => api.post(`/admin/products/${enc(sn)}/force-release`, { reason }),
  /** 该 SN 的测试会话时间线（试了几次、每次跑到哪崩的） */
  sessions: (sn) => api.get(`/admin/products/${enc(sn)}/sessions`),
}

/** 测试会话：续测断点与锁接管 */
export const sessionApi = {
  list: (params) => api.get('/admin/sessions', { params }),
  abort: (sessionId, reason) => api.post(`/admin/sessions/${enc(sessionId)}/abort`, { reason }),
  zombieLocks: (params) => api.get('/admin/sessions/zombie-locks', { params }),
}

/** 事件台账与追溯 */
export const recordApi = {
  list: (params) => api.get('/admin/records', { params }),
  trace: (sn) => api.get(`/admin/records/trace/${enc(sn)}`),
}

/** 维修处置履历 */
export const repairApi = {
  list: (params) => api.get('/admin/repairs', { params }),
  create: (data) => api.post('/admin/repairs', data),
  stats: (params) => api.get('/admin/repairs/stats', { params }),
}

/** 仪表盘统计 */
export const metricsApi = {
  overview: (params) => api.get('/admin/metrics/overview', { params }),
}

export default api
