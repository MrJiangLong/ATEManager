<template>
  <div>
    <div class="tab-head tab-head-end">
      <div class="filter-row">
        <el-button size="small" :icon="CopyDocument" @click="cloneVisible = true">{{ t('configs.clone') }}</el-button>
        <el-button type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="paged" stripe size="small">
      <el-table-column prop="process_id" :label="t('configs.processId')" width="220">
        <template #default="{ row }"><span class="code">{{ row.process_id }}</span></template>
      </el-table-column>
      <el-table-column prop="process_name" :label="t('configs.processName')" min-width="220" />
      <el-table-column :label="t('configs.tableModelCount')" width="180">
        <template #default="{ row }">
          <!-- 机型多时折叠为 +N，悬浮查看全部（与机台管理页绑定工位同款交互） -->
          <div v-if="row.models?.length" class="model-tags">
            <el-tag v-for="m in row.models.slice(0, 2)" :key="m" size="small" effect="plain">{{ m }}</el-tag>
            <el-tooltip v-if="row.models.length > 2" :content="row.models.join(', ')" placement="top">
              <el-tag size="small" effect="plain" type="info">+{{ row.models.length - 2 }}</el-tag>
            </el-tooltip>
          </div>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="station_count" :label="t('configs.tableStationCount')" width="90" align="right" />
      <el-table-column prop="item_count" :label="t('configs.tableItemCount')" width="100" align="right" />
      <el-table-column :label="t('common.status')" width="90" align="center">
        <template #default="{ row }">
          <el-tag :type="row.is_active ? 'success' : 'info'" size="small" effect="plain">
            {{ row.is_active ? 'ON' : 'OFF' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="210" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" :icon="View" @click="openDetail(row)">
            {{ t('configs.detail') }}
          </el-button>
          <el-button link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
          <el-button link type="danger" size="small" @click="onDelete(row)">{{ t('common.delete') }}</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <div class="pager">
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="processes.length"
        :page-sizes="[20, 50, 100]"
        layout="total, sizes, prev, pager, next"
        background
        size="small"
      />
    </div>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? t('configs.editProcess') : t('configs.newProcess')"
      width="480px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('configs.processId')" required>
          <el-input v-model="form.process_id" :disabled="isEdit" placeholder="PROC-SCOPE-MSO-AWG" />
        </el-form-item>
        <el-form-item :label="t('configs.processName')">
          <el-input v-model="form.process_name" placeholder="TEK数字示波器-带AWG选件流程" />
        </el-form-item>
        <div class="switch-row">
          <el-checkbox v-model="form.is_active">{{ t('common.enabled') }}</el-checkbox>
        </div>
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
          <el-input v-model="cloneForm.to_process" placeholder="PROC-SCOPE-MSO6-BASE" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cloneVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="cloneSaving" @click="submitClone">{{ t('common.confirm') }}</el-button>
      </template>
    </el-dialog>
    <ProcessDetailDrawer v-model="detailVisible" :process="detailRow" @edit="openEdit" />
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Plus, View } from '@element-plus/icons-vue'
import { processApi, routingApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import ProcessDetailDrawer from '../../components/ProcessDetailDrawer.vue'
import { useLocalPagination } from '../../composables/usePagination'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { processes, loading, loadProcesses } = useProcesses()
const { page, pageSize, paged } = useLocalPagination(processes)

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({ process_id: '', process_name: '', is_active: true })

const cloneVisible = ref(false)
const cloneSaving = ref(false)
const cloneForm = reactive({ from_process: '', to_process: '' })

// 流程详情抽屉
const detailVisible = ref(false)
const detailRow = ref(null)
function openDetail(row) {
  detailRow.value = row
  detailVisible.value = true
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, { process_id: '', process_name: '', is_active: true })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  Object.assign(form, {
    process_id: row.process_id,
    process_name: row.process_name || '',
    is_active: row.is_active !== false,
  })
  dialogVisible.value = true
}

async function submit() {
  if (!form.process_id.trim()) return ElMessage.warning(t('errors.requiredField'))
  saving.value = true
  try {
    const payload = { process_name: form.process_name || form.process_id, is_active: form.is_active }
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

<style scoped>
.switch-row { display: flex; gap: 20px; }
/* 机型标签：换行排布，超 2 个折叠为 +N（tooltip 看全部） */
.model-tags { display: flex; flex-wrap: wrap; gap: 4px; }
</style>
