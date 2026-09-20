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
          <el-tag size="small" effect="plain" type="info">{{ data.product.process_id }}</el-tag>
          <el-tag v-if="data.product.is_completed" size="small" type="success">{{ $t('trace.completed') }}</el-tag>
        </template>
        <div class="steps">
          <div v-for="(step, i) in data.steps" :key="step.station_id" class="step" :class="stepClass(step)">
            <div class="step-dot">
              <el-icon v-if="step.passed"><Select /></el-icon>
              <span v-else>{{ i + 1 }}</span>
            </div>
            <div class="step-body">
              <div class="step-name">{{ step.station_id }}</div>
              <div class="step-sub muted">{{ step.station_name }}</div>
              <div class="step-deps muted" v-if="step.depends_on?.length">
                {{ $t('trace.dependsOn') }}: {{ step.depends_on.join(', ') }}
              </div>
              <div class="step-meta">
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
                  {{ $t('records.checkoutId') }}: <span class="code">{{ row.executed_items?.checkout_id || '-' }}</span>
                  · {{ $t('records.durationMs') }}: {{ fmtDurationMs(row.duration_ms) }}
                </div>
                <el-table :data="row.executed_items?.items || []" size="small" border>
                  <el-table-column :label="$t('configs.caseId')" min-width="220">
                    <template #default="{ row: it }">
                      <el-tooltip :content="it.case_id" placement="top" :show-after="400">
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
                </el-table>
              </div>
            </template>
          </el-table-column>
          <el-table-column prop="record_id" :label="$t('records.tableId')" width="110" sortable />
          <el-table-column prop="station_id" :label="$t('records.tableStation')" width="140">
            <template #default="{ row }"><span class="code">{{ row.station_id }}</span></template>
          </el-table-column>
          <el-table-column :label="$t('common.result')" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="resultTagType(row.overall_result)">
                {{ $t(`result.${row.overall_result}`) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('records.tableItems')" min-width="90" align="right">
            <template #default="{ row }">{{ (row.executed_items?.items || []).length }}</template>
          </el-table-column>
          <el-table-column prop="client_id" :label="$t('records.tableClient')" width="150" show-overflow-tooltip>
          <template #default="{ row }"><span class="code">{{ row.client_id }}</span></template>
        </el-table-column>
          <el-table-column :label="$t('records.tableValid')" width="90">
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
            <template #default="{ row }"><span class="code">{{ row.station_id }}</span></template>
          </el-table-column>
          <el-table-column prop="client_id" :label="$t('sessions.tableClient')" width="150" show-overflow-tooltip>
            <template #default="{ row }"><span class="code">{{ row.client_id }}</span></template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableAttempt')" width="120">
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
          <el-table-column :label="$t('sessions.tableStatus')" width="120">
            <template #default="{ row }">
              <el-tag size="small" :type="sessionTagType(row.status)" effect="plain">
                {{ $t(`sessionStatus.${row.status}`) }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableCheckpoint')" width="100" align="right">
            <template #default="{ row }">{{ row.item_count }}</template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableStarted')" width="170">
            <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.started_at) }}</span></template>
          </el-table-column>
          <el-table-column :label="$t('sessions.tableEndReason')" min-width="200" show-overflow-tooltip>
            <template #default="{ row }"><span class="muted">{{ row.end_reason || '—' }}</span></template>
          </el-table-column>
          <template #empty><EmptyState :text="$t('common.noData')" /></template>
        </el-table>
      </DataCard>

      <DataCard :title="$t('trace.repairs')">
        <el-table :data="data.repairs" stripe size="small">
          <el-table-column prop="repair_id" :label="$t('repairs.tableId')" width="90" />
          <el-table-column :label="$t('repairs.tableAction')" width="130" align="center">
            <template #default="{ row }">
              <el-tag :type="repairTagType(row.repair_action)" size="small">{{ $t(`repair.${row.repair_action}`) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="target_station" :label="$t('repairs.tableTarget')" width="140" />
          <el-table-column prop="reason" :label="$t('repairs.tableReason')" min-width="240" show-overflow-tooltip />
          <el-table-column prop="technician_id" :label="$t('repairs.tableTech')" width="120" />
          <el-table-column :label="$t('repairs.tableTime')" width="170">
            <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
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
import { productApi, recordApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import StatTile from '../components/StatTile.vue'
import CaseIdText from '../components/CaseIdText.vue'
import { attemptTipText, fmtDateTime, fmtDurationMs, repairTagType, resultTagType, sessionTagType } from '../utils/format'

const { t } = useI18n()
const route = useRoute()
const router = useRouter()

const sn = ref(decodeURIComponent(route.params.sn || ''))
const inputSn = ref(sn.value)
const data = ref(null)
const sessions = ref([])
const loading = ref(false)

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
        { text: `${t('trace.fwBaseline')} ${data.value.product.target_fw_version || '-'}` },
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
        { text: `${t('products.tableClient')} ${data.value.product.current_client || '-'}` },
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
onMounted(() => load())
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
</style>

