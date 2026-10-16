import { computed, ref } from 'vue'
import { clientApi } from '../api'
import { useProcesses } from './useProcesses'

/**
 * 编码 → 名称 的字典映射（工位 / 机台 / 流程），供表格编码单元格的悬浮提示使用。
 *
 * 规则：字典里设置了名字且名字与编码不同才返回名字；否则返回空字符串，
 * 调用方（CodeTip 组件）据此不渲染 tooltip —— 编码本身就是兜底展示。
 *
 * 流程与工位复用 useProcesses 的全局共享缓存；机台清单在此单例加载一次。
 */
const clientsMap = ref(new Map())
let clientsLoaded = false
let clientsLoading = null

function strictName(map, id) {
  if (!id) return ''
  const name = map.get(id) || ''
  return name && name !== id ? name : ''
}

export function useDictionaries() {
  const { processes, stations, loadProcesses } = useProcesses()

  const stationMap = computed(() => new Map(stations.value.map((s) => [s.station_id, s.station_name || ''])))
  const processMap = computed(() => new Map(processes.value.map((p) => [p.process_id, p.process_name || ''])))

  async function ensureClients() {
    if (clientsLoaded) return
    if (!clientsLoading) {
      clientsLoading = clientApi
        .list()
        .then((res) => {
          clientsMap.value = new Map(res.data.map((c) => [c.client_id, c.client_name || '']))
          clientsLoaded = true
        })
        .finally(() => {
          clientsLoading = null
        })
    }
    await clientsLoading
  }

  async function ensureLoaded() {
    await Promise.all([loadProcesses(), ensureClients()])
  }

  return {
    ensureLoaded,
    stationName: (id) => strictName(stationMap.value, id),
    clientName: (id) => strictName(clientsMap.value, id),
    processName: (id) => strictName(processMap.value, id),
  }
}
