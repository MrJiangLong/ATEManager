<template>
  <el-drawer v-model="visible" size="640px" destroy-on-close @opened="load">
    <!-- 头部：只放身份（编号 + 状态） -->
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
          <span class="st-count">{{ stationItems[s.station_id]?.length || 0 }} {{ t('configs.caseUnit') }}</span>
        </div>
        <div class="st-meta">
          <span class="muted">{{ t('configs.dependsOn') }}:</span>
          <template v-if="s.depends_on?.length">
            <el-tag v-for="d in s.depends_on" :key="d" size="small" effect="plain">{{ d }}</el-tag>
          </template>
          <span v-else class="muted">{{ t('configs.noDeps') }}</span>
          <span class="muted st-timeout">{{ t('configs.stationTimeout') }}: {{ timeoutMin(s) }}</span>
        </div>

        <el-table :data="stationItems[s.station_id] || []" size="small" border>
          <el-table-column :label="t('configs.caseId')" min-width="200">
            <template #default="{ row: it }">
              <el-tooltip :content="it.case_id" placement="top" :show-after="400">
                <span class="code case-ellipsis">{{ it.case_id }}</span>
              </el-tooltip>
            </template>
          </el-table-column>
          <el-table-column :label="t('configs.itemName')" min-width="110" show-overflow-tooltip>
            <template #default="{ row: it }">{{ it.item_name || '—' }}</template>
          </el-table-column>
          <el-table-column :label="t('configs.itemRule')" width="80" align="center">
            <template #default="{ row: it }">
              <el-tag size="small" :type="it.is_mandatory === false ? 'info' : 'warning'" effect="plain">
                {{ it.is_mandatory === false ? t('configs.optional') : t('configs.mandatory') }}
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
      <el-button @click="visible = false">{{ t('common.close') }}</el-button>
      <el-button @click="exportJson" :loading="exporting">{{ t('configs.exportJson') }}</el-button>
      <el-button type="primary" @click="$emit('edit', process)">{{ t('common.edit') }}</el-button>
    </template>
  </el-drawer>
</template>

<script setup>
import { computed, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { routingApi } from '../api'

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
const validate = ref(null)

// items 按 station_id 分组，供各工步卡片取用
const stationItems = computed(() => {
  const map = {}
  for (const it of items.value) {
    ;(map[it.station_id] ||= []).push(it)
  }
  return map
})

const summary = computed(() => {
  const mand = items.value.filter((i) => i.is_mandatory !== false).length
  return `${t('configs.stationCount', { n: steps.value.length })} · ${t('configs.caseCount', { n: items.value.length, m: mand })}`
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
    const [topo, val] = await Promise.all([
      routingApi.topology(props.process.process_id),
      routingApi.validate(props.process.process_id).catch(() => null), // 校验失败不阻塞查看
    ])
    steps.value = topo.data.steps || []
    items.value = topo.data.items || []
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
.meta {
  display: flex; flex-direction: column; gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--app-border, #eef1f7); border-radius: 8px;
}
.meta-row { display: flex; align-items: baseline; gap: 10px; }
.meta-label { flex: none; width: 48px; color: var(--app-text-muted, #8a94a6); font-size: 12.5px; }
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
.st-head { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; min-width: 0; }
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
.st-count { margin-left: auto; flex: none; color: var(--app-text-muted, #8a94a6); font-size: 12.5px; }
.st-meta { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; margin-bottom: 10px; font-size: 12.5px; }
.st-timeout { margin-left: auto; }
.case-ellipsis {
  display: block; max-width: 100%; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap;
}
</style>
