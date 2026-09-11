<template>
  <div class="rank-list">
    <div
      v-for="(row, i) in items"
      :key="row.case_id"
      class="rank-row"
      :title="row.case_id"
      @click="onCopy(row.case_id)"
    >
      <span class="rank-no" :class="{ 'is-top': i < 3 }">{{ i + 1 }}</span>
      <div class="rank-main">
        <CaseIdText :value="row.case_id" class="rank-id" />
        <span v-if="row.item_name && row.item_name !== row.case_id" class="rank-name muted">
          {{ row.item_name }}
        </span>
      </div>
      <span class="rank-count">{{ row.fail_count }}</span>
    </div>
    <EmptyState v-if="!items?.length" :text="emptyText" />
  </div>
</template>

<script setup>
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import EmptyState from './EmptyState.vue'
import CaseIdText from './CaseIdText.vue'

const props = defineProps({
  items: { type: Array, default: () => [] },
  emptyText: { type: String, default: '' },
})

const { t } = useI18n()

async function onCopy(text) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // 非安全上下文 / 无剪贴板权限时降级
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    try {
      document.execCommand('copy')
    } catch {
      /* 复制失败不打断浏览 */
    }
    document.body.removeChild(ta)
  }
  ElMessage.success(t('dashboard.copyCaseId'))
}
</script>

<style scoped>
.rank-list { display: flex; flex-direction: column; }
.rank-row {
  display: grid;
  grid-template-columns: 32px 1fr 64px;
  align-items: center;
  gap: 14px;
  height: 48px;
  padding: 0 10px;
  border-bottom: 1px dashed var(--app-border);
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s ease;
}
.rank-row:last-child { border-bottom: none; }
.rank-row:hover { background: #f7f9ff; }

.rank-no {
  width: 28px;
  height: 28px;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12.5px;
  font-weight: 700;
  color: var(--app-text-sub);
  background: #f0f2f7;
  font-variant-numeric: tabular-nums;
}
.rank-no.is-top {
  color: #fff;
  background: #2f6bff;
  box-shadow: 0 2px 6px rgba(47, 107, 255, 0.25);
}

.rank-main {
  min-width: 0;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.rank-id {
  font-size: 13px;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rank-name {
  font-size: 11.5px;
  flex-shrink: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* 失败次数：与用例号（13px）同量级，靠字重与 tabular-nums 对齐，不做大号数字 */
.rank-count {
  text-align: right;
  font-size: 14px;
  font-weight: 650;
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.2px;
}
</style>
