<template>
  <div>
    <div class="tab-head tab-head-end">
      <el-button type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
    </div>

    <el-table v-loading="loading" :data="list" stripe size="small">
      <el-table-column prop="station_id" :label="t('configs.stationId')" width="180">
        <template #default="{ row }"><span class="code">{{ row.station_id }}</span></template>
      </el-table-column>
      <el-table-column prop="station_name" :label="t('configs.stationName')" min-width="220" />
      <el-table-column prop="timeout_sec" :label="t('configs.timeoutSec')" width="140" align="right">
        <template #default="{ row }">{{ row.timeout_sec }} s</template>
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
      :title="isEdit ? t('configs.editStation') : t('configs.newStation')"
      width="480px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('configs.stationId')" required>
          <el-input v-model="form.station_id" :disabled="isEdit" placeholder="CAL-PARAM" />
        </el-form-item>
        <el-form-item :label="t('configs.stationName')">
          <el-input v-model="form.station_name" placeholder="校准-指标测试站位" />
        </el-form-item>
        <el-form-item :label="t('configs.timeoutSec')">
          <el-input-number v-model="form.timeout_sec" :min="30" :max="86400" :step="30" style="width:100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { stationApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { loadProcesses } = useProcesses()
const list = ref([])
const loading = ref(false)

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({ station_id: '', station_name: '', timeout_sec: 1800 })

async function loadStations() {
  loading.value = true
  try {
    const res = await stationApi.list()
    list.value = res.data
  } catch (e) {
    list.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, { station_id: '', station_name: '', timeout_sec: 1800 })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  Object.assign(form, {
    station_id: row.station_id,
    station_name: row.station_name || '',
    timeout_sec: row.timeout_sec || 1800,
  })
  dialogVisible.value = true
}

async function submit() {
  if (!form.station_id.trim()) return ElMessage.warning(t('errors.requiredField'))
  saving.value = true
  try {
    const payload = {
      station_name: form.station_name || form.station_id,
      timeout_sec: form.timeout_sec,
    }
    if (isEdit.value) {
      await stationApi.update(form.station_id, payload)
    } else {
      await stationApi.create({ station_id: form.station_id.trim(), ...payload })
    }
    ElMessage.success(t('common.saveSuccess'))
    dialogVisible.value = false
    loadStations()
    // 工位字典属于共享主数据，落库后刷新全局缓存供其它页面下拉使用
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(t('configs.yesDeleteStation', { name: row.station_id }), t('common.confirm'), {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await stationApi.remove(row.station_id)
    ElMessage.success(t('common.deleteSuccess'))
    loadStations()
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(() => loadStations())
</script>
