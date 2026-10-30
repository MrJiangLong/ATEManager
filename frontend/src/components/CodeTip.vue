<template>
  <el-tooltip :content="tip" :disabled="!tip" placement="top" :show-after="300">
    <span><slot /></span>
  </el-tooltip>
</template>

<script setup>
import { computed } from 'vue'

/**
 * 编码悬浮显示字典名：name 为空（未设置/查无此典）或与编码相同时不渲染提示，
 * 编码本身就是兜底展示。
 *
 * 传 code 时提示内容为「名称（编码）」——这样列上无需再挂 show-overflow-tooltip
 * （两者同时存在会在 hover 时弹出两个重叠的浮层）。
 */
const props = defineProps({
  name: { type: String, default: '' },
  code: { type: String, default: '' },
})

const tip = computed(() => {
  const name = (props.name || '').trim()
  const code = (props.code || '').trim()
  if (name && code && name !== code) return `${name}（${code}）`
  return name && name !== code ? name : ''
})
</script>
