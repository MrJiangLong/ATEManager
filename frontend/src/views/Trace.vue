<template>
  <div class="trace fade-up">
    <PageToolbar
      :title="$t('trace.title')"
      :subtitle="$t('trace.subtitle')"
      :breadcrumb="[{ key: 'p', label: $t('menu.products') }, { key: 't', label: sn }]"
    >
      <el-input v-model="inputSn" clearable :placeholder="$t('trace.targetSn')" style="width:220px" @keyup.enter="go" />
      <el-button type="primary" @click="go">{{ $t('common.search') }}</el-button>
      <template #extra>
        <el-button :icon="ArrowLeft" @click="router.push('/products')">{{ $t('trace.backToList') }}</el-button>
      </template>
    </PageToolbar>

    <div v-if="loading" class="loading-box"><el-skeleton :rows="6" animated /></div>

    <template v-else-if="data">
      <div class="kpi-row">
        <StatTile tone="blue" :icon="Cpu" :label="$t('common.sn')" :value="data.product.sn" :legend="snLegend" />
        <StatTile
          tone="green"
          :icon="CircleCheck"
          :label="$t('trace.progress', { passed: data.product.passed_count, total: data.product.total_steps })"
          :value="data.product.passed_count"
          :legend="progressLegend"
        />
        <StatTile tone="purple" :icon="Cpu" :label="$t('common.status')" :value="statusText" :legend="statusLegend" />
      </div>

      <DataCard :title="$t('configs.tabTopology')">
        <template #extra>
          <el-tag size="small" effect="plain" type="info">
            <CodeTip :code="data.product.process_id" :name="processName(data.product.process_id)">
              {{ data.product.process_id }}
            </CodeTip>
          </el-tag>
          <el-tag v-if="data.product.is_completed" size="small" type="success">{{ $t('trace.completed') }}</el-tag>
        </template>
        <div class="steps">
          <div v-for="(step, i) in data.steps" :key="step.station_id" class="step" :class="stepClass(step)">
            <div class="step-dot">
              <el-icon v-if="step.passed"><Select /></el-icon>
              <span v-else>{{ i + 1 }}</span>
            </div>
            <div class="step-body">
              <div class="step-name code">
                <CodeTip :code="step.station_id" :name="step.station_name">{{ step.station_id }}</CodeTip>
              </div>
              <div class="step-sub muted">{{ step.station_name }}</div>
              <div class="step-deps muted" v-if="step.depends_on?.length">
                {{ $t('trace.dependsOn') }}: {{ step.depends_on.join(', ') }}
              </div>
              <div class="step-meta">
                <el-tag v-if="step.is_optional" size="small" type="info" effect="plain">
                  {{ $t('trace.optional') }}
                </el-tag>
                <el-tag size="small" :type="step.passed ? 'success' : 'info'" effect="plain">
                  {{ step.passed ? $t('trace.passed') : $t('trace.pending') }}
                </el-tag>
                <el-tag v-if="step.last_result" size="small" :type="resultTagType(step.last_result)">
                  {{ $t(`result.${step.last_result}`) }}
                </el-tag>
                <span class="muted" v-if="step.last_time">{{ fmtDateTime(step.last_time) }}</span>
              </div>
            </div>
          </div>
        </div>
      </DataCard>

      <DataCard v-if="data.repairs.length" :title="$t('trace.repairs')">
        <el-table :data="data.repairs" stripe size="small">
          <el-table-column prop="repair_id" :label="$t('repairs.tableId')" width="90">
            <template #default="{ row }"><span class="code">{{ row.repair_id }}</span></template>
          </el-table-column>
          <el-table-column :label="$t('repairs.tableAction')" width="130" align="center">
            <template #default="{ row }">
              <el-tag :type="repairTagType(row.repair_action)" size="small">{{ $t(`repair.${row.repair_action}`) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="target_station" :label="$t('repairs.tableTarget')" width="140" />
          <el-table-column prop="reason" :label="$t('repairs.tableReason')" min-width="240" show-overflow-tooltip />
          <el-table-column prop="technician_id" :label="$t('repairs.tableTech')" width="120" />
          <el-table-column :label="$t('repairs.undoCol')" width="120" align="center">
            <template #default="{ row }">
              <el-tooltip
                v-if="row.undone_at"
                :content="`${$t('repairs.undoneBy')}: ${row.undone_by} · ${fmtDateTime(row.undone_at)}`"
                placement="top"
                :show-after="300"
              >
                <el-tag size="small" type="warning" effect="plain">{{ $t('repairs.undoneTag') }}</el-tag>
              </el-tooltip>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="$t('repairs.tableTime')" width="170">
            <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
          </el-table-column>
          <template #empty><EmptyState :text="$t('common.noData')" /></template>
        </el-table>
      </DataCard>

      <DataCard v-if="reportJobs.length" :title="$t('trace.reportTitle')">
        <el-table :data="reportJobs" stripe size="small">
          <el-table-column :label="$t('reports.tableStatus')" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="STATUS_META[row.status]?.tag || 'info'">
                {{ $t(`reports.status_${row.status}`) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('reports.tableArtifacts')" min-width="220">
            <template #default="{ row }">
              <div class="artifact-list">
                <div v-for="(art, index) in row.artifacts || []" :key="index" class="artifact-row">
                  <el-tag size="small" effect="plain" class="art-type" :type="art.failed ? 'danger' : 'info'">
                    {{ $t(`reports.type_${art.type}`) }}
                  </el-tag>
                  <el-tooltip :disabled="!art.purged" :content="t('reports.mesArchived')" placement="top" :show-after="300">
                    <button class="dl-chip" :disabled="dlDisabled(art, 'xlsx')" @click="download(row, index, 'xlsx')">
                      {{ $t('reports.excel') }}
                    </button>
                  </el-tooltip>
                  <el-tooltip
                    :disabled="(!art.pdf || art.pdf_status === 'done') && !art.purged"
                    :content="dlTip(art)"
                    placement="top"
                    :show-after="300"
                  >
                    <button class="dl-chip" :disabled="dlDisabled(art, 'pdf')" @click="download(row, index, 'pdf')">
                      {{ $t('reports.pdf') }}
                      <span v-if="art.pdf && art.pdf_status !== 'done'" class="pdf-dot" :class="`pdf-${art.pdf_status}`" />
                    </button>
                  </el-tooltip>
                </div>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="$t('reports.failedItems')" min-width="120">
            <template #default="{ row }">
              <el-tooltip
                v-if="(row.failed_items || []).length"
                :content="(row.failed_items || []).join('\n')"
                placement="top"
                :show-after="300"
              >
                <span class="failed-items">{{ (row.failed_items || []).join('; ') }}</span>
              </el-tooltip>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column :label="$t('reports.tableMes')" width="115">
            <template #default="{ row }">
              <el-tooltip v-if="row.mes_message" :content="row.mes_message" placement="top" :show-after="300">
                <el-tag size="small" :type="MES_META[row.mes_status] || 'info'" effect="plain">
                  {{ $t(`reports.mes_${row.mes_status}`) }}
                </el-tag>
              </el-tooltip>
              <el-tag v-else size="small" :type="MES_META[row.mes_status] || 'info'" effect="plain">
                {{ $t(`reports.mes_${row.mes_status}`) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('reports.tableCreated')" width="170">
            <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
          </el-table-column>
        </el-table>
      </DataCard>

      <DataCard :title="$t('trace.ledger')">
        <el-table
          :data="data.records"
          stripe
          size="small"
          row-key="record_id"
          :default-sort="{ prop: 'record_id', order: 'ascending' }"
        >
          <el-table-column type="expand">
            <template #default="{ row }">
              <div class="items-panel">
                <div class="items-head muted">
                  {{ $t('records.checkoutId') }}: <span class="code">{{ row.executed_items?.checkout_id || '—' }}</span>
                  · {{ $t('records.durationMs') }}: {{ fmtDurationMs(row.duration_ms) }}
                </div>
                <el-table :data="row.executed_items?.items || []" size="small" border>
                  <el-table-column :label="$t('configs.caseId')" min-width="220">
                    <template #default="{ row: it }">
                      <el-tooltip :content="caseLabel(row.station_id, it.case_id)" placement="top" :show-after="400">
                        <CaseIdText :value="it.case_id" />
                      </el-tooltip>
                    </template>
                  </el-table-column>
                  <el-table-column :label="$t('common.result')" width="90">
                    <template #default="{ row: it }">
                      <el-tag size="small" :type="resultTagType(it.result)">
                        {{ $t(`result.${it.result}`) }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column :label="$t('trace.executedItems')" min-width="240">
                    <template #default="{ row: it }">
                      <el-tooltip :content="JSON.stringify(it.values || {})" placement="top" :show-after="400">
                        <span class="code values-cell">{{ JSON.stringify(it.values || {}) }}</span>
                      </el-tooltip>
                    </template>
                  </el-table-column>
                  <el-table-column prop="message" :label="$t('repairs.tableReason')" min-width="160" show-overflow-tooltip />
                  <el-table-column :label="$t('records.artifacts')" min-width="160">
                    <template #default="{ row: it, $index: ii }">
                      <template v-if="(it.files || []).length">
                        <button
                          v-for="(f, fi) in it.files"
                          :key="f.object_key"
                          class="dl-chip"
                          @click="downloadArtifact(row, ii, fi, t)"
                        >{{ f.filename }}</button>
                      </template>
                      <span v-else class="muted">—</span>
                    </template>
                  </el-table-column>
                </el-table>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="record_id" :label="$t('records.tableId')" width="110" sortable>
            <template #default="{ row }"><span class="code">{{ row.record_id }}</span></template>
          </el-table-column>
          <el-table-column prop="station_id" :label="$t('records.tableStation')" width="160">
            <template #default="{ row }">
              <CodeTip :code="row.station_id" :name="stationName(row.station_id)"><span class="code">{{ row.station_id }}</span></CodeTip>
            </template>
          </el-table-column>
          <el-table-column prop="client_id" :label="$t('records.tableClient')" width="180" show-overflow-tooltip>
          <template #default="{ row }">
            <CodeTip :code="row.client_id" :name="clientName(row.client_id)"><span class="code">{{ row.client_id }}</span></CodeTip>
          </template>
          </el-table-column>
          <el-table-column :label="$t('records.tableItems')" min-width="90" align="right">
            <template #default="{ row }">{{ (row.executed_items?.items || []).length }}</template>
          </el-table-column>
          <el-table-column :label="$t('common.result')" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="resultTagType(row.overall_result)">
                {{ $t(`result.${row.overall_result}`) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('records.tableValid')" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="row.is_valid ? 'success' : 'info'" effect="plain">
                {{ row.is_valid ? $t('records.valid') : $t('records.invalid') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('records.tableTime')" width="170">
            <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
          </el-table-column>
          <template #empty><EmptyState :text="$t('common.noData')" /></template>
        </el-table>
      </DataCard>

      <DataCard :title="$t('sessions.title')">
        <el-table :data="sessions" stripe size="small">
          <el-table-column prop="station_id" :label="$t('sessions.tableStation')" width="140">
            <template #default="{ row }">
              <CodeTip :code="row.station_id" :name="stationName(row.station_id)"><span class="code">{{ row.station_id }}</span></CodeTip>
            </template>
          </el-table-column>
          <el-table-column prop="client_id" :label="$t('sessions.tableClient')" width="180" show-overflow-tooltip>
            <template #default="{ row }">
              <CodeTip :code="row.client_id" :name="clientName(row.client_id)"><span class="code">{{ row.client_id }}</span></CodeTip>
            </template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableAttempt')" width="180">
            <template #default="{ row }">
              <el-tooltip :content="attemptTipText(row.attempt, t)" placement="top">
                <span class="attempt-track">
                  <span
                    v-for="n in Math.min(row.attempt, 6)"
                    :key="n"
                    class="attempt-node"
                    :class="{ current: n === row.attempt }"
                  />
                  <span v-if="row.attempt > 6" class="muted more">+{{ row.attempt - 6 }}</span>
                </span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableCheckpoint')" width="180" align="right">
            <template #default="{ row }">{{ row.item_count }}</template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableEndReason')" min-width="180" show-overflow-tooltip>
            <template #default="{ row }"><span class="muted">{{ row.end_reason || '—' }}</span></template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableStatus')" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="sessionTagType(row.status)" effect="plain">
                {{ $t(`sessionStatus.${row.status}`) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableStarted')" width="170">
            <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.started_at) }}</span></template>
          </el-table-column>
          <template #empty><EmptyState :text="$t('common.noData')" /></template>
        </el-table>
      </DataCard>
    </template>

    <DataCard v-else>
      <EmptyState :text="$t('common.noData')" />
    </DataCard>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowLeft, CircleCheck, Cpu, Select } from '@element-plus/icons-vue'
import { productApi, recordApi, reportApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import StatTile from '../components/StatTile.vue'
import CaseIdText from '../components/CaseIdText.vue'
import CodeTip from '../components/CodeTip.vue'
import { useDictionaries } from '../composables/useDictionaries'
import { attemptTipText, fmtDateTime, fmtDurationMs, repairTagType, resultTagType, sessionTagType } from '../utils/format'
import { downloadArtifact } from '../utils/artifacts'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()
const { stationName, clientName, processName, ensureLoaded, ensureCaseNames, caseLabel } = useDictionaries()

const sn = ref(decodeURIComponent(route.params.sn || ''))
const inputSn = ref(sn.value)
const data = ref(null)
const sessions = ref([])
const reportJobs = ref([])
const loading = ref(false)

/* 与报告页 ReportJobsPanel 同一套状态映射，保证两处观感一致 */
const STATUS_META = {
  pending: { tag: 'info' },
  running: { tag: 'warning' },
  success: { tag: 'success' },
  partial: { tag: 'warning' },
  failed: { tag: 'danger' },
  invalid: { tag: 'danger' },
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
async function download(row, index, variant) {
  try {
    const res = await reportApi.downloadUrl(row.job_id, index, variant)
    window.open(res.data.url, '_blank')
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
}

async function load() {
  if (!sn.value) return
  loading.value = true
  try {
    const res = await recordApi.trace(sn.value)
    data.value = res.data
  } catch (e) {
    data.value = null
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
  try {
    const res = await productApi.sessions(sn.value)
    sessions.value = res.data
  } catch {
    sessions.value = []
  }
  // 出厂报告（只读展示，操作入口在报告页）；加载失败静默，不打断追溯主数据
  try {
    const res = await reportApi.listJobs({ sn: sn.value })
    reportJobs.value = res.data.items || []
  } catch {
    reportJobs.value = []
  }
}

function go() {
  const target = inputSn.value.trim()
  if (!target) return
  router.push(`/trace/${encodeURIComponent(target)}`)
}

const statusText = computed(() => {
  if (!data.value) return '-'
  if (data.value.product.is_completed) return t('status.COMPLETED')
  return t(`status.${data.value.product.current_status}`)
})
const snLegend = computed(() =>
  data.value
    ? [
        { text: `${t('common.model')} ${data.value.product.product_model}` },
        { text: `${t('trace.fwBaseline')} ${data.value.product.target_fw_version || '—'}` },
      ]
    : []
)
const progressLegend = computed(() => [
  {
    text: data.value?.product.is_completed ? t('trace.completed') : t('status.IN_PROCESS'),
    emphasis: true,
  },
])
const statusLegend = computed(() =>
  data.value
    ? [
        { text: `${t('products.tableFailCount')} ${data.value.product.fail_count}` },
        { text: `${t('products.tableClient')} ${data.value.product.current_client || '—'}` },
      ]
    : []
)

function stepClass(step) {
  if (step.passed) return 'step--done'
  return 'step--pending'
}

watch(() => route.params.sn, (v) => {
  sn.value = decodeURIComponent(v || '')
  inputSn.value = sn.value
  load()
})
onMounted(() => {
  ensureLoaded()
  ensureCaseNames()
  load()
})
</script>

<style scoped>
.trace { display: flex; flex-direction: column; gap: 16px; }
.loading-box { background: #fff; border-radius: var(--app-card-radius); padding: 20px; }

.steps { display: flex; flex-wrap: wrap; gap: 8px; }
/* 步骤卡片：原本用绝对定位的 8px 连接线填补 gap，但 flex-wrap 换行后，
   新行首项的连接线会渲染到容器左外侧形成悬空短线，故去掉连接线，
   改由序号圆点表达先后顺序 */
.step {
  display: flex;
  gap: 10px;
  flex: 1 1 200px;
  min-width: 190px;
  padding: 14px;
  border: 1px solid var(--app-border);
  border-radius: 12px;
  background: #fff;
}
.step--done { border-color: rgba(18, 183, 106, 0.45); background: rgba(18, 183, 106, 0.05); }
.step-dot {
  width: 26px; height: 26px; flex-shrink: 0; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; font-weight: 700; color: #fff; background: #c3cfe6;
}
.step--done .step-dot { background: var(--app-success); }
.step-body { min-width: 0; }
.step-name { font-size: 13.5px; font-weight: 600; }
.step-sub { font-size: 11.5px; margin-top: 2px; }
.step-deps { font-size: 11px; margin-top: 4px; }
.step-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-top: 8px; font-size: 11.5px; }

/* 尝试点阵：与测试会话页保持一致，末端实心块表示"当前这一次" */
.attempt-track { display: inline-flex; align-items: center; gap: 4px; }
.attempt-node {
  width: 14px;
  height: 5px;
  border-radius: 3px;
  background: var(--app-border, #dfe4ee);
}
.attempt-node.current { background: var(--app-warning, #f59e0b); }
.more { font-size: 11px; }
</style>
<style scoped>
/* 执行明细（values JSON）：单行省略，悬浮看完整内容 */
.values-cell {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 出厂报告产物网格：类型 + Excel/PDF 下载芯片，与报告页同款 */
.artifact-list { display: flex; flex-direction: column; gap: 5px; }
.artifact-row {
  display: grid;
  grid-template-columns: 88px 50px 50px auto;
  align-items: center;
  gap: 8px;
}
.art-type { justify-content: center; }
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
.failed-items { color: var(--el-color-danger); font-size: 12px; }
</style>

