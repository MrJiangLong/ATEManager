<template>
  <div>
    <div class="tab-head">
      <div class="filter-row">
        <el-select v-model="processId" filterable :placeholder="t('common.process')" class="select-flow">
          <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id">
            <div class="opt-row" :class="{ 'opt-row--tight': !(processName(p.process_id)) || (processName(p.process_id)) === (p.process_id) }" :title="[p.process_id, processName(p.process_id)].filter((v, i, a) => v && a.indexOf(v) === i).join('  ')">
              <span class="opt-code">{{ p.process_id }}</span>
              <span class="opt-name">{{ processName(p.process_id) }}</span>
            </div>
          </el-option>
        </el-select>
        <el-select v-model="stationFilter" clearable :placeholder="t('configs.filterStation')" class="select-station">
          <el-option v-for="s in processStations" :key="s.station_id" :value="s.station_id" :label="s.station_id">
            <div class="opt-row" :class="{ 'opt-row--tight': !(s.station_name || '') || (s.station_name || '') === (s.station_id) }" :title="[s.station_id, s.station_name || ''].filter((v, i, a) => v && a.indexOf(v) === i).join('  ')">
              <span class="opt-code">{{ s.station_id }}</span>
              <span class="opt-name">{{ s.station_name || '' }}</span>
            </div>
          </el-option>
        </el-select>
        <el-select v-model="setFilter" clearable filterable :placeholder="t('configs.caseSetFilter')" class="select-set">
          <el-option v-for="s in setList" :key="s.set_id" :value="s.set_id" :label="s.set_id">
            <div
              class="opt-row"
              :class="{ 'opt-row--tight': !s.set_name || s.set_name === s.set_id }"
              :title="[s.set_id, s.set_name].filter((v, i, a) => v && a.indexOf(v) === i).join('  ')"
            >
              <span class="opt-code">{{ s.set_id }}</span>
              <span class="opt-name">{{ s.set_name || '' }}</span>
            </div>
          </el-option>
        </el-select>
      </div>
      <div class="filter-row">
        <el-button :icon="RefreshRight" size="small" @click="loadItems">{{ t('common.refresh') }}</el-button>
        <el-button v-if="canManage" type="primary" size="small" :icon="Plus" :disabled="!processId" @click="openCreate">
          {{ t('configs.newItem') }}
        </el-button>
        <!-- 分割：左侧是用例维护，右侧是集合维护（两个独立功能模块） -->
        <el-divider v-if="canManage" direction="vertical" />
        <el-button v-if="canManage" size="small" :icon="FolderAdd" :disabled="!processId" @click="openCreateSet">
          {{ t('configs.newCaseSet') }}
        </el-button>
        <el-tooltip
          v-if="canManage"
          :content="t('configs.delCaseSetTip')"
          placement="top"
          :disabled="canDeleteSet"
        >
          <span>
            <el-button
              type="danger"
              plain
              size="small"
              :icon="Delete"
              :disabled="!canDeleteSet"
              @click="removeSet"
            >
              {{ t('configs.delCaseSet') }}
            </el-button>
          </span>
        </el-tooltip>
      </div>
    </div>

    <el-table v-loading="loading" :data="paged" stripe size="small">
      <el-table-column :label="t('configs.sortOrder')" width="130">
        <template #default="{ row }">
          <el-input-number
            v-model="row.sort_order"
            :min="1"
            :max="999"
            controls-position="right"
            :value-on-clear="null"
            style="width:100%"
            @change="onOrderChange(row)"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.tabStations')" width="150">
        <template #default="{ row }">
          <el-tooltip
            :content="stationNameOf(row.station_id) || row.station_id"
            placement="top"
            :show-after="400"
            :disabled="!stationNameOf(row.station_id)"
          >
            <span class="code">{{ row.station_id }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.caseSetCol')" width="130">
        <template #header>
          <span :title="t('configs.caseSetTip')">{{ t('configs.caseSetCol') }}</span>
        </template>
        <template #default="{ row }">
          <el-tooltip
            :content="setNameOf(row.station_id, row.set_id) || row.set_id"
            placement="top"
            :show-after="400"
            :disabled="!setNameOf(row.station_id, row.set_id)"
          >
            <span class="code">{{ row.set_id }}</span>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.caseId')" min-width="240">
        <template #default="{ row }">
          <el-tooltip :content="row.case_id" placement="top" :show-after="400">
            <CaseIdText :value="row.case_id" />
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column prop="item_name" :label="t('configs.itemName')" min-width="200" show-overflow-tooltip />
      <el-table-column :label="t('configs.mandatory')" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="row.is_mandatory ? 'warning' : 'info'" size="small" effect="plain">
            {{ row.is_mandatory ? t('configs.mandatory') : t('configs.optional') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.status')" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'" size="small" effect="plain">
            {{ row.is_active ? 'ON' : 'OFF' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="120" align="center" fixed="right">
        <template #default="{ row }">
          <template v-if="canManage && !isStationLocked(row.station_id)">
            <el-button link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
            <el-button v-if="canManage" link type="danger" size="small" @click="onDelete(row)">{{ t('common.delete') }}</el-button>
          </template>
          <el-tooltip v-else-if="canManage" :content="t('configs.itemsLockedHint')" placement="top">
            <span>
              <el-icon :size="16" style="color: var(--el-text-color-secondary)">
                <Lock />
              </el-icon>
            </span>
          </el-tooltip>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="filtered.length"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        background
        size="small"
      />
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? t('configs.editItem') : t('configs.newItem')"
      width="520px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('configs.tabStations')" required>
          <el-select v-model="form.station_id" filterable :disabled="isEdit" style="width:100%">
            <el-option
              v-for="s in processStations"
              :key="s.station_id"
              :value="s.station_id"
              :label="isStationLocked(s.station_id) ? `${s.station_id}（${t('configs.baselineLocked')}）` : s.station_id"
              :disabled="isStationLocked(s.station_id)"
            >
              <div class="opt-row" :class="{ 'opt-row--tight': !(s.station_name || '') || (s.station_name || '') === (s.station_id) }" :title="[s.station_id, s.station_name || ''].filter((v, i, a) => v && a.indexOf(v) === i).join('  ')">
                <span class="opt-code">{{ s.station_id }}</span>
                <span class="opt-name">{{ s.station_name || '' }}</span>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="t('configs.caseSetCol')" required>
          <el-select
            v-model="form.set_id"
            filterable
            allow-create
            default-first-option
            :disabled="isEdit"
            :placeholder="t('configs.caseSetPh')"
            style="width:100%"
          >
            <el-option
              v-for="s in setList"
              :key="s.set_id"
              :value="s.set_id"
              :label="s.set_name && s.set_name !== s.set_id ? `${s.set_name}（${s.set_id}）` : s.set_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('configs.caseId')" required>
          <el-input v-model="form.case_id" :disabled="isEdit" :placeholder="t('configs.caseIdPh')" />
        </el-form-item>
        <el-form-item :label="t('configs.itemName')">
          <el-input v-model="form.item_name" placeholder="CHn幅度校准" />
        </el-form-item>
        <div class="switch-row">
          <el-checkbox v-model="form.is_mandatory">{{ t('configs.mandatory') }}</el-checkbox>
          <el-checkbox v-model="form.is_active">{{ t('common.enabled') }}</el-checkbox>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog
      v-model="setDialogVisible"
      :title="t('configs.newCaseSet')"
      width="480px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="setForm" label-position="top">
        <el-form-item :label="t('configs.tabStations')" required>
          <el-select v-model="setForm.station_id" filterable style="width:100%">
            <el-option v-for="s in processStations" :key="s.station_id" :value="s.station_id" :label="s.station_id">
              <div class="opt-row" :class="{ 'opt-row--tight': !(s.station_name || '') || (s.station_name || '') === (s.station_id) }" :title="[s.station_id, s.station_name || ''].filter((v, i, a) => v && a.indexOf(v) === i).join('  ')">
                <span class="opt-code">{{ s.station_id }}</span>
                <span class="opt-name">{{ s.station_name || '' }}</span>
              </div>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item :label="t('configs.caseSetCode')" required>
          <el-input v-model="setForm.set_id" placeholder="S1" />
        </el-form-item>
        <el-form-item :label="t('configs.caseSetName')">
          <el-input v-model="setForm.set_name" :placeholder="t('configs.caseSetNamePh')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="setDialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="setSaving" @click="submitSet">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../../stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Delete, FolderAdd, Lock, Plus, RefreshRight } from '@element-plus/icons-vue'
import { routingApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useTabRefresh } from '../../composables/useTabRefresh'
import { useLocalPagination } from '../../composables/usePagination'
import { useProcesses } from '../../composables/useProcesses'
import CaseIdText from '../../components/CaseIdText.vue'

const { t } = useI18n()
const { canManage } = useAuth()
const { processes, processName, loadProcesses } = useProcesses()

const processId = ref('')
const stationFilter = ref('')
const setFilter = ref('')
const items = ref([])
const steps = ref([])
const caseSets = ref([])
const loading = ref(false)

/** 集合主数据（字段码 → 名称）；用例里出现的未知集合按字段码兜底，避免历史数据看不见 */
const setList = computed(() => {
  const map = new Map()
  for (const s of caseSets.value) {
    const prev = map.get(s.set_id)
    map.set(s.set_id, {
      set_id: s.set_id,
      set_name: prev?.set_name || s.set_name || '',
    })
  }
  for (const it of items.value) {
    if (!map.has(it.set_id)) map.set(it.set_id, { set_id: it.set_id, set_name: '' })
  }
  return [...map.values()].sort((a, b) => (a.set_id < b.set_id ? -1 : 1))
})
const setOptions = computed(() => setList.value.map((s) => s.set_id))
const setNameMap = computed(() => {
  const map = new Map()
  for (const s of caseSets.value) map.set(`${s.station_id}|${s.set_id}`, s.set_name || '')
  return map
})
function setNameOf(stationId, setId) {
  return setNameMap.value.get(`${stationId}|${setId}`) || ''
}

const filtered = computed(() =>
  setFilter.value ? items.value.filter((i) => i.set_id === setFilter.value) : items.value
)
const { page, pageSize, paged } = useLocalPagination(filtered)

/** 删除集合要定位到 (工位, 字段码)：当前筛选下能唯一确定一套集合才允许删除 */
const selectedSetStation = computed(() => {
  if (!setFilter.value) return ''
  const rows = caseSets.value.filter(
    (s) => s.set_id === setFilter.value && (!stationFilter.value || s.station_id === stationFilter.value)
  )
  return rows.length === 1 ? rows[0].station_id : ''
})
const canDeleteSet = computed(() => !!processId.value && !!selectedSetStation.value)

const setDialogVisible = ref(false)
const setSaving = ref(false)
const setForm = reactive({ station_id: '', set_id: '', set_name: '' })

/** 当前流程拓扑内的工位（筛选与新增只用本流程工位，按工步序排列） */
const processStations = computed(() =>
  steps.value.map((s) => ({ station_id: s.station_id, station_name: s.station_name || '' }))
)
const stationNameMap = computed(
  () => new Map(processStations.value.map((s) => [s.station_id, s.station_name]))
)
/** 列内展示字段码，悬停提示名称（与集合列同一风格；无名称时不出提示） */
function stationNameOf(stationId) {
  return stationNameMap.value.get(stationId) || ''
}

/** 锁定工位的用例清单由基线固化（不可手工增删改） */
const lockedMap = computed(() => {
  const map = {}
  for (const s of steps.value) {
    if (s.baseline_locked) map[s.station_id] = true
  }
  return map
})
function isStationLocked(stationId) {
  return !!lockedMap.value[stationId]
}

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({
  item_id: null,
  station_id: '',
  set_id: '',
  case_id: '',
  item_name: '',
  is_mandatory: true,
  is_active: true,
})

async function loadItems() {
  if (!processId.value) {
    items.value = []
    caseSets.value = []
    steps.value = []
    return
  }
  loading.value = true
  try {
    const [itemRes, stepRes, setRes] = await Promise.all([
      routingApi.listItems(processId.value, stationFilter.value),
      routingApi.listSteps(processId.value),
      routingApi.listCaseSets(processId.value, stationFilter.value),
    ])
    items.value = itemRes.data
    steps.value = stepRes.data
    caseSets.value = setRes.data
  } catch (e) {
    items.value = []
    caseSets.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

function openCreateSet() {
  Object.assign(setForm, {
    station_id: stationFilter.value || processStations.value[0]?.station_id || '',
    set_id: '',
    set_name: '',
  })
  setDialogVisible.value = true
}

async function submitSet() {
  if (!setForm.station_id || !setForm.set_id.trim()) {
    return ElMessage.warning(t('errors.requiredField'))
  }
  setSaving.value = true
  try {
    await routingApi.createCaseSet({
      process_id: processId.value,
      station_id: setForm.station_id,
      set_id: setForm.set_id.trim(),
      set_name: setForm.set_name.trim(),
    })
    ElMessage.success(t('common.saveSuccess'))
    setDialogVisible.value = false
    await loadItems()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    setSaving.value = false
  }
}

/** 删除左侧筛选选中的集合（连带其用例清单）：不可恢复，二次确认 */
async function removeSet() {
  const setId = setFilter.value
  const stationId = selectedSetStation.value
  if (!setId || !stationId) {
    return ElMessage.warning(t('configs.delCaseSetTip'))
  }
  const name = setNameOf(stationId, setId) || setId
  try {
    await ElMessageBox.confirm(
      t('configs.delCaseSetConfirm', { name, set_id: setId, station_id: stationId }),
      t('common.confirm'),
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await routingApi.deleteCaseSet(processId.value, stationId, setId)
    ElMessage.success(t('common.deleteSuccess'))
    setFilter.value = ''
    await loadItems()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, {
    item_id: null,
    station_id: stationFilter.value || processStations.value[0]?.station_id || '',
    set_id: setFilter.value || setOptions.value[0] || '',
    case_id: '',
    item_name: '',
    is_mandatory: true,
    is_active: true,
  })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  Object.assign(form, {
    item_id: row.item_id,
    station_id: row.station_id,
    set_id: row.set_id,
    case_id: row.case_id,
    item_name: row.item_name || '',
    is_mandatory: !!row.is_mandatory,
    is_active: !!row.is_active,
  })
  dialogVisible.value = true
}

async function submit() {
  if (!form.station_id || !form.set_id || !form.case_id.trim()) {
    return ElMessage.warning(t('errors.requiredField'))
  }
  saving.value = true
  try {
    const payload = {
      item_name: form.item_name || form.case_id,
      is_mandatory: form.is_mandatory,
      is_active: form.is_active,
    }
    if (isEdit.value) {
      await routingApi.updateItem(form.item_id, payload)
    } else {
      await routingApi.createItem({
        process_id: processId.value,
        station_id: form.station_id,
        set_id: form.set_id,
        case_id: form.case_id.trim(),
        ...payload,
      })
    }
    ElMessage.success(t('common.saveSuccess'))
    dialogVisible.value = false
    loadItems()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

/** 行内直接改执行序号：立即落库（清空 = 重置为未编号，排到队尾） */
async function onOrderChange(row) {
  try {
    await routingApi.updateItem(row.item_id, { sort_order: row.sort_order ?? null })
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
    loadItems()
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(t('common.yesDeleteItem'), t('common.confirm'), { type: 'warning' })
  } catch {
    return
  }
  try {
    await routingApi.removeItem(row.item_id)
    ElMessage.success(t('common.deleteSuccess'))
    loadItems()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// 切流程：工位/集合筛选都必须落在新流程内（先清空，再触发下面的统一拉取）
watch(processId, () => {
  stationFilter.value = ''
  setFilter.value = ''
})
// 换工位：集合筛选可能不属于该工位
watch(stationFilter, () => {
  setFilter.value = ''
})
watch([processId, stationFilter], () => {
  page.value = 1
  loadItems()
})
// 集合筛选在前端本地完成，无需重新拉取
watch(setFilter, () => {
  page.value = 1
})

useTabRefresh('items', loadItems)

onMounted(async () => {
  await loadProcesses()
  if (!processId.value && processes.value.length) {
    // 赋值即触发 processId 监听里的首次拉取，避免重复请求
    processId.value = processes.value[0].process_id
    return
  }
  await loadItems()
})
</script>

<style scoped>
.switch-row { display: flex; gap: 20px; }
</style>
