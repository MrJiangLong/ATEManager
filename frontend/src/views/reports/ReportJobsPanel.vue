<template>
  <div class="jobs-panel">
    <div class="tab-head">
      <div class="filter-row">
        <el-input v-model="filters.sn" clearable :placeholder="t('reports.searchSn')" style="width:180px" @keyup.enter="search" />
        <el-select v-model="filters.model" clearable :placeholder="t('reports.filterModel')" style="width:160px">
          <el-option v-for="m in models" :key="m" :value="m" :label="m" />
        </el-select>
        <el-select v-model="filters.status" clearable :placeholder="t('reports.all')" style="width:140px">
          <el-option v-for="s in JOB_STATUSES" :key="s" :value="s" :label="t(`reports.status_${s}`)" />
        </el-select>
        <el-button :icon="RefreshRight" size="small" @click="load">{{ t('common.refresh') }}</el-button>
      </div>
    </div>

    <el-table
      v-loading="loading"
      :data="items"
      stripe
      size="small"
      :row-class-name="({ row }) => (row.failed_items || []).length || row.artifacts?.some((a) => a.failed) ? 'row-warn' : ''"
    >
      <el-table-column prop="sn" :label="t('reports.tableSn')" min-width="140" show-overflow-tooltip>
        <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
      </el-table-column>
      <el-table-column prop="model" :label="t('reports.tableModel')" min-width="120" show-overflow-tooltip />
      <el-table-column :label="t('reports.tableSource')" width="96">
        <template #default="{ row }">
          <el-tag size="small" effect="plain" :type="row.auto ? 'info' : 'primary'">
            {{ row.auto ? t('reports.auto') : t('reports.manual') }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.tableStatus')" width="120" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag size="small" :type="STATUS_META[row.status]?.tag || 'info'">
            {{ t(`reports.status_${row.status}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.tableArtifacts')" min-width="320">
        <template #default="{ row }">
          <div class="artifact-list">
            <div v-for="(art, index) in row.artifacts || []" :key="index" class="artifact-row">
              <el-tag size="small" effect="plain" class="art-type" :type="art.failed ? 'danger' : 'info'">
                {{ t(`reports.type_${art.type}`) }}
              </el-tag>
              <el-tooltip :disabled="!art.purged" :content="t('reports.mesArchived')" placement="top" :show-after="300">
                <button class="dl-chip" :disabled="dlDisabled(art, 'xlsx')" @click="download(row, index, 'xlsx')">
                  {{ t('reports.excel') }}
                </button>
              </el-tooltip>
              <el-tooltip
                :disabled="(!art.pdf || art.pdf_status === 'done') && !art.purged"
                :content="dlTip(art)"
                placement="top"
                :show-after="300"
              >
                <span class="chip-wrap">
                  <button
                    class="dl-chip"
                    :disabled="dlDisabled(art, 'pdf')"
                    @click="download(row, index, 'pdf')"
                  >
                    {{ t('reports.pdf') }}
                    <span v-if="art.pdf && art.pdf_status !== 'done'" class="pdf-dot" :class="`pdf-${art.pdf_status}`" />
                  </button>
                </span>
              </el-tooltip>
              <el-tag v-if="art.failed" size="small" type="danger" effect="plain">
                {{ t('reports.artifactFailed') }}
              </el-tag>
            </div>
          </div>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.tableMes')" width="110">
        <template #default="{ row }">
          <el-tooltip v-if="row.mes_message" :content="row.mes_message" placement="top" :show-after="300">
            <el-tag size="small" :type="MES_META[row.mes_status] || 'info'" effect="plain">
              {{ t(`reports.mes_${row.mes_status}`) }}
            </el-tag>
          </el-tooltip>
          <el-tag v-else size="small" :type="MES_META[row.mes_status] || 'info'" effect="plain">
            {{ t(`reports.mes_${row.mes_status}`) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.failedItems')" min-width="150">
        <template #default="{ row }">
          <el-tooltip
            v-if="(row.failed_items || []).length"
            :content="(row.failed_items || []).join('\n')"
            placement="top"
            :show-after="300"
          >
            <el-tag size="small" type="danger" effect="plain">{{ (row.failed_items || []).length }}</el-tag>
          </el-tooltip>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('reports.tableCreated')" min-width="160" show-overflow-tooltip>
        <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
      </el-table-column>
      <el-table-column v-if="canOperate" :label="t('common.action')" width="170" fixed="right">
        <template #default="{ row }">
          <el-button
            link type="primary" size="small"
            @click="retry(row)"
          >{{ row.status === 'failed' || row.status === 'partial' ? t('reports.retry') : t('reports.regenerate') }}</el-button>
          <el-button
            v-if="row.mes_status === 'failed' || row.mes_status === 'skipped'"
            link type="primary" size="small"
            @click="resendMes(row)"
          >{{ t('reports.resendMes') }}</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { RefreshRight } from '@element-plus/icons-vue'
import EmptyState from '../../components/EmptyState.vue'
import { reportApi } from '../../api'
import { useAuth } from '../../stores/auth'
import { usePolling } from '../../composables/usePolling'
import { fmtDateTime } from '../../utils/format'

const { t } = useI18n()
const { role } = useAuth()
// 重试/重传为 operator+；viewer 只读浏览
const canOperate = computed(() => ['operator', 'admin'].includes(role.value))

const JOB_STATUSES = ['pending', 'running', 'success', 'partial', 'failed']
const STATUS_META = {
  pending: { tag: 'info' },
  running: { tag: 'warning' },
  success: { tag: 'success' },
  partial: { tag: 'warning' },
  failed: { tag: 'danger' },
}
const MES_META = { none: 'info', skipped: 'warning', pending: 'warning', success: 'success', failed: 'danger' }

// MES 上传成功的产物（purged）已不在 ATEManager 存储中，下载置灰并给出说明
function dlDisabled(art, variant) {
  if (art.purged) return true
  if (variant === 'pdf') return !art.pdf || art.pdf_status !== 'done'
  return !art.filename
}
function dlTip(art) {
  if (art.purged) return t('reports.mesArchived')
  return t(`reports.pdf_${art.pdf_status}`)
}

const loading = ref(false)
const items = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const models = ref([])
const filters = reactive({ sn: '', model: '', status: '' })

function params() {
  const p = { page: page.value, page_size: pageSize.value }
  if (filters.sn) p.sn = filters.sn
  if (filters.model) p.model = filters.model
  if (filters.status) p.status = filters.status
  return p
}

async function load(silent = false) {
  if (!silent) loading.value = true
  try {
    const res = await reportApi.listJobs(params())
    items.value = res.data.items
    total.value = res.data.total
  } catch (e) {
    if (!silent) ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    if (!silent) loading.value = false
  }
}

function search() {
  if (page.value === 1) load()
  else page.value = 1
}

async function retry(row) {
  try {
    await reportApi.retry(row.job_id)
    ElMessage.success(t('reports.retryOk'))
    load()
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
}

async function resendMes(row) {
  try {
    await reportApi.resendMes(row.job_id)
    ElMessage.success(t('reports.mesResent'))
    load()
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
}

async function download(row, index, variant) {
  try {
    const res = await reportApi.downloadUrl(row.job_id, index, variant)
    window.open(res.data.url, '_blank')
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
}

watch([page, pageSize], () => load())
watch(
  () => [filters.sn, filters.model, filters.status],
  () => {
    page.value = 1
    search()
  }
)

// 自适应轮询（仅本 Tab 激活时发请求）：有任务在途（排队/生成中/PDF 转换中）每 8s 取一次，
// 全部空闲每 30s 取一次兜底。定时器固定 8s 节奏，按距上次请求的时长门控是否真的发请求
const props = defineProps({ active: { type: Boolean, default: false } })
const hasActiveWork = computed(() =>
  items.value.some(
    (row) =>
      row.status === 'pending' ||
      row.status === 'running' ||
      (row.artifacts || []).some((a) => a.pdf && ['pending', 'claimed'].includes(a.pdf_status))
  )
)
let lastPollAt = 0
usePolling(() => {
  if (!props.active) return
  const now = Date.now()
  if (now - lastPollAt < (hasActiveWork.value ? 8000 : 30000) - 50) return
  lastPollAt = now
  load(true)
}, 8000)

onMounted(async () => {
  try {
    const res = await reportApi.models()
    models.value = res.data
  } catch {
    models.value = []
  }
  load()
})
</script>

<style scoped>
/* 面板撑满 Tab 高度：表格弹性填充、分页钉底；容器样式复用全局 tab-head/pager（与 RouteConfig 子页同构） */
.jobs-panel { display: flex; flex-direction: column; height: 100%; }
.jobs-panel :deep(.el-table) { flex: 1 1 auto; min-height: 0; }
.artifact-list { display: flex; flex-direction: column; gap: 5px; }
/* 网格固定列：类型 88px + Excel/PDF chip 各 50px，各行严格纵向对齐 */
.artifact-row {
  display: grid;
  grid-template-columns: 88px 50px 50px auto;
  align-items: center;
  gap: 8px;
}
.art-type { justify-content: center; }
.chip-wrap { display: inline-flex; }
.dl-chip {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  min-width: 50px;
  padding: 2px 6px;
  font-size: 11.5px;
  line-height: 16px;
  font-family: var(--app-mono, monospace);
  border-radius: 4px;
  border: 1px solid var(--el-color-primary-light-7, #d9ecff);
  background: var(--el-color-primary-light-9, #ecf5ff);
  color: var(--el-color-primary);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.dl-chip:hover:not(:disabled) { border-color: var(--el-color-primary); background: var(--el-color-primary-light-8, #e1effe); }
.dl-chip:disabled {
  border-style: dashed;
  border-color: var(--el-border-color, #dcdfe6);
  background: transparent;
  color: var(--el-text-color-disabled, #a8abb2);
  cursor: not-allowed;
}
.pdf-dot {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
}
.pdf-pending, .pdf-claimed { background: var(--el-color-warning); }
.pdf-failed { background: var(--el-color-danger); }
:deep(tr.row-warn > td) { background: rgba(245, 108, 108, 0.05) !important; }
</style>
