<template>
  <div class="rules-panel">
    <div class="tab-head">
      <div class="filter-row">
        <span class="muted">{{ t('rules.hint') }}</span>
      </div>
      <el-button type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('rules.addRule') }}</el-button>
    </div>

    <el-table :data="rows" v-loading="loading" size="small" border stripe class="rules-table">
      <el-table-column prop="rule" :label="t('rules.ruleId')" min-width="110" show-overflow-tooltip />
      <el-table-column :label="t('rules.models')" min-width="200">
        <template #default="{ row }">
          <el-tooltip :disabled="row.models.length <= 3" :content="row.models.join(', ')" placement="top">
            <div class="models-cell">
              <el-tag v-for="m in row.models.slice(0, 3)" :key="m" size="small" effect="plain" class="prefix-tag">{{ m }}</el-tag>
              <el-tag v-if="row.models.length > 3" size="small" effect="plain" type="info">+{{ row.models.length - 3 }}</el-tag>
            </div>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column :label="t('rules.script')" width="150" align="center" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.has_script" class="script-name">{{ row.script_name }}</span>
          <el-tag v-else size="small" effect="plain" type="danger">{{ t('rules.scriptMissing') }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('rules.templates')" width="90" align="center">
        <template #default="{ row }">{{ row.templates.length }}</template>
      </el-table-column>
      <el-table-column :label="t('rules.autoTrigger')" width="90" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.auto_trigger" @change="(v) => toggleFlag(row, 'auto_trigger', v)" />
        </template>
      </el-table-column>
      <el-table-column :label="t('rules.enabled')" width="90" align="center">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" @change="(v) => toggleFlag(row, 'enabled', v)" />
        </template>
      </el-table-column>
      <el-table-column :label="t('rules.updatedBy')" width="180">
        <template #default="{ row }">
          <div>{{ row.updated_by || '—' }}</div>
          <div class="muted">{{ fmtDateTime(row.updated_at) }}</div>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="280" align="center" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openScript(row)">{{ t('rules.uploadScript') }}</el-button>
          <el-button link type="primary" size="small" @click="openTemplates(row)">{{ t('rules.templates') }}</el-button>
          <el-button link type="primary" size="small" @click="openLedger(row)">{{ t('rules.ledger') }}</el-button>
          <el-button link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
          <el-button link type="danger" size="small" @click="onDelete(row)">{{ t('common.delete') }}</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <!-- 报告规则新建/编辑 -->
    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? t('rules.editTitle') : t('rules.createTitle')"
      width="460px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('rules.ruleId')" required>
          <el-input v-model="form.rule" :disabled="isEdit" placeholder="tek_mso" />
        </el-form-item>
        <el-form-item :label="t('rules.models')" required>
          <el-input v-model="form.modelsText" placeholder="DPO1052, DPO1202, MSO1254HD" />
          <div class="field-hint">{{ t('rules.modelsHint') }}</div>
        </el-form-item>
        <el-form-item :label="t('rules.mesUrl')">
          <el-input v-model="form.mes_url" :placeholder="t('rules.mesUrlPh')" />
        </el-form-item>
        <el-form-item :label="t('rules.options')">
          <div class="switch-row">
            <label class="switch-item">
              <el-switch v-model="form.auto_trigger" />
              <span>{{ t('rules.autoTrigger') }}</span>
            </label>
            <label class="switch-item">
              <el-switch v-model="form.enabled" />
              <span>{{ t('rules.enabled') }}</span>
            </label>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 插件脚本上传 -->
    <el-dialog
      v-model="scriptDialog"
      :title="t('rules.scriptTitle', { name: current?.rule })"
      width="460px"
      destroy-on-close
      class="form-dialog"
    >
      <el-upload drag accept=".py" :auto-upload="false" :limit="1" :on-change="(f) => (scriptFile = f.raw)">
        <div class="upload-hint">{{ t('rules.scriptHint') }}</div>
        <div v-if="current?.has_script" class="upload-hint">{{ t('rules.scriptOverwrite', { name: current.script_name }) }}</div>
      </el-upload>
      <template #footer>
        <el-button @click="scriptDialog = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submitScript">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>

    <!-- 模板管理 -->
    <el-dialog
      v-model="tplDialog"
      :title="t('rules.templatesTitle', { name: current?.rule })"
      width="520px"
      destroy-on-close
      class="form-dialog"
    >
      <el-upload drag accept=".xlsx" multiple :auto-upload="false" :on-change="onTplChange" :file-list="tplFiles" :on-remove="(f, list) => (tplFiles = list)">
        <div class="upload-hint">{{ t('rules.templatesHint') }}</div>
      </el-upload>
      <el-table :data="current?.templates || []" size="small" border max-height="220" class="tpl-table">
        <el-table-column :label="t('rules.filename')" min-width="240">
          <template #default="{ row }">{{ row }}</template>
        </el-table-column>
        <el-table-column :label="t('common.action')" width="80" align="center">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="onTplDelete(row)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button @click="tplDialog = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" :disabled="!tplFiles.length" @click="submitTemplates">
          {{ t('rules.upload') }} ({{ tplFiles.length }})
        </el-button>
      </template>
    </el-dialog>

    <!-- 标准器（按规则隔离） -->
    <el-dialog
      v-model="ledgerDialog"
      :title="t('rules.ledgerTitle', { name: current?.rule })"
      width="min(1080px, 94%)"
      destroy-on-close
      class="form-dialog"
    >
      <div class="ledger-toolbar">
        <el-button size="small" type="primary" :icon="Plus" @click="openStdCreate">{{ t('rules.addStandard') }}</el-button>
      </div>
      <el-table :data="standards" v-loading="stdLoading" size="small" border max-height="560">
        <el-table-column prop="manufacturer" :label="t('rules.stdManufacturer')" width="110" />
        <el-table-column prop="pc_name" :label="t('rules.stdPcName')" min-width="130" />
        <el-table-column prop="user_id" :label="t('rules.stdUserId')" width="100" />
        <el-table-column prop="model" :label="t('rules.stdModel')" min-width="120" />
        <el-table-column prop="sn" :label="t('rules.stdSn')" min-width="120" />
        <el-table-column :label="t('rules.stdDate')" width="120">
          <template #default="{ row }">{{ row.cal_date || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t('common.action')" width="120" align="center" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openStdEdit(row)">{{ t('common.edit') }}</el-button>
            <el-button link type="danger" size="small" @click="onStdDelete(row)">{{ t('common.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState :text="t('common.noData')" /></template>
      </el-table>
    </el-dialog>

    <!-- 标准器新建/编辑 -->
    <el-dialog
      v-model="stdDialog"
      :title="stdEdit ? t('rules.stdEdit') : t('rules.stdCreate')"
      width="460px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="stdForm" label-position="top">
        <el-form-item :label="t('rules.stdManufacturer')" required>
          <el-input v-model="stdForm.manufacturer" placeholder="Fluke / UNI-T" />
        </el-form-item>
        <el-form-item :label="t('rules.stdPcName')" required>
          <el-input v-model="stdForm.pc_name" placeholder="QC5" />
        </el-form-item>
        <el-form-item :label="t('rules.stdUserId')">
          <el-input v-model="stdForm.user_id" />
        </el-form-item>
        <el-form-item :label="t('rules.stdModel')" required>
          <el-input v-model="stdForm.model" placeholder="9500B" />
        </el-form-item>
        <el-form-item :label="t('rules.stdSn')" required>
          <el-input v-model="stdForm.sn" />
        </el-form-item>
        <el-form-item :label="t('rules.stdDate')">
          <el-date-picker v-model="stdForm.cal_date" type="date" value-format="YYYY-MM-DD" style="width: 100%" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="stdDialog = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submitStd">{{ t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { ruleApi, standardsApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { fmtDateTime } from '../../utils/format'

const { t } = useI18n()

const rows = ref([])
const standards = ref([])
const loading = ref(false)
const stdLoading = ref(false)
const ledgerDialog = ref(false)
const saving = ref(false)

const dialogVisible = ref(false)
const isEdit = ref(false)
const current = ref(null)
const form = reactive({ rule: '', modelsText: '', mes_url: '', auto_trigger: true, enabled: true })

const scriptDialog = ref(false)
const scriptFile = ref(null)

const tplDialog = ref(false)
const tplFiles = ref([])

const stdDialog = ref(false)
const stdEdit = ref(false)
const stdEditId = ref(null)
const stdForm = reactive({ manufacturer: '', pc_name: '', user_id: '', model: '', sn: '', cal_date: '' })

async function load() {
  loading.value = true
  try {
    rows.value = (await ruleApi.list()).data
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    loading.value = false
  }
}

async function loadStandards() {
  if (!current.value) return
  stdLoading.value = true
  try {
    standards.value = (await standardsApi.list(current.value.rule)).data
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    stdLoading.value = false
  }
}

function openLedger(row) {
  current.value = row
  ledgerDialog.value = true
  loadStandards()
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, { rule: '', modelsText: '', mes_url: '', auto_trigger: true, enabled: true })
  dialogVisible.value = true
}

function openEdit(row) {
  isEdit.value = true
  current.value = row
  Object.assign(form, {
    rule: row.rule,
    modelsText: row.models.join(', '),
    mes_url: row.mes_url || '',
    auto_trigger: row.auto_trigger,
    enabled: row.enabled,
  })
  dialogVisible.value = true
}

function payload() {
  return {
    rule: form.rule.trim(),
    models: form.modelsText.split(/[,，\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean),
    mes_url: form.mes_url.trim() || null,
    auto_trigger: form.auto_trigger,
    enabled: form.enabled,
  }
}

async function submit() {
  const body = payload()
  if (!body.rule || !body.models.length) {
    ElMessage.warning(t('rules.invalidInput'))
    return
  }
  saving.value = true
  try {
    if (isEdit.value) {
      await ruleApi.update(current.value.rule, body)
    } else {
      await ruleApi.create(body)
    }
    ElMessage.success(t('common.saved'))
    dialogVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function toggleFlag(row, key, value) {
  try {
    await ruleApi.update(row.rule, {
      rule: row.rule,
      models: row.models,
      mes_url: row.mes_url,
      auto_trigger: key === 'auto_trigger' ? value : row.auto_trigger,
      enabled: key === 'enabled' ? value : row.enabled,
    })
    Object.assign(row, { [key]: value })
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onDelete(row) {
  await ElMessageBox.confirm(t('rules.deleteConfirm', { name: row.rule }), t('common.delete'), { type: 'warning' })
  try {
    await ruleApi.remove(row.rule)
    ElMessage.success(t('common.deleted'))
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function openScript(row) {
  current.value = row
  scriptFile.value = null
  scriptDialog.value = true
}

async function submitScript() {
  if (!scriptFile.value) {
    ElMessage.warning(t('rules.chooseFile'))
    return
  }
  saving.value = true
  try {
    await ruleApi.uploadScript(current.value.rule, scriptFile.value)
    ElMessage.success(t('common.saved'))
    scriptDialog.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

function openTemplates(row) {
  current.value = row
  tplFiles.value = []
  tplDialog.value = true
}

function onTplChange(_file, list) {
  tplFiles.value = list.map((f) => f.raw)
}

async function submitTemplates() {
  saving.value = true
  try {
    await ruleApi.uploadTemplates(current.value.rule, tplFiles.value)
    ElMessage.success(t('common.saved'))
    tplDialog.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onTplDelete(filename) {
  try {
    await ruleApi.removeTemplate(current.value.rule, filename)
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

function openStdCreate() {
  if (!current.value) {
    return
  }
  stdEdit.value = false
  Object.assign(stdForm, { manufacturer: '', pc_name: '', user_id: '', model: '', sn: '', cal_date: '' })
  stdDialog.value = true
}

function openStdEdit(row) {
  stdEdit.value = true
  stdEditId.value = row.id
  Object.assign(stdForm, row)
  stdDialog.value = true
}

async function submitStd() {
  if (!stdForm.manufacturer || !stdForm.pc_name || !stdForm.model || !stdForm.sn) {
    ElMessage.warning(t('rules.invalidInput'))
    return
  }
  saving.value = true
  try {
    if (stdEdit.value) {
      await standardsApi.update(stdEditId.value, { ...stdForm, rule: current.value.rule })
    } else {
      await standardsApi.create({ ...stdForm, rule: current.value.rule })
    }
    ElMessage.success(t('common.saved'))
    stdDialog.value = false
    loadStandards()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onStdDelete(row) {
  await ElMessageBox.confirm(t('rules.stdDeleteConfirm', { name: `${row.manufacturer} ${row.model}` }), t('common.delete'), { type: 'warning' })
  try {
    await standardsApi.remove(row.id)
    ElMessage.success(t('common.deleted'))
    loadStandards()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(() => {
  load()
  loadStandards()
})
</script>

<style scoped>
/* 面板撑满 Tab 高度：规则表填充剩余空间，标准器表定高滚动；工具栏复用全局 tab-head */
.rules-panel { display: flex; flex-direction: column; height: 100%; }
.prefix-tag { margin-right: 4px; }
.models-cell { white-space: nowrap; overflow: hidden; }
.script-name { font-size: 12px; font-family: var(--app-mono, monospace); }
.field-hint { font-size: 12px; color: var(--app-text-muted, #8a94a6); margin-top: 4px; line-height: 1.4; }
.switch-row { display: flex; align-items: center; gap: 24px; }
.switch-item { display: inline-flex; align-items: center; gap: 8px; font-size: 13px; color: var(--app-text, #303133); }
.upload-hint { padding: 18px 0; color: var(--app-text-muted, #8a94a6); font-size: 13px; }
.rules-table { flex: 1 1 auto; min-height: 0; }
/* 操作列恢复原宽后压缩按钮间距（el-button 默认 12px），保证英文五按钮不裁切 */
.rules-table :deep(td .el-button + .el-button) { margin-left: 6px; }
.tpl-table { margin-top: 10px; }
.ledger-toolbar { display: flex; justify-content: flex-end; margin-bottom: 10px; }
</style>
