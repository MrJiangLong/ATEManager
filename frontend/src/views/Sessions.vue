<template>
  <div class="sessions fade-up">
    <PageToolbar :title="$t('sessions.title')" :subtitle="$t('sessions.subtitle')">
      <el-input
        v-model="filters.sn"
        clearable
        :placeholder="$t('sessions.searchSn')"
        style="width: 170px"
        @keyup.enter="search"
      />
      <el-select v-model="filters.station_id" clearable :placeholder="$t('sessions.filterStation')" style="width: 150px">
        <el-option v-for="s in stations" :key="s.station_id" :value="s.station_id" :label="s.station_id" />
      </el-select>
      <el-select v-model="filters.status" clearable :placeholder="$t('sessions.filterStatus')" style="width: 140px">
        <el-option v-for="s in SESSION_STATUS_LIST" :key="s" :value="s" :label="$t(`sessionStatus.${s}`)" />
      </el-select>
      
      <el-select v-model="filters.view" clearable :placeholder="$t('sessions.filterView')" style="width: 150px">
        <el-option value="abnormal" :label="$t('sessions.abnormalOnly')" />
        <el-option value="zombie" :label="$t('sessions.zombieOnly')" />
      </el-select>
      <template #extra>
        <el-button :icon="Refresh" @click="search">{{ $t('common.refresh') }}</el-button>
      </template>
    </PageToolbar>

    <DataCard>
      <el-table v-loading="loading" :data="items" stripe size="small" style="width: 100%; table-layout: fixed">
        <el-table-column prop="sn" :label="$t('sessions.tableSn')" min-width="160">
          <template #default="{ row }">
            <el-button link type="primary" @click="goTrace(row.sn)"><span class="code">{{ row.sn }}</span></el-button>
          </template>
        </el-table-column>
        <el-table-column prop="station_id" :label="$t('sessions.tableStation')" min-width="130">
          <template #default="{ row }"><span class="code">{{ row.station_id }}</span></template>
        </el-table-column>
        <el-table-column prop="client_id" :label="$t('sessions.tableClient')" min-width="150" show-overflow-tooltip>
          <template #default="{ row }"><span class="code">{{ row.client_id }}</span></template>
        </el-table-column>

        <!-- 状态列承载「这个会话现在怎么样」：运行中带心跳，异常终止带原因 -->
        <el-table-column :label="$t('sessions.tableStatus')" min-width="210">
          <template #default="{ row }">
            <div class="cell-stack">
              <span class="status-line">
                <el-tag
                  size="small"
                  :type="sessionTagType(row.status)"
                  :effect="row.status === 'RUNNING' ? 'dark' : 'plain'"
                >
                  {{ $t(`sessionStatus.${row.status}`) }}
                </el-tag>
                <span v-if="row.status === 'RUNNING'" class="live" :class="liveClass(row)">
                  <i class="live-dot" />{{ fmtRelative(row.last_heartbeat_at || row.started_at, t) }}
                </span>
              </span>
              <span v-if="row.end_reason" class="muted cell-sub end-reason" :title="row.end_reason">
                {{ row.end_reason }}
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column :label="$t('sessions.tableAttempt')" width="120">
          <template #default="{ row }">
            <el-tooltip :content="attemptTipText(row.attempt, t)" placement="top">
              <span class="attempt-track">
                <span
                  v-for="n in Math.min(row.attempt, 6)"
                  :key="n"
                  class="attempt-node"
                  :class="{ current: n === row.attempt }"
                />
                <span v-if="row.attempt > 6" class="muted more">+{{ row.attempt - 6 }}</span>
              </span>
            </el-tooltip>
          </template>
        </el-table-column>

        <el-table-column :label="$t('sessions.tableCheckpoint')" width="100" align="right">
          <template #default="{ row }">
            <span :class="{ 'muted': !row.item_count }">{{ row.item_count }}</span>
          </template>
        </el-table-column>

        <el-table-column :label="$t('sessions.tableStarted')" min-width="170">
          <template #default="{ row }"><span class="muted">{{ fmtDateTime(row.started_at) }}</span></template>
        </el-table-column>

        <el-table-column :label="$t('common.action')" width="150" align="center" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :icon="View" @click="openDrawer(row)">
              {{ row.end_reason ? $t('sessions.viewReason') : $t('sessions.viewDetail') }}
            </el-button>
            <el-button
              v-if="canOperate && row.status === 'RUNNING'"
              link
              type="danger"
              size="small"
              :icon="SwitchButton"
              @click="onAbort(row)"
            />
          </template>
        </el-table-column>
        <template #empty><EmptyState :text="$t('common.noData')" /></template>
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

    <SessionDrawer
      v-model="drawerVisible"
      :session="current"
      @abort="onAbort"
      @force-release="onForceRelease"
      @trace="(row) => goTrace(row.sn)"
    />
  </div>
</template>

