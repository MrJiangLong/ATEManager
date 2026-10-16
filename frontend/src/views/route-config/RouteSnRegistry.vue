<template>
  <div>
    <div class="tab-head">
      <div class="filter-row">
        <el-input
          v-model="snFilter"
          clearable
          :placeholder="t('snRegistry.filterSn')"
          style="width:220px"
          @input="page = 1"
        />
        <el-button :icon="RefreshRight" size="small" @click="loadRegistry">{{ t('common.refresh') }}</el-button>
      </div>
      <el-button v-if="canManage" type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
    </div>

    <el-table v-loading="loading" :data="paged" stripe size="small">
      <el-table-column prop="sn" :label="t('snRegistry.sn')" width="220">
        <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
      </el-table-column>
      <el-table-column prop="note" :label="t('snRegistry.note')" min-width="220">
        <template #default="{ row }">
          <span v-if="row.note">{{ row.note }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="registered_by" :label="t('snRegistry.registeredBy')" width="140">
        <template #default="{ row }">
          <span v-if="row.registered_by">{{ row.registered_by }}</span>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" :label="t('snRegistry.registeredAt')" width="170">
        <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="100" align="center" fixed="right">
        <template #default="{ row }">
          <el-button v-if="canManage" link type="danger" size="small" @click="onDelete(row)">{{ t('snRegistry.unregister') }}</el-button>
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
      :title="t('snRegistry.newEntry')"
      width="500px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top">
        <el-form-item :label="t('snRegistry.sn')" required>
          <el-input v-model="form.sn" placeholder="DEV-001" />
        </el-form-item>
        <el-form-item :label="t('snRegistry.note')">
          <el-input v-model="form.note" :placeholder="t('snRegistry.notePlaceholder')" />
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
import { computed, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../../stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, RefreshRight } from '@element-plus/icons-vue'
import { snRegistryApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useLocalPagination } from '../../composables/usePagination'
import { fmtDateTime } from '../../utils/format'

const { t } = useI18n()
const { canManage } = useAuth()

const list = ref([])
const loading = ref(false)
const snFilter = ref('')

const dialogVisible = ref(false)
const saving = ref(false)
const form = reactive({ sn: '', note: '' })

const filtered = computed(() => {
  const kw = snFilter.value.trim().toLowerCase()
  return kw ? list.value.filter((r) => r.sn.toLowerCase().includes(kw)) : list.value
})
const { page, pageSize, paged } = useLocalPagination(filtered)

async function loadRegistry() {
  loading.value = true
  try {
    const res = await snRegistryApi.list()
    list.value = res.data
  } catch (e) {
    list.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  Object.assign(form, { sn: '', note: '' })
  dialogVisible.value = true
}

async function submit() {
  if (!form.sn.trim()) {
    return ElMessage.warning(t('errors.requiredField'))
  }
  saving.value = true
  try {
    await snRegistryApi.create({ sn: form.sn.trim(), note: form.note.trim() })
    ElMessage.success(t('common.saveSuccess'))
    dialogVisible.value = false
    loadRegistry()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onDelete(row) {
  // 统一强制提示：注销仅恢复进站资格，不触碰任何记录；
  // 若设备曾运行研发测试代码，重新入库前须由工艺员显式走 RESET 重新流线。
  try {
    await ElMessageBox.confirm(t('snRegistry.confirmUnregister', { name: row.sn }), t('common.confirm'), {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await snRegistryApi.remove(row.sn)
    ElMessage.success(t('common.deleteSuccess'))
    loadRegistry()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

onMounted(loadRegistry)
</script>
