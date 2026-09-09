<template>
  <div class="products fade-up">
    <PageToolbar :title="$t('products.title')" :subtitle="$t('products.subtitle')">
      <el-input v-model="filters.sn" clearable :placeholder="$t('products.searchSn')" style="width:180px" @keyup.enter="search" />
      <el-select v-model="filters.process_id" clearable :placeholder="$t('products.filterProcess')" style="width:200px">
        <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
      </el-select>
      <el-select v-model="filters.product_model" clearable :placeholder="$t('products.filterModel')" style="width:160px">
        <el-option v-for="m in models" :key="m.product_model" :value="m.product_model" :label="m.product_model" />
      </el-select>
      <el-select v-model="filters.current_status" clearable :placeholder="$t('products.filterStatus')" style="width:140px">
        <el-option v-for="s in WIP_FILTER_STATUSES" :key="s" :value="s" :label="$t(`status.${s}`)" />
      </el-select>
      <el-checkbox v-model="zombieOnly" :label="$t('products.zombieOnly')" />
      <template #extra>
        <el-button :icon="Refresh" @click="search">{{ $t('common.refresh') }}</el-button>
      </template>
    </PageToolbar>

    <DataCard>
      <!-- table-layout:fixed 让列宽严格按声明分配；SN 左固定 + 操作右固定，
           列多导致横向滚动时主键与操作始终可见 -->
      <el-table v-loading="loading" :data="items" stripe size="small" style="width:100%; table-layout:fixed">
        <el-table-column prop="sn" :label="$t('products.tableSn')" width="152" fixed show-overflow-tooltip>
          <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
        </el-table-column>
        <el-table-column :label="$t('products.tableModel')" width="128" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="model-cell">
              <span>{{ row.product_model }}</span>
              <span class="muted model-process">{{ row.process_id || '-' }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="$t('products.tableStatus')" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.current_status)" size="small">{{ statusLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('products.tableProgress')" min-width="150">
          <template #default="{ row }">
            <div class="progress-cell">
              <el-progress
                :percentage="progressPct(row)"
                :stroke-width="8"
                :show-text="false"
                class="progress-bar"
              />
              <span class="progress-text muted">{{ row.passed_count }}/{{ row.total_steps }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="fail_count" :label="$t('products.tableFailCount')" width="80" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.fail_count" type="danger" size="small">{{ row.fail_count }}</el-tag>
            <span v-else class="muted">0</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('products.tableLock')" width="176">
          <template #default="{ row }">
            <template v-if="row.current_status === 'TESTING'">
              <el-tooltip :content="lockTip(row)" placement="top">
                <el-tag size="small" :type="lockTagType(row)" :effect="row.lock_zombie ? 'dark' : 'plain'">
                  {{ row.lock_zombie ? $t('products.lockZombie') : $t('products.lockHeld') }}
                </el-tag>
              </el-tooltip>
              <div class="lock-meta" :class="{ 'text-danger': row.lock_zombie }">
                {{ $t('products.lockMeta', { held: fmtDurationSec(row.lock_held_sec), idle: row.lock_idle_sec }) }}
              </div>
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('products.tableUpdated')" width="104">
          <template #default="{ row }">
            <el-tooltip :content="fmtDateTime(row.updated_at)" placement="top">
              <span class="muted">{{ fmtRelative(row.updated_at, t) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.action')" width="120" align="center" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :icon="View" @click="openDrawer(row)">
              {{ $t('common.detail') }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState :text="$t('common.noData')" /></template>
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
    </DataCard>

    <RepairDialog
      v-model="repairVisible"
      :preset-sn="repairSn"
      :station-options="repairStations"
      @saved="onRepairSaved"
    />

    <ProductDrawer
      v-model="drawerVisible"
      :row="current"
      @repair="openRepair"
      @force-release="onForceRelease"
      @trace="(row) => goTrace(row.sn)"
    />
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, View } from '@element-plus/icons-vue'
import { modelApi, productApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import ProductDrawer from '../components/ProductDrawer.vue'
import RepairDialog from '../components/RepairDialog.vue'
import { usePolling } from '../composables/usePolling'
import { useProcesses } from '../composables/useProcesses'
import { LOCK_GRACE_SEC, WIP_FILTER_STATUSES } from '../utils/constants'
import { fmtDateTime, fmtDurationSec, fmtRelative, statusTagType } from '../utils/format'

const { t } = useI18n()
const router = useRouter()
const { processes, loadProcesses } = useProcesses()

const items = ref([])
const models = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = reactive({ sn: '', process_id: '', product_model: '', current_status: '' })
const zombieOnly = ref(false)

const repairVisible = ref(false)
const repairSn = ref('')
const repairStations = ref([])

const drawerVisible = ref(false)
const current = ref(null)

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.sn) params.sn = filters.sn
    if (filters.process_id) params.process_id = filters.process_id
    if (filters.product_model) params.product_model = filters.product_model
    if (filters.current_status) params.current_status = filters.current_status
    if (zombieOnly.value) params.zombie_only = true
    const res = await productApi.list(params)
    items.value = res.data.items
    total.value = res.data.total
  } catch (e) {
    items.value = []
    total.value = 0
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

/** 筛选变化 / 手动刷新：回到第一页再查询（page watch 会触发 load） */
function search() {
  if (page.value === 1) load()
  else page.value = 1
}

function statusLabel(row) {
  if (row.is_completed) return t('status.COMPLETED')
  return t(`status.${row.current_status}`)
}
function progressPct(row) {
  if (!row.total_steps) return 0
  return Math.round((row.passed_count / row.total_steps) * 100)
}
function goTrace(sn) {
  router.push(`/trace/${encodeURIComponent(sn)}`)
}
function openDrawer(row) {
  current.value = row
  drawerVisible.value = true
}

function openRepair(row) {
  repairSn.value = row.sn
  repairStations.value = row.passed_stations || []
  repairVisible.value = true
}

function onRepairSaved() {
  load()
}

/** 锁健康度：失联红 / 接近宽限黄 / 正常绿 */
function lockTagType(row) {
  if (row.lock_zombie) return 'danger'
  if (row.lock_idle_sec >= LOCK_GRACE_SEC * 0.6) return 'warning'
  return 'success'
}
function lockTip(row) {
  return [
    `${t('products.lockHeldTip')}: ${fmtDurationSec(row.lock_held_sec)}`,
    `${t('products.lockIdleTip')}: ${fmtDurationSec(row.lock_idle_sec)}`,
    `${t('products.lockLeaseTip')}: ${fmtDurationSec(row.lock_lease_remaining_sec)}`,
    row.lock_zombie ? t('products.lockZombieTip') : '',
  ]
    .filter(Boolean)
    .join('\n')
}

async function onForceRelease(row) {
  let reason
  try {
    const { value } = await ElMessageBox.prompt(
      t('products.forceReleaseTip', { sn: row.sn }),
      t('products.forceRelease'),
      {
        inputPlaceholder: t('products.forceReleasePh'),
        inputValidator: (v) => !!String(v || '').trim(),
        type: 'warning',
      }
    )
    reason = String(value || '').trim()
  } catch {
    return
  }
  try {
    await productApi.forceRelease(row.sn, reason)
    ElMessage.success(t('products.forceReleaseSuccess'))
    drawerVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

watch([page, pageSize], () => load())
watch(
  () => [filters.sn, filters.process_id, filters.product_model, filters.current_status],
  () => search()
)
watch(zombieOnly, () => search())

onMounted(async () => {
  await loadProcesses()
  try {
    const res = await modelApi.list({})
    models.value = res.data
  } catch (e) {
    models.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
  await load()
})

/* 锁状态是"准实时"数据：机台崩溃后需在分钟级被发现，故轮询刷新 */
usePolling(load, 20000)
</script>

<style scoped>
/* 滚动条收敛到表格内部：页面本身不滚动，表头常驻。
   每一级父容器都必须是 flex 列 + min-height:0，高度才能从 main 传到 el-table */
.products {
  display: flex;
  flex-direction: column;
  gap: 16px;
  flex: 1 1 auto;
  min-height: 0;
  height: 100%;
}
.products :deep(.data-card) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
  overflow: hidden;
}
.products :deep(.data-card .card-body) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}
/* el-table 只有拿到显式 height 才会启用"固定表头 + 内部滚动" */
.products :deep(.el-table) {
  flex: 1 1 auto;
  min-height: 0;
  height: calc(100% - 56px);
}
.products :deep(.pager) {
  flex: 0 0 auto;
  margin-top: var(--app-space-3);
}

.progress-cell { display: flex; align-items: center; gap: 8px; }
/* 机型 + 流程两行：把工艺列的信息并入机型列，省一整列 */
.model-cell { display: flex; flex-direction: column; line-height: 1.35; }
.model-process { font-size: 11px; }
.progress-bar { flex: 1; min-width: 48px; }
.progress-text { width: 40px; text-align: right; font-variant-numeric: tabular-nums; }

/* 锁列两行：上行为状态标签，下行为持锁时长与心跳断流时长 */
.lock-meta {
  font-size: 11px;
  color: var(--app-text-muted, #8a94a6);
  margin-top: 3px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
/* 操作列三个按钮不换行，避免行高被撑开 */
.products :deep(.el-table .cell) { white-space: nowrap; }
.products :deep(.el-table__fixed-right .cell),
.products :deep(.el-table__fixed .cell) { padding-left: 8px; padding-right: 8px; }
</style>
