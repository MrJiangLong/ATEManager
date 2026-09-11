<template>
  <div class="clients fade-up">
    <PageToolbar :title="$t('clients.title')" :subtitle="$t('clients.subtitle')">
      <el-select v-model="stationFilter" clearable :placeholder="$t('clients.filterStation')" style="width:180px">
        <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
      </el-select>
      <template #extra>
        <el-button :icon="Refresh" @click="load">{{ $t('common.refresh') }}</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">{{ $t('clients.newClient') }}</el-button>
      </template>
    </PageToolbar>

    <DataCard>
      <el-table v-loading="loading" :data="items" stripe size="small">
        <el-table-column prop="client_id" :label="$t('clients.tableClient')" width="170">
          <template #default="{ row }"><span class="code">{{ row.client_id }}</span></template>
        </el-table-column>
        <el-table-column :label="$t('clients.clientName')" width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.client_name">{{ row.client_name }}</span>
            <span v-else class="muted">{{ row.client_id }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('clients.tableStation')" width="170">
          <template #default="{ row }">
            <el-tag v-if="row.station_id" size="small" effect="plain" type="primary">{{ row.station_id }}</el-tag>
            <el-tag v-else size="small" effect="plain" type="danger">{{ $t('clients.unbound') }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="ip_address" :label="$t('clients.tableIp')" width="140" />
        <el-table-column :label="$t('clients.tableAppVersion')" width="130">
          <template #default="{ row }">
            <span v-if="row.app_version" class="code">{{ row.app_version }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('clients.tableOnline')" width="100">
          <template #default="{ row }">
            <span class="online-dot" :class="{ on: row.online }" />
            <span class="muted">{{ row.online ? $t('clients.online') : $t('clients.offline') }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="$t('clients.tableLastSeen')" width="170">
          <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.last_seen_at) }}</span></template>
        </el-table-column>
        <el-table-column :label="$t('clients.tableHolding')" min-width="180">
          <template #default="{ row }">
            <el-tag v-if="row.holding_sn" size="small" type="warning">{{ row.holding_sn }}</el-tag>
            <span v-else class="muted">—</span>
            <div v-if="row.holding_sn && !row.online" class="holding-lost text-danger">
              {{ $t('clients.holdingLost') }}
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="$t('common.action')" width="200" align="center" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button
              v-if="row.holding_sn"
              link
              type="warning"
              size="small"
              @click="onReleaseLock(row)"
            >
              {{ $t('products.forceRelease') }}
            </el-button>
            <el-button link type="danger" size="small" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState :text="$t('common.noData')" /></template>
      </el-table>
    </DataCard>

    <el-dialog
      v-model="dialogVisible"
      :title="isEdit ? $t('clients.editClient') : $t('clients.newClient')"
      width="480px"
      destroy-on-close
      class="form-dialog"
    >
      <el-form :model="form" label-position="top" @submit.prevent="submit">
        <el-form-item :label="$t('clients.clientId')" required>
          <el-input v-model="form.client_id" :disabled="isEdit" :placeholder="$t('clients.clientIdPh')" />
        </el-form-item>
        <el-form-item :label="$t('clients.clientName')">
          <el-input v-model="form.client_name" :placeholder="$t('clients.clientNamePh')" />
        </el-form-item>
        <el-form-item :label="$t('clients.bindStation')" :required="!isEdit">
          <el-select v-model="form.station_id" filterable clearable style="width:100%">
            <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
          </el-select>
        </el-form-item>
        <el-form-item :label="$t('clients.ipAddress')">
          <el-input v-model="form.ip_address" placeholder="10.1.60.11" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ $t('common.cancel') }}</el-button>
        <el-button type="primary" :loading="saving" @click="submit">{{ $t('common.save') }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { clientApi, productApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import { usePolling } from '../composables/usePolling'
import { useProcesses } from '../composables/useProcesses'
import { fmtDateTime } from '../utils/format'

const { t } = useI18n()
const { stations, loadProcesses } = useProcesses()

const items = ref([])
const loading = ref(false)
const stationFilter = ref('')

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({ client_id: '', client_name: '', station_id: '', ip_address: '' })

async function load() {
  loading.value = true
  try {
    const params = stationFilter.value ? { station_id: stationFilter.value } : {}
    const res = await clientApi.list(params)
    items.value = res.data
  } catch (e) {
    items.value = []
    ElMessage.error(e.message || t('errors.loadFailed'))
  } finally {
    loading.value = false
  }
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, {
    client_id: '',
    client_name: '',
    station_id: stationFilter.value || '',
    ip_address: '',
  })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  Object.assign(form, {
    client_id: row.client_id,
    client_name: row.client_name || '',
    station_id: row.station_id || '',
    ip_address: row.ip_address || '',
  })
  dialogVisible.value = true
}

async function submit() {
  if (!form.client_id.trim()) return ElMessage.warning(t('errors.requiredField'))
  // 上位机首次上报会自动注册为"未绑定"，这类机台要能改 IP/保持解绑，故仅新建时强制绑定工位
  if (!isEdit.value && !form.station_id) return ElMessage.warning(t('errors.requiredField'))
  saving.value = true
  try {
    const payload = {
      station_id: form.station_id,
      client_name: form.client_name || null,
      ip_address: form.ip_address || null,
    }
    if (isEdit.value) {
      await clientApi.update(form.client_id, payload)
    } else {
      await clientApi.create({ client_id: form.client_id.trim(), ...payload })
    }
    ElMessage.success(t('common.saveSuccess'))
    dialogVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  } finally {
    saving.value = false
  }
}

async function onReleaseLock(row) {
  let reason
  try {
    const { value } = await ElMessageBox.prompt(
      t('products.forceReleaseTip', { sn: row.holding_sn }),
      t('products.forceRelease'),
      {
        inputPlaceholder: t('products.forceReleasePh'),
        inputValidator: (v) => !!String(v || '').trim(),
        type: 'warning',
      }
    )
    reason = String(value || '').trim()
  } catch {
    return
  }
  try {
    await productApi.forceRelease(row.holding_sn, reason)
    ElMessage.success(t('products.forceReleaseSuccess'))
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onDelete(row) {
  try {
    await ElMessageBox.confirm(
      t('clients.yesDelete', { name: row.client_id }),
      t('common.confirm'),
      { type: 'warning' }
    )
  } catch {
    return
  }
  try {
    await clientApi.remove(row.client_id)
    ElMessage.success(t('common.deleteSuccess'))
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

watch(stationFilter, () => load())
onMounted(async () => {
  await loadProcesses()
  await load()
})

/* 机台在线/持锁状态需分钟级可见，否则崩溃机台会长期"假装在线" */
usePolling(load, 20000)
</script>

<style scoped>
.clients { display: flex; flex-direction: column; gap: 16px; }
.online-dot {
  display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: #c3cfe6; margin-right: 6px; vertical-align: middle;
}
.online-dot.on { background: var(--app-success); box-shadow: 0 0 6px rgba(18, 183, 106, 0.8); }
.holding-lost { font-size: 11px; margin-top: 2px; }
</style>
