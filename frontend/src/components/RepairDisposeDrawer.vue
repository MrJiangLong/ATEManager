<template>
  <el-drawer
    :model-value="modelValue"
    :title="t('repairs.singleDispose')"
    size="680px"
    class="form-dialog"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <el-form :model="form" label-position="top" @submit.prevent="submit">
      <el-form-item :label="t('common.sn')" required>
        <el-input
          v-model="form.sn"
          :placeholder="t('products.repairSnPh')"
          clearable
          :disabled="Boolean(presetSn)"
          @blur="lookupSn()"
          @keyup.enter="lookupSn()"
          @clear="clearSnInfo"
        />
        <div v-if="snInfoLoading" class="sn-hint muted">{{ t('repairs.snLooking') }}</div>
        <div v-else-if="snInfoError === 'not_found'" class="sn-hint text-danger">{{ t('repairs.snNotFound') }}</div>
        <div v-else-if="snInfo" class="sn-summary">
          <el-tag :type="statusTagType(snStatusKey)" size="small">{{ t(`status.${snStatusKey}`) }}</el-tag>
          <span class="muted">{{ t('products.tableFailCount') }} {{ snInfo.product.fail_count }}</span>
          <span class="muted">{{ passedStations.map((s) => stationName(s) || s).join(' → ') || t('products.stepPending') }}</span>
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
          <el-option v-for="s in passedStations" :key="s" :value="s" :label="stationLabel(s)" />
        </el-select>
      </el-form-item>
      <el-form-item :label="t('products.repairReason')" required>
        <el-input v-model="form.reason" type="textarea" :rows="3" :placeholder="t('products.repairReasonPh')" />
      </el-form-item>
    </el-form>
    <template #footer>
      <div class="drawer-footer">
        <el-button :icon="Close" @click="emit('update:modelValue', false)">{{ t('common.close') }}</el-button>
        <el-button
          type="primary"
          :icon="Check"
          :loading="saving"
          :disabled="snInfoError === 'not_found' || (needsTarget && !passedStations.length)"
          @click="submit"
        >
          {{ t('repairs.execute') }}
        </el-button>
      </div>
    </template>
  </el-drawer>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Check, Close } from '@element-plus/icons-vue'
import { recordApi, repairApi } from '../api'
import { REPAIR_ACTIONS, REPAIR_ACTIONS_WITH_TARGET } from '../utils/constants'
import { statusTagType } from '../utils/format'
import { useDictionaries } from '../composables/useDictionaries'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  /** 预填 SN（在制品入口传入后锁定，进站反查自动执行） */
  presetSn: { type: String, default: '' },
})
const emit = defineEmits(['update:modelValue', 'saved'])

const { t } = useI18n()
const { stationName, ensureLoaded } = useDictionaries()
const saving = ref(false)

/** 工位显示：代码（字典名称）；无名称时代码兜底 */
function stationLabel(s) {
  const n = stationName(s)
  return n ? `${s}（${n}）` : s
}
const form = reactive({ sn: '', repair_action: 'RETEST', target_station: '', reason: '' })

const needsTarget = computed(() => REPAIR_ACTIONS_WITH_TARGET.includes(form.repair_action))

// ---- SN 反查回显：提交前让维修员看到该件的当前状态与可回退工位 ----
const snInfo = ref(null)
const snInfoLoading = ref(false)
const snInfoError = ref('')

const snStatusKey = computed(() => snStatusKeyOf(snInfo.value?.product))
const passedStations = computed(() => passedPathOf(snInfo.value || {}))

function snStatusKeyOf(product) {
  if (!product) return 'IDLE'
  return product.is_completed ? 'COMPLETED' : product.current_status
}
function passedPathOf(trace) {
  return (trace.steps || []).filter((s) => s.passed).map((s) => s.station_id)
}

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

watch(
  () => props.modelValue,
  (open) => {
    if (!open) return
    ensureLoaded()
    form.sn = props.presetSn
    form.repair_action = 'RETEST'
    form.target_station = ''
    form.reason = ''
    clearSnInfo()
    if (props.presetSn) lookupSn(true)
  }
)

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
      t(CONFIRM_KEY[form.repair_action], {
        sn: form.sn.trim(),
        station: form.target_station ? stationLabel(form.target_station) : '—',
      }),
      t('common.confirm'),
      { type: form.repair_action === 'SCRAP' ? 'error' : 'warning' },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    await postDisposition({ confirm_downstream: false })
    ElMessage.success(t('repairs.created'))
    emit('update:modelValue', false)
    emit('saved')
  } catch (e) {
    await handleDownstreamRequired(e)
  } finally {
    saving.value = false
  }
}

/** 依赖重测：服务端要求确认一并清除依赖本工位数据的下游工位（409 downstream_invalidation_required） */
async function handleDownstreamRequired(e) {
  if (e?.code !== 'downstream_invalidation_required') {
    ElMessage.error(e.message)
    return
  }
  const stations = (e?.data?.stations || []).map((s) => stationLabel(s)).join('、')
  try {
    await ElMessageBox.confirm(
      t('repairs.downstreamMsg', { stations }),
      t('repairs.downstreamTitle'),
      { type: 'warning', confirmButtonText: t('repairs.downstreamConfirm') },
    )
  } catch {
    return
  }
  saving.value = true
  try {
    await postDisposition({ confirm_downstream: true })
    ElMessage.success(t('repairs.created'))
    emit('update:modelValue', false)
    emit('saved')
  } catch (err) {
    ElMessage.error(err.message)
  } finally {
    saving.value = false
  }
}

async function postDisposition(extra) {
  await repairApi.create({
    sn: form.sn.trim(),
    repair_action: form.repair_action,
    target_station: needsTarget.value ? form.target_station : undefined,
    reason: form.reason.trim(),
    ...extra,
  })
}
</script>

<style scoped>
.action-group { display: flex; flex-wrap: wrap; }
.sn-hint { margin-top: 4px; font-size: 12px; }
.sn-summary { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 4px; }
.text-danger { color: var(--el-color-danger); }
</style>
