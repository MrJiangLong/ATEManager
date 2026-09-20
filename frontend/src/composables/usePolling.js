import { onActivated, onBeforeUnmount, onDeactivated, onMounted } from 'vue'

/**
 * 页面级轮询：仅在页面可见时按固定间隔执行，切后台或 keep-alive 切走时暂停。
 *
 * 锁与会话状态手动刷新不足以发现卡死机台，引入 WebSocket 又过重，故用轮询过渡。
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

  onDeactivated(stop)
  onActivated(start)
}
