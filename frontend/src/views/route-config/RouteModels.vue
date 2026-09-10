<template>
  <div>
    <div class="tab-head">
      <div class="filter-row">
        <el-select v-model="processFilter" clearable :placeholder="t('configs.filterProcess')" style="width:220px">
          <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
        </el-select>
        <el-button :icon="RefreshRight" size="small" @click="loadModels">{{ t('common.refresh') }}</el-button>
      </div>
      <el-button type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
    </div>

    <el-table v-loading="loading" :data="filtered" stripe size="small">
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
      <el-table-column :label="t('configs.fwRule')" width="140">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="row.fw_match_rule === 'min' ? 'warning' : 'info'">
            {{ row.fw_match_rule === 'min' ? t('configs.fwRuleMin') : t('configs.fwRuleExact') }}
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
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, RefreshRight } from '@element-plus/icons-vue'
import { modelApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { processes, processName, loadProcesses } = useProcesses()

const list = ref([])
const loading = ref(false)
const processFilter = ref('')

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({
  product_model: '',
  process_id: '',
  target_fw_version: '',
  fw_match_rule: 'exact',
})

const filtered = computed(() =>
  processFilter.value ? list.value.filter((m) => m.process_id === processFilter.value) : list.value
)

// 停用（归档）的流程不再接受新绑定；但已绑该流程的机型仍需可见可改，故保留当前值
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
  Object.assign(form, {
    product_model: '',
    process_id: processFilter.value || processes.value[0]?.process_id || '',
    target_fw_version: '',
    fw_match_rule: 'exact',
  })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  Object.assign(form, {
    product_model: row.product_model,
    process_id: row.process_id,
    target_fw_version: row.target_fw_version || '',
    fw_match_rule: row.fw_match_rule || 'exact',
  })
  dialogVisible.value = true
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
    }
    if (isEdit.value) {
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

watch(processFilter, () => loadModels())
onMounted(async () => {
  await loadProcesses()
  await loadModels()
})
</script>
