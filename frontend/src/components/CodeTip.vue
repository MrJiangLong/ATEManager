<template>
  <el-tooltip :content="name" :disabled="!name || name === code" placement="top" :show-after="300">
    <span><slot /></span>
  </el-tooltip>
</template>

<script setup>
/**
 * 编码悬浮显示字典名：name 为空（未设置/查无此典）或与编码相同时不渲染提示，
 * 编码本身就是兜底展示。调用方传 name（字典名）与可选 code（编码）即可。
 */
defineProps({
  name: { type: String, default: '' },
  code: { type: String, default: '' },
})
</script>
