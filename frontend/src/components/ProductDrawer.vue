<template>
  <el-drawer
    v-model="visible"
    :title="$t('products.drawerTitle')"
    size="680px"
    destroy-on-close
    class="product-drawer"
    @open="onOpen"
  >
    <template v-if="row">
      <!-- 概要 -->
      <div class="summary">
        <el-tag :type="statusTagType(detail?.current_status || row.current_status)" size="small">
          {{ statusLabel }}
        </el-tag>
        <span class="summary-sn code">{{ row.sn }}</span>
        <span class="muted">{{ row.product_model }} · {{ row.process_id || '-' }}</span>
        <el-tag v-if="!row.fw_match" size="small" type="warning" effect="plain">
          {{ $t('products.fwOffBaseline') }}
        </el-tag>
      </div>

      <!-- 工艺进度：工步时间线 -->
      <div class="block">
        <div class="block-title">
          {{ $t('products.sectionProgress') }}
          <span class="muted count">({{ row.passed_count }}/{{ row.total_steps }})</span>
        </div>
        <el-skeleton v-if="loading" :rows="3" animated />
        <div v-else-if="steps.length" class="steps">
          <div v-for="(step, i) in steps" :key="step.station_id" class="step" :class="{ done: step.passed }">
            <span class="step-index">
              <el-icon v-if="step.passed"><Select /></el-icon>
              <template v-else>{{ i + 1 }}</template>
            </span>
            <span class="step-name code">{{ step.station_id }}</span>
            <el-tag v-if="step.last_result" size="small" :type="resultTagType(step.last_result)">
              {{ $t(`result.${step.last_result}`) }}
            </el-tag>
            <el-tag v-else size="small" type="info" effect="plain">{{ $t('products.stepPending') }}</el-tag>
            <span class="muted step-time">{{ step.last_time ? fmtDateTime(step.last_time) : '' }}</span>
          </div>
        </div>
        <EmptyState v-else :text="$t('common.noData')" />
      </div>

      <!-- 基本信息 -->
      <div class="block">
        <div class="block-title">{{ $t('products.sectionBasic') }}</div>
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item :label="$t('products.tableModel')">{{ row.product_model }}</el-descriptions-item>
          <el-descriptions-item :label="$t('products.tableProcess')">{{ row.process_id || '-' }}</el-descriptions-item>
          <el-descriptions-item :label="$t('products.tableFw')">
            <span class="code">{{ row.current_fw_version }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('products.baseline')">
            {{ row.target_fw_version || '-' }}
          </el-descriptions-item>
          <el-descriptions-item :label="$t('products.tableFailCount')">
            <el-tag v-if="row.fail_count" type="danger" size="small">{{ row.fail_count }}</el-tag>
            <span v-else>0</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('products.tableClient')">
            <span class="code">{{ row.current_client || '—' }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('products.tableUpdated')" :span="2">
            {{ fmtDateTime(row.updated_at) }}
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <!-- 租约锁：仅测试中有意义 -->
      <div v-if="row.current_status === 'TESTING'" class="block">
        <div class="block-title">{{ $t('products.tableLock') }}</div>
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item :label="$t('products.tableStatus')">
            <el-tag size="small" :type="row.lock_zombie ? 'danger' : 'success'" :effect="row.lock_zombie ? 'dark' : 'plain'">
              {{ row.lock_zombie ? $t('products.lockZombie') : $t('products.lockHeld') }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('products.lockHeldTip')">
            {{ fmtDurationSec(row.lock_held_sec) }}
          </el-descriptions-item>
          <el-descriptions-item :label="$t('products.lockIdleTip')">
            <span :class="{ 'text-danger': row.lock_zombie }">{{ fmtDurationSec(row.lock_idle_sec) }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('products.lockLeaseTip')" :span="2">
            {{ fmtDurationSec(row.lock_lease_remaining_sec) }}
          </el-descriptions-item>
        </el-descriptions>
        <div v-if="row.lock_zombie" class="reason-box text-danger">{{ $t('products.lockZombieTip') }}</div>
      </div>

      <!-- 最近事件 -->
      <div class="block">
        <div class="block-title">{{ $t('products.sectionEvents') }}</div>
        <el-skeleton v-if="loading" :rows="3" animated />
        <div v-else-if="events.length" class="events">
          <div v-for="r in events" :key="r.record_id" class="event">
            <el-tag size="small" :type="resultTagType(r.overall_result)">{{ $t(`result.${r.overall_result}`) }}</el-tag>
            <span class="code">{{ r.station_id }}</span>
            <span class="muted">{{ fmtDateTime(r.created_at) }}</span>
            <el-tag v-if="!r.is_valid" size="small" type="info" effect="plain">{{ $t('records.invalid') }}</el-tag>
          </div>
        </div>
        <EmptyState v-else :text="$t('products.noEvents')" />
      </div>
    </template>

    <template #footer>
      <div class="drawer-actions">
        <el-button @click="visible = false">{{ $t('common.close') }}</el-button>
        <el-button :icon="View" @click="emit('trace', row)">{{ $t('products.viewTrace') }}</el-button>
        <el-button type="warning" :icon="Tools" @click="emit('repair', row)">
          {{ $t('products.repairAction') }}
        </el-button>
        <el-button
          v-if="row?.current_status === 'TESTING'"
          type="danger"
          plain
          :icon="Unlock"
          @click="emit('force-release', row)"
        >
          {{ $t('products.forceRelease') }}
        </el-button>
      </div>
    </template>
  </el-drawer>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { Select, Tools, Unlock, View } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { recordApi } from '../api'
import EmptyState from './EmptyState.vue'
import { fmtDateTime, fmtDurationSec, resultTagType, statusTagType } from '../utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  row: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'repair', 'force-release', 'trace'])

const { t } = useI18n()

// 最近事件区最多展示条数；超出在容器内滚动，避免撑长抽屉挤动底部按钮
const EVENT_LIMIT = 20

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const loading = ref(false)
const detail = ref(null)
const steps = ref([])
const events = ref([])

const statusLabel = computed(() => {
  const status = detail.value?.current_status || props.row?.current_status
  if (props.row?.is_completed) return t('status.COMPLETED')
  return t(`status.${status}`)
})

async function onOpen() {
  if (!props.row?.sn) return
  loading.value = true
  try {
    const res = await recordApi.trace(props.row.sn)
    detail.value = res.data.product
    steps.value = res.data.steps || []
    // 后端按 record_id 升序返回该 SN 的全部记录：取最新 20 条并倒序，最新在上
    events.value = (res.data.records || []).slice(-EVENT_LIMIT).reverse()
  } catch (e) {
    steps.value = []
    events.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

// 行数据被轮询刷新时同步抽屉内的实时字段（锁状态等）
watch(() => props.row?.sn, () => {
  steps.value = []
  events.value = []
})
</script>

<style scoped>
.summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--app-border);
}
.summary-sn { font-size: 14px; font-weight: 600; }

.block { margin-top: 18px; }
.block-title { font-size: 13px; font-weight: 600; margin-bottom: 8px; }
.block-title .count { font-weight: 400; }

.steps { display: flex; flex-direction: column; gap: 6px; }
.step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  font-size: 12.5px;
}
.step.done { border-color: rgba(18, 183, 106, 0.45); background: rgba(18, 183, 106, 0.05); }
.step-index {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  color: #fff;
  background: #c3cfe6;
}
.step.done .step-index { background: var(--app-success, #12b76a); }
.step-name { min-width: 96px; }
.step-time { margin-left: auto; font-size: 11.5px; }

/* 独立滚动容器：事件再多也只在这一块内滚动，底部操作按钮位置恒定 */
.events {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 232px;
  overflow-y: auto;
  padding: 8px 10px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  overscroll-behavior: contain;
}
.event { display: flex; align-items: center; gap: 8px; font-size: 12.5px; flex-shrink: 0; }

.reason-box {
  margin-top: 8px;
  background: rgba(245, 108, 108, 0.06);
  border: 1px solid rgba(245, 108, 108, 0.3);
  border-radius: 8px;
  padding: 8px 12px;
  font-size: 12.5px;
  line-height: 1.6;
}
.drawer-actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
</style>
