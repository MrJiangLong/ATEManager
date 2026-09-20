<template>
  <div class="dashboard fade-up">
    <PageToolbar :title="$t('dashboard.title')" :subtitle="$t('dashboard.subtitle')">
      <el-select v-model="processFilter" clearable :placeholder="$t('common.process')" style="width:200px">
        <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
      </el-select>
      <el-radio-group v-model="windowDays" size="small" class="window-switch">
        <el-radio-button v-for="d in WINDOWS" :key="d" :value="d">
          {{ d }} {{ $t('common.unitDays') }}
        </el-radio-button>
      </el-radio-group>
      <template #extra>
        <el-button :icon="Refresh" @click="loadAll()">{{ $t('common.refresh') }}</el-button>
      </template>
    </PageToolbar>

    <div class="kpi-row">
      <StatTile
        :icon="Tickets"
        tone="blue"
        :label="$t('dashboard.wipTitle')"
        :value="wip.total ?? 0"
        :unit="$t('dashboard.wipTotal')"
        :legend="wipLegend"
        :hint="$t('dashboard.wipHint')"
        clickable
        @click="go('/products')"
      />
      <StatTile
        :icon="Notebook"
        tone="teal"
        :label="$t('dashboard.todayTitle')"
        :value="today.total ?? 0"
        :unit="$t('dashboard.todayTotal')"
        :legend="todayLegend"
        :hint="$t('dashboard.todayHint')"
        clickable
        @click="go('/records')"
      />
      <StatTile
        :icon="CircleCheck"
        tone="green"
        :label="$t('dashboard.yieldTitle')"
        :value="yieldRate"
        unit="%"
        :legend="yieldLegend"
        :hint="$t('dashboard.yieldHint')"
      />
      <StatTile
        :icon="Monitor"
        tone="indigo"
        :label="$t('dashboard.clientsTitle')"
        :value="clients.online ?? 0"
        :unit="`/ ${clients.total ?? 0} ${$t('dashboard.clientsOnline')}`"
        :legend="clientsLegend"
        :hint="$t('dashboard.stationsHint')"
        clickable
        @click="go('/clients')"
      />
      <StatTile
        :tone="lockTone"
        :icon="Lock"
        :label="$t('dashboard.lockTitle')"
        :value="locks.active ?? 0"
        :unit="$t('dashboard.lockUnit')"
        :legend="lockLegend"
        :hint="$t('dashboard.lockHint')"
        clickable
        @click="go('/sessions')"
      />
    </div>

    <DataCard :title="$t('dashboard.trendTitle')" :loading="loading">
      <template #extra>
        <span class="muted">{{ windowDays }} {{ $t('common.unitDays') }}</span>
      </template>
      <div ref="trendEl" class="chart" />
      <EmptyState v-if="!loading && !hasTrend" :text="$t('dashboard.noData')" />
    </DataCard>

    <div class="grid-2">
      <DataCard :title="$t('dashboard.stationYieldTitle')">
        <YieldTable
          :rows="overview?.station_yield"
          :column-label="$t('dashboard.stationColName')"
          :search-ph="$t('dashboard.stationSearchPh')"
          :empty-text="$t('dashboard.noData')"
          show-first-pass
          :first-pass-label="$t('dashboard.colFirstPass')"
        />
      </DataCard>
      <DataCard :title="$t('dashboard.processYieldTitle')">
        <template #extra>
          <span class="muted">{{ $t('dashboard.processYieldHint', { n: overview?.process_unit_yield_pending ?? 0 }) }}</span>
        </template>
        <YieldTable
          :rows="overview?.process_unit_yield"
          :column-label="$t('dashboard.processColName')"
          :search-ph="$t('dashboard.processSearchPh')"
          :empty-text="$t('dashboard.noData')"
          show-first-pass
          :first-pass-label="$t('dashboard.colFirstPass')"
        />
      </DataCard>
    </div>

    <DataCard :title="$t('dashboard.topFailedTitle')">
      <TopFailedList :items="overview?.top_failed_items || []" :empty-text="$t('dashboard.noData')" />
    </DataCard>
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import * as echarts from 'echarts'
import {
  CircleCheck,
  Lock,
  Monitor,
  Notebook,
  Refresh,
  Tickets,
} from '@element-plus/icons-vue'
import { metricsApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import StatTile from '../components/StatTile.vue'
import TopFailedList from '../components/TopFailedList.vue'
import YieldTable from '../components/YieldTable.vue'
import { usePolling } from '../composables/usePolling'
import { useProcesses } from '../composables/useProcesses'

const WINDOWS = [7, 14, 30]

const { t, locale } = useI18n()
const router = useRouter()
const { processes, loadProcesses } = useProcesses()

const windowDays = ref(14)
const processFilter = ref('')
const loading = ref(false)
const overview = ref(null)
const trendEl = ref(null)
let trendChart = null

const wip = computed(() => overview.value?.wip || {})
const today = computed(() => overview.value?.today || {})
const clients = computed(() => overview.value?.clients || {})
const locks = computed(() => overview.value?.locks || {})
const hasTrend = computed(() => (overview.value?.trend || []).some((d) => d.total > 0))

const yieldRate = computed(() => today.value.pass_rate ?? 0)
/** 只有异常才上色：有僵尸锁的工位锁卡片转为红色调 */
const lockTone = computed(() => (locks.value.zombie ? 'danger' : 'slate'))
/**
 * 窗口整体良率：优先取后端按量加权的结果（overview.window）。
 * 后端尚未升级到返回 window 的版本时，用 trend 的 passed/total 自行加权兜底，
 * 避免 `?? 0` 直接把均值显示成 0%。
 * 注意：绝不能对 trend 的日良率取算术平均——无产出日会被按 0% 计入。
 */
const windowYield = computed(() => {
  const w = overview.value?.window
  if (w && w.total > 0) return w.pass_rate
  const trend = overview.value?.trend || []
  const total = trend.reduce((sum, d) => sum + (d.total || 0), 0)
  const passed = trend.reduce((sum, d) => sum + (d.passed || 0), 0)
  return total ? Math.round((passed / total) * 1000) / 10 : 0
})

/* 图例一律中性灰点：只有异常项（锁定 / 报废 / 失败 / 僵尸 / 异常会话）标红，
   窗口均值作为对照基线用品牌蓝强调，避免出现多色仪表盘 */
const wipLegend = computed(() => [
  { text: `${t('status.IDLE')} ${wip.value.idle ?? 0}` },
  { text: `${t('status.TESTING')} ${wip.value.testing ?? 0}` },
  { text: `${t('status.LOCKED')} ${wip.value.locked ?? 0}`, alert: (wip.value.locked ?? 0) > 0 },
  { text: `${t('status.SCRAPPED')} ${wip.value.scrapped ?? 0}`, alert: (wip.value.scrapped ?? 0) > 0 },
  { text: `${t('status.COMPLETED')} ${wip.value.completed ?? 0}` },
])
const todayLegend = computed(() => [
  { text: `${t('result.PASS')} ${today.value.passed ?? 0}` },
  { text: `${t('result.FAIL')} ${today.value.failed ?? 0}`, alert: (today.value.failed ?? 0) > 0 },
])
const yieldLegend = computed(() => [
  { text: t('dashboard.windowAvg', { days: windowDays.value, n: windowYield.value }), emphasis: true },
  { text: `${t('result.PASS')} ${today.value.passed ?? 0}` },
  { text: `${t('result.FAIL')} ${today.value.failed ?? 0}`, alert: (today.value.failed ?? 0) > 0 },
])
const clientsLegend = computed(() => [
  { text: `${t('dashboard.productTotal')} ${overview.value?.product_total ?? 0}` },
  { text: `${t('configs.tabProcesses')} ${processes.value.length}` },
])
const lockLegend = computed(() => [
  { text: `${t('dashboard.lockActive')} ${locks.value.active ?? 0}` },
  { text: `${t('dashboard.lockZombie')} ${locks.value.zombie ?? 0}`, alert: (locks.value.zombie ?? 0) > 0 },
  {
    text: `${t('sessions.kpiAbnormal')} ${locks.value.sessions_abnormal ?? 0}`,
    alert: (locks.value.sessions_abnormal ?? 0) > 0,
  },
])

function go(path) {
  router.push(path)
}

/** silent=true 用于后台轮询：不切 loading，避免整页闪烁 */
let requestSeq = 0
async function loadAll(silent = false) {
  // 切窗口/切流程时旧请求可能后到，用序号丢弃过期响应，避免均值串到上一个窗口
  const seq = ++requestSeq
  if (!silent) loading.value = true
  try {
    const params = { days: windowDays.value }
    if (processFilter.value) params.process_id = processFilter.value
    const [overviewRes] = await Promise.all([metricsApi.overview(params), loadProcesses()])
    if (seq !== requestSeq) return
    overview.value = overviewRes.data
    await nextTick()
    renderTrend()
  } catch (e) {
    if (seq === requestSeq) ElMessage.error(e.message)
  } finally {
    if (!silent && seq === requestSeq) loading.value = false
  }
}

function renderTrend() {
  if (!trendEl.value) return
  if (!trendChart) trendChart = echarts.init(trendEl.value)
  const trend = overview.value?.trend || []
  // 字体族跟随界面语言，保证英文界面不出现中文回退字
  const fontFamily = locale.value === 'en' ? 'var(--app-font-sans)' : 'var(--app-font-sans-zh)'

  const avgPass = windowYield.value
  const lastIdx = trend.length - 1

  trendChart.setOption({
    grid: { left: 4, right: 4, top: 36, bottom: 4, containLabel: true },
    tooltip: {
      trigger: 'axis',
      backgroundColor: '#ffffff',
      borderColor: '#e3e8f2',
      borderWidth: 1,
      padding: [10, 12],
      extraCssText: 'box-shadow: 0 6px 18px rgba(31, 43, 77, 0.08); border-radius: 8px;',
      textStyle: { color: '#1f2b4d', fontSize: 12, fontFamily },
      axisPointer: {
        type: 'line',
        lineStyle: { color: 'rgba(47, 107, 255, 0.25)', type: 'dashed' },
      },
      formatter: (params) => {
        if (!params?.length) return ''
        const date = params[0].axisValueLabel
        let html = `<div style="font-size:11px;color:#9aa6bd;margin-bottom:6px;letter-spacing:.3px">${date}</div>`
        params.forEach((p) => {
          const isRate = p.seriesName === t('dashboard.chartAxisPassRate')
          const unit = isRate ? '%' : ''
          // 良率线的点已空心化（白色填充），tooltip 圆点固定用品牌色避免白底隐身
          const dotColor = isRate ? '#2f6bff' : p.color
          const dot = `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${dotColor};margin-right:6px;vertical-align:middle"></span>`
          html += `<div style="display:flex;align-items:center;justify-content:space-between;gap:18px;line-height:20px">`
          html += `<span style="color:#6b7a99">${dot}${p.seriesName}</span>`
          html += `<span style="font-weight:600;color:#1f2b4d;font-variant-numeric:tabular-nums">${p.value}${unit}</span>`
          html += `</div>`
        })
        return html
      },
    },
    legend: {
      data: [t('dashboard.chartAxisTotal'), t('dashboard.chartAxisPassRate')],
      top: 0,
      right: 0,
      icon: 'roundRect',
      itemWidth: 8,
      itemHeight: 8,
      itemGap: 14,
      textStyle: { fontSize: 11.5, fontFamily, color: '#6b7a99' },
    },
    xAxis: {
      type: 'category',
      data: trend.map((d) => d.date.slice(5)),
      boundaryGap: true,
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { fontFamily, fontSize: 11, color: '#9aa6bd', margin: 10 },
    },
    yAxis: [
      {
        type: 'value',
        minInterval: 1,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { fontFamily, fontSize: 11, color: '#9aa6bd' },
        splitLine: { lineStyle: { color: '#eef0f6', type: 'dashed' } },
      },
      {
        type: 'value',
        max: 100,
        min: 0,
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { formatter: '{value}%', fontFamily, fontSize: 11, color: '#9aa6bd' },
        splitLine: { show: false },
      },
    ],
    series: [
      {
        name: t('dashboard.chartAxisTotal'),
        type: 'bar',
        data: trend.map((d) => d.total),
        barWidth: 10,
        barGap: '40%',
        itemStyle: { borderRadius: [5, 5, 0, 0], color: '#e3eaff' },
        emphasis: { itemStyle: { color: '#2f6bff' } },
        z: 1,
      },
      {
        name: t('dashboard.chartAxisPassRate'),
        type: 'line',
        yAxisIndex: 1,
        smooth: 0.5,
        data: trend.map((d) => d.pass_rate),
        symbol: 'circle',
        symbolSize: 6,
        showSymbol: true,
        lineStyle: { color: '#2f6bff', width: 2.5, cap: 'round' },
        itemStyle: { color: '#fff', borderColor: '#2f6bff', borderWidth: 1.5 },
        emphasis: { focus: 'series', scale: 1.6 },
        areaStyle: {
          color: {
            type: 'linear',
            x: 0, y: 0, x2: 0, y2: 1,
            colorStops: [
              { offset: 0, color: 'rgba(47, 107, 255, 0.18)' },
              { offset: 1, color: 'rgba(47, 107, 255, 0.00)' },
            ],
          },
        },
        z: 3,
        markPoint: lastIdx >= 0
          ? {
              symbol: 'circle',
              symbolSize: 8,
              data: [{ coord: [lastIdx, trend[lastIdx]?.pass_rate ?? 0] }],
              itemStyle: { color: '#2f6bff', borderColor: '#fff', borderWidth: 2, shadowBlur: 6, shadowColor: 'rgba(47, 107, 255, 0.35)' },
              label: { show: false },
            }
          : undefined,
        markLine: avgPass
          ? {
              silent: true,
              symbol: 'none',
              data: [{ yAxis: avgPass }],
              lineStyle: { color: '#9aa6bd', type: 'dashed', width: 1 },
              label: {
                show: true,
                position: 'insideEndTop',
                formatter: t('dashboard.chartAvgLine', { days: windowDays.value, n: avgPass }),
                fontSize: 10,
                color: '#9aa6bd',
                fontFamily,
              },
            }
          : undefined,
      },
    ],
  })
}

function onResize() {
  trendChart?.resize()
}

let containerObserver = null

onMounted(() => {
  loadAll()
  window.addEventListener('resize', onResize)
  if (trendEl.value && typeof ResizeObserver !== 'undefined') {
    containerObserver = new ResizeObserver(() => trendChart?.resize())
    containerObserver.observe(trendEl.value)
  }
})
onBeforeUnmount(() => {
  window.removeEventListener('resize', onResize)
  containerObserver?.disconnect()
  containerObserver = null
  trendChart?.dispose()
  trendChart = null
})
watch([windowDays, processFilter], () => loadAll())
watch(locale, () => {
  if (overview.value) renderTrend()
})

/* 锁与在线状态是准实时数据：60s 静默轮询，保持与其他列表页一致 */
usePolling(() => loadAll(true), 60000)
</script>

<style scoped>
.dashboard { display: flex; flex-direction: column; gap: 16px; }
.chart { width: 100%; height: 300px; }
.window-switch { margin-left: 4px; }
</style>

