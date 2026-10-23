import { computed, ref } from 'vue'
import { clientApi, routingApi } from '../api'
import { useProcesses } from './useProcesses'

/**
 * 编码 → 名称 的字典映射（工位 / 机台 / 流程 / 用例），供表格编码单元格的悬浮提示使用。
 *
 * 规则：字典里设置了名字且名字与编码不同才返回名字；否则返回空字符串，
 * 调用方（CodeTip 组件）据此不渲染 tooltip —— 编码本身就是兜底展示。
 *
 * 流程与工位复用 useProcesses 的全局共享缓存；机台清单在此单例加载一次。
 * 用例名称不在台账/会话探针数据里，需从各流程拓扑惰性拉取（仅需用到 tooltip 的页面调用）。
 */
const clientsMap = ref(new Map())
let clientsLoaded = false
let clientsLoading = null

const caseNames = ref(new Map())
let caseNamesLoaded = false
let caseNamesLoading = null

function caseKey(stationId, caseId) {
  return JSON.stringify([stationId, caseId])
}

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

  // 用例名称：按 (工位, 用例ID) 汇总各流程拓扑，一次拉齐后全局复用；失败静默，tooltip 退回纯用例ID
  async function ensureCaseNames() {
    if (caseNamesLoaded) return
    if (!caseNamesLoading) {
      caseNamesLoading = (async () => {
        await loadProcesses()
        const results = await Promise.all(processes.value.map((p) => routingApi.topology(p.process_id)))
        const map = new Map()
        for (const res of results) {
          for (const it of res.data.items || []) {
            const k = caseKey(it.station_id, it.case_id)
            if (!map.get(k)) map.set(k, it.item_name || '')
          }
        }
        caseNames.value = map
        caseNamesLoaded = true
      })()
        .catch(() => {})
        .finally(() => {
          caseNamesLoading = null
        })
    }
    await caseNamesLoading
  }

  // tooltip 文案统一为「用例ID(用例名称)」，查不到名称时退回纯用例ID
  function caseLabel(stationId, caseId) {
    const name = caseNames.value.get(caseKey(stationId, caseId)) || ''
    return name && name !== caseId ? `${caseId}(${name})` : caseId
  }

  return {
    ensureLoaded,
    ensureCaseNames,
    caseLabel,
    stationName: (id) => strictName(stationMap.value, id),
    clientName: (id) => strictName(clientsMap.value, id),
    processName: (id) => strictName(processMap.value, id),
  }
}
