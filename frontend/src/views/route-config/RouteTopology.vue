<template>
  <div>
    <div class="tab-head">
      <div class="filter-row">
        <el-select v-model="processId" filterable :placeholder="t('common.process')" style="width:300px">
          <el-option v-for="p in processes" :key="p.process_id" :value="p.process_id" :label="p.process_id" />
        </el-select>
        <div class="seq-group">
          <el-switch
            v-model="seqEnabled"
            :disabled="!processId || !canManage"
            @change="onToggleSequence"
          />
          <span class="seq-label">{{ t('configs.strictSequence') }}</span>
          <el-popover placement="bottom" :width="340" trigger="hover">
            <template #reference>
              <el-icon class="seq-help"><QuestionFilled /></el-icon>
            </template>
            <div class="seq-help-body">
              <div><b>{{ t('configs.seqHelpOnTitle') }}</b>{{ t('configs.seqHelpOn') }}</div>
              <div style="margin-top: 6px"><b>{{ t('configs.seqHelpOffTitle') }}</b>{{ t('configs.seqHelpOff') }}</div>
              <div class="muted" style="margin-top: 6px">{{ t('configs.seqHelpNote') }}</div>
            </div>
          </el-popover>
        </div>
      </div>
      <div class="filter-row">
        <el-button size="small" :icon="CircleCheck" :disabled="!processId" @click="onValidate">
          {{ t('configs.validate') }}
        </el-button>

        <el-button size="small" :icon="Plus" :disabled="!processId" @click="addStep">{{ t('configs.addStep') }}</el-button>
        <el-button type="primary" size="small" :loading="saving" :disabled="!processId" @click="save">
          {{ t('configs.saveTopology') }}
        </el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="steps" stripe size="small">
      <el-table-column :label="t('configs.stepOrder')" width="130">
        <template #default="{ row }">
          <el-input-number v-model="row.step_order" :min="1" :max="999" controls-position="right" style="width:100%" />
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.tabStations')" width="200">
        <template #default="{ row }">
          <el-select v-model="row.station_id" filterable style="width:100%">
            <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.dependsOn')" min-width="320">
        <template #default="{ row }">
          <el-select
            v-model="row.depends_on"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            :max-collapse-tags="5"
            style="width:100%"
          >
            <el-option
              v-for="s in steps"
              :key="s.station_id"
              :value="s.station_id"
              :label="s.station_id"
              :disabled="s.station_id === row.station_id"
            />
          </el-select>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.optionalStep')" width="80" align="center">
        <template #header>
          <span :title="t('configs.optionalStepTip')">{{ t('configs.optionalStep') }}</span>
        </template>
        <template #default="{ row }">
          <el-switch v-model="row.is_optional" size="small" />
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.invalidatesDownstream')" width="120" align="center">
        <template #header>
          <span :title="t('configs.invalidatesDownstreamTip')">{{ t('configs.invalidatesDownstream') }}</span>
        </template>
        <template #default="{ row }">
          <el-switch v-model="row.invalidates_downstream" size="small" />
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.baselineCol')" width="130" align="center">
        <template #header>
          <span :title="t('configs.baselineColTip')">{{ t('configs.baselineCol') }}</span>
        </template>
        <template #default="{ row }">
          <el-tag v-if="row.baseline_locked" type="success" size="small" :title="baselineLockedTitle(row)">
            {{ t('configs.baselineLocked') }}
          </el-tag>
          <el-tag
            v-else-if="(row.baseline_manifest || []).length"
            :type="baselineDrifted(row) ? 'warning' : 'info'"
            :effect="baselineDrifted(row) ? 'light' : 'plain'"
            :title="baselineDrifted(row) ? t('configs.baselineDriftTip') : ''"
          >
            {{ t('configs.baselineDraftN', { n: row.baseline_manifest.length }) }}{{ baselineDrifted(row) ? ' *' : '' }}
          </el-tag>
          <span v-else class="muted">—</span>
        </template>
      </el-table-column>
      <el-table-column :label="t('configs.itemCount')" width="90" align="right">
        <template #default="{ row }">{{ itemCountOf(row.station_id) }}</template>
      </el-table-column>
      <el-table-column :label="t('common.action')" width="140" align="center">
        <template #default="{ row, $index }">
          <el-tooltip
            v-if="canManage && !row.baseline_locked"
            :content="(row.baseline_manifest || []).length
              ? t('configs.lockBaseline')
              : t('configs.baselineNoDraftTip')"
            placement="top"
          >
            <span>
              <el-button
                link
                type="primary"
                size="small"
                :icon="Unlock"
                style="font-size: 18px"
                :disabled="!(row.baseline_manifest || []).length"
                @click="openLock(row)"
              />
            </span>
          </el-tooltip>
          <el-tooltip
            v-if="canManage && row.baseline_locked"
            :content="t('configs.unlockBaseline')"
            placement="top"
          >
            <span>
              <el-button
                link
                type="warning"
                size="small"
                :icon="Lock"
                style="font-size: 18px"
                @click="unlockBaseline(row)"
              />
            </span>
          </el-tooltip>
          <el-button v-if="canManage" link type="danger" size="small" @click="removeRow($index)">{{ t('common.delete') }}</el-button>
        </template>
      </el-table-column>
      <template #empty><EmptyState :text="t('common.noData')" /></template>
    </el-table>

    <el-drawer v-model="validateVisible" size="680px" destroy-on-close>
      <div v-if="validateResult">
        <el-alert
          :title="validateMeta.title"
          :type="validateMeta.type"
          :closable="false"
          show-icon
        />
        <el-table v-if="validateResult.issues.length" :data="validateResult.issues" size="small" style="margin-top:12px">
          <el-table-column :label="t('configs.issueLevel')" width="100" align="center">
            <template #default="{ row }">
              <el-tag :type="row.level === 'error' ? 'danger' : 'warning'" size="small">{{ row.level }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="code" :label="t('configs.issueCode')" width="170" show-overflow-tooltip>
            <template #default="{ row }"><span class="code">{{ row.code }}</span></template>
          </el-table-column>
          <el-table-column prop="detail" :label="t('configs.issueDetail')" min-width="180" show-overflow-tooltip />
        </el-table>
        <EmptyState v-else :text="t('configs.validateOk')" />
      </div>
    </el-drawer>

    <!-- 锁定基线确认：分层展示（锁定对象 → 差异 → 锁定后效果） -->
    <el-dialog
      v-model="lockVisible"
      :title="t('configs.baselineLockTitle')"
      width="680px"
      destroy-on-close
      class="form-dialog"
    >
      <!-- ① 锁定对象 -->
      <div class="bl-sec">
        <div class="bl-sec-title">{{ t('configs.baselineTarget') }}</div>
        <div class="bl-target">
          <span class="code">{{ lockRow?.station_id }}</span>
          <span v-if="lockRow?.station_name" class="muted">（{{ lockRow.station_name }}）</span>
          <el-tag size="small" effect="plain" type="info" class="bl-count">
            {{ t('configs.baselineDraftN', { n: (lockRow?.baseline_manifest || []).length }) }}
          </el-tag>
          <el-tag v-if="lockDiff.reordered" type="warning" size="small" effect="plain" class="bl-count">
            {{ t('configs.baselineDiffOrder') }}
          </el-tag>
        </div>
      </div>

      <!-- ② 与现有用例清单的差异 -->
      <div class="bl-sec">
        <div class="bl-sec-title">{{ t('configs.baselineDiffTitle') }}</div>
        <div v-if="diffEmpty" class="muted bl-none">{{ t('configs.baselineDiffNone') }}</div>
        <template v-else>
          <div v-if="lockDiff.added.length" class="bl-diff-row">
            <el-tag type="success" size="small" effect="light">
              {{ t('configs.baselineDiffAdded') }}（{{ lockDiff.added.length }}）
            </el-tag>
            <div class="bl-diff-list">
              <el-tooltip v-for="c in lockDiff.added" :key="c" :content="c" placement="top">
                <CaseIdText :value="c" class="bl-diff-item" />
              </el-tooltip>
            </div>
          </div>
          <div v-if="lockDiff.removed.length" class="bl-diff-row">
            <el-tag type="danger" size="small" effect="light">
              {{ t('configs.baselineDiffRemoved') }}（{{ lockDiff.removed.length }}）
            </el-tag>
            <div class="bl-diff-list">
              <el-tooltip v-for="c in lockDiff.removed" :key="c" :content="c" placement="top">
                <CaseIdText :value="c" class="bl-diff-item bl-diff-del" />
              </el-tooltip>
            </div>
          </div>
        </template>
      </div>

      <!-- ③ 为缺名称的用例补录（新增项 / 名称仍是 nodeid 的项） -->
      <div v-if="nameTargets.length" class="bl-sec">
        <div class="bl-sec-title">{{ t('configs.baselineNamesTitle') }}</div>
        <div v-for="row in nameTargets" :key="row.case_id" class="bl-name-row">
          <el-tooltip :content="row.case_id" placement="top">
            <CaseIdText :value="row.case_id" class="bl-name-code" />
          </el-tooltip>
          <el-input
            v-model="nameEdits[row.case_id]"
            size="small"
            maxlength="128"
            :placeholder="t('configs.baselineNamePlaceholder')"
            style="flex: 1"
          />
        </div>
      </div>

      <!-- ④ 锁定后的效果 -->
      <div class="bl-sec">
        <div class="bl-sec-title">{{ t('configs.baselineEffectTitle') }}</div>
        <ul class="bl-effect">
          <li>{{ t('configs.baselineEffect1') }}</li>
          <li>{{ t('configs.baselineEffect2') }}</li>
        </ul>
        <el-alert :title="t('configs.baselineLockWarn2')" type="warning" :closable="false" style="margin-top: 8px" />
      </div>

      <template #footer>
        <el-button @click="lockVisible = false">{{ t('common.cancel') }}</el-button>
        <el-button type="primary" :icon="Lock" :loading="locking" @click="confirmLock">
          {{ t('configs.lockBaseline') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../../stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { CircleCheck, Lock, Plus, QuestionFilled, Unlock } from '@element-plus/icons-vue'
import { processApi, routingApi } from '../../api'
import EmptyState from '../../components/EmptyState.vue'
import CaseIdText from '../../components/CaseIdText.vue'
import { fmtDateTime } from '../../utils/format'
import { useTabRefresh } from '../../composables/useTabRefresh'
import { useProcesses } from '../../composables/useProcesses'

const { t } = useI18n()
const { canManage } = useAuth()
const { processes, stations, loadProcesses } = useProcesses()

const processId = ref('')
const steps = ref([])
const seqEnabled = ref(true)
const loading = ref(false)
const saving = ref(false)
const savedStations = ref([])

const validateVisible = ref(false)
const validateResult = ref(null)

const validateMeta = computed(() => {
  const r = validateResult.value
  if (!r) return { title: t('configs.validateTitle'), type: 'info' }
  const warns = (r.issues || []).filter((i) => i.level === 'warning').length
  if (!r.ok) return { title: t('configs.validateFail'), type: 'error' }
  if (warns > 0) return { title: `${t('configs.validateWarn')}（${warns}）`, type: 'warning' }
  return { title: t('configs.validateOk'), type: 'success' }
})
const allItems = ref([])

const itemCountMap = computed(() => {
  const map = {}
  for (const item of allItems.value) {
    if (!item.is_active) continue
    map[item.station_id] = (map[item.station_id] || 0) + 1
  }
  return map
})
function itemCountOf(stationId) {
  return itemCountMap.value[stationId] || 0
}

async function loadSteps() {
  if (!processId.value) {
    steps.value = []
    allItems.value = []
    return
  }
  loading.value = true
  try {
    const [stepRes, itemRes] = await Promise.all([
      routingApi.listSteps(processId.value),
      routingApi.listItems(processId.value),
    ])
    steps.value = stepRes.data.map((s) => ({ ...s, depends_on: [...(s.depends_on || [])] }))
    savedStations.value = steps.value.map((s) => s.station_id)
    allItems.value = itemRes.data
  } catch (e) {
    steps.value = []
    savedStations.value = []
    allItems.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

/** 本次保存会从拓扑中移除的工位（新增/改序不算）。 */
function removedStations() {
  const current = new Set(steps.value.map((s) => s.station_id).filter(Boolean))
  return savedStations.value.filter((sid) => sid && !current.has(sid))
}

function addStep() {
  const nextOrder = steps.value.length ? Math.max(...steps.value.map((s) => s.step_order)) + 10 : 10
  steps.value.push({ station_id: '', step_order: nextOrder, depends_on: [], is_optional: false, invalidates_downstream: false })
}

function removeRow(index) {
  steps.value.splice(index, 1)
}

async function save() {
  if (!processId.value) return
  if (steps.value.some((s) => !s.station_id)) return ElMessage.warning(t('errors.requiredField'))

  const dropped = removedStations()
  const droppedItems = allItems.value.filter((i) => dropped.includes(i.station_id)).length
  // 影响面预演（只读、非阻断）：移除工位/在制品受影响的提前提示；预演失败不阻断保存
  try {
    const impact = (
      await routingApi.topologyImpact(
        processId.value,
        steps.value.map((s) => ({
          station_id: s.station_id,
          step_order: s.step_order,
          depends_on: s.depends_on || [],
          is_optional: !!s.is_optional,
          invalidates_downstream: !!s.invalidates_downstream,
        }))
      )
    ).data
    if (impact.affected_count || impact.removed_items || impact.machines_lost.length || dropped.length) {
      const lines = []
      if (dropped.length) {
        lines.push(t('configs.impactDropped', { stations: dropped.join(', '), count: droppedItems }))
      }
      if (impact.affected_count) {
        lines.push(t('configs.impactWip', { count: impact.affected_count, blocked: impact.blocked_count }))
      }
      if (impact.machines_lost.length) {
        lines.push(t('configs.impactMachines', { count: impact.machines_lost.length }))
      }
      try {
        await ElMessageBox.confirm(lines.join('；'), t('common.confirm'), { type: 'warning' })
      } catch {
        return
      }
    }
  } catch {
    // 预演失败（接口异常）不阻断保存
  }

  saving.value = true
  try {
    const res = await routingApi.saveSteps(
      processId.value,
      steps.value.map((s) => ({
        station_id: s.station_id,
        step_order: s.step_order,
        depends_on: s.depends_on || [],
        is_optional: !!s.is_optional,
        invalidates_downstream: !!s.invalidates_downstream,
      }))
    )
    ElMessage.success(t('configs.topologySaved'))
    const residues = res.data?.removed_wip || []
    if (residues.length) {
      const detail = residues
        .map((r) => `${r.station_id}：${r.wip_sns.join('、')}`)
        .join('；')
      ElMessage.warning({
        message: t('configs.wipResidue', { list: detail }),
        duration: 8000,
        showClose: true,
      })
    }
    await loadSteps()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

const lockVisible = ref(false)
const lockRow = ref(null)
const locking = ref(false)

/** 工位当前生效用例的有序清单（按执行序号排序）。 */
function activeCasesOf(stationId) {
  return allItems.value
    .filter((i) => i.station_id === stationId && i.is_active)
    .sort((a, b) => (a.sort_order ?? 1e9) - (b.sort_order ?? 1e9))
    .map((i) => i.case_id)
}

/** 已锁定标签的悬停说明：基线项数与捕获时间（本地时区，走全站统一 fmtDateTime）。 */
function baselineLockedTitle(row) {
  const n = (row.baseline_manifest || []).length
  return t('configs.baselineLockedTip', { n, time: fmtDateTime(row.baseline_captured_at) })
}

/** 草稿（进站捕获序列）与现有用例清单是否不一致：增删或顺序任一变化即为漂移。 */
function baselineDrifted(row) {
  const draft = row.baseline_manifest || []
  const current = activeCasesOf(row.station_id)
  if (draft.length !== current.length) return true
  return draft.some((c, i) => c !== current[i])
}

/** 草稿（进站捕获序列）与现有用例清单的差异预览 */
const lockDiff = computed(() => {
  const row = lockRow.value
  if (!row) return { added: [], removed: [], reordered: false }
  const draft = row.baseline_manifest || []
  const current = activeCasesOf(row.station_id)
  return {
    added: draft.filter((c) => !current.includes(c)),
    removed: current.filter((c) => !draft.includes(c)),
    reordered:
      draft.filter((c) => current.includes(c)).join('|') !==
      current.filter((c) => draft.includes(c)).join('|'),
  }
})

const diffEmpty = computed(
  () => !lockDiff.value.added.length && !lockDiff.value.removed.length && !lockDiff.value.reordered
)

/** 需要补录名称的草稿项：新增项，或现有名称为 nodeid 兜底的项。 */
const nameTargets = computed(() => {
  const row = lockRow.value
  if (!row) return []
  return nameTargetsList(row).map((cid) => ({ case_id: cid }))
})

/** nodeid 末段作为默认名称（tests/x.py::TestA::test_a → test_a）。 */
function shortNameOf(caseId) {
  const parts = String(caseId).split('::')
  return parts[parts.length - 1] || caseId
}

const nameEdits = ref({})

function openLock(row) {
  lockRow.value = { ...row }
  const edits = {}
  for (const t2 of nameTargetsList(row)) edits[t2.case_id] = shortNameOf(t2.case_id)
  nameEdits.value = edits
  lockVisible.value = true
}

/** openLock 时按行数据构建补录目标（不依赖 computed 的刷新时机）。 */
function nameTargetsList(row) {
  const draft = row.baseline_manifest || []
  const nameOf = {}
  for (const it of allItems.value) {
    if (it.station_id === row.station_id && it.is_active) nameOf[it.case_id] = it.item_name
  }
  return draft.filter((cid) => !nameOf[cid] || nameOf[cid] === cid)
}

async function confirmLock() {
  if (!lockRow.value) return
  locking.value = true
  try {
    const names = {}
    for (const [cid, name] of Object.entries(nameEdits.value || {})) {
      const v = (name || '').trim()
      if (v && v !== cid) names[cid] = v
    }
    const res = await routingApi.lockBaseline({
      process_id: processId.value,
      station_id: lockRow.value.station_id,
      names,
    })
    for (const w of res.data?.warnings || []) ElMessage.warning(w)
    ElMessage.success(t('configs.baselineLockedDone'))
    lockVisible.value = false
    await loadSteps()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    locking.value = false
  }
}

async function unlockBaseline(row) {
  try {
    await ElMessageBox.confirm(t('configs.baselineUnlockConfirm'), t('common.confirm'), {
      type: 'warning',
    })
  } catch {
    return
  }
  try {
    await routingApi.unlockBaseline({
      process_id: processId.value,
      station_id: row.station_id,
    })
    ElMessage.success(t('configs.baselineUnlocked'))
    await loadSteps()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onValidate() {
  try {
    const res = await routingApi.validate(processId.value)
    validateResult.value = res.data
    validateVisible.value = true
  } catch (e) {
    ElMessage.error(e.message)
  }
}

/** 从流程列表同步「严格顺序」开关状态（流程级语义，随工艺切换）。 */
function syncSeqFlag() {
  const p = processes.value.find((x) => x.process_id === processId.value)
  seqEnabled.value = p ? p.is_sequential !== false : true
}

async function onToggleSequence(value) {
  // 关闭方向会弱化防呆（仅按依赖判定）：先确认，取消则回弹
  if (!value) {
    try {
      await ElMessageBox.confirm(t('configs.seqConfirmOff'), t('common.confirm'), { type: 'warning' })
    } catch {
      seqEnabled.value = true
      return
    }
  }
  try {
    await processApi.update(processId.value, { is_sequential: !!value })
    ElMessage.success(
      value ? t('configs.strictSequenceOn') : t('configs.strictSequenceOff')
    )
    // 流程/工位是全局共享缓存：落库后必须 force 刷新，否则本页开关会被旧值弹回
    await loadProcesses({ force: true })
    syncSeqFlag()
  } catch (e) {
    ElMessage.error(e.message)
    syncSeqFlag()
  }
}

watch(processId, () => {
  syncSeqFlag()
  loadSteps()
})

useTabRefresh('topology', async () => {
  // 切回本页时同步流程字典（is_sequential 可能在别处被改）再刷新拓扑
  await loadProcesses({ force: true })
  syncSeqFlag()
  await loadSteps()
})

onMounted(async () => {
  await loadProcesses()
  if (!processId.value && processes.value.length) processId.value = processes.value[0].process_id
  syncSeqFlag()
  await loadSteps()
})
</script>

<style scoped>
.seq-group {
  display: flex;
  align-items: center;
  gap: 4px;
  /* 与流程选择框（默认尺寸 32px）等高，避免开关视觉上“矮一截” */
  height: 32px;
}
.seq-label {
  font-size: 14px;
  line-height: 32px;
  color: var(--el-text-color-regular);
  margin-left: 2px;
}
.seq-help {
  color: var(--el-text-color-secondary);
  cursor: help;
  font-size: 18px;
}
.seq-help-body {
  font-size: 12.5px;
  line-height: 1.7;
  color: var(--el-text-color-regular);
}
.bl-sec + .bl-sec {
  margin-top: 14px;
}
.bl-sec-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-primary);
  margin-bottom: 6px;
}
.bl-target {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.bl-count {
  margin-left: 4px;
}
.bl-none {
  margin-top: 4px;
}
.bl-diff-row + .bl-diff-row {
  margin-top: 10px;
}
.bl-diff-list {
  margin-top: 4px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.bl-diff-item {
  font-size: 12px;
  line-height: 1.6;
  min-width: 0;
  max-width: 100%;
}
.bl-name-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 6px;
}
.bl-name-code {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  line-height: 1.5;
}
.bl-diff-del {
  text-decoration: line-through;
  opacity: 0.75;
}
.bl-effect {
  margin: 0;
  padding-left: 18px;
}
.bl-effect li {
  font-size: 13px;
  line-height: 1.9;
  color: var(--el-text-color-regular);
}
</style>

