<template>
  <div>
    <div class="tab-head">
      <div class="filter-row">
        <el-select v-model="processId" filterable :placeholder="t('common.process')" style="width:240px">
          <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
        </el-select>
        <el-button size="small" :icon="CircleCheck" :disabled="!processId" @click="onValidate">
          {{ t('configs.validate') }}
        </el-button>
      </div>
      <div class="filter-row">
        <el-button size="small" :icon="Plus" :disabled="!processId" @click="addStep">{{ t('configs.addStep') }}</el-button>
        <el-button type="primary" size="small" :loading="saving" :disabled="!processId" @click="save">
          {{ t('configs.saveTopology') }}
        </el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="steps" stripe size="small">
      <el-table-column :label="t('configs.stepOrder')" width="130">
        <template #default="{ row }">
          <el-input-number v-model="row.step_order" :min="1" :max="999" controls-position="right" style="width:100%" />
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.tabStations')" width="200">
        <template #default="{ row }">
          <el-select v-model="row.station_id" filterable style="width:100%">
            <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.dependsOn')" min-width="320">
        <template #default="{ row }">
          <el-select
            v-model="row.depends_on"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :max-collapse-tags="5"
            style="width:100%"
          >
            <el-option
              v-for="s in steps"
              :key="s.station_id"
              :value="s.station_id"
              :label="s.station_id"
              :disabled="s.station_id === row.station_id"
            />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.itemCount')" width="90" align="right">
        <template #default="{ row }">{{ itemCountOf(row.station_id) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="90" align="center">
        <template #default="{ $index }">
          <el-button v-if="isAdmin" link type="danger" size="small" @click="removeRow($index)">{{ t('common.delete') }}</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <el-drawer v-model="validateVisible" size="560px" destroy-on-close>
      <div v-if="validateResult">
        <el-alert
          :title="validateMeta.title"
          :type="validateMeta.type"
          :closable="false"
          show-icon
        />
        <el-table v-if="validateResult.issues.length" :data="validateResult.issues" size="small" style="margin-top:12px">
          <el-table-column :label="t('configs.issueLevel')" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="row.level === 'error' ? 'danger' : 'warning'" size="small">{{ row.level }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="code" :label="t('configs.issueCode')" width="170" show-overflow-tooltip>
            <template #default="{ row }"><span class="code">{{ row.code }}</span></template>
          </el-table-column>
          <el-table-column prop="detail" :label="t('configs.issueDetail')" min-width="180" show-overflow-tooltip />
        </el-table>
        <EmptyState v-else :text="t('configs.validateOk')" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../../stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Plus } from '@element-plus/icons-vue'
import { routingApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { isAdmin } = useAuth()
const { processes, stations, loadProcesses } = useProcesses()

const processId = ref('')
const steps = ref([])
const loading = ref(false)
const saving = ref(false)
const savedStations = ref([])

const validateVisible = ref(false)
const validateResult = ref(null)

const validateMeta = computed(() => {
  const r = validateResult.value
  if (!r) return { title: t('configs.validateTitle'), type: 'info' }
  const warns = (r.issues || []).filter((i) => i.level === 'warning').length
  if (!r.ok) return { title: t('configs.validateFail'), type: 'error' }
  if (warns > 0) return { title: `${t('configs.validateWarn')}（${warns}）`, type: 'warning' }
  return { title: t('configs.validateOk'), type: 'success' }
})
const allItems = ref([])

const itemCountMap = computed(() => {
  const map = {}
  for (const item of allItems.value) {
    if (!item.is_active) continue
    map[item.station_id] = (map[item.station_id] || 0) + 1
  }
  return map
})
function itemCountOf(stationId) {
  return itemCountMap.value[stationId] || 0
}

async function loadSteps() {
  if (!processId.value) {
    steps.value = []
    allItems.value = []
    return
  }
  loading.value = true
  try {
    const [stepRes, itemRes] = await Promise.all([
      routingApi.listSteps(processId.value),
      routingApi.listItems(processId.value),
    ])
    steps.value = stepRes.data.map((s) => ({ ...s, depends_on: [...(s.depends_on || [])] }))
    savedStations.value = steps.value.map((s) => s.station_id)
    allItems.value = itemRes.data
  } catch (e) {
    steps.value = []
    savedStations.value = []
    allItems.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

/** 本次保存会从拓扑中移除的工位（新增/改序不算）。 */
function removedStations() {
  const current = new Set(steps.value.map((s) => s.station_id).filter(Boolean))
  return savedStations.value.filter((sid) => sid && !current.has(sid))
}

function addStep() {
  const nextOrder = steps.value.length ? Math.max(...steps.value.map((s) => s.step_order)) + 10 : 10
  steps.value.push({ station_id: '', step_order: nextOrder, depends_on: [] })
}

function removeRow(index) {
  steps.value.splice(index, 1)
}

async function save() {
  if (!processId.value) return
  if (steps.value.some((s) => !s.station_id)) return ElMessage.warning(t('errors.requiredField'))

  const dropped = removedStations()
  const droppedItems = allItems.value.filter((i) => dropped.includes(i.station_id)).length
  if (droppedItems) {
    try {
      await ElMessageBox.confirm(
        t('configs.removeStepWarn', { stations: dropped.join(', '), count: droppedItems }),
        t('common.confirm'),
        { type: 'warning' }
      )
    } catch {
      return
    }
  }

  saving.value = true
  try {
    await routingApi.saveSteps(
      processId.value,
      steps.value.map((s) => ({
        station_id: s.station_id,
        step_order: s.step_order,
        depends_on: s.depends_on || [],
      }))
    )
    ElMessage.success(t('configs.topologySaved'))
    await loadSteps()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onValidate() {
  try {
    const res = await routingApi.validate(processId.value)
    validateResult.value = res.data
    validateVisible.value = true
  } catch (e) {
    ElMessage.error(e.message)
  }
}

watch(processId, () => loadSteps())

onMounted(async () => {
  await loadProcesses()
  if (!processId.value && processes.value.length) processId.value = processes.value[0].process_id
  await loadSteps()
})
</script>

