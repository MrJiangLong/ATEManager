import { onBeforeUnmount, onMounted } from 'vue'

/**
 * 页面级轮询：仅在页面可见时按固定间隔执行，切后台自动暂停。
 *
 * 锁状态与会话是"准实时"数据，手动刷新不足以发现卡死的机台，
 * 但引入 WebSocket 成本过高，故用轻量轮询过渡。
 *
 * 注册后随组件挂载/卸载自动启停，调用方无需接收返回值。
 *
 * @param {Function} callback 每轮执行的刷新函数
 * @param {number} intervalMs 轮询间隔（默认 20s）
 */
export function usePolling(callback, intervalMs = 20000) {
  let timer = null

  function stop() {
    if (timer) {
      clearInterval(timer)
      timer = null
    }
  }

  function tick() {
    if (typeof document !== 'undefined' && document.hidden) return
    Promise.resolve(callback()).catch(() => {})
  }

  function start() {
    stop()
    timer = setInterval(tick, Math.max(5000, intervalMs))
  }

  function onVisibilityChange() {
    if (document.hidden) stop()
    else start()
  }

  onMounted(() => {
    start()
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', onVisibilityChange)
    }
  })

  onBeforeUnmount(() => {
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
    stop()
  })
}
