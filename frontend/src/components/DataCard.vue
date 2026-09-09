<template>
  <div class="data-card">
    <div v-if="title || $slots.extra" class="card-head">
      <h3 v-if="title" class="card-title">{{ title }}</h3>
      <!-- extra 必须聚成单个 flex 项：card-head 是 space-between，
           裸渲染多元素插槽会被摊满整行（标题居左、其余均匀散开） -->
      <div v-if="$slots.extra" class="card-extra">
        <slot name="extra" />
      </div>
    </div>
    <div class="card-body" v-loading="loading">
      <slot />
    </div>
  </div>
</template>

<script setup>
defineProps({
  title: { type: String, default: '' },
  loading: { type: Boolean, default: false },
})
</script>

<style scoped>
.card-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--app-text);
  margin: 0;
  line-height: 1.4;
}
</style>
