<template>
  <header class="page-toolbar">
    <div v-if="hasCrumb" class="page-toolbar-crumb">
      <span
        v-for="(c, i) in breadcrumb"
        :key="c.key || i"
        class="crumb"
        :class="{ 'crumb-current': i === breadcrumb.length - 1 }"
      >
        {{ c.label }}<span v-if="i < breadcrumb.length - 1" class="crumb-sep">/</span>
      </span>
    </div>

    <div v-if="hasRow1" class="page-toolbar-row page-toolbar-row--top">
      <div class="page-toolbar-text">
        <h2 v-if="title" class="page-toolbar-title">{{ title }}</h2>
        <p v-if="subtitle" class="page-toolbar-sub">{{ subtitle }}</p>
      </div>
    </div>

    <div v-if="hasRow2" class="page-toolbar-row page-toolbar-row--filters">
      <div class="page-toolbar-filters">
        <slot />
      </div>
      <div v-if="$slots.extra" class="page-toolbar-extra">
        <slot name="extra" />
      </div>
    </div>
  </header>
</template>

<script setup>
import { computed, useSlots } from 'vue'

const props = defineProps({
  title: { type: String, default: '' },
  subtitle: { type: String, default: '' },
  breadcrumb: { type: Array, default: () => [] },
})

const slots = useSlots()

const hasCrumb = computed(() => Array.isArray(props.breadcrumb) && props.breadcrumb.length > 0)
const hasRow1 = computed(() => Boolean(props.title || props.subtitle))
const hasRow2 = computed(() => Boolean(slots.default || slots.extra))
</script>

<style scoped>
/* 页头采用"无卡片"平面式：标题与筛选直接落在内容底色上，
   仅让数据卡片带边框，减少视觉噪音、突出正文 */
.page-toolbar {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-bottom: 18px;
}
.page-toolbar-crumb {
  font-size: 12px;
  color: var(--app-text-faint);
  display: flex;
  gap: 6px;
  align-items: center;
}
.crumb { display: inline-flex; align-items: center; gap: 6px; }
.crumb-sep { color: #c3cfe6; }
.crumb-current { color: var(--app-text); font-weight: 500; }

.page-toolbar-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.page-toolbar-row--top { justify-content: space-between; }
.page-toolbar-text { min-width: 0; flex: 1; }
.page-toolbar-title {
  font-size: 20px;
  font-weight: 700;
  color: var(--app-text);
  margin: 0;
  line-height: 1.3;
  letter-spacing: 0.2px;
}
.page-toolbar-sub {
  font-size: 13px;
  color: var(--app-text-sub);
  margin: 4px 0 0;
  line-height: 1.5;
}
.page-toolbar-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  flex: 1;
  min-width: 0;
}
.page-toolbar-extra {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
</style>
