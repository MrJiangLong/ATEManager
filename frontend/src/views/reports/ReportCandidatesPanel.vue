<template>
  <div class="candidates-panel">
    <div class="tab-head">
      <div class="filter-row">
        <el-input v-model="filters.sn" clearable :placeholder="t('reports.searchSn')" style="width:180px" @keyup.enter="load" />
        <el-select v-model="filters.model" clearable :placeholder="t('reports.filterModel')" style="width:160px">
          <el-option v-for="m in models" :key="m" :value="m" :label="m" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          :start-placeholder="t('records.startDate')"
          :end-placeholder="t('records.endDate')"
          style="width:240px"
        />
        <el-button :icon="RefreshRight" size="small" @click="load">{{ t('common.refresh') }}</el-button>
      </div>
      <div class="filter-row">
        <span v-if="selected.length" class="selected-count">{{ t('reports.selected', { n: selected.length }) }}</span>
        <el-button size="small" @click="selectAll">{{ t('reports.selectAll') }}</el-button>
        <el-button size="small" @click="invertSelection">{{ t('reports.invert') }}</el-button>
        <el-button size="small" type="primary" :disabled="!canOperate || selected.length === 0" @click="generate">
          {{ t('reports.genReport') }}<span v-if="selected.length">&nbsp;({{ selected.length }})</span>
        </el-button>
      </div>
    </div>

    <el-table
        ref="table"
        v-loading="loading"
        :data="items"
        stripe
        size="small"
        :row-class-name="({ row }) => (selected.includes(row) ? 'row-selected' : '')"
        @selection-change="(rows) => (selected = rows)"
      >
        <el-table-column type="selection" width="42" :selectable="() => canOperate" />
        <el-table-column prop="sn" :label="t('reports.tableSn')" min-width="150" show-overflow-tooltip>
          <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
        </el-table-column>
        <el-table-column prop="product_model" :label="t('reports.tableModel')" min-width="130" show-overflow-tooltip />
        <el-table-column prop="current_fw_version" :label="t('reports.tableFw')" min-width="110" show-overflow-tooltip />
        <el-table-column :label="t('reports.tableCompleted')" min-width="170" show-overflow-tooltip>
          <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.completed_at) }}</span></template>
        </el-table-column>
        <el-table-column :label="t('reports.tableJobState')" width="130">
          <template #default="{ row }">
            <el-tag v-if="row.has_job" size="small" type="info" effect="plain">{{ t('reports.hasJob') }}</el-tag>
          </template>
        </el-table-column>
        <template #empty><EmptyState :text="t('reports.noCandidates')" /></template>
      </el-table>
      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          size="small"
        />
      </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { RefreshRight } from '@element-plus/icons-vue'
import EmptyState from '../../components/EmptyState.vue'
import { reportApi } from '../../api'
import { useAuth } from '../../stores/auth'
import { usePolling } from '../../composables/usePolling'
import { fmtDateTime } from '../../utils/format'

const { t } = useI18n()
const { role } = useAuth()
const props = defineProps({ active: { type: Boolean, default: false } })
// 生成为 operator+；viewer 只读浏览
const canOperate = computed(() => ['operator', 'admin'].includes(role.value))

const loading = ref(false)
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const selected = ref([])
const table = ref(null)
const models = ref([])
const dateRange = ref(null)
const filters = reactive({ sn: '', model: '' })

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.model) params.model = filters.model
    if (filters.sn) params.sn = filters.sn
    if (dateRange.value?.[0]) params.date_from = dateRange.value[0]
    if (dateRange.value?.[1]) params.date_to = dateRange.value[1]
    const res = await reportApi.candidates(params)
    items.value = res.data.items
    total.value = res.data.total
    if (silent) restoreSelection()
  } catch (e) {
    if (silent) return
    items.value = []
    total.value = 0
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

// 静默轮询会整体替换行对象，勾选必须按 SN 回放，否则用户选到一半被轮询清空
function restoreSelection() {
  const sns = new Set(selected.value.map((row) => row.sn))
  if (!sns.size) return
  nextTick(() => {
    items.value.forEach((row) => {
      if (sns.has(row.sn)) table.value?.toggleRowSelection(row, true)
    })
  })
}

function selectAll() {
  items.value.forEach((row) => table.value?.toggleRowSelection(row, true))
}

function invertSelection() {
  items.value.forEach((row) => table.value?.toggleRowSelection(row))
}

async function generate() {
  const sns = selected.value.map((row) => row.sn)
  try {
    await ElMessageBox.confirm(t('reports.genConfirm', { n: sns.length }), t('common.confirm'), {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    const res = await reportApi.batch({ sns })
    const { created, skipped } = res.data
    ElMessage.success(t('reports.genOk', { created }))
    if (skipped.length) {
      ElMessage.warning(t('reports.genSkipped', { n: skipped.length }))
    }
    lastGenerateAt.value = Date.now() // 触发快轮询窗口：建档状态快速上屏
    table.value?.clearSelection()
    load()
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
}

watch([page, pageSize], () => load())
watch(
  () => [filters.sn, filters.model, dateRange.value],
  () => {
    page.value = 1
    load()
  }
)

onMounted(async () => {
  try {
    const res = await reportApi.models()
    models.value = res.data
  } catch {
    models.value = []
  }
  load()
})

// 自适应静默轮询：点过「生成报告」后 60 秒内每 8s 取一次（建档状态快速跟进），平时每 30s 兜底
const lastGenerateAt = ref(0)
let lastPollAt = 0
usePolling(() => {
  if (!props.active) return
  const now = Date.now()
  if (now - lastPollAt < (now - lastGenerateAt.value < 60000 ? 8000 : 30000) - 50) return
  lastPollAt = now
  load(true)
}, 8000)
</script>

<style scoped>
/* 面板撑满 Tab 高度：表格弹性填充、分页钉底；容器样式复用全局 tab-head/pager（与 RouteConfig 子页同构） */
.candidates-panel { display: flex; flex-direction: column; height: 100%; }
.candidates-panel :deep(.el-table) { flex: 1 1 auto; min-height: 0; }
.panel-toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.toolbar-spacer { flex: 1 1 auto; }
.selected-count { font-size: 13px; color: var(--app-primary, #2f6bff); }
:deep(tr.row-selected > td) { background: var(--app-primary-soft, rgba(47, 107, 255, 0.06)) !important; }
</style>
