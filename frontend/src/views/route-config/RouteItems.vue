<template>
  <div>
    <div class="tab-head">
      <div class="filter-row">
        <el-select v-model="processId" filterable :placeholder="t('common.process')" style="width:240px">
          <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
        </el-select>
        <el-select v-model="stationFilter" clearable :placeholder="t('configs.filterStation2')" style="width:180px">
          <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
        </el-select>
        <el-button :icon="RefreshRight" size="small" @click="loadItems">{{ t('common.refresh') }}</el-button>
      </div>
      <el-button type="primary" size="small" :icon="Plus" :disabled="!processId" @click="openCreate">
        {{ t('configs.newItem') }}
      </el-button>
    </div>

    <el-table v-loading="loading" :data="items" stripe size="small">
      <el-table-column prop="station_id" :label="t('configs.tabStations')" width="150">
        <template #default="{ row }"><span class="code">{{ row.station_id }}</span></template>
      </el-table-column>
      <el-table-column prop="case_id" :label="t('configs.caseId')" min-width="240" show-overflow-tooltip>
        <template #default="{ row }"><span class="code">{{ row.case_id }}</span></template>
      </el-table-column>
      <el-table-column prop="item_name" :label="t('configs.itemName')" min-width="200" show-overflow-tooltip />
      <el-table-column :label="t('configs.mandatory')" width="100" align="center">
        <template #default="{ row }">
          <el-tag :type="row.is_mandatory ? 'danger' : 'info'" size="small" :effect="row.is_mandatory ? 'dark' : 'plain'">
            {{ row.is_mandatory ? t('configs.mandatory') : t('configs.optional2') }}
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
          <el-button link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
          <el-button link type="danger" size="small" @click="onDelete(row)">{{ t('common.delete') }}</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? t('configs.editItem') : t('configs.newItem')"
      width="600px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-row :gutter="12">
          <el-col :span="12">
            <el-form-item :label="t('configs.tabStations')" required>
              <el-select v-model="form.station_id" filterable :disabled="isEdit" style="width:100%">
                <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="12">
            <el-form-item :label="t('configs.itemName')">
              <el-input v-model="form.item_name" placeholder="CHn幅度校准" />
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item :label="t('configs.caseId')" required>
          <el-input v-model="form.case_id" :disabled="isEdit" :placeholder="t('configs.caseIdPh')" />
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
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, RefreshRight } from '@element-plus/icons-vue'
import { routingApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { processes, stations, loadProcesses } = useProcesses()

const processId = ref('')
const stationFilter = ref('')
const items = ref([])
const loading = ref(false)

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({
  item_id: null,
  station_id: '',
  case_id: '',
  item_name: '',
  is_mandatory: true,
  is_active: true,
})

async function loadItems() {
  if (!processId.value) {
    items.value = []
    return
  }
  loading.value = true
  try {
    const res = await routingApi.listItems(processId.value, stationFilter.value)
    items.value = res.data
  } catch (e) {
    items.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, {
    item_id: null,
    station_id: stationFilter.value || '',
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
    case_id: row.case_id,
    item_name: row.item_name || '',
    is_mandatory: !!row.is_mandatory,
    is_active: !!row.is_active,
  })
  dialogVisible.value = true
}

async function submit() {
  if (!form.station_id || !form.case_id.trim()) return ElMessage.warning(t('errors.requiredField'))
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

watch([processId, stationFilter], () => loadItems())

onMounted(async () => {
  await loadProcesses()
  if (!processId.value && processes.value.length) processId.value = processes.value[0].process_id
  await loadItems()
})
</script>

<style scoped>
.switch-row { display: flex; gap: 20px; }
</style>
