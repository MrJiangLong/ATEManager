import { computed, ref, watch } from 'vue'

/**
 * 本地全量数据的分页。
 * 主数据类接口一次返回全量列表，翻页在前端做即可：
 * - paged      当前页切片，直接绑到 el-table 的 :data
 * - 总数变少（删除/刷新）时自动回落页码，避免停留在空白页
 * - 过滤条件变化回到第一页由调用方在各自的 watch 里置 page.value = 1
 */
export function useLocalPagination(source) {
  const page = ref(1)
  const pageSize = ref(20)
  const paged = computed(() => {
    const start = (page.value - 1) * pageSize.value
    return source.value.slice(start, start + pageSize.value)
  })
  watch(
    () => source.value.length,
    (len) => {
      const maxPage = Math.max(1, Math.ceil(len / pageSize.value))
      if (page.value > maxPage) page.value = maxPage
    }
  )
  return { page, pageSize, paged }
}
