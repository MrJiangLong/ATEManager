import { onActivated, onBeforeUnmount, onDeactivated, onMounted } from 'vue'

/**
 * 页面级轮询：按固定间隔执行，keep-alive 切走或组件卸载时停止。
 *
 * 锁与会话状态手动刷新不足以发现卡死机台，引入 WebSocket 又过重，故用轮询过渡。
 * 注意：不做 document.hidden 判断——嵌入式 webview（IDE 内置预览等）可能恒报不可见，
 * 会导致轮询整体失效；工厂内网桌面场景后台多几个请求可接受，数据实时性优先。
 *
 * @param {Function} callback 每轮执行的刷新函数
 * @param {number} intervalMs 轮询间隔（默认 20s）；自适应频率在 callback 内做时间门控
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
    Promise.resolve(callback()).catch(() => {})
  }

  function start() {
    stop()
    timer = setInterval(tick, Math.max(5000, intervalMs))
  }

  onMounted(start)
  onBeforeUnmount(stop)
  onDeactivated(stop)
  onActivated(start)
}
