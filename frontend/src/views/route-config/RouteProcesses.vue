<template>
  <div>
    <div class="tab-head tab-head-end">
      <div class="filter-row">
        <el-button v-if="isAdmin" size="small" :icon="CopyDocument" @click="cloneVisible = true">{{ t('configs.clone') }}</el-button>
        <el-button size="small" :icon="Download" :loading="exportingAll" @click="exportAllJson">
          {{ t('configs.exportAll') }}
        </el-button>
        <el-button v-if="isAdmin" size="small" :icon="Upload" @click="importVisible = true">{{ t('configs.importJson') }}</el-button>
        <el-button v-if="isAdmin" type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="paged" stripe size="small">
      <el-table-column prop="process_id" :label="t('configs.processId')" width="220">
        <template #default="{ row }"><span class="code">{{ row.process_id }}</span></template>
      </el-table-column>
      <el-table-column prop="process_name" :label="t('configs.processName')" min-width="220" />
      <el-table-column :label="t('configs.tableModelCount')" width="180">
        <template #default="{ row }">
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
          <el-button v-if="isAdmin" link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
          <el-button v-if="isAdmin" link type="danger" size="small" @click="onDelete(row)">{{ t('common.delete') }}</el-button>
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
    <ProcessDetailDrawer v-model:visible="detailVisible" :process="detailRow" @edit="openEdit" />

    <el-dialog
      v-model="importVisible"
      :title="t('configs.importTitle')"
      width="520px"
      destroy-on-close
      class="form-dialog"
      @closed="resetImport"
    >
      <input ref="importFileEl" type="file" accept=".json,application/json" style="display: none" @change="onImportFile" />
      <el-button class="import-pick" :icon="FolderOpened" @click="importFileEl?.click()">
        {{ t('configs.importPick') }}
      </el-button>
      <template v-if="importDoc">
        <div class="import-preview">
          <div class="imp-file">
            <el-icon><Document /></el-icon>
            <span class="imp-file-name">{{ importFileName }}</span>
          </div>

          <template v-if="!importBatch">
            <div class="imp-head">
              <span class="code imp-pid">{{ importDoc.process?.process_id }}</span>
              <span class="muted">{{ importDoc.process?.process_name || '—' }}</span>
            </div>
            <div class="imp-stats">
              <div class="imp-stat">
                <b>{{ importDoc.stations?.length || 0 }}</b><span>{{ t('configs.impStations') }}</span>
              </div>
              <div class="imp-stat">
                <b>{{ importDoc.items?.length || 0 }}</b><span>{{ t('configs.impCases') }}</span>
              </div>
              <div class="imp-stat">
                <b>{{ importMandatory }}</b><span>{{ t('configs.impMandatory') }}</span>
              </div>
              <div class="imp-stat">
                <b>{{ importDoc.models?.length || 0 }}</b><span>{{ t('configs.impModels') }}</span>
              </div>
            </div>
            <div class="import-row">
              <span class="import-label">{{ t('configs.importIdLabel') }}</span>
              <el-input v-model="importId" placeholder="PROC-SCOPE-DPO-BASE" />
            </div>
          </template>

          <template v-else>
            <div class="imp-stats">
              <div class="imp-stat">
                <b>{{ importBatch.length }}</b><span>{{ t('configs.impProcesses') }}</span>
              </div>
            </div>
            <div class="imp-tags-scroll">
              <el-tag v-for="p in importBatch" :key="p.process?.process_id" size="small" effect="plain" class="code">
                {{ p.process?.process_id }}
              </el-tag>
            </div>
            <div class="import-row">
              <span class="import-label">{{ t('configs.importIdLabel') }}</span>
              <span class="muted">{{ t('configs.importBatchKeepId') }}</span>
            </div>
          </template>

          <div v-if="importing && importBatch" class="import-progress">
            <el-progress
              :percentage="importProgress.total ? Math.round((importProgress.current / importProgress.total) * 100) : 0"
              :stroke-width="8"
            />
            <span class="muted import-progress-text">
              {{ t('configs.importProgress', { i: importProgress.current, n: importProgress.total, id: importProgress.id }) }}
            </span>
          </div>
        </div>
      </template>
      <template #footer>
        <el-button @click="importVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="importing" :disabled="!importDoc" @click="submitImport">
          {{ t('configs.importDo') }}
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="resultVisible" :title="t('configs.importResultTitle')" width="520px" class="form-dialog">
      <template v-if="importResult">
        <div class="imp-summary">
          <el-tag type="success" effect="plain" size="large">{{ t('configs.impOk') }} {{ importResult.ok.length }}</el-tag>
          <el-tag v-if="importResult.skipped.length" type="warning" effect="plain" size="large">
            {{ t('configs.impSkip') }} {{ importResult.skipped.length }}
          </el-tag>
          <el-tag v-if="importResult.failed.length" type="danger" effect="plain" size="large">
            {{ t('configs.impFail') }} {{ importResult.failed.length }}
          </el-tag>
        </div>
        <div v-if="importResult.ok.length" class="imp-sec">
          <div class="imp-label">{{ t('configs.impOkList') }}</div>
          <div class="imp-tags">
            <el-tag v-for="p in importResult.ok" :key="p" size="small" effect="plain" type="success">{{ p }}</el-tag>
          </div>
        </div>
        <div v-if="importResult.skipped.length" class="imp-sec">
          <div class="imp-label">{{ t('configs.impSkipList') }}</div>
          <div class="imp-tags">
            <el-tag v-for="p in importResult.skipped" :key="p" size="small" effect="plain" type="warning">{{ p }}</el-tag>
          </div>
        </div>
        <div v-if="importResult.failed.length" class="imp-sec">
          <div class="imp-label">{{ t('configs.impFailList') }}</div>
          <div v-for="f in importResult.failed" :key="f.id" class="imp-fail">
            <span class="code imp-fail-id">{{ f.id }}</span>
            <span class="imp-fail-msg">{{ f.msg }}</span>
          </div>
        </div>
      </template>
      <template #footer>
        <el-button type="primary" @click="resultVisible = false">{{ t('common.close') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../../stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CopyDocument, Document, Download, FolderOpened, Plus, Upload, View } from '@element-plus/icons-vue'
import { processApi, routingApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import ProcessDetailDrawer from '../../components/ProcessDetailDrawer.vue'
import { useLocalPagination } from '../../composables/usePagination'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { isAdmin } = useAuth()
const { processes, loading, loadProcesses } = useProcesses()
const { page, pageSize, paged } = useLocalPagination(processes)

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({ process_id: '', process_name: '', is_active: true })

const cloneVisible = ref(false)
const cloneSaving = ref(false)
const cloneForm = reactive({ from_process: '', to_process: '' })

const detailVisible = ref(false)
const detailRow = ref(null)
function openDetail(row) {
  detailRow.value = row
  detailVisible.value = true
}

const exportingAll = ref(false)
async function exportAllJson() {
  exportingAll.value = true
  try {
    const res = await routingApi.exportAllProcesses()
    const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
    const a = document.createElement('a')
    a.href = URL.createObjectURL(new Blob([res.data], { type: 'application/json' }))
    a.download = `processes-all-${stamp}.json`
    a.click()
    URL.revokeObjectURL(a.href)
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    exportingAll.value = false
  }
}

const importVisible = ref(false)
const importing = ref(false)
const importDoc = ref(null)
const importId = ref('')
const importFileName = ref('')
const importFileEl = ref(null)
const importMandatory = computed(() =>
  (importDoc.value?.items || []).filter((i) => i.is_mandatory !== false).length,
)
const importBatch = computed(() => importDoc.value?.__batch || null)

function resetImport() {
  importDoc.value = null
  importId.value = ''
  importFileName.value = ''
}

function onImportFile(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  importFileName.value = file.name
  const reader = new FileReader()
  reader.onload = () => {
    try {
      const doc = JSON.parse(reader.result)
      if (Array.isArray(doc.processes)) {
        if (!doc.processes.length) throw new Error('empty export file')
        importDoc.value = { __batch: doc.processes }
        importId.value = `${doc.processes.length}`
        return
      }
      if (!doc.process?.process_id || !Array.isArray(doc.steps) || !Array.isArray(doc.items)) {
        throw new Error('missing process/steps/items')
      }
      importDoc.value = doc
      importId.value = doc.process.process_id
    } catch (err) {
      ElMessage.error(`${t('configs.importBadFile')}: ${err.message}`)
    }
  }
  reader.readAsText(file, 'utf-8')
}

const resultVisible = ref(false)
const importResult = ref(null)
const importProgress = reactive({ current: 0, total: 0, id: '' })

async function submitImport() {
  if (!importDoc.value) return
  if (importBatch.value) {
    importing.value = true
    importProgress.current = 0
    importProgress.total = importBatch.value.length
    importProgress.id = ''
    const okList = []
    const skipList = []
    const failList = []
    try {
      for (const single of importBatch.value) {
        const pid = single.process.process_id
        importProgress.id = pid
        try {
          await routingApi.importProcess(single)
          okList.push(pid)
        } catch (e) {
          if (e.code === 'process_already_exists') skipList.push(pid)
          else failList.push({ id: pid, msg: e.message })
        } finally {
          importProgress.current += 1
        }
      }
    } finally {
      importing.value = false
    }
    importVisible.value = false
    importResult.value = { ok: okList, skipped: skipList, failed: failList }
    resultVisible.value = true
    loadProcesses({ force: true })
    return
  }
  importing.value = true
  try {
    const doc = {
      ...importDoc.value,
      process: { ...importDoc.value.process, process_id: importId.value.trim() },
    }
    const res = await routingApi.importProcess(doc)
    const b = res.data || {}
    ElMessage.success(
      `${t('configs.importDone')} +${b.stations_created || 0} / +${b.models_created || 0}` +
        ` / +${b.steps_created || 0} / +${b.items_created || 0}` +
        (b.models_skipped ? ` (${t('configs.modelsSkipped', { n: b.models_skipped })})` : ''),
    )
    importVisible.value = false
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    importing.value = false
  }
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
/* 导入预览 */
.import-pick { margin-bottom: 14px; }
.import-preview { display: flex; flex-direction: column; gap: 12px; margin-top: 6px; }
.import-row { display: flex; align-items: center; gap: 10px; }
.import-label { flex: none; width: 110px; color: var(--app-text-muted, #8a94a6); font-size: 12.5px; }
/* 批量导入进度 */
.import-progress { display: flex; flex-direction: column; gap: 6px; }
.import-progress-text { font-size: 12.5px; }
/* 导入预览：文件信息 / 标题 / 统计卡 / 批量标签云 */
.imp-file {
  display: flex; align-items: center; gap: 6px;
  font-size: 12.5px; color: var(--app-text-muted, #8a94a6);
}
.imp-file-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.imp-head { display: flex; align-items: baseline; gap: 10px; min-width: 0; }
.imp-pid { font-size: 15px; font-weight: 600; }
.imp-stats { display: flex; gap: 8px; }
.imp-stat {
  flex: 1; display: flex; flex-direction: column; align-items: center; gap: 2px;
  padding: 10px 0; border: 1px solid var(--app-border, #eef1f7); border-radius: 8px;
}
.imp-stat b { font-size: 16px; line-height: 1.2; }
.imp-stat span { font-size: 12px; color: var(--app-text-muted, #8a94a6); }
.imp-tags-scroll {
  display: flex; flex-wrap: wrap; gap: 4px;
  max-height: 132px; overflow-y: auto;
  padding: 8px; border: 1px dashed var(--app-border, #eef1f7); border-radius: 8px;
}
/* 批量导入结果对话框 */
.imp-summary { display: flex; gap: 8px; margin-bottom: 14px; }
.imp-sec { margin-top: 12px; }
.imp-label { color: var(--app-text-muted, #8a94a6); font-size: 12.5px; margin-bottom: 6px; }
.imp-tags { display: flex; flex-wrap: wrap; gap: 4px; }
.imp-fail {
  display: flex; align-items: baseline; gap: 8px;
  padding: 6px 8px; margin-bottom: 4px;
  background: var(--app-danger-bg, #fdf0ef); border-radius: 6px;
  font-size: 12.5px;
}
.imp-fail-id { flex: none; font-weight: 600; }
.imp-fail-msg { color: var(--app-text-muted, #8a94a6); word-break: break-all; }
</style>

