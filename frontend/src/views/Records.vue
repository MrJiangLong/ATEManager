<template>
  <div class="records fade-up">
    <PageToolbar :title="$t('records.title')" :subtitle="$t('records.subtitle')">
      <el-input v-model="filters.sn" clearable :placeholder="$t('records.searchSn')" style="width:170px" @keyup.enter="search" />
      <el-select v-model="filters.station_id" clearable :placeholder="$t('records.filterStation')" style="width:160px">
        <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
      </el-select>
      <el-select v-model="filters.product_model" clearable :placeholder="$t('records.filterModel')" style="width:150px">
        <el-option v-for="m in models" :key="m.product_model" :value="m.product_model" :label="m.product_model" />
      </el-select>
      <el-select v-model="filters.overall_result" clearable :placeholder="$t('records.filterResult')" style="width:110px">
        <el-option v-for="r in TEST_RESULTS" :key="r" :value="r" :label="$t(`result.${r}`)" />
      </el-select>
      <el-select v-model="filters.is_valid" clearable :placeholder="$t('records.filterValid')" style="width:130px">
        <el-option :value="true" :label="$t('records.validOnly')" />
        <el-option :value="false" :label="$t('records.invalidOnly')" />
      </el-select>
      <el-date-picker
        v-model="dateRange"
        type="daterange"
        value-format="YYYY-MM-DD"
        :start-placeholder="$t('records.startDate')"
        :end-placeholder="$t('records.endDate')"
        style="width:250px"
      />
      <template #extra>
        <el-button :icon="Refresh" @click="search">{{ $t('common.refresh') }}</el-button>
      </template>
    </PageToolbar>

    <DataCard>
      <el-table v-loading="loading" :data="items" stripe size="small" style="width:100%; table-layout:fixed">
        <el-table-column type="expand">
          <template #default="{ row }">
            <div class="items-panel">
              <div class="items-head muted">
                {{ $t('records.checkoutId') }}: <span class="code">{{ row.executed_items?.checkout_id || '-' }}</span>
                · {{ $t('trace.fwBaseline') }}: {{ row.executed_items?.firmware || '-' }}
                · {{ $t('records.durationMs') }}: {{ fmtDurationMs(row.duration_ms) }}
              </div>
              <el-table :data="row.executed_items?.items || []" size="small" border>
                <el-table-column prop="case_id" :label="$t('configs.caseId')" min-width="220" show-overflow-tooltip>
                  <template #default="{ row: it }"><span class="code">{{ it.case_id }}</span></template>
                </el-table-column>
                <el-table-column :label="$t('common.result')" width="90">
                  <template #default="{ row: it }">
                    <el-tag size="small" :type="resultTagType(it.result)">
                      {{ $t(`result.${it.result}`) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column :label="$t('trace.executedItems')" min-width="240">
                  <template #default="{ row: it }"><span class="code">{{ JSON.stringify(it.values || {}) }}</span></template>
                </el-table-column>
                <el-table-column prop="message" :label="$t('repairs.tableReason')" min-width="160" />
              </el-table>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="record_id" :label="$t('records.tableId')" min-width="100" />
        <el-table-column prop="sn" :label="$t('records.tableSn')" min-width="150">
          <template #default="{ row }">
            <el-button link type="primary" @click="goTrace(row.sn)"><span class="code">{{ row.sn }}</span></el-button>
          </template>
        </el-table-column>
        <el-table-column prop="station_id" :label="$t('records.tableStation')" min-width="160">
          <template #default="{ row }"><span class="code">{{ row.station_id }}</span></template>
        </el-table-column>
        <el-table-column :label="$t('common.result')" min-width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="resultTagType(row.overall_result)">
              {{ $t(`result.${row.overall_result}`) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('records.tableItems')" min-width="100" align="right">
          <template #default="{ row }">{{ (row.executed_items?.items || []).length }}</template>
        </el-table-column>
        <el-table-column prop="client_id" :label="$t('records.tableClient')" min-width="150" />
        <el-table-column :label="$t('records.tableDuration')" min-width="120" align="right">
          <template #default="{ row }">{{ fmtDurationMs(row.duration_ms) }}</template>
        </el-table-column>
        <el-table-column :label="$t('records.tableValid')" min-width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="row.is_valid ? 'success' : 'info'" effect="plain">
              {{ row.is_valid ? $t('records.valid') : $t('records.invalid') }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="$t('records.tableTime')" min-width="200">
          <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
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
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { modelApi, recordApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import { useProcesses } from '../composables/useProcesses'
import { TEST_RESULTS } from '../utils/constants'
import { fmtDateTime, fmtDurationMs, resultTagType } from '../utils/format'

const { t } = useI18n()
const router = useRouter()
const { stations, loadProcesses } = useProcesses()

const items = ref([])
const models = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const dateRange = ref(null)
const filters = reactive({ sn: '', station_id: '', product_model: '', overall_result: '', is_valid: '' })

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (filters.sn) params.sn = filters.sn
    if (filters.station_id) params.station_id = filters.station_id
    if (filters.product_model) params.product_model = filters.product_model
    if (filters.overall_result) params.overall_result = filters.overall_result
    if (filters.is_valid !== '') params.is_valid = filters.is_valid
    if (dateRange.value?.[0]) params.date_from = dateRange.value[0]
    if (dateRange.value?.[1]) params.date_to = dateRange.value[1]
    const res = await recordApi.list(params)
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

function goTrace(sn) {
  router.push(`/trace/${encodeURIComponent(sn)}`)
}

watch([page, pageSize], () => load())
watch(
  () => [filters.sn, filters.station_id, filters.product_model, filters.overall_result, filters.is_valid, dateRange.value],
  () => search()
)

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
</script>

<style scoped>
/* 关键链路：每个层级的父都必须是 flex 列 + min-height:0，
   高度才能从 main 一路传到 el-table */
.records {
  display: flex;
  flex-direction: column;
  gap: 16px;
  flex: 1 1 auto;
  min-height: 0;
  height: 100%;
}
.records :deep(.data-card) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}
.records :deep(.data-card .card-body) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}
/* el-table 在 Element Plus 中只有显式 height 才会启用"固定表头+内滚"，
   给一个 calc(100% - 分页器高度) 让它真正占满 */
.records :deep(.el-table) {
  flex: 1 1 auto;
  min-height: 0;
  height: calc(100% - 56px);
}
.records :deep(.pager) {
  flex: 0 0 auto;
  margin-top: var(--app-space-3);
}
</style>
