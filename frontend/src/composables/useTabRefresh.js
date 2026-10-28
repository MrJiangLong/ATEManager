import { inject, watch } from 'vue'

/**
 * 工艺配置 Tab 切换到指定页时触发数据刷新。
 *
 * el-tabs 的六个 Tab 组件常驻挂载（切换不重挂），数据只在 onMounted 加载一次，
 * 导致跨 Tab 操作后（如拓扑里锁定基线、别处改了清单）切回本页看到的是旧数据。
 * 由 RouteConfig 通过 provide('configTab', activeTab) 下发当前 Tab，这里按需 reload，
 * 既保证切换即刷新，又保留组件内的筛选/工艺选择状态（不靠重挂载）。
 *
 * @param {string} name 本组件对应的 Tab name
 * @param {Function} refresh 切换回本 Tab 时执行的加载函数
 */
export function useTabRefresh(name, refresh) {
  const activeTab = inject('configTab', null)
  if (!activeTab) return
  watch(activeTab, (v) => {
    if (v === name) refresh()
  })
}
