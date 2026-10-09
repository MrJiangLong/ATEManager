import { ElMessage } from 'element-plus'
import { recordApi } from '../api'

/** 展平 executed_items.items[].files 的全局下载索引（与后端 _record_files 同序） */
export function flatFileIndex(row, itemIndex, fileIndex) {
  const items = row.executed_items?.items || []
  let n = 0
  for (let i = 0; i < itemIndex; i++) n += (items[i]?.files || []).length
  return n + fileIndex
}

/** 测试产物下载：后端返回 {filename, url}（MinIO 预签名），新窗口打开 */
export async function downloadArtifact(row, itemIndex, fileIndex, t) {
  try {
    const res = await recordApi.artifactFile(row.record_id, flatFileIndex(row, itemIndex, fileIndex))
    const blob = res.data instanceof Blob ? res.data : new Blob([res.data])
    const { url } = JSON.parse(await blob.text())
    if (!url) throw new Error(t('errors.loadFailed'))
    window.open(url, '_blank')
  } catch (e) {
    ElMessage.error(e.message || t('errors.loadFailed'))
  }
}
