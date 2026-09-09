<template>
  <div class="repairs fade-up">
    <PageToolbar :title="$t('repairs.title')" :subtitle="$t('repairs.subtitle')" />

    <DataCard :title="$t('repairs.quickTitle')">
      <el-form :model="form" label-position="top" class="quick-form" @submit.prevent="submit">
        <el-form-item :label="t('common.sn')" required>
          <el-input v-model="form.sn" :placeholder="t('products.repairSnPh')" />
        </el-form-item>
        <el-form-item :label="t('products.repairAction')" required>
          <el-radio-group v-model="form.repair_action" class="action-group">
            <el-radio-button v-for="a in REPAIR_ACTIONS" :key="a" :value="a">{{ t(`repair.${a}`) }}</el-radio-button>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="needsTarget" :label="t('products.repairTargetStation')" required>
          <el-select v-model="form.target_station" filterable style="width:100%">
            <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t('products.repairReason')" required>
          <el-input v-model="form.reason" type="textarea" :rows="3" :placeholder="t('products.repairReasonPh')" />
        </el-form-item>
        <el-button type="primary" :loading="saving" @click="submit">{{ t('common.confirm') }}</el-button>
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
        <el-table-column :label="t('repairs.tableAction')" width="130">
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
import { repairApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import { useProcesses } from '../composables/useProcesses'
import { REPAIR_ACTIONS, REPAIR_ACTIONS_WITH_TARGET } from '../utils/constants'
import { fmtDateTime, repairTagType } from '../utils/format'

const { t } = useI18n()
const { stations, loadProcesses } = useProcesses()

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
    await load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

watch([page, pageSize], () => load())
watch([snFilter, actionFilter], () => search())

onMounted(async () => {
  await loadProcesses()
  await load()
})
</script>

<style scoped>
.repairs { display: flex; flex-direction: column; gap: 16px; }
.quick-form { max-width: 520px; }
.action-group { display: flex; flex-wrap: wrap; }
</style>
