<template>
  <div class="repairs fade-up">
    <PageToolbar :title="$t('repairs.title')" :subtitle="$t('repairs.subtitle')" />

    <!-- 面板顶部：处置动作分布统计（原环图口径，统一底色 + 动作色文字） -->
    <div class="stat-row">
      <div class="stat-tile">
        <div class="stat-value stat-total-value">{{ stats.total }}</div>
        <div class="stat-label">{{ t('repairs.statTotal') }}</div>
      </div>
      <div
        v-for="it in stats.items"
        :key="it.action"
        class="stat-tile"
      >
        <div class="stat-value" :style="{ color: ACTION_COLORS[it.action] || '#909399' }">{{ it.count }}</div>
        <div class="stat-label">{{ t(`repair.${it.action}`) }}</div>
      </div>
    </div>

    <!-- 台账与记录区（占满剩余高度，表格内部滚动） -->
    <DataCard class="ledger-card">
      <div class="filter-row">
        <el-input v-model="snFilter" clearable :placeholder="t('common.sn')" style="width:180px" @keyup.enter="search" />
        <el-select v-model="actionFilter" clearable :placeholder="t('repairs.tableAction')" style="width:150px">
          <el-option v-for="a in REPAIR_ACTIONS" :key="a" :value="a" :label="t(`repair.${a}`)" />
        </el-select>
        <el-input v-model="techFilter" clearable :placeholder="t('repairs.tableTech')" style="width:130px" @keyup.enter="search" />
        <el-select v-model="statusFilter" clearable :placeholder="t('repairs.undoCol')" style="width:120px">
          <el-option :value="'valid'" :label="t('repairs.statusValid')" />
          <el-option :value="'undone'" :label="t('repairs.statusUndone')" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          value-format="YYYY-MM-DD"
          :start-placeholder="t('common.startDate')"
          :end-placeholder="t('common.endDate')"
        />
        <div class="filter-right">
          <el-button :icon="RefreshRight" @click="search">{{ t('common.refresh') }}</el-button>
          <el-button :icon="Download" @click="exportCsv">{{ t('repairs.exportCsv') }}</el-button>
          <el-button v-if="canOperate" :icon="Tickets" @click="openBatch">{{ t('repairs.batchWizard') }}</el-button>
          <el-button v-if="canOperate" type="primary" :icon="Pointer" @click="openSingle">{{ t('repairs.singleDispose') }}</el-button>
        </div>
      </div>

      <div class="table-fill">
        <el-table
          v-loading="loading"
          :data="items"
          stripe
          size="small"
          height="100%"
          style="width: 100%; table-layout: fixed"
        >
        <el-table-column prop="repair_id" :label="t('repairs.tableId')" width="80">
          <template #default="{ row }"><span class="code">{{ row.repair_id }}</span></template>
        </el-table-column>
        <el-table-column prop="sn" :label="t('repairs.tableSn')" width="160">
          <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
        </el-table-column>
        <el-table-column :label="t('repairs.tableAction')" width="130" align="center">
          <template #default="{ row }">
            <el-tag :type="repairTagType(row.repair_action)" size="small">{{ t(`repair.${row.repair_action}`) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target_station" :label="t('repairs.tableTarget')" width="130">
          <template #default="{ row }">
            <CodeTip v-if="row.target_station" :code="row.target_station" :name="stationName(row.target_station)">
              <span class="code">{{ row.target_station }}</span>
            </CodeTip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="reason" :label="t('repairs.tableReason')" min-width="220" show-overflow-tooltip />
        <el-table-column prop="technician_id" :label="t('repairs.tableTech')" width="110" />
        <el-table-column :label="t('repairs.undoCol')" width="100" align="center">
          <template #default="{ row }">
            <el-tooltip
              v-if="row.undone_at"
              :content="`${t('repairs.undoneBy')}: ${row.undone_by} · ${fmtDateTime(row.undone_at)}`"
              placement="top"
              :show-after="300"
            >
              <el-tag size="small" type="warning" effect="plain">{{ t('repairs.undoneTag') }}</el-tag>
            </el-tooltip>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('repairs.tableTime')" width="160">
          <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column :label="t('common.action')" width="130" align="center" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">{{ t('repairs.detail') }}</el-button>
            <el-button
              v-if="canOperate && !row.undone_at && row.repair_action !== 'SCRAP'"
              link
              type="warning"
              size="small"
              @click="onUndo(row)"
            >
              {{ t('repairs.undo') }}
            </el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState :text="t('common.noData')" /></template>
        </el-table>
      </div>

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

    <!-- 单件处置抽屉（共享组件：与在制管理入口同一套确认与依赖重测逻辑） -->
    <RepairDisposeDrawer v-model="singleVisible" @saved="onDisposeSaved" />

    <!-- 批量处置抽屉（与单件同构：清单 + 动作 + 工位 + 原因，执行前自动校验 + 汇总确认） -->
    <el-drawer v-model="batchVisible" :title="t('repairs.batchWizard')" size="680px" class="form-dialog">
      <el-form label-position="top" @submit.prevent="executeBatch">
        <el-form-item :label="t('repairs.batchSnList')" required>
          <el-input v-model="batch.snText" type="textarea" :rows="7" :placeholder="t('repairs.batchSnListPh')" />
          <div class="muted sn-hint">
            {{ t('repairs.batchCount', { n: batchSns.length }) }} · {{ t('repairs.batchLimitTip') }}
          </div>
        </el-form-item>
        <el-form-item :label="t('products.repairAction')" required>
          <el-radio-group v-model="batch.repair_action" class="action-group">
            <el-radio-button v-for="a in BATCH_ACTIONS" :key="a" :value="a">{{ t(`repair.${a}`) }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item :label="t('products.repairTargetStation')" required>
          <el-select
            v-model="batch.target_station"
            filterable
            allow-create
            default-first-option
            :loading="stationOptionsLoading"
            :placeholder="t('repairs.batchStationPh')"
            style="width:100%"
          >
            <el-option v-for="s in stationOptions" :key="s" :value="s" :label="stationLabel(s)" />
          </el-select>
          <div class="muted sn-hint">{{ t('repairs.batchStationUnion') }}</div>
        </el-form-item>
        <el-form-item :label="t('products.repairReason')" required>
          <el-input v-model="batch.reason" type="textarea" :rows="2" :placeholder="t('products.repairReasonPh')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="drawer-footer">
          <el-button :icon="Close" @click="batchVisible = false">{{ t('common.close') }}</el-button>
          <el-button type="primary" :icon="Check" :loading="batchSaving" :disabled="!batchSns.length" @click="executeBatch">
            {{ t('repairs.execute') }}
          </el-button>
        </div>
      </template>

      <!-- 批量全流程弹窗：依赖重测确认 / 汇总确认 / 结果与撤销，明细表内嵌（列宽适配内容） -->
      <el-dialog
        v-model="confirmDialog.visible"
        :title="confirmDialog.stage === 'downstream' ? t('repairs.batchStepDownstream', { n: dialogStep }) : confirmDialog.stage === 'result' ? t('repairs.batchStepResult', { n: dialogStep }) : t('repairs.batchStepExecute', { n: dialogStep })"
        width="1200px"
        append-to-body
        :close-on-click-modal="false"
      >
        <el-alert
          :type="confirmDialog.stage === 'downstream' ? 'warning' : confirmDialog.stage === 'result' ? (batchUndone ? 'info' : (batchResults.every((i) => i.ok) ? 'success' : 'warning')) : 'info'"
          :closable="false"
        >
          <template #title>
            <span class="confirm-alert">
              <el-icon v-if="confirmDialog.stage === 'result'"><InfoFilled /></el-icon>
              <span>{{ confirmDialog.message }}</span>
            </span>
          </template>
        </el-alert>
        <!-- 第 1 步：只看受影响的 SN 与其下游工位（明细并入表格，警示条不再堆文本） -->
        <el-table
          v-if="confirmDialog.stage === 'downstream'"
          :data="cascadeRows"
          size="small"
          max-height="360"
          style="margin-top:12px; width:100%"
        >
          <el-table-column prop="sn" :label="t('common.sn')" width="160">
            <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
          </el-table-column>
          <el-table-column :label="t('repairs.colRetestStation')" min-width="160">
            <template #default="{ row }">{{ stationLabel(batch.target_station) }}</template>
          </el-table-column>
          <el-table-column :label="t('repairs.colDownstream')" min-width="220">
            <template #default="{ row }">{{ row.stations }}</template>
          </el-table-column>
        </el-table>
        <el-table
          v-else
          :data="batchResults"
          size="small"
          max-height="360"
          style="margin-top:12px; width:100%"
        >
          <el-table-column prop="sn" :label="t('common.sn')" width="160">
            <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
          </el-table-column>
          <el-table-column prop="product_model" :label="t('configs.modelId')" min-width="110">
            <template #default="{ row }">{{ row.product_model || '—' }}</template>
          </el-table-column>
          <el-table-column prop="process_id" :label="t('configs.modelProcess')" min-width="150">
            <template #default="{ row }">
              <CodeTip :code="row.process_id" :name="processName(row.process_id)">{{ row.process_id || '—' }}</CodeTip>
            </template>
          </el-table-column>
          <el-table-column :label="t('repairs.colResult')" min-width="70" align="center">
            <template #default="{ row }">
              <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? 'OK' : 'FAIL' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('repairs.colRevoked')" min-width="130" show-overflow-tooltip>
            <template #default="{ row }">{{ (row.revoked_stations || []).map((s) => stationLabel(s)).join('、') || '—' }}</template>
          </el-table-column>
          <el-table-column prop="invalidating_records" :label="t('repairs.colRecords')" min-width="90" align="center" />
          <el-table-column :label="t('repairs.colMessage')" min-width="140" show-overflow-tooltip>
            <template #default="{ row }">
              <span v-if="!row.ok" class="text-danger">{{ row.code }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
        </el-table>
        <template #footer>
          <el-button
            v-if="confirmDialog.stage === 'result' && !batchUndone"
            type="warning"
            plain
            :icon="RefreshLeft"
            :loading="undoSaving"
            @click="onBatchUndo"
          >
            {{ t('repairs.batchUndo') }}
          </el-button>
          <el-button :icon="Close" @click="confirmDialog.visible = false">
            {{ confirmDialog.stage === 'result' ? t('common.close') : t('common.cancel') }}
          </el-button>
          <el-button
            v-if="confirmDialog.stage !== 'result'"
            type="primary"
            :icon="confirmDialog.stage === 'downstream' ? RefreshLeft : Check"
            :loading="confirmDialog.loading"
            @click="onConfirmProceed"
          >
            {{ confirmDialog.stage === 'downstream' ? t('repairs.downstreamConfirm') : t('repairs.execute') }}
          </el-button>
        </template>
      </el-dialog>
    </el-drawer>

    <!-- 详情抽屉：本次处置 + 工件档案  -->
    <el-drawer v-model="detailVisible" :title="t('repairs.detailTitle')" size="680px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <!-- 本次处置 -->
          <div class="detail-section-title">{{ t('repairs.detailThisRepair') }}</div>
          <el-descriptions :column="2" size="small" border>
            <el-descriptions-item :label="t('repairs.tableAction')">
              <el-tag :type="repairTagType(detailRepair.repair_action)" size="small">
                {{ t(`repair.${detailRepair.repair_action}`) }}
              </el-tag>
              <el-tag v-if="detailRepair.undone_at" size="small" type="info" effect="plain" style="margin-left:4px">
                {{ t('repairs.undoneTag') }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item :label="t('repairs.tableTarget')">{{ detailRepair.target_station || '—' }}</el-descriptions-item>
            <el-descriptions-item :label="t('repairs.tableTech')">{{ detailRepair.technician_id }}</el-descriptions-item>
            <el-descriptions-item :label="t('repairs.tableTime')" :span="2">{{ fmtDateTime(detailRepair.created_at) }}</el-descriptions-item>
            <el-descriptions-item :label="t('products.repairReason')" :span="2">{{ detailRepair.reason || '—' }}</el-descriptions-item>
          </el-descriptions>

          <!-- 工件档案 -->
          <div class="detail-section-title">{{ t('repairs.detailProduct') }}</div>
          <el-descriptions :column="2" size="small" border>
            <el-descriptions-item :label="t('common.sn')">
              <span class="code">{{ detail.product.sn }}</span>
            </el-descriptions-item>
            <el-descriptions-item :label="t('configs.modelId')">{{ detail.product.product_model }}</el-descriptions-item>
            <el-descriptions-item :label="t('configs.modelProcess')" :span="2">{{ detail.product.process_id || '—' }}</el-descriptions-item>
            <el-descriptions-item :label="t('repairs.colStatus')">
              <el-tag :type="statusTagType(snStatusKeyOf(detail.product))" size="small">
                {{ t(`status.${snStatusKeyOf(detail.product)}`) }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item :label="t('products.tableFailCount')">{{ detail.product.fail_count }}</el-descriptions-item>
            <el-descriptions-item :label="t('repairs.detailCurrentFw')">{{ detail.product.current_fw_version || '—' }}</el-descriptions-item>
            <el-descriptions-item :label="t('configs.targetFw')">{{ detail.product.target_fw_version || '—' }}</el-descriptions-item>
          </el-descriptions>
          <el-alert v-if="detail.product.lock_zombie" type="error" :closable="false" style="margin-top:10px">
            {{ t('repairs.detailZombie') }}
          </el-alert>
        </template>
        <EmptyState v-else-if="!detailLoading" :text="t('common.noData')" />
      </div>
      <template #footer>
        <div class="drawer-footer">
          <el-button :icon="Close" @click="detailVisible = false">{{ t('common.close') }}</el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Close, Download, InfoFilled, Pointer, RefreshLeft, RefreshRight, Tickets } from '@element-plus/icons-vue'
import { recordApi, repairApi } from '../api'
import { useAuth } from '../stores/auth'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import CodeTip from '../components/CodeTip.vue'
import { useDictionaries } from '../composables/useDictionaries'
import { REPAIR_ACTIONS } from '../utils/constants'
import RepairDisposeDrawer from '../components/RepairDisposeDrawer.vue'
import { fmtDateTime, repairTagType, statusTagType } from '../utils/format'

const { t } = useI18n()
const { canOperate } = useAuth()
const { stationName, clientName, processName, ensureLoaded } = useDictionaries()

/** 工位显示：代码（字典名称）；无名称时代码兜底（与处置抽屉同口径） */
function stationLabel(s) {
  const n = stationName(s)
  return n ? `${s}（${n}）` : s
}

const items = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const snFilter = ref('')
const actionFilter = ref('')
const techFilter = ref('')
const statusFilter = ref('')
const dateRange = ref(null)
const saving = ref(false)

// ---- 处置动作分布统计（原环图口径：各动作累计次数） ----
const ACTION_COLORS = { RETEST: '#f59e0b', ROLLBACK: '#7c5cff', RESET: '#909399', SCRAP: '#ef4444' }
const stats = ref({ total: 0, items: [] })

async function loadStats() {
  try {
    const res = await repairApi.stats()
    stats.value = res.data
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
}

// ---- 台账 ----
async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (snFilter.value) params.sn = snFilter.value
    if (actionFilter.value) params.repair_action = actionFilter.value
    if (techFilter.value) params.technician = techFilter.value
    if (statusFilter.value) params.undone = statusFilter.value === 'undone'
    if (dateRange.value?.[0]) params.date_from = dateRange.value[0]
    if (dateRange.value?.[1]) params.date_to = dateRange.value[1]
    const res = await repairApi.list(params)
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

function search() {
  if (page.value === 1) load()
  else page.value = 1
}

/** 导出当前筛选结果（本页）为 CSV（带 BOM，Excel 直开） */
function exportCsv() {
  const head = ['repair_id', 'sn', 'repair_action', 'target_station', 'reason', 'technician_id', 'created_at']
  const rows = items.value.map((r) => [
    r.repair_id,
    r.sn,
    r.repair_action,
    r.target_station || '',
    (r.reason || '').replace(/[\r\n,]/g, ' '),
    r.technician_id,
    r.created_at || '',
  ])
  const csv =
    '\uFEFF' +
    [head, ...rows]
      .map((r) => r.map((c) => `"${String(c).replace(/"/g, '""')}"`).join(','))
      .join('\r\n')
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `repairs_${Date.now()}.csv`
  a.click()
  URL.revokeObjectURL(a.href)
}

// ---- 撤销处置：服务端判定可撤（最新一条 + 零后续动作 + 非 SCRAP），失败原因直接透出 ----
async function onUndo(row) {
  try {
    await ElMessageBox.confirm(
      t('repairs.undoConfirm', { sn: row.sn, action: t(`repair.${row.repair_action}`) }),
      t('common.confirm'),
      { type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await repairApi.undo(row.repair_id)
    ElMessage.success(t('repairs.undoSuccess'))
    await Promise.all([load(), loadStats()])
  } catch (e) {
    ElMessage.error(e.message)
  }
}

// ---- 详情抽屉：本次处置 + SN 追溯 ----
const detailVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const detailRepair = ref(null) // 台账行点击的那条处置记录

function snStatusKeyOf(product) {
  if (!product) return 'IDLE'
  return product.is_completed ? 'COMPLETED' : product.current_status
}
function passedPathOf(trace) {
  return (trace.steps || []).filter((s) => s.passed).map((s) => s.station_id)
}

function openDetail(row) {
  detailVisible.value = true
  detailLoading.value = true
  detail.value = null
  detailRepair.value = { ...row }
  recordApi
    .trace(row.sn)
    .then((res) => {
      detail.value = res.data
    })
    .catch((e) => ElMessage.error(e.message || t('errors.loadFailed')))
    .finally(() => {
      detailLoading.value = false
    })
}

// ---- 单件处置抽屉（共享组件 RepairDisposeDrawer：确认与依赖重测逻辑单点维护） ----
const singleVisible = ref(false)

function openSingle() {
  singleVisible.value = true
}

function onDisposeSaved() {
  Promise.all([load(), loadStats()])
}

// ---- 批量处置抽屉：与单件同构，仅清单为多 SN（RESET/SCRAP 服务端禁批量） ----
const BATCH_ACTIONS = ['RETEST', 'ROLLBACK']
const batchVisible = ref(false)
const batch = reactive({ snText: '', repair_action: 'RETEST', target_station: '', reason: '' })
const batchSaving = ref(false)
const batchResults = ref([])
const lastBatchId = ref('')
const batchUndone = ref(false)
const undoSaving = ref(false)

/** 批量撤销：按 batch_id 整批撤销（逐条独立生效，产线已动的件被跳过并回显原因） */
async function onBatchUndo() {
  const n = batchResults.value.filter((i) => i.ok).length || batchResults.value.length
  try {
    await ElMessageBox.confirm(
      t('repairs.batchUndoConfirm', { n }),
      t('repairs.downstreamTitle'),
      { type: 'warning', confirmButtonText: t('common.confirm') },
    )
  } catch {
    return
  }
  undoSaving.value = true
  try {
    const res = await repairApi.batchUndo({ batch_id: lastBatchId.value })
    batchResults.value = res.data.items
    batchUndone.value = true
    confirmDialog.message = t('repairs.batchUndoSummary', { ok: res.data.ok_count, fail: res.data.fail_count })
    await Promise.all([load(), loadStats()])
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    undoSaving.value = false
  }
}

const batchSns = computed(() =>
  batch.snText
    .split(/[\s,;，；]+/)
    .map((s) => s.trim())
    .filter(Boolean)
)

// 目标工位下拉：汇总清单内全部 SN 已盖章工位的并集（也允许手动输入其他工位）
const stationOptions = ref([])
const stationOptionsLoading = ref(false)
let stationFetchSeq = 0
let stationFetchTimer = null

watch(
  () => batchSns.value.join('\n'),
  (joined) => {
    batchResults.value = []
    clearTimeout(stationFetchTimer)
    const sns = joined ? batchSns.value : []
    if (!sns.length) {
      stationOptions.value = []
      return
    }
    // 防抖：粘贴/逐字编辑 SN 清单时不逐键发起反查
    stationFetchTimer = setTimeout(() => fetchStationUnion(sns), 400)
  },
)

async function fetchStationUnion(sns) {
  const seq = ++stationFetchSeq
  stationOptionsLoading.value = true
  try {
    const settled = await Promise.allSettled(sns.map((sn) => recordApi.trace(sn)))
    if (seq !== stationFetchSeq) return // 输入已变，丢弃过期结果
    const union = new Set()
    for (const r of settled) {
      if (r.status !== 'fulfilled') continue
      for (const s of r.value.data.steps || []) if (s.passed) union.add(s.station_id)
    }
    stationOptions.value = [...union].sort()
  } finally {
    if (seq === stationFetchSeq) stationOptionsLoading.value = false
  }
}

function openBatch() {
  Object.assign(batch, { snText: '', repair_action: 'RETEST', target_station: '', reason: '' })
  batchResults.value = []
  lastBatchId.value = ''
  batchUndone.value = false
  batchVisible.value = true
}

/** 单按钮流程：执行 → 预演 → 确认弹窗（内嵌预演明细表，依赖重测与汇总两阶段）→ 生效 */
const checkData = ref(null)
const confirmDownstream = ref(false)
const confirmDialog = reactive({ visible: false, stage: 'execute', message: '', loading: false })
const dialogStep = ref(0)
/** 第 1 步明细：命中级联的 SN → 带名称的下游工位清单 */
const cascadeRows = computed(() =>
  (checkData.value?.items || [])
    .filter((i) => !i.ok && i.code === 'downstream_invalidation_required')
    .map((i) => ({
      sn: i.sn,
      stations: ((i.message || '').split(': ')[1] || '')
        .split(', ')
        .filter(Boolean)
        .map((s) => stationLabel(s))
        .join('、'),
    })),
)

async function runPrecheck(confirm) {
  const res = await repairApi.batch({
    items: batchSns.value,
    repair_action: batch.repair_action,
    target_station: batch.target_station.trim() || undefined,
    reason: batch.reason.trim(),
    dry_run: true,
    confirm_downstream: confirm,
  })
  checkData.value = res.data
  batchResults.value = res.data.items
  return res.data.ok_count > 0
}

function summaryMessage() {
  const check = checkData.value
  const processes = new Set(
    (check.items || []).filter((i) => i.ok && i.process_id).map((i) => i.process_id),
  )
  const scope = [
    ...new Set((check.items || []).filter((i) => i.ok).flatMap((i) => i.revoked_stations || [])),
  ]
  const scopeText = scope.length
    ? ' ' + t('repairs.batchScope', { list: scope.map((s) => stationLabel(s)).join('、') })
    : ''
  return (
    t('repairs.batchConfirm', {
      ok: check.ok_count,
      fail: check.fail_count,
      action: t(`repair.${batch.repair_action}`),
    }) +
    scopeText +
    (processes.size > 1 ? ' ' + t('repairs.batchMultiProcess', { n: processes.size }) : '')
  )
}

function openConfirm(stage, message) {
  dialogStep.value += 1
  confirmDialog.stage = stage
  confirmDialog.message = message
  confirmDialog.visible = true
}

async function executeBatch() {
  if (!batchSns.value.length) return ElMessage.warning(t('repairs.snRequired'))
  if (!batch.target_station.trim()) return ElMessage.warning(t('products.targetRequired'))
  if (!batch.reason.trim()) return ElMessage.warning(t('repairs.reasonRequired'))
  confirmDownstream.value = false
  dialogStep.value = 0
  batchSaving.value = true
  try {
    // 第一步：预演（服务端只跑守卫不落库），把不可处置的件提前暴露出来
    await runPrecheck(false)
    // 依赖重测检查必须先于"全部失败"判断：单 SN 批次可能全部失败原因都是级联
    const cascade = checkData.value.items.filter(
      (i) => !i.ok && i.code === 'downstream_invalidation_required',
    )
    if (cascade.length) {
      const detail = cascade
        .map((i) => {
          const stations = (i.message || '').split(': ')[1] || ''
          return `${i.sn}：${stations.split(', ').map((s) => stationLabel(s)).join('、')}`
        })
        .join('；')
      openConfirm('downstream', t('repairs.batchDownstreamMsg', { list: detail }))
      return
    }
    if (!checkData.value.ok_count) {
      ElMessage.error(t('repairs.batchAllFailed'))
      return
    }
    openConfirm('execute', summaryMessage())
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    batchSaving.value = false
  }
}

async function onConfirmProceed() {
  if (confirmDialog.stage === 'downstream') {
    confirmDialog.loading = true
    try {
      const ok = await runPrecheck(true)
      if (!ok) {
        confirmDialog.visible = false
        ElMessage.error(t('repairs.batchAllFailed'))
        return
      }
      confirmDownstream.value = true
      openConfirm('execute', summaryMessage())
    } catch (e) {
      ElMessage.error(e.message)
    } finally {
      confirmDialog.loading = false
    }
    return
  }
  // 第二步：确认后真正生效；弹窗切换到结果阶段（结果表 + 撤销本批）
  batchSaving.value = true
  try {
    const res = await repairApi.batch({
      items: batchSns.value,
      repair_action: batch.repair_action,
      target_station: batch.target_station.trim() || undefined,
      reason: batch.reason.trim(),
      dry_run: false,
      confirm_downstream: confirmDownstream.value,
    })
    batchResults.value = res.data.items
    lastBatchId.value = res.data.batch_id || ''
    batchUndone.value = false
    confirmDialog.stage = 'result'
    confirmDialog.message = t('repairs.batchSummary', { ok: res.data.ok_count, fail: res.data.fail_count })
    await Promise.all([load(), loadStats()])
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    batchSaving.value = false
  }
}

watch([page, pageSize], () => load())
watch([snFilter, actionFilter, techFilter, statusFilter, dateRange], () => search())

onMounted(() => {
  ensureLoaded()
  load()
  loadStats()
})
</script>

<style scoped>
/* 页面撑满视口：指标条固定高度，台账卡片吃掉剩余空间，表格内部滚动 */
.repairs {
  display: flex;
  flex-direction: column;
  gap: 16px;
  height: 100%;
}
.ledger-card {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.ledger-card :deep(.card-body) {
  flex: 1 1 auto;
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.table-fill {
  flex: 1 1 auto;
  min-height: 0;
}
/* 指标条：统一浅底瓷砖（固定高度，不参与拉伸），数字用动作色区分 */
.stat-row { display: flex; gap: 16px; flex-wrap: wrap; flex: 0 0 auto; }
.stat-tile {
  flex: 1 1 0;
  min-width: 150px;
  background: var(--app-card-bg, #fff);
  border: 1px solid var(--app-card-border, #e4e7ed);
  border-radius: var(--app-card-radius, 8px);
  padding: 14px 18px;
}
.stat-value { font-size: 28px; font-weight: 700; color: var(--app-text); font-variant-numeric: tabular-nums; line-height: 1.2; }
.stat-total-value { color: var(--app-text); }
.stat-label { margin-top: 4px; font-size: 12.5px; color: #9aa6bd; }
.filter-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
/* 筛选行右侧动作簇：导出 / 批量 / 单件，与左侧筛选同行靠右 */
.filter-right { display: flex; align-items: center; gap: 10px; margin-left: auto; }
.action-group { display: flex; flex-wrap: wrap; }
.drawer-footer { display: flex; justify-content: flex-end; }
/* 确认弹窗警示条：Stamp 图标与文案同行（el-alert 无自定义 icon 属性，走 title 插槽） */
.confirm-alert { display: inline-flex; align-items: center; gap: 6px; }
/* 批量结果上方的撤销条：仅在最近一次批量可撤时显示 */
.batch-undo-bar { margin: 12px 0 8px; display: flex; justify-content: flex-end; }
/* SN 回显：状态标签 + 失败计数 + 已盖章路径，一行排布 */
.sn-hint { margin-top: 4px; font-size: 12.5px; }
/* 详情抽屉分区标题 */
.detail-section-title {
  margin: 18px 0 8px;
  font-size: 13.5px;
  font-weight: 650;
  color: var(--app-text);
}
.detail-section-title:first-of-type { margin-top: 0; }
.sn-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12.5px;
}
</style>
