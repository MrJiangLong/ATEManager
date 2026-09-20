<template>
  <div class="clients fade-up">
    <PageToolbar :title="$t('clients.title')" :subtitle="$t('clients.subtitle')">
      <el-input
        v-model="keyword"
        clearable
        :placeholder="$t('clients.searchPh')"
        style="width: 220px"
      />
      <el-select v-model="stationFilter" clearable :placeholder="$t('clients.filterStation')" style="width:180px">
        <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
      </el-select>
      <template #extra>
        <el-button :icon="Refresh" @click="load">{{ $t('common.refresh') }}</el-button>
        <el-button v-if="canOperate" type="primary" :icon="Plus" @click="openCreate">{{ $t('clients.newClient') }}</el-button>
      </template>
    </PageToolbar>

    <DataCard>
      <el-table
        v-loading="loading"
        :data="paged"
        stripe
        size="small"
        style="width: 100%; table-layout: fixed"
      >
        
        <el-table-column :label="$t('clients.tableMachine')" min-width="140">
          <template #default="{ row }">
            <div class="cell-stack">
              <span class="code">{{ row.client_id }}</span>
              <span v-if="row.client_name" class="muted cell-sub">{{ row.client_name }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="$t('clients.tableStation')" width="260">
          <template #default="{ row }">
            <div v-if="row.bound_stations && row.bound_stations.length" class="cell-stack">
              
              <div class="station-tags">
                <el-tag
                  v-for="s in row.bound_stations.slice(0, 3)"
                  :key="s"
                  size="small"
                  effect="plain"
                  type="primary"
                >{{ s }}</el-tag>
                <el-tooltip
                  v-if="row.bound_stations.length > 3"
                  :content="row.bound_stations.join(', ')"
                  placement="top"
                >
                  <el-tag size="small" effect="plain" type="info">
                    +{{ row.bound_stations.length - 3 }}
                  </el-tag>
                </el-tooltip>
              </div>
            </div>
            <el-tag v-else size="small" effect="plain" type="danger">{{ $t('clients.unbound') }}</el-tag>
          </template>
        </el-table-column>
        
        <el-table-column :label="$t('clients.tableAccess')" min-width="170">
          <template #default="{ row }">
            
            <div v-if="row.ip_address || row.app_version" class="cell-stack">
              <span v-if="row.ip_address" class="cell-sub">{{ row.ip_address }}</span>
              <span v-if="row.app_version" class="muted cell-sub">{{ row.app_version }}</span>
            </div>
            <span v-else class="muted cell-sub">{{ $t('clients.notReported') }}</span>
          </template>
        </el-table-column>
        
        <el-table-column :label="$t('clients.tableState')" min-width="170">
          <template #default="{ row }">
            <div class="cell-stack state-cell" :class="row.online ? 'is-online' : 'is-offline'">
              <span class="state-line">
                <i class="state-dot" />
                <span class="state-text">{{ row.online ? $t('clients.online') : $t('clients.offline') }}</span>
              </span>
              <span class="muted cell-sub">{{ fmtRelative(row.last_seen_at, t) }}</span>
            </div>
          </template>
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
            <el-button v-if="canOperate" link type="primary" size="small" @click="openEdit(row)">{{ $t('common.edit') }}</el-button>
            <el-button
              v-if="canOperate && row.holding_sn"
              link
              type="warning"
              size="small"
              @click="onReleaseLock(row)"
            >
              {{ $t('products.forceRelease') }}
            </el-button>
            <el-button v-if="canOperate" link type="danger" size="small" @click="onDelete(row)">{{ $t('common.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty><EmptyState :text="$t('common.noData')" /></template>
      </el-table>

      <div class="pager">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="filtered.length"
          :page-sizes="[20, 50, 100]"
          layout="total, sizes, prev, pager, next"
          background
          size="small"
        />
      </div>
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
          <el-select
            v-model="form.bound_stations"
            multiple
            filterable
            collapse-tags
            :max-collapse-tags="3"
            collapse-tags-tooltip
            :placeholder="$t('clients.bindStationPh')"
            style="width:100%"
          >
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
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../stores/auth'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { clientApi, productApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import { usePolling } from '../composables/usePolling'
import { useProcesses } from '../composables/useProcesses'
import { fmtRelative } from '../utils/format'

const { t } = useI18n()
const { canOperate } = useAuth()
const { stations, loadProcesses } = useProcesses()

const items = ref([])
const loading = ref(false)
const stationFilter = ref('')
const keyword = ref('')

const dialogVisible = ref(false)
const isEdit = ref(false)
const saving = ref(false)
const form = reactive({ client_id: '', client_name: '', bound_stations: [], ip_address: '' })

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

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return items.value
  return items.value.filter((c) =>
    [c.client_id, c.client_name, c.ip_address].some((v) =>
      String(v || '').toLowerCase().includes(kw)
    )
  )
})

const page = ref(1)
const pageSize = ref(20)
const paged = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filtered.value.slice(start, start + pageSize.value)
})
watch([keyword, stationFilter], () => {
  page.value = 1
})
// 删除或刷新后总数变少时，防止当前页越界留白
watch(() => filtered.value.length, (len) => {
  const maxPage = Math.max(1, Math.ceil(len / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})

function openCreate() {
  isEdit.value = false
  Object.assign(form, {
    client_id: '',
    client_name: '',
    bound_stations: stationFilter.value ? [stationFilter.value] : [],
    ip_address: '',
  })
  dialogVisible.value = true
}
function openEdit(row) {
  isEdit.value = true
  Object.assign(form, {
    client_id: row.client_id,
    client_name: row.client_name || '',
    bound_stations: [...(row.bound_stations || [])],
    ip_address: row.ip_address || '',
  })
  dialogVisible.value = true
}

async function submit() {
  if (!form.client_id.trim()) return ElMessage.warning(t('errors.requiredField'))
  // 上位机首次上报会自动注册为"未绑定"，这类机台要能改 IP/保持解绑，故仅新建时强制绑定工位
  if (!isEdit.value && !form.bound_stations.length) return ElMessage.warning(t('errors.requiredField'))
  saving.value = true
  try {
    const payload = {
      bound_stations: form.bound_stations || [],
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
/* 单元格内两行堆叠：主信息一行、次要信息一行，形成主次层次 */
.cell-stack { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.cell-sub { font-size: 12.5px; line-height: 1.4; }
/* 绑定工位标签：换行排布，超 3 个折叠为 +N（tooltip 看全部） */
.station-tags { display: flex; flex-wrap: wrap; gap: 4px; }

/* 状态列：小号文字 + 圆点，靠颜色区分而非字号字重，避免喧宾夺主 */
.state-line { display: inline-flex; align-items: center; gap: 6px; }
.state-dot {
  width: 7px; height: 7px; border-radius: 50%;
  background: var(--app-border, #c3cfe6); flex: none;
}
.state-text { font-weight: 500; }
.state-cell.is-online .state-dot { background: var(--app-success, #12b76a); }
.state-cell.is-online .state-text { color: var(--app-success, #12b76a); }
.state-cell.is-offline .state-dot { background: var(--app-danger, #f56c6c); }
.state-cell.is-offline .state-text { color: var(--app-danger, #f56c6c); }

/* 字号与其他页面同一体系：正文继承默认 14px，辅助 12.5px 对齐全局 muted/code */
.clients :deep(.el-table .el-table__cell) { padding: 11px 12px; }

/* 分页脚靠右，与表格留出间距 */
.pager { display: flex; justify-content: flex-end; padding-top: 12px; }
.holding-lost { font-size: 11px; margin-top: 2px; }
</style>

