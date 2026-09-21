<template>
  <span class="code case-id">
    <span class="case-id-head">{{ head }}</span><span class="case-id-tail">{{ tail }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  value: { type: String, default: '' },
})

// 以最后一个 :: 为界：之前是路径与类名（可压缩），之后是方法名（必须保留）。
const splitAt = computed(() => String(props.value ?? '').lastIndexOf('::'))
const head = computed(() => (splitAt.value < 0 ? '' : props.value.slice(0, splitAt.value + 2)))
const tail = computed(() => (splitAt.value < 0 ? props.value : props.value.slice(splitAt.value + 2)))
</script>

<style scoped>
.case-id {
  display: inline-flex;
  min-width: 0;
  max-width: 100%;
  align-items: baseline;
}
/* min-width:0 是让 flex item 能被压缩的前提 */
.case-id-head {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.case-id-tail {
  flex: none;
  white-space: nowrap;
}
</style>

