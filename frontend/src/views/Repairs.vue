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
        <el-select v-model="dayFilter" style="width:110px">
          <el-option :value="0" :label="t('repairs.dayAll')" />
          <el-option :value="1" :label="t('repairs.dayToday')" />
          <el-option :value="3" :label="t('repairs.day3')" />
          <el-option :value="7" :label="t('repairs.day7')" />
        </el-select>
        <el-input v-model="snFilter" clearable :placeholder="t('common.sn')" style="width:180px" @keyup.enter="search" />
        <el-select v-model="actionFilter" clearable :placeholder="t('repairs.tableAction')" style="width:150px">
          <el-option v-for="a in REPAIR_ACTIONS" :key="a" :value="a" :label="t(`repair.${a}`)" />
        </el-select>
        <el-input v-model="techFilter" clearable :placeholder="t('repairs.tableTech')" style="width:130px" @keyup.enter="search" />
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
        <el-table-column prop="sn" :label="t('repairs.tableSn')" width="150">
          <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
        </el-table-column>
        <el-table-column :label="t('repairs.tableAction')" width="130" align="center">
          <template #default="{ row }">
            <el-tag :type="repairTagType(row.repair_action)" size="small">{{ t(`repair.${row.repair_action}`) }}</el-tag>
            <el-tag v-if="row.undone_at" size="small" type="info" effect="plain" style="margin-left:4px">
              {{ t('repairs.undoneTag') }}
            </el-tag>
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

    <!-- 单件处置抽屉 -->
    <el-drawer v-model="singleVisible" :title="t('repairs.singleDispose')" size="650px" class="form-dialog">
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-form-item :label="t('common.sn')" required>
          <el-input
            v-model="form.sn"
            :placeholder="t('products.repairSnPh')"
            clearable
            @blur="lookupSn()"
            @keyup.enter="lookupSn()"
            @clear="clearSnInfo"
          />
          <div v-if="snInfoLoading" class="sn-hint muted">{{ t('repairs.snLooking') }}</div>
          <div v-else-if="snInfoError === 'not_found'" class="sn-hint text-danger">{{ t('repairs.snNotFound') }}</div>
          <div v-else-if="snInfo" class="sn-summary">
            <el-tag :type="statusTagType(snStatusKey)" size="small">{{ t(`status.${snStatusKey}`) }}</el-tag>
            <span class="muted">{{ t('products.tableFailCount') }} {{ snInfo.product.fail_count }}</span>
            <span class="muted">{{ passedStations.join(' → ') || t('products.stepPending') }}</span>
            <span v-if="snInfo.product.current_status === 'TESTING'" class="text-danger">
              {{ t('repairs.snTestingHint') }}
            </span>
          </div>
        </el-form-item>
        <el-form-item :label="t('products.repairAction')" required>
          <el-radio-group v-model="form.repair_action" class="action-group">
            <el-radio-button v-for="a in REPAIR_ACTIONS" :key="a" :value="a">{{ t(`repair.${a}`) }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="needsTarget" required>
          <template #label>
            {{ t('products.repairTargetStation') }}
            <span v-if="!passedStations.length" class="text-danger">{{ t('repairs.noPassedStation') }}</span>
          </template>
          <el-select
            v-model="form.target_station"
            filterable
            :disabled="!passedStations.length"
            :placeholder="t('products.repairTargetStation')"
            style="width:100%"
          >
            <el-option v-for="s in passedStations" :key="s" :value="s" :label="s" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('products.repairReason')" required>
          <el-input v-model="form.reason" type="textarea" :rows="3" :placeholder="t('products.repairReasonPh')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="drawer-footer">
          <el-button @click="singleVisible = false">{{ t('common.close') }}</el-button>
          <el-button
            type="primary"
            :loading="saving"
            :disabled="snInfoError === 'not_found' || (needsTarget && !passedStations.length)"
            @click="submit"
          >
            {{ t('repairs.execute') }}
          </el-button>
        </div>
      </template>
    </el-drawer>

    <!-- 批量处置抽屉（与单件同构：清单 + 动作 + 工位 + 原因，执行前自动校验 + 汇总确认） -->
    <el-drawer v-model="batchVisible" :title="t('repairs.batchWizard')" size="650px" class="form-dialog">
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
            <el-option v-for="s in stationOptions" :key="s" :value="s" :label="s" />
          </el-select>
          <div class="muted sn-hint">{{ t('repairs.batchStationUnion') }}</div>
        </el-form-item>
        <el-form-item :label="t('products.repairReason')" required>
          <el-input v-model="batch.reason" type="textarea" :rows="2" :placeholder="t('products.repairReasonPh')" />
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="drawer-footer">
          <el-button @click="batchVisible = false">{{ t('common.close') }}</el-button>
          <el-button type="primary" :loading="batchSaving" :disabled="!batchSns.length" @click="executeBatch">
            {{ t('repairs.execute') }}
          </el-button>
        </div>
      </template>
      <el-table
        v-if="batchResults.length"
        :data="batchResults"
        stripe
        size="small"
        max-height="360"
        style="margin-top:14px; width:100%"
      >
        <el-table-column prop="sn" label="SN" width="140">
          <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
        </el-table-column>
        <el-table-column prop="product_model" :label="t('configs.modelId')" width="110">
          <template #default="{ row }">{{ row.product_model || '—' }}</template>
        </el-table-column>
        <el-table-column prop="process_id" :label="t('configs.modelProcess')" width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <CodeTip :code="row.process_id" :name="processName(row.process_id)">{{ row.process_id || '—' }}</CodeTip>
          </template>
        </el-table-column>
        <el-table-column :label="t('repairs.colResult')" width="70" align="center">
          <template #default="{ row }">
            <el-tag :type="row.ok ? 'success' : 'danger'" size="small">{{ row.ok ? 'OK' : 'FAIL' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t('repairs.colRevoked')" min-width="120" show-overflow-tooltip>
          <template #default="{ row }">{{ (row.revoked_stations || []).join('、') || '—' }}</template>
        </el-table-column>
        <el-table-column prop="invalidating_records" :label="t('repairs.colRecords')" width="90" align="center" />
        <el-table-column :label="t('repairs.colMessage')" min-width="130" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="!row.ok" class="text-danger">{{ row.code }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>

    <!-- 详情抽屉：本次处置 + 工件档案  -->
    <el-drawer v-model="detailVisible" :title="t('repairs.detailTitle')" size="650px">
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
          <el-button @click="detailVisible = false">{{ t('common.close') }}</el-button>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Download, Pointer, RefreshRight, Tickets } from '@element-plus/icons-vue'
import { recordApi, repairApi } from '../api'
import { useAuth } from '../stores/auth'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import CodeTip from '../components/CodeTip.vue'
import { useDictionaries } from '../composables/useDictionaries'
import { REPAIR_ACTIONS, REPAIR_ACTIONS_WITH_TARGET } from '../utils/constants'
import { fmtDateTime, repairTagType, statusTagType } from '../utils/format'

const { t } = useI18n()
const { canOperate } = useAuth()
const { stationName, clientName, processName, ensureLoaded } = useDictionaries()

const items = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const snFilter = ref('')
const actionFilter = ref('')
const techFilter = ref('')
const dayFilter = ref(0)
const saving = ref(false)
const form = reactive({ sn: '', repair_action: 'RETEST', target_station: '', reason: '' })

const needsTarget = computed(() => REPAIR_ACTIONS_WITH_TARGET.includes(form.repair_action))

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
    if (dayFilter.value) params.days = dayFilter.value
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

// ---- 单件处置抽屉 ----
const singleVisible = ref(false)

function openSingle() {
  Object.assign(form, { sn: '', repair_action: 'RETEST', target_station: '', reason: '' })
  clearSnInfo()
  singleVisible.value = true
}

// ---- SN 反查回显：提交前让维修员看到该件的当前状态与可回退工位 ----
const snInfo = ref(null)
const snInfoLoading = ref(false)
const snInfoError = ref('')

const snStatusKey = computed(() => snStatusKeyOf(snInfo.value?.product))
const passedStations = computed(() => passedPathOf(snInfo.value || {}))

function clearSnInfo() {
  snInfo.value = null
  snInfoError.value = ''
}

async function lookupSn(force = false) {
  const sn = form.sn.trim()
  if (!sn) {
    clearSnInfo()
    return
  }
  if (!force && snInfo.value?.product?.sn === sn) return
  snInfoLoading.value = true
  snInfoError.value = ''
  try {
    const res = await recordApi.trace(sn)
    if (form.sn.trim() !== sn) return
    snInfo.value = res.data
  } catch (e) {
    if (form.sn.trim() !== sn) return
    snInfo.value = null
    if (e.status === 404) {
      snInfoError.value = 'not_found'
    } else {
      snInfoError.value = ''
      ElMessage.error(e.message || t('errors.loadFailed'))
    }
  } finally {
    snInfoLoading.value = false
  }
}

watch([needsTarget, passedStations], () => {
  if (needsTarget.value && !passedStations.value.includes(form.target_station)) {
    form.target_station = passedStations.value[0] || ''
  }
})

/** 动作差异化后果提示：破坏力越强，警示越重（SCRAP 不可逆） */
const CONFIRM_KEY = {
  RETEST: 'repairs.confirmRetest',
  ROLLBACK: 'repairs.confirmRollback',
  RESET: 'repairs.confirmReset',
  SCRAP: 'repairs.confirmScrap',
}

async function submit() {
  if (!form.sn.trim()) return ElMessage.warning(t('repairs.snRequired'))
  if (!snInfo.value && !snInfoError.value) await lookupSn(true)
  if (snInfoError.value === 'not_found') return ElMessage.warning(t('repairs.snNotFound'))
  if (!form.reason.trim()) return ElMessage.warning(t('repairs.reasonRequired'))
  if (needsTarget.value && !form.target_station) return ElMessage.warning(t('products.targetRequired'))
  try {
    await ElMessageBox.confirm(
      t(CONFIRM_KEY[form.repair_action], { sn: form.sn.trim(), station: form.target_station || '—' }),
      t('common.confirm'),
      { type: form.repair_action === 'SCRAP' ? 'error' : 'warning' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    await repairApi.create({
      sn: form.sn.trim(),
      repair_action: form.repair_action,
      target_station: needsTarget.value ? form.target_station : undefined,
      reason: form.reason.trim(),
    })
    ElMessage.success(t('repairs.created'))
    singleVisible.value = false
    form.reason = ''
    await Promise.all([load(), loadStats()])
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

// ---- 批量处置抽屉：与单件同构，仅清单为多 SN（RESET/SCRAP 服务端禁批量） ----
const BATCH_ACTIONS = ['RETEST', 'ROLLBACK']
const batchVisible = ref(false)
const batch = reactive({ snText: '', repair_action: 'RETEST', target_station: '', reason: '' })
const batchSaving = ref(false)
const batchResults = ref([])

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
  batchVisible.value = true
}

/** 单按钮流程：点击执行 → 先自动校验（dry_run 不落库）→ 确认框汇总成败 → 确认后真正生效 */
async function executeBatch() {
  if (!batchSns.value.length) return ElMessage.warning(t('repairs.snRequired'))
  if (!batch.target_station.trim()) return ElMessage.warning(t('products.targetRequired'))
  if (!batch.reason.trim()) return ElMessage.warning(t('repairs.reasonRequired'))
  batchSaving.value = true
  let check
  try {
    // 第一步：校验（服务端只跑守卫不落库），把不可处置的件提前暴露出来
    const res = await repairApi.batch({
      items: batchSns.value,
      repair_action: batch.repair_action,
      target_station: batch.target_station.trim() || undefined,
      reason: batch.reason.trim(),
      dry_run: true,
    })
    check = res.data
    batchResults.value = check.items
  } catch (e) {
    batchSaving.value = false
    return ElMessage.error(e.message)
  }
  if (!check.ok_count) {
    batchSaving.value = false
    return ElMessage.error(t('repairs.batchAllFailed'))
  }
  // 第二步：汇总确认后真正生效；混合流程批次明确警示（同工位名在各流程内容不同）
  try {
    const processes = new Set(
      check.items.filter((i) => i.ok && i.process_id).map((i) => i.process_id),
    )
    await ElMessageBox.confirm(
      t('repairs.batchConfirm', {
        ok: check.ok_count,
        fail: check.fail_count,
        action: t(`repair.${batch.repair_action}`),
      }) +
        (processes.size > 1
          ? ' ' + t('repairs.batchMultiProcess', { n: processes.size })
          : ''),
      t('common.confirm'),
      { type: 'warning' },
    )
  } catch {
    batchSaving.value = false
    return
  }
  try {
    const res = await repairApi.batch({
      items: batchSns.value,
      repair_action: batch.repair_action,
      target_station: batch.target_station.trim() || undefined,
      reason: batch.reason.trim(),
      dry_run: false,
    })
    batchResults.value = res.data.items
    ElMessage.success(t('repairs.batchSummary', { ok: res.data.ok_count, fail: res.data.fail_count }))
    await Promise.all([load(), loadStats()])
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    batchSaving.value = false
  }
}

watch([page, pageSize], () => load())
watch([snFilter, actionFilter, techFilter, dayFilter], () => search())

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