<script setup>
import { onMounted, reactive, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../stores/auth'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Refresh, SwitchButton, View } from '@element-plus/icons-vue'
import { productApi, sessionApi } from '../api'
import DataCard from '../components/DataCard.vue'
import EmptyState from '../components/EmptyState.vue'
import PageToolbar from '../components/PageToolbar.vue'
import SessionDrawer from '../components/SessionDrawer.vue'
import { usePolling } from '../composables/usePolling'
import { useProcesses } from '../composables/useProcesses'
import { LOCK_GRACE_SEC, SESSION_STATUS_LIST } from '../utils/constants'
import { attemptTipText, fmtDateTime, fmtRelative, sessionTagType } from '../utils/format'

const { t } = useI18n()
const { canOperate } = useAuth()
const router = useRouter()
const { stations, loadProcesses } = useProcesses()

const items = ref([])
const loading = ref(false)
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = reactive({ sn: '', station_id: '', status: '', view: '' })

const drawerVisible = ref(false)
const current = ref(null)

/** 心跳健康度：失联红 / 接近宽限黄 / 新鲜绿 */
function liveClass(row) {
  if (row.lock_idle_sec > LOCK_GRACE_SEC) return 'is-lost'
  if (row.lock_idle_sec > LOCK_GRACE_SEC * 0.6) return 'is-warn'
  return 'is-live'
}

async function load() {
  loading.value = true
  try {
    if (filters.view === 'zombie') {
      const res = await sessionApi.zombieLocks({ page: page.value, page_size: pageSize.value })
      items.value = res.data.items
      total.value = res.data.total
    } else {
      const params = { page: page.value, page_size: pageSize.value }
      if (filters.sn) params.sn = filters.sn
      if (filters.station_id) params.station_id = filters.station_id
      // 视图选「异常终止」时优先于具体状态（沿用后端 abnormal_only 语义）
      if (filters.view === 'abnormal') params.abnormal_only = true
      else if (filters.status) params.status = filters.status
      const res = await sessionApi.list(params)
      items.value = res.data.items
      total.value = res.data.total
    }
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

function goTrace(sn) {
  router.push(`/trace/${encodeURIComponent(sn)}`)
}

function openDrawer(row) {
  current.value = row
  drawerVisible.value = true
}

async function askReason(titleKey, tipKey, sn) {
  try {
    const { value } = await ElMessageBox.prompt(
      t(tipKey, { sn }),
      t(titleKey),
      {
        inputPlaceholder: t('sessions.reasonPh'),
        inputValidator: (v) => !!String(v || '').trim(),
        type: 'warning',
      }
    )
    return String(value || '').trim()
  } catch {
    return null
  }
}

async function onAbort(row) {
  const reason = await askReason('sessions.abort', 'sessions.abortTip', row.sn)
  if (!reason) return
  try {
    await sessionApi.abort(row.session_id, reason)
    ElMessage.success(t('sessions.abortSuccess'))
    drawerVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

async function onForceRelease(row) {
  const reason = await askReason('products.forceRelease', 'products.forceReleaseTip', row.sn)
  if (!reason) return
  try {
    await productApi.forceRelease(row.sn, reason)
    ElMessage.success(t('products.forceReleaseSuccess'))
    drawerVisible.value = false
    load()
  } catch (e) {
    ElMessage.error(e.message)
  }
}

watch([page, pageSize], () => load())
watch(
  () => [filters.sn, filters.station_id, filters.status, filters.view],
  () => search()
)

onMounted(async () => {
  await loadProcesses()
  await load()
})

usePolling(async () => {
  await load()
}, 20000)
</script>

<style scoped>
.sessions {
  display: flex;
  flex-direction: column;
  gap: 16px;
  flex: 1 1 auto;
  min-height: 0;
  height: 100%;
}
.sessions :deep(.data-card),
.sessions :deep(.data-card .card-body) {
  display: flex;
  flex-direction: column;
  flex: 1 1 auto;
  min-height: 0;
}
/* 单元格两行堆叠：主信息 + 次要说明，形成主次层次 */
.cell-stack { display: flex; flex-direction: column; gap: 3px; min-width: 0; }
.cell-sub { font-size: 12.5px; line-height: 1.4; }
.status-line { display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; }
/* 结束原因可能很长：单行截断，完整内容走 title */
.end-reason { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

/* 易读优先：行高与内边距大于默认紧凑表格 */
.sessions :deep(.el-table .el-table__cell) { padding: 11px 12px; }

.sessions :deep(.el-table) {
  flex: 1 1 auto;
  min-height: 0;
  height: calc(100% - 56px);
}
.sessions :deep(.pager) {
  flex: 0 0 auto;
  margin-top: var(--app-space-3);
}

/* 尝试点阵：末端的实心块表示"当前这一次" */
.attempt-track { display: inline-flex; align-items: center; gap: 4px; }
.attempt-node {
  width: 14px;
  height: 5px;
  border-radius: 3px;
  background: var(--app-border, #dfe4ee);
}
.attempt-node.current { background: var(--app-warning, #f59e0b); }
.more { font-size: 11px; }

/* 心跳呼吸点 */
.live { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; }
.live-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--app-border, #c3cfe6);
}
.live.is-live .live-dot {
  background: var(--app-success, #12b76a);
  box-shadow: 0 0 6px rgba(18, 183, 106, 0.8);
  animation: pulse 2s ease-in-out infinite;
}
.live.is-warn .live-dot { background: var(--app-warning, #f59e0b); }
.live.is-lost .live-dot {
  background: var(--app-danger, #f56c6c);
  box-shadow: 0 0 6px rgba(245, 108, 108, 0.9);
}
.live.is-lost { color: var(--app-danger, #f56c6c); font-weight: 600; }

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}
</style>

