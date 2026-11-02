<template>
  <el-drawer v-model="visible" size="680px" destroy-on-close @opened="load">
    <template #header>
      <div class="d-head">
        <span class="code d-id">{{ process?.process_id }}</span>
        <el-tag :type="process?.is_active !== false ? 'success' : 'info'" size="small" effect="plain">
          {{ process?.is_active !== false ? 'ON' : 'OFF' }}
        </el-tag>
      </div>
    </template>

    <div v-loading="loading">
      <!-- ① 概要：名称 + 属性清单（label: value 对齐） -->
      <div class="d-name">{{ process?.process_name || '—' }}</div>

      <div class="meta">
        <div class="meta-row">
          <span class="meta-label">{{ t('configs.models') }}</span>
          <span class="meta-value">
            <template v-if="process?.models?.length">
              <el-tag v-for="m in process.models" :key="m" size="small" effect="plain" type="warning">{{ m }}</el-tag>
            </template>
            <span v-else class="muted">—</span>
          </span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ t('configs.scale') }}</span>
          <span class="meta-value">{{ summary }}</span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ t('configs.strictSequence') }}</span>
          <span class="meta-value">
            <el-tag :type="sequential ? 'success' : 'info'" size="small" effect="plain">
              {{ sequential ? t('configs.seqStrict') : t('configs.seqDepsOnly') }}
            </el-tag>
            <span class="muted">{{ sequential ? t('configs.seqStrictTip') : t('configs.seqDepsOnlyTip') }}</span>
          </span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ t('configs.baselineCol') }}</span>
          <span class="meta-value">
            <el-tag v-if="lockedCount" type="success" size="small" effect="plain">
              {{ t('configs.baselineLockedStations', { n: lockedCount }) }}
            </el-tag>
            <span v-else class="muted">{{ t('configs.baselineNone') }}</span>
          </span>
        </div>
        <div class="meta-row">
          <span class="meta-label">{{ t('configs.topology') }}</span>
          <span class="meta-value">
            <el-skeleton v-if="loading" :rows="1" animated style="width: 140px" />
            <template v-else-if="validate">
              <el-tag v-if="!validate.ok" type="danger" size="small" effect="plain">✗ {{ t('configs.topoBad') }}</el-tag>
              <el-tooltip v-else-if="warnCount > 0" :content="issueText" placement="right">
                <el-tag type="warning" size="small" effect="plain">⚠ {{ t('configs.topoOk') }}（{{ warnCount }} {{ t('configs.warnUnit') }}）</el-tag>
              </el-tooltip>
              <el-tag v-else type="success" size="small" effect="plain">✓ {{ t('configs.topoOk') }}</el-tag>
            </template>
            <span v-else class="muted">—</span>
          </span>
        </div>
      </div>

      <!-- ② 工步卡片：每站一张卡，卡内 = 头（序号/工位/计数）+ 属性行 + 用例表 -->
      <div class="sec-title">{{ t('configs.stepsSection') }}</div>

      <div v-for="(s, idx) in steps" :key="s.station_id" class="st-card">
        <div class="st-head">
          <span class="st-order">{{ idx + 1 }}</span>
          <span class="code st-id">{{ s.station_id }}</span>
          <span class="st-name">{{ s.station_name }}</span>
          <el-tag v-if="s.is_optional" size="small" type="info" effect="plain">{{ t('configs.optionalStep') }}</el-tag>
          <el-tooltip
            v-if="s.baseline_locked"
            :content="lockedTitle(s)"
            placement="top"
            :show-after="400"
          >
            <el-tag size="small" type="success" effect="plain">{{ t('configs.baselineLocked') }}</el-tag>
          </el-tooltip>
          <el-tooltip
            v-else-if="draftCount(s)"
            :content="t('configs.baselineDriftTip')"
            placement="top"
            :show-after="400"
            :disabled="!drifted(s)"
          >
            <el-tag size="small" type="info" effect="plain">
              {{ t('configs.baselineDraftN', { n: draftCount(s) }) }}
            </el-tag>
          </el-tooltip>
          <span class="st-count">
            <el-tooltip
              v-for="cs in stationSets(s.station_id)"
              :key="cs.set_id"
              :content="cs.set_name ? `${cs.set_name}（${cs.set_id}）` : cs.set_id"
              placement="top"
              :show-after="400"
              :disabled="!cs.set_name || cs.set_name === cs.set_id"
            >
              <span class="st-set">{{ cs.set_id }} ×{{ cs.count }}</span>
            </el-tooltip>
            <span v-if="inactiveCount(s.station_id)" class="st-off">
              {{ t('configs.casesInactiveN', { n: inactiveCount(s.station_id) }) }}
            </span>
          </span>
        </div>
        <div class="st-meta">
          <span class="muted">{{ t('configs.dependsOn') }}:</span>
          <template v-if="s.depends_on?.length">
            <el-tag v-for="d in s.depends_on" :key="d" size="small" effect="plain">{{ d }}</el-tag>
          </template>
          <span v-else class="muted">{{ t('configs.noDeps') }}</span>
          <el-tag
            v-if="s.invalidates_downstream"
            size="small"
            type="warning"
            effect="plain"
            :title="t('configs.invalidatesDownstreamTip')"
          >{{ t('configs.invalidatesDownstream') }}</el-tag>
          <span class="muted st-timeout">{{ t('configs.stationTimeout') }}: {{ timeoutMin(s) }}</span>
        </div>

        <el-table :data="stationItems[s.station_id] || []" size="small" border>
          <el-table-column :label="t('configs.caseSetCol')" width="90">
            <template #default="{ row: it }">
              <el-tooltip
                :content="setNameOf(s.station_id, it.set_id) || it.set_id"
                placement="top"
                :show-after="400"
                :disabled="!setNameOf(s.station_id, it.set_id) || setNameOf(s.station_id, it.set_id) === it.set_id"
              >
                <el-tag size="small" effect="plain" type="info">{{ it.set_id }}</el-tag>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column :label="t('configs.caseId')" min-width="200">
            <template #default="{ row: it }">
              <el-tooltip :content="it.case_id" placement="top" :show-after="400">
                <span class="code case-ellipsis">{{ it.case_id }}</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column :label="t('configs.itemName')" min-width="90" show-overflow-tooltip>
            <template #default="{ row: it }">{{ it.item_name || '—' }}</template>
          </el-table-column>
          <el-table-column :label="t('configs.itemRule')" width="80" align="center">
            <template #default="{ row: it }">
              <el-tag size="small" :type="it.is_mandatory === false ? 'info' : 'warning'" effect="plain">
                {{ it.is_mandatory === false ? t('configs.optional') : t('configs.mandatory') }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="t('common.status')" width="80" align="center">
            <template #default="{ row: it }">
              <el-tag size="small" :type="it.is_active === false ? 'info' : 'success'" effect="plain">
                {{ it.is_active === false ? t('common.disabled') : t('common.enabled') }}
              </el-tag>
            </template>
          </el-table-column>
          <template #empty><span class="muted">{{ t('configs.noCases') }}</span></template>
        </el-table>
      </div>

      <div v-if="!loading && !steps.length" class="muted" style="text-align: center; padding: 24px 0">
        {{ t('common.noData') }}
      </div>
    </div>

    <template #footer>
      <el-button :icon="Close" @click="visible = false">{{ t('common.close') }}</el-button>
      <el-button :icon="Download" @click="exportJson" :loading="exporting">{{ t('configs.exportJson') }}</el-button>
      <el-button type="primary" :icon="Edit" @click="$emit('edit', process)">{{ t('common.edit') }}</el-button>
    </template>
  </el-drawer>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { Close, Download, Edit } from '@element-plus/icons-vue'
import { routingApi } from '../api'
import { fmtDateTime } from '../utils/format'

const props = defineProps({
  visible: { type: Boolean, default: false },
  process: { type: Object, default: null },
})
const emit = defineEmits(['update:visible', 'edit'])

const { t } = useI18n()
const visible = computed({
  get: () => props.visible,
  set: (v) => emit('update:visible', v),
})

const loading = ref(false)
const steps = ref([])
const items = ref([])
const caseSets = ref([])
const validate = ref(null)

/** 工位 → 该工位用例行（含停用行：表格里用「状态」列区分） */
const stationItems = computed(() => {
  const map = {}
  for (const it of items.value) {
    ;(map[it.station_id] ||= []).push(it)
  }
  // 同工位内按集合 + 执行序号排序，多套集合各自成段
  for (const list of Object.values(map)) {
    list.sort((a, b) =>
      a.set_id === b.set_id
        ? (a.sort_order ?? 1e9) - (b.sort_order ?? 1e9)
        : (a.set_id < b.set_id ? -1 : 1)
    )
  }
  return map
})

/** 用例集合主数据（含空集合）+ 用例里出现但未建档的集合兜底 */
const declaredSets = computed(() => {
  const map = new Map()
  for (const cs of caseSets.value) {
    map.set(`${cs.station_id}|${cs.set_id}`, {
      station_id: cs.station_id,
      set_id: cs.set_id,
      set_name: cs.set_name || '',
    })
  }
  for (const it of items.value) {
    const key = `${it.station_id}|${it.set_id}`
    if (!map.has(key)) map.set(key, { station_id: it.station_id, set_id: it.set_id, set_name: '' })
  }
  return [...map.values()]
})

const setCount = computed(() => declaredSets.value.length)

const setNameMap = computed(
  () => new Map(caseSets.value.map((c) => [`${c.station_id}|${c.set_id}`, c.set_name || '']))
)
function setNameOf(stationId, setId) {
  return setNameMap.value.get(`${stationId}|${setId}`) || ''
}

/** 该工位的各套集合（含 0 项的集合）与启用项数，按字段码排序 */
function stationSets(stationId) {
  return declaredSets.value
    .filter((s) => s.station_id === stationId)
    .sort((a, b) => (a.set_id < b.set_id ? -1 : 1))
    .map((s) => ({
      ...s,
      set_name: s.set_name || setNameOf(stationId, s.set_id),
      count: (stationItems.value[stationId] || []).filter(
        (i) => i.set_id === s.set_id && i.is_active !== false
      ).length,
    }))
}

/** 该工位停用项数（不计入规模统计，单独提示） */
function inactiveCount(stationId) {
  return (stationItems.value[stationId] || []).filter((i) => i.is_active === false).length
}

/** 一套集合的启用用例有序清单（基线漂移比对用） */
function setCaseIds(stationId, setId) {
  return (stationItems.value[stationId] || [])
    .filter((i) => i.set_id === setId && i.is_active !== false)
    .sort((a, b) => (a.sort_order ?? 1e9) - (b.sort_order ?? 1e9))
    .map((i) => i.case_id)
}

/** 草稿项数（录制态进站捕获序列） */
function draftCount(step) {
  return (step.baseline_manifest || []).length
}

/** 草稿与任何一套集合都不一致即视为漂移 */
function drifted(step) {
  const draft = step.baseline_manifest || []
  if (!draft.length) return false
  const sets = stationSets(step.station_id)
  if (!sets.length) return true
  return !sets.some((cs) => {
    const ids = setCaseIds(step.station_id, cs.set_id)
    return ids.length === draft.length && ids.every((c, i) => c === draft[i])
  })
}

/** 已锁定标签的悬停说明：集合套数、启用项数与捕获时间 */
function lockedTitle(step) {
  const sets = stationSets(step.station_id)
  return t('configs.baselineLockedTip', {
    n: sets.length,
    m: sets.reduce((k, cs) => k + cs.count, 0),
    time: fmtDateTime(step.baseline_captured_at),
  })
}

/** 流程级策略：顺序防呆（is_sequential 默认开） */
const sequential = computed(() => props.process?.is_sequential !== false)
const lockedCount = computed(() => steps.value.filter((s) => s.baseline_locked).length)

/** 规模只统计启用项：停用行单独在工步卡片上提示 */
const summary = computed(() => {
  const active = items.value.filter((i) => i.is_active !== false)
  const mand = active.filter((i) => i.is_mandatory !== false).length
  return [
    t('configs.stationCount', { n: steps.value.length }),
    t('configs.caseCount', { n: active.length, m: mand }),
    t('configs.caseSetN', { n: setCount.value }),
  ].join(' · ')
})

const warnCount = computed(() =>
  (validate.value?.issues || []).filter((i) => i.level === 'warning').length,
)

const issueText = computed(() =>
  (validate.value?.issues || []).map((i) => i.detail || i.code).join('；'),
)

const exporting = ref(false)
async function exportJson() {
  if (!props.process?.process_id) return
  exporting.value = true
  try {
    const res = await routingApi.exportProcess(props.process.process_id)
    const blob = new Blob([res.data], { type: 'application/json' })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(blob)
    a.download = `${props.process.process_id}.json`
    a.click()
    URL.revokeObjectURL(a.href)
  } finally {
    exporting.value = false
  }
}

function timeoutMin(s) {
  const sec = s.timeout_sec || 0
  return sec % 60 === 0 ? `${Math.round(sec / 60)} min` : `${sec} s`
}

async function load() {
  if (!props.process?.process_id) return
  loading.value = true
  try {
    const [topo, val, sets] = await Promise.all([
      routingApi.topology(props.process.process_id),
      routingApi.validate(props.process.process_id).catch(() => null), // 校验失败不阻塞查看
      routingApi.listCaseSets(props.process.process_id).catch(() => null), // 集合名称（失败只影响提示）
    ])
    steps.value = topo.data.steps || []
    items.value = topo.data.items || []
    caseSets.value = sets ? sets.data || [] : []
    validate.value = val ? val.data : null
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
/* 头部：编号 + 状态 */
.d-head { display: flex; align-items: center; gap: 10px; }
.d-id { font-size: 15px; font-weight: 600; }

/* ① 概要 */
.d-name {
  font-size: 14px; font-weight: 500;
  color: var(--app-text, #303133);
  margin-bottom: 14px;
}
/* 概要区：两列网格（标题列宽随最长标题自适应，值的起始位置统一对齐） */
.meta {
  display: grid;
  grid-template-columns: max-content minmax(0, 1fr);
  column-gap: 14px; row-gap: 10px;
  padding: 12px 14px;
  border: 1px solid var(--app-border, #eef1f7); border-radius: 8px;
}
.meta-row { display: contents; }
.meta-label {
  white-space: nowrap;
  color: var(--app-text-muted, #8a94a6); font-size: 12.5px;
}
.meta-value { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; min-width: 0; }

/* ② 工步卡片 */
.sec-title {
  margin: 18px 0 10px; color: var(--app-text-muted, #8a94a6);
  font-size: 12.5px; letter-spacing: 1px;
}
.st-card {
  border: 1px solid var(--app-border, #eef1f7); border-radius: 8px;
  padding: 12px 14px; margin-bottom: 10px;
}
.st-head {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  margin-bottom: 8px; min-width: 0;
}
.st-order {
  flex: none; width: 20px; height: 20px; border-radius: 50%;
  background: var(--app-primary, #2f6bff); color: #fff;
  display: inline-flex; align-items: center; justify-content: center;
  font-size: 11.5px; font-weight: 600;
}
.st-id { font-weight: 600; }
.st-name {
  color: var(--app-text-muted, #8a94a6); font-size: 12.5px;
  min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.st-count {
  margin-left: auto; flex: none; display: flex; align-items: center; gap: 6px;
  color: var(--app-text-muted, #8a94a6); font-size: 12.5px;
}
/* 每套集合的「字段码 ×项数」小标（空集合也会显示 ×0） */
.st-set {
  padding: 0 6px; border-radius: 4px;
  border: 1px solid var(--app-border, #eef1f7);
  background: var(--el-fill-color-lighter, #fafafa);
  font-family: var(--app-font-mono, monospace);
  line-height: 18px;
}
.st-off { color: var(--app-text-muted, #8a94a6); }
.st-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; font-size: 12.5px; }
.st-timeout { margin-left: auto; }
.case-ellipsis {
  display: block; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
</style>

