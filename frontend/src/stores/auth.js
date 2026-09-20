import { reactive, computed } from 'vue'
import { authApi, TOKEN_KEY } from '../api'

const state = reactive({
  token: localStorage.getItem(TOKEN_KEY) || '',
  user: null,
  checked: false,
})

// 路由守卫（首次导航早于组件挂载）与 App.vue 挂载共用同一请求，避免竞态与重复请求
let refreshPromise = null

export function useAuth() {
  const isLoggedIn = computed(() => Boolean(state.token) && Boolean(state.user))
  const role = computed(() => state.user?.role || 'viewer')
  const isAdmin = computed(() => role.value === 'admin')
  // 产线操作（机台绑定/维修处置/强制解锁/中止会话）：operator 及以上
  const canOperate = computed(() => role.value === 'operator' || role.value === 'admin')

  function refresh() {
    if (!state.token) {
      state.checked = true
      return Promise.resolve(false)
    }
    // 已有进行中的请求则复用，避免并发水合
    if (refreshPromise) return refreshPromise

    const tokenAtStart = state.token

    refreshPromise = authApi
      .me()
      .then((res) => {
        if (state.token !== tokenAtStart) return Boolean(state.token && state.user)
        state.user = res.data
        return true
      })
      .catch((error) => {
        // 仅当失败的 token 仍是当前 token 时才清除凭证，避免旧请求 401 误清新登录态
        if (state.token !== tokenAtStart) return false
        // 仅认证失效(401)才清除本地凭证；网络超时/后端重启等临时错误
        // 保留 token，避免弱网下把有效登录态误降级为游客
        if (error?.status === 401) {
          state.token = ''
          state.user = null
          localStorage.removeItem(TOKEN_KEY)
        }
        return false
      })
      .finally(() => {
        state.checked = true
        refreshPromise = null
      })
    return refreshPromise
  }

  async function login(username, password) {
    const res = await authApi.login({ username, password })
    state.token = res.data.access_token
    state.user = res.data.user
    state.checked = true
    localStorage.setItem(TOKEN_KEY, state.token)
    return res.data.user
  }

  function logout() {
    state.token = ''
    state.user = null
    state.checked = true
    localStorage.removeItem(TOKEN_KEY)
  }

  return { state, isLoggedIn, role, isAdmin, canOperate, refresh, login, logout }
}
