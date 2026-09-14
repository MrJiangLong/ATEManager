<template>
  <div class="repairs fade-up">
    <PageToolbar :title="$t('repairs.title')" :subtitle="$t('repairs.subtitle')" />

    <DataCard :title="$t('repairs.quickTitle')">
      <el-form :model="form" label-position="top" class="quick-form" @submit.prevent="submit">
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
        <el-button
          type="primary"
          :loading="saving"
          :disabled="snInfoError === 'not_found' || (needsTarget && !passedStations.length)"
          @click="submit"
        >
          {{ t('common.confirm') }}
        </el-button>
      </el-form>
    </DataCard>

    <DataCard :title="t('menu.repairs')">
      <template #extra>
        <el-input v-model="snFilter" clearable :placeholder="t('common.sn')" style="width:180px" @keyup.enter="search" />
        <el-select v-model="actionFilter" clearable :placeholder="t('repairs.tableAction')" style="width:160px">
          <el-option v-for="a in REPAIR_ACTIONS" :key="a" :value="a" :label="t(`repair.${a}`)" />
        </el-select>
        <el-button :icon="Refresh" @click="search">{{ t('common.refresh') }}</el-button>
      </template>

      <el-table v-loading="loading" :data="items" stripe size="small">
        <el-table-column prop="repair_id" :label="t('repairs.tableId')" width="80" />
        <el-table-column prop="sn" :label="t('repairs.tableSn')" width="150">
          <template #default="{ row }"><span class="code">{{ row.sn }}</span></template>
        </el-table-column>
        <el-table-column :label="t('repairs.tableAction')" width="130" align="center">
          <template #default="{ row }">
            <el-tag :type="repairTagType(row.repair_action)" size="small">{{ t(`repair.${row.repair_action}`) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="target_station" :label="t('repairs.tableTarget')" width="140" />
        <el-table-column prop="reason" :label="t('repairs.tableReason')" min-width="220" show-overflow-tooltip />
        <el-table-column prop="technician_id" :label="t('repairs.tableTech')" width="120" />
        <el-table-column :label="t('repairs.tableTime')" width="160">
          <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.created_at) }}</span></template>
        </el-table-column>
        <template #empty><EmptyState :text="t('common.noData')" /></template>
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import { recordApi, repairApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import { REPAIR_ACTIONS, REPAIR_ACTIONS_WITH_TARGET } from '../utils/constants'
import { fmtDateTime, repairTagType, statusTagType } from '../utils/format'

const { t } = useI18n()

const items = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const snFilter = ref('')
const actionFilter = ref('')
const saving = ref(false)
const form = reactive({ sn: '', repair_action: 'RETEST', target_station: '', reason: '' })

const needsTarget = computed(() => REPAIR_ACTIONS_WITH_TARGET.includes(form.repair_action))

// ---- SN 反查回显：提交前让维修员看到该件的当前状态与可回退工位 ----
const snInfo = ref(null) // trace 接口返回 { product, steps, records, repairs }
const snInfoLoading = ref(false)
const snInfoError = ref('') // '' | 'not_found'

const snStatusKey = computed(() => {
  const product = snInfo.value?.product
  if (!product) return 'IDLE'
  return product.is_completed ? 'COMPLETED' : product.current_status
})
/** 已盖章工位，按工步顺序排列（RETEST/ROLLBACK 的合法目标） */
const passedStations = computed(() => {
  const steps = snInfo.value?.steps || []
  return steps.filter((s) => s.passed).map((s) => s.station_id)
})

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
    if (form.sn.trim() !== sn) return // 输入已变，丢弃过期结果
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

/** 回显变化后修正目标工位：当前选中值不在已盖章清单里就重置为第一个 */
watch([needsTarget, passedStations], () => {
  if (needsTarget.value && !passedStations.value.includes(form.target_station)) {
    form.target_station = passedStations.value[0] || ''
  }
})

async function load() {
  loading.value = true
  try {
    const params = { page: page.value, page_size: pageSize.value }
    if (snFilter.value) params.sn = snFilter.value
    if (actionFilter.value) params.repair_action = actionFilter.value
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

async function submit() {
  if (!form.sn.trim()) return ElMessage.warning(t('repairs.snRequired'))
  // 用户可能没让输入框失焦就直接点提交：先补一次反查
  if (!snInfo.value && !snInfoError.value) await lookupSn(true)
  if (snInfoError.value === 'not_found') return ElMessage.warning(t('repairs.snNotFound'))
  if (!form.reason.trim()) return ElMessage.warning(t('repairs.reasonRequired'))
  if (needsTarget.value && !form.target_station) return ElMessage.warning(t('products.targetRequired'))
  saving.value = true
  try {
    await repairApi.create({
      sn: form.sn.trim(),
      repair_action: form.repair_action,
      target_station: needsTarget.value ? form.target_station : undefined,
      reason: form.reason.trim(),
    })
    ElMessage.success(t('repairs.created'))
    form.reason = ''
    await Promise.all([load(), lookupSn(true)]) // 处置会改变在制状态，回显同步刷新
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

watch([page, pageSize], () => load())
watch([snFilter, actionFilter], () => search())

onMounted(() => load())
</script>

<style scoped>
.repairs { display: flex; flex-direction: column; gap: 16px; }
.quick-form { max-width: 520px; }
.action-group { display: flex; flex-wrap: wrap; }
/* SN 回显：状态标签 + 失败计数 + 已盖章路径，一行排布 */
.sn-hint { margin-top: 4px; font-size: 12.5px; }
.sn-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 6px;
  font-size: 12.5px;
}
</style>
