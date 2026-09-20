<template>
  <el-drawer
    v-model="visible"
    :title="$t('sessions.drawerTitle')"
    size="640px"
    destroy-on-close
    class="session-drawer"
  >
    <template v-if="session">
      <div class="summary">
        <el-tag :type="sessionTagType(session.status)" :effect="session.status === 'RUNNING' ? 'dark' : 'plain'" size="small">
          {{ $t(`sessionStatus.${session.status}`) }}
        </el-tag>
        <span class="summary-sn code">{{ session.sn }}</span>
        <span class="muted">{{ session.station_id }} · {{ session.client_id }}</span>
      </div>

      <div class="block">
        <div class="block-title">{{ $t('sessions.sectionAttempts') }}</div>
        <div class="attempt-track">
          <span
            v-for="n in Math.min(session.attempt, 6)"
            :key="n"
            class="attempt-node"
            :class="{ current: n === session.attempt }"
          />
          <span v-if="session.attempt > 6" class="muted more">+{{ session.attempt - 6 }}</span>
          <span class="attempt-text">{{ attemptText }}</span>
        </div>
      </div>

      <div class="block">
        <div class="block-title">{{ $t('sessions.sectionBasic') }}</div>
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item :label="$t('sessions.sessionId')">
            <span class="code">{{ session.session_id }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('sessions.tableStation')">
            {{ session.station_id }}
          </el-descriptions-item>
          <el-descriptions-item :label="$t('sessions.tableClient')">
            <span class="code">{{ session.client_id }}</span>
          </el-descriptions-item>
          <el-descriptions-item :label="$t('sessions.tableStarted')">
            {{ fmtDateTime(session.started_at) }}
          </el-descriptions-item>
          <el-descriptions-item :label="$t('sessions.heartbeatAt')">
            {{ session.last_heartbeat_at ? fmtDateTime(session.last_heartbeat_at) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="$t('sessions.endedAt')">
            {{ session.ended_at ? fmtDateTime(session.ended_at) : '—' }}
          </el-descriptions-item>
          <el-descriptions-item :label="$t('sessions.endedBy')" :span="2">
            {{ session.ended_by || '—' }}
          </el-descriptions-item>
        </el-descriptions>
      </div>

      <div class="block">
        <div class="block-title">{{ $t('sessions.tableEndReason') }}</div>
        <div v-if="session.end_reason" class="reason-box">{{ session.end_reason }}</div>
        <div v-else class="reason-box muted">{{ $t('sessions.noEndReason') }}</div>
      </div>

      <div class="block">
        <div class="block-title">
          {{ $t('sessions.sectionCheckpoint') }}
          <span class="muted count">({{ session.item_count }})</span>
        </div>
        <el-table v-if="session.items?.length" :data="session.items" size="small" border max-height="260">
          <el-table-column :label="$t('configs.caseId')" min-width="220" show-overflow-tooltip>
            <template #default="{ row }">
              <div class="case-cell">
                <span class="code">{{ shortCaseId(row.case_id) }}</span>
                <span v-if="caseOwner(row.case_id)" class="muted case-owner">{{ caseOwner(row.case_id) }}</span>
              </div>
            </template>
          </el-table-column>
          <el-table-column :label="$t('common.result')" width="90" align="center">
            <template #default="{ row }">
              <el-tag size="small" :type="resultTagType(row.result)">{{ row.result || '-' }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column :label="$t('records.tableDuration')" width="100" align="right">
            <template #default="{ row }">{{ fmtDurationMs(row.duration_ms) }}</template>
          </el-table-column>
          <el-table-column prop="message" :label="$t('repairs.tableReason')" min-width="140" />
        </el-table>
        <EmptyState v-else :text="$t('sessions.noCheckpoint')" />
        <div v-if="cursorText" class="cursor-line muted">
          {{ $t('sessions.sectionCursor') }}: <span class="code">{{ cursorText }}</span>
        </div>
      </div>
    </template>

    <template #footer>
      <div class="drawer-actions">
        <el-button @click="visible = false">{{ $t('common.close') }}</el-button>
        <el-button :icon="View" @click="emit('trace', session)">{{ $t('products.viewTrace') }}</el-button>
        <el-button
          v-if="canOperate && session?.status === 'RUNNING'"
          type="danger"
          plain
          :icon="SwitchButton"
          @click="emit('abort', session)"
        >
          {{ $t('sessions.abort') }}
        </el-button>
        <el-button v-if="canOperate" type="warning" :icon="Unlock" @click="emit('force-release', session)">
          {{ $t('products.forceRelease') }}
        </el-button>
      </div>
    </template>
  </el-drawer>
</template>

<script setup>
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '../stores/auth'
import { SwitchButton, Unlock, View } from '@element-plus/icons-vue'
import EmptyState from './EmptyState.vue'
import { attemptTipText, caseOwner, fmtDateTime, fmtDurationMs, resultTagType, sessionTagType, shortCaseId } from '../utils/format'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  session: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'abort', 'force-release', 'trace'])

const { t } = useI18n()
const { canOperate } = useAuth()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const attemptText = computed(() => attemptTipText(props.session?.attempt || 1, t))

const cursorText = computed(() => {
  const cursor = props.session?.cursor
  if (!cursor || !Object.keys(cursor).length) return ''
  return JSON.stringify(cursor)
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

/* 末端实心节点表示"当前这一次" */
.attempt-track { display: flex; align-items: center; gap: 6px; }
.attempt-node {
  width: 16px;
  height: 6px;
  border-radius: 3px;
  background: var(--app-border);
}
.attempt-node.current { background: var(--app-warning, #f59e0b); }
.more { font-size: 12px; }
.attempt-text { font-size: 12px; color: var(--app-text-muted, #8a94a6); margin-left: 6px; }

.reason-box {
  background: rgba(0, 0, 0, 0.03);
  border: 1px solid var(--app-border);
  border-radius: 8px;
  padding: 10px 12px;
  font-size: 13px;
  line-height: 1.6;
  word-break: break-word;
}

.case-cell { display: flex; flex-direction: column; line-height: 1.3; }
.case-owner { font-size: 11px; }
.cursor-line { margin-top: 8px; font-size: 12px; }

.drawer-actions { display: flex; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
</style>
