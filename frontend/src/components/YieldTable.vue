<template>
  <div class="yield-wrap">
    <div v-if="hasData" class="yield-tools">
      <el-input
        v-model="keyword"
        clearable
        size="small"
        :placeholder="searchPh || $t('common.search')"
        class="tool-search"
      />
      <el-select v-model="sortBy" size="small" class="tool-sort">
        <el-option value="rate" :label="$t('dashboard.sortByRate')" />
        <el-option value="total" :label="$t('dashboard.sortByTotal')" />
      </el-select>
      <el-checkbox v-model="alertOnly" size="small" class="tool-alert">
        {{ $t('dashboard.alertOnly') }}
      </el-checkbox>
    </div>

    <div v-if="shownRows.length" class="yield-head" :style="gridStyle">
      <span class="th-name">{{ columnLabel }}</span>
      <span class="th-rate" :title="$t('dashboard.colRateTip')">{{ $t('dashboard.colRate') }}</span>
      <span v-if="showFirstPass" class="th-fpy" :title="$t('dashboard.colFpyTip')">{{ firstPassLabel || $t('dashboard.colFirstPass') }}</span>
      <span v-if="showFirstPass" class="th-fpy" :title="$t('dashboard.colFinalPassTip')">{{ $t('dashboard.colFinalPass') }}</span>
    </div>

    <div class="yield-scroll">
      <div
        v-for="row in shownRows"
        :key="row.key"
        class="yield-row"
        :class="rowClass(row.pass_rate)"
        :style="gridStyle"
      >
        <span class="yield-key" :title="row.key">{{ row.key }}</span>
        <span
          class="yield-rate"
          :class="rateClass(row.pass_rate)"
          :title="`${$t('dashboard.colRateTip')}：${row.passed} / ${row.total}`"
        >{{ row.pass_rate }}%</span>
        <span
          v-if="showFirstPass"
          class="yield-fpy"
          :title="`${$t('dashboard.colFpyTip')}：${row.first_pass} / ${row.fpy_total || row.total}`"
        >{{ row.first_pass_rate }}%</span>
        <span
          v-if="showFirstPass"
          class="yield-fpy"
          :title="`${$t('dashboard.colFinalPassTip')}：${row.final_pass} / ${row.fpy_total || row.total}`"
        >{{ row.final_pass_rate }}%</span>
      </div>
      <EmptyState v-if="!shownRows.length" :text="emptyText" />
    </div>

    <div v-if="hasData" class="yield-foot">
      <span class="foot-count muted">
        {{ $t('dashboard.showingCount', { n: shownRows.length, total: rows.length }) }}
      </span>
      <el-button
        v-if="filtered.length > PREVIEW_SIZE"
        link
        type="primary"
        size="small"
        @click="expanded = !expanded"
      >
        {{ expanded ? $t('dashboard.collapse') : $t('dashboard.showAll') }}
      </el-button>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import EmptyState from './EmptyState.vue'

const props = defineProps({
  rows: { type: Array, default: () => [] },
  emptyText: { type: String, default: '' },
  columnLabel: { type: String, default: '' },
  searchPh: { type: String, default: '' },
  /** 启用直通率（FPY）列；同时主良率列切换为件级"最终通过"口径（有过任一 PASS 即算过） */
  showFirstPass: { type: Boolean, default: false },
  firstPassLabel: { type: String, default: '' },
})

/** 概览页默认只渲染前 N 项，避免上千项一次性进 DOM */
const PREVIEW_SIZE = 10

const keyword = ref('')
const sortBy = ref('rate')
const alertOnly = ref(false)
const expanded = ref(false)

const hasData = computed(() => (props.rows?.length || 0) > 0)

/** 多两列（直通率 + 通过率）时压缩列宽，保持整卡宽度不变 */
const gridStyle = computed(() =>
  props.showFirstPass
    ? { gridTemplateColumns: '1fr 72px 78px 78px' }
    : { gridTemplateColumns: '1fr 84px' }
)

/** 过滤：关键词 + 仅看异常（<95% 视为需关注） */
const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return (props.rows || []).filter((r) => {
    if (kw && !String(r.key).toLowerCase().includes(kw)) return false
    if (alertOnly.value && Number(r.pass_rate) >= 95) return false
    return true
  })
})

/** 排序：良率升序（最差在前，概览页默认）；或产量降序（量大的在前） */
const sorted = computed(() => {
  const list = [...filtered.value]
  if (sortBy.value === 'total') {
    list.sort((a, b) => (Number(b.total) || 0) - (Number(a.total) || 0))
  } else {
    list.sort((a, b) => (Number(a.pass_rate) || 0) - (Number(b.pass_rate) || 0))
  }
  return list
})

/** 展示：搜索时给出全量匹配结果，否则只预览前 N 项 */
const shownRows = computed(() => {
  if (expanded.value || keyword.value.trim()) return sorted.value
  return sorted.value.slice(0, PREVIEW_SIZE)
})

/** 数据整批刷新（切换时间窗/流程）后回到预览态，避免残留上一次的展开 */
watch(
  () => props.rows,
  () => {
    expanded.value = false
  }
)

/** 主指标：达标品牌蓝、接近阈值浅蓝、低于 80% 红 */
function rateClass(rate) {
  const v = Number(rate) || 0
  if (v >= 95) return 'good'
  if (v >= 80) return 'warn'
  return 'bad'
}

/** 行底色：<80% 弱红、<95% 弱蓝；>=95% 透明 */
function rowClass(rate) {
  const v = Number(rate) || 0
  if (v < 80) return 'row-bad'
  if (v < 95) return 'row-warn'
  return ''
}
</script>

<style scoped>
.yield-wrap {
  display: flex;
  flex-direction: column;
}

/* 检索条 */
.yield-tools {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  flex-wrap: wrap;
}
.tool-search { flex: 1 1 auto; max-width: 170px; }
.tool-sort { width: 108px; flex-shrink: 0; }
.tool-alert { margin-right: 0; }

.yield-head,
.yield-row {
  display: grid;
  grid-template-columns: 1fr 72px 84px;
  align-items: center;
  gap: 12px;
  padding: 0 8px;
}
.yield-head {
  height: 30px;
  font-size: 11px;
  color: var(--app-text-faint);
  letter-spacing: 0.6px;
  text-transform: uppercase;
  border-bottom: 1px solid var(--app-border);
}
.th-rate,
.th-fpy { text-align: center; }

/* 滚动区：卡片高度恒定 */
.yield-scroll {
  max-height: 320px;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.yield-row {
  height: 40px;
  border-bottom: 1px dashed var(--app-border);
  border-radius: 6px;
  transition: background 0.15s ease;
}
.yield-row:hover { background: #f7f9ff; }
.yield-row.row-bad { background: rgba(245, 108, 108, 0.06); }
.yield-row.row-warn { background: rgba(47, 107, 255, 0.04); }

.yield-key {
  font-size: 13px;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}
/* 良率：与项名（13px）同一量级，只靠字重与颜色区分主次，避免"大号数字"抢走整页重心 */
.yield-rate {
  text-align: center;
  font-size: 14px;
  font-weight: 650;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.2px;
}
.yield-fpy {
  text-align: center;
  font-size: 12.5px;
  font-weight: 500;
  color: var(--app-text-faint);
  font-variant-numeric: tabular-nums;
}
.yield-rate.good { color: #2f6bff; }
.yield-rate.warn { color: #8aa8ff; }
.yield-rate.bad { color: #ef4444; }

/* 底部计数 / 展开 */
.yield-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid var(--app-border);
}
.foot-count { font-size: 11.5px; font-variant-numeric: tabular-nums; }
</style>

