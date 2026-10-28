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
      <div class="tab-actions">
        <el-button size="small" :icon="Download" @click="exportRegistry">{{ t('snRegistry.export') }}</el-button>
        <el-button v-if="canManage" size="small" :icon="Upload" :loading="importing" @click="importInput?.click()">{{ t('snRegistry.import') }}</el-button>
        <el-button v-if="canManage" type="primary" size="small" :icon="Plus" @click="openCreate">{{ t('common.add') }}</el-button>
      </div>
    </div>
    <input
      ref="importInput"
      type="file"
      accept=".json,application/json"
      style="display: none"
      @change="onImportFile"
    />

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
      <el-table-column :label="t('common.action')" width="120" align="center" fixed="right">
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
import { Download, Plus, RefreshRight, Upload } from '@element-plus/icons-vue'
import { snRegistryApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import { useTabRefresh } from '../../composables/useTabRefresh'
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

const importing = ref(false)
const importInput = ref(null)

/** 导出登记清单为 JSON 文件（与工艺配置导出同款下载方式） */
function exportRegistry() {
  snRegistryApi
    .export()
    .then((res) => {
      const stamp = new Date().toISOString().slice(0, 10).replace(/-/g, '')
      const a = document.createElement('a')
      a.href = URL.createObjectURL(new Blob([JSON.stringify(res.data, null, 2)], { type: 'application/json' }))
      a.download = `sn-registry-${stamp}.json`
      a.click()
      URL.revokeObjectURL(a.href)
    })
    .catch((e) => ElMessage.error(e.message || t('errors.loadFailed')))
}

/** 一键导入：选文件即解析入库（已存在的 SN 服务端跳过），与机台导入同款交互 */
function onImportFile(e) {
  const file = e.target.files?.[0]
  e.target.value = ''
  if (!file) return
  const reader = new FileReader()
  reader.onload = async () => {
    let items = []
    try {
      const doc = JSON.parse(reader.result)
      items = (Array.isArray(doc?.items) ? doc.items : []).filter(
        (i) => i && String(i.sn || '').trim(),
      )
    } catch {
      return ElMessage.error(t('snRegistry.importInvalid'))
    }
    if (!items.length) return ElMessage.warning(t('snRegistry.importEmpty'))
    importing.value = true
    try {
      const res = await snRegistryApi.import({ items })
      ElMessage.success(
        t('snRegistry.importDone', { created: res.data.created, skipped: res.data.skipped }),
      )
      loadRegistry()
    } catch (err) {
      ElMessage.error(err.message)
    } finally {
      importing.value = false
    }
  }
  reader.readAsText(file)
}

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

useTabRefresh('snRegistry', loadRegistry)

onMounted(loadRegistry)
</script>

<style scoped>
/* 工具栏右侧动作簇：导出 / 导入 / 新增（tab-head 为 space-between，左筛选右动作） */
.tab-actions { display: flex; align-items: center; gap: 10px; }
</style>
