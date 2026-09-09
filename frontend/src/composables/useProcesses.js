import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import i18n from '../i18n'
import { processApi, stationApi } from '../api'

/**
 * 全局共享的主数据缓存。
 * 工艺流程与工位字典属于低频静态数据，跨页面复用同一份，避免重复请求。
 *
 * 注意：任何修改流程/工位的视图在落库成功后必须调用 loadProcesses({ force: true })
 * 刷新共享缓存，否则其它页面的下拉框会继续展示旧数据。
 */
const processes = ref([])
const stations = ref([])
const loading = ref(false)
let loaded = false

async function loadProcesses({ force = false } = {}) {
  if (loaded && !force) return processes.value
  loading.value = true
  try {
    const [pRes, sRes] = await Promise.all([processApi.list(), stationApi.list()])
    processes.value = pRes.data
    stations.value = sRes.data
    loaded = true
  } catch (e) {
    // 字典加载失败会让整页筛选失效，必须显式反馈而不是静默空白
    ElMessage.error(e.message || i18n.global.t('errors.loadFailed'))
    if (force) {
      processes.value = []
      stations.value = []
    }
  } finally {
    loading.value = false
  }
  return processes.value
}

function processName(processId) {
  return processes.value.find((p) => p.process_id === processId)?.process_name || processId
}

export function useProcesses() {
  return { processes, stations, loading, loadProcesses, processName }
}
