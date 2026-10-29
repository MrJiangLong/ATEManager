<template>
  <div>
    <div class="tab-head">
      <div class="filter-row">
        <el-select v-model="processFilter" clearable :placeholder="t('configs.filterProcess')" style="width:300px">
          <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
        </el-select>
      </div>
      <div class="filter-row">
        <el-button :icon="RefreshRight" size="small" @click="loadModels">{{ t('common.refresh') }}</el-button>
        <el-button v-if="canManage" type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="paged" stripe size="small">
      <el-table-column prop="product_model" :label="t('configs.modelId')" width="180">
        <template #default="{ row }"><span class="code">{{ row.product_model }}</span></template>
      </el-table-column>
      <el-table-column :label="t('configs.modelProcess')" min-width="220">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" type="primary">{{ row.process_id }}</el-tag>
          <span class="muted" style="margin-left:8px">{{ processName(row.process_id) }}</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.targetFw')" width="120">
        <template #default="{ row }">
          <el-tag size="small" type="success" effect="plain">{{ row.target_fw_version }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.fwRule')" width="180">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="row.fw_match_rule === 'min' ? 'warning' : 'info'">
            {{ row.fw_match_rule === 'min' ? t('configs.fwRuleMin') : t('configs.fwRuleExact') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.targetApp')" width="130">
        <template #default="{ row }">
          <el-tag v-if="row.target_app_version" size="small" effect="plain">{{ row.target_app_version }}</el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.appRule')" width="180">
        <template #default="{ row }">
          <el-tag
            v-if="row.target_app_version"
            size="small"
            effect="plain"
            :type="row.app_match_rule === 'min' ? 'warning' : 'info'"
          >
            {{ row.app_match_rule === 'min' ? t('configs.fwRuleMin') : t('configs.fwRuleExact') }}
          </el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.passthroughMode')" width="90" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.passthrough" size="small" type="warning" effect="plain">
            {{ t('configs.passthroughOn') }}
          </el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="120" align="center" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canManage" link type="primary" size="small" @click="openEdit(row)">{{ t('common.edit') }}</el-button>
          <el-button v-if="canManage" link type="danger" size="small" @click="onDelete(row)">{{ t('common.delete') }}</el-button>
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
      :title="isEdit ? t('configs.editModel') : t('configs.newModel')"
      width="500px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('configs.modelId')" required>
          <el-input v-model="form.product_model" :disabled="isEdit" placeholder="MSO4054B" />
        </el-form-item>
        <el-form-item :label="t('configs.modelProcess')" required>
          <el-select v-model="form.process_id" filterable style="width:100%">
            <el-option
              v-for="p in selectableProcesses"
              :key="p.process_id"
              :value="p.process_id"
              :label="p.process_id"
            />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('configs.targetFw')" required>
          <el-input v-model="form.target_fw_version" placeholder="V3.20" />
        </el-form-item>
        <el-form-item :label="t('configs.fwRule')">
          <el-select v-model="form.fw_match_rule" style="width:100%">
            <el-option value="exact" :label="t('configs.fwRuleExact')" />
            <el-option value="min" :label="t('configs.fwRuleMin')" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('configs.targetApp')">
          <el-input v-model="form.target_app_version" placeholder="V1.0.0（留空 = 不卡控）" />
        </el-form-item>
        <el-form-item :label="t('configs.appRule')">
          <el-select v-model="form.app_match_rule" style="width:100%">
            <el-option value="min" :label="t('configs.fwRuleMin')" />
            <el-option value="exact" :label="t('configs.fwRuleExact')" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('configs.passthroughMode')">
          <div>
            <el-switch v-model="form.passthrough" @change="onPassthroughChange" />
            <span class="muted" style="margin-left:8px">{{ t('configs.passthroughModeTip') }}</span>
          </div>
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../../stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, RefreshRight } from '@element-plus/icons-vue'
import { modelApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useTabRefresh } from '../../composables/useTabRefresh'
import { useLocalPagination } from '../../composables/usePagination'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { canManage } = useAuth()
const { processes, processName, loadProcesses } = useProcesses()

const list = ref([])
const loading = ref(false)
const processFilter = ref('')

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
// 编辑起点是否为直通：关直通时触发清线确认，确认后才携带 confirm_drain
const wasPassthrough = ref(false)
// 编辑起点的流程：改绑时先做影响面预演，确认后才携带 confirm_switch
const originalProcessId = ref('')
const form = reactive({
  product_model: '',
  process_id: '',
  target_fw_version: '',
  fw_match_rule: 'exact',
  target_app_version: '',
  app_match_rule: 'min',
  passthrough: false,
})

const filtered = computed(() =>
  processFilter.value ? list.value.filter((m) => m.process_id === processFilter.value) : list.value
)
const { page, pageSize, paged } = useLocalPagination(filtered)

const selectableProcesses = computed(() =>
  processes.value.filter((p) => p.is_active || p.process_id === form.process_id)
)

async function loadModels() {
  loading.value = true
  try {
    const res = await modelApi.list({})
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
  wasPassthrough.value = false
  Object.assign(form, {
    product_model: '',
    process_id: processFilter.value || processes.value[0]?.process_id || '',
    target_fw_version: '',
    fw_match_rule: 'exact',
    target_app_version: '',
    app_match_rule: 'min',
    passthrough: false,
  })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  wasPassthrough.value = !!row.passthrough
  originalProcessId.value = row.process_id
  Object.assign(form, {
    product_model: row.product_model,
    process_id: row.process_id,
    target_fw_version: row.target_fw_version || '',
    fw_match_rule: row.fw_match_rule || 'exact',
    target_app_version: row.target_app_version || '',
    app_match_rule: row.app_match_rule || 'min',
    passthrough: !!row.passthrough,
  })
  dialogVisible.value = true
}

// 关直通强制清线弹窗：取消则回滚 switch；确认后提交携带 confirm_drain
async function onPassthroughChange(val) {
  if (isEdit.value && wasPassthrough.value && !val) {
    try {
      await ElMessageBox.confirm(t('configs.drainConfirmText'), t('common.confirm'), {
        type: 'warning',
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
      })
      form.confirm_drain = true
    } catch {
      form.passthrough = true // 取消：回滚 switch 状态
    }
  } else {
    form.confirm_drain = false
  }
}

async function submit() {
  if (!form.product_model.trim() || !form.process_id || !form.target_fw_version.trim()) {
    return ElMessage.warning(t('errors.requiredField'))
  }
  saving.value = true
  try {
    const payload = {
      process_id: form.process_id,
      target_fw_version: form.target_fw_version.trim(),
      fw_match_rule: form.fw_match_rule,
      target_app_version: form.target_app_version.trim(),
      app_match_rule: form.app_match_rule,
      passthrough: form.passthrough,
    }
    if (isEdit.value) {
      if (wasPassthrough.value && !form.passthrough) payload.confirm_drain = true
      // 工艺切换：改绑流程且存在在制品时，先预演影响面并要求显式确认（R2 交集结转）
      if (form.process_id !== originalProcessId.value) {
        const impact = (await modelApi.switchImpact(form.product_model, form.process_id)).data
        if (impact.wip_count > 0) {
          const toTest = impact.wip.reduce((n, w) => n + w.to_test_stations.length, 0)
          try {
            await ElMessageBox.confirm(
              t('configs.switchImpactConfirm', {
                count: impact.wip_count,
                stations: toTest,
                machines: impact.machines_lost.length,
              }),
              t('common.confirm'),
              { type: 'warning', confirmButtonText: t('common.confirm'), cancelButtonText: t('common.cancel') }
            )
          } catch {
            saving.value = false
            return
          }
          payload.confirm_switch = true
        }
      }
      await modelApi.update(form.product_model, payload)
    } else {
      await modelApi.create({ product_model: form.product_model.trim(), ...payload })
    }
    ElMessage.success(t('common.saveSuccess'))
    dialogVisible.value = false
    loadModels()
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(t('configs.yesDeleteModel', { name: row.product_model }), t('common.confirm'), {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await modelApi.remove(row.product_model)
    ElMessage.success(t('common.deleteSuccess'))
    loadModels()
    loadProcesses({ force: true })
  } catch (e) {
    ElMessage.error(e.message)
  }
}

watch(processFilter, () => {
  page.value = 1
  loadModels()
})
useTabRefresh('models', loadModels)

onMounted(async () => {
  await loadProcesses()
  await loadModels()
})
</script>

