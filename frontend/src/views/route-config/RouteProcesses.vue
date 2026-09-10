<template>
  <div>
    <div class="tab-head tab-head-end">
      <div class="filter-row">
        <el-button size="small" :icon="CopyDocument" @click="cloneVisible = true">{{ t('configs.clone') }}</el-button>
        <el-button type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="processes" stripe size="small">
      <el-table-column prop="process_id" :label="t('configs.processId')" width="220">
        <template #default="{ row }"><span class="code">{{ row.process_id }}</span></template>
      </el-table-column>
      <el-table-column prop="process_name" :label="t('configs.processName')" min-width="220" />
      <el-table-column :label="t('configs.tableModelCount')" width="180">
        <template #default="{ row }">
          <el-tag v-for="m in row.models" :key="m" size="small" effect="plain" style="margin:2px">{{ m }}</el-tag>
          <span v-if="!row.models?.length" class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="station_count" :label="t('configs.tableStationCount')" width="90" align="right" />
      <el-table-column prop="item_count" :label="t('configs.tableItemCount')" width="100" align="right" />
      <el-table-column :label="t('configs.tableVersion')" width="80" align="right">
        <template #default="{ row }"><span class="muted">v{{ row.version ?? 1 }}</span></template>
      </el-table-column>
      <el-table-column :label="t('common.status')" width="100" align="center">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="row.is_active ? 'success' : 'info'">
            {{ row.is_active ? t('common.enabled') : t('common.disabled') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="180" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
          <el-button
            link
            size="small"
            :type="row.is_active ? 'info' : 'success'"
            @click="onToggleActive(row)"
          >
            {{ row.is_active ? t('common.disabled') : t('common.enabled') }}
          </el-button>
          <el-button link type="danger" size="small" @click="onDelete(row)">{{ t('common.delete') }}</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? t('configs.editProcess') : t('configs.newProcess')"
      width="480px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('configs.processId')" required>
          <el-input v-model="form.process_id" :disabled="isEdit" placeholder="PROC_TEK_MSO" />
        </el-form-item>
        <el-form-item :label="t('configs.processName')">
          <el-input v-model="form.process_name" placeholder="TEK数字示波器-带AWG选件流程" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="cloneVisible" :title="t('configs.cloneTitle')" width="460px" destroy-on-close class="form-dialog">
      <el-form :model="cloneForm" label-position="top">
        <el-form-item :label="t('configs.cloneFrom')" required>
          <el-select v-model="cloneForm.from_process" filterable style="width:100%">
            <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('configs.cloneTo')" required>
          <el-input v-model="cloneForm.to_process" placeholder="PROC_TEK_MSO6B" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cloneVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="cloneSaving" @click="submitClone">{{ t('common.confirm') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Plus } from '@element-plus/icons-vue'
import { processApi, routingApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { processes, loading, loadProcesses } = useProcesses()

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({ process_id: '', process_name: '' })

const cloneVisible = ref(false)
const cloneSaving = ref(false)
const cloneForm = reactive({ from_process: '', to_process: '' })

function openCreate() {
  isEdit.value = false
  Object.assign(form, { process_id: '', process_name: '' })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  Object.assign(form, { process_id: row.process_id, process_name: row.process_name || '' })
  dialogVisible.value = true
}

async function submit() {
  if (!form.process_id.trim()) return ElMessage.warning(t('errors.requiredField'))
  saving.value = true
  try {
    const payload = { process_name: form.process_name || form.process_id }
    if (isEdit.value) {
      await processApi.update(form.process_id, payload)
    } else {
      await processApi.create({ process_id: form.process_id.trim(), ...payload })
    }
    ElMessage.success(t('common.saveSuccess'))
    dialogVisible.value = false
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onToggleActive(row) {
  try {
    await processApi.update(row.process_id, { is_active: !row.is_active })
    ElMessage.success(t('common.saveSuccess'))
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(t('configs.yesDelete', { name: row.process_id }), t('common.confirm'), {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await processApi.remove(row.process_id)
    ElMessage.success(t('common.deleteSuccess'))
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function submitClone() {
  if (!cloneForm.from_process || !cloneForm.to_process.trim()) {
    return ElMessage.warning(t('errors.requiredField'))
  }
  cloneSaving.value = true
  try {
    const res = await routingApi.clone({
      from_process: cloneForm.from_process,
      to_process: cloneForm.to_process.trim(),
    })
    ElMessage.success(`${t('common.save')} · ${res.data.cloned_steps} steps / ${res.data.cloned_items} items`)
    cloneVisible.value = false
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    cloneSaving.value = false
  }
}

onMounted(() => loadProcesses())
</script>
