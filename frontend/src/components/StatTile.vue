<template>
  <div
    class="stat-tile subtle-card"
    :class="{ 'is-clickable': clickable }"
    :role="clickable ? 'button' : null"
    :tabindex="clickable ? 0 : null"
    @click="onClick"
    @keydown.enter="onClick"
  >
    <div class="stat-head">
      <div class="stat-icon" :class="iconClass">
        <el-icon><component :is="icon" /></el-icon>
      </div>
      <div class="stat-label">{{ label }}</div>
      <el-icon v-if="clickable" class="stat-arrow"><ArrowRight /></el-icon>
    </div>
    <div class="stat-value">
      <span class="num">{{ value }}</span>
      <span v-if="unit" class="unit">{{ unit }}</span>
    </div>
    <div v-if="legend?.length" class="stat-legend">
      <span
        v-for="(item, i) in legend"
        :key="i"
        :class="['legend-item', { emphasis: item.emphasis, alert: item.alert }]"
      >
        <i class="dot" :style="dotStyle(item)" />
        {{ item.text }}
      </span>
    </div>
    <div v-if="hint" class="stat-foot">
      <span class="muted">{{ hint }}</span>
    </div>
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { ArrowRight } from '@element-plus/icons-vue'

const props = defineProps({
  label: { type: String, required: true },
  value: { type: [Number, String], required: true },
  unit: { type: String, default: '' },
  icon: { type: [Object, Function], required: true },
  tone: {
    type: String,
    default: 'slate',
    validator: (v) => ['slate', 'blue', 'teal', 'green', 'indigo', 'purple', 'danger'].includes(v),
  },
  legend: { type: Array, default: () => [] },
  hint: { type: String, default: '' },

  /** 可点击：显示跳转箭头与 hover 抬升 */
  clickable: { type: Boolean, default: false },
})
const emit = defineEmits(['click'])

/** 图标块用极低饱和 tint 区分各指标：常规为中性 slate，danger 表示异常（僵尸锁） */
const iconClass = computed(() => 'tone-' + props.tone)

/** 图例圆点：默认中性灰，emphasis 用品牌蓝，alert 用红 */
function dotStyle(item) {
  if (item.alert) return { background: '#ef4444' }
  if (item.emphasis) return { background: '#2f6bff' }
  return { background: '#c3ccdd' }
}

function onClick() {
  if (props.clickable) emit('click')
}
</script>

<style scoped>
.stat-tile {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 18px 20px;
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}
.stat-tile.is-clickable { cursor: pointer; }
.stat-tile.is-clickable:hover,
.stat-tile.is-clickable:focus-visible {
  transform: translateY(-2px);
  border-color: #c6d6ff;
  box-shadow: 0 6px 16px rgba(16, 28, 61, 0.06);
  outline: none;
}
.stat-head { display: flex; align-items: center; gap: 10px; }
.stat-icon {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  flex-shrink: 0;
}
.stat-icon.tone-slate  { background: #f1f4f9; color: #5b6b8c; border: 1px solid #e6eaf2; }
.stat-icon.tone-blue   { background: #eef3ff; color: #2f6bff; border: 1px solid #dce7ff; }
.stat-icon.tone-teal   { background: #e4f6f5; color: #0d9488; border: 1px solid #c9e9e6; }
.stat-icon.tone-green  { background: #e9f7ef; color: #15a35a; border: 1px solid #cdeedb; }
.stat-icon.tone-indigo { background: #eef0fb; color: #5b54d6; border: 1px solid #e0e2f7; }
.stat-icon.tone-purple { background: #f3effb; color: #7c3aed; border: 1px solid #e7def7; }
.stat-icon.tone-danger { background: #fef3f2; color: #b42318; border: 1px solid #fbd5d1; }
.stat-label {
  font-size: 12.5px;
  color: var(--app-text-muted);
  font-weight: 500;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.stat-arrow { color: #c3ccdd; font-size: 14px; flex-shrink: 0; }
.stat-tile.is-clickable:hover .stat-arrow { color: var(--app-primary); }
.stat-value { display: flex; align-items: baseline; gap: 6px; }
.stat-value .num {
  font-size: 28px;
  font-weight: 700;
  color: var(--app-text);
  font-variant-numeric: tabular-nums;
  line-height: 1.1;
}
.stat-value .unit { color: var(--app-text-muted); font-size: 12.5px; }
.stat-legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: #6b7a99; }
.stat-legend .emphasis { color: var(--app-primary); font-weight: 600; }
.stat-legend .alert { color: #b42318; }
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  margin-right: 4px;
}
.stat-foot { font-size: 11px; color: var(--app-text-muted); }
</style>
