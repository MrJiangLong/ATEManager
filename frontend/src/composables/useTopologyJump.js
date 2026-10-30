import { inject } from 'vue'
import { useRouter } from 'vue-router'

/**
 * 「跳到某流程的工步拓扑」的共用逻辑（机型页流程名 / 流程管理页流程 ID 共用）。
 *
 * 为什么要注入请求而不是只改 URL：工艺配置各 Tab 在同一路由（/configs）下，
 * 手动切 Tab 不更新 URL，只靠 route.query 会导致“点第二次没反应”。
 * 这里用 nonce 递增的注入请求，保证同一流程重复点击也能触发；URL 仅作可分享深链。
 */
export function useTopologyJump() {
  const router = useRouter()
  const configTab = inject('configTab', null)
  const topologyRequest = inject('topologyRequest', null)

  function gotoTopology(processId) {
    if (!processId) return
    if (topologyRequest) {
      topologyRequest.value = { pid: processId, nonce: (topologyRequest.value?.nonce || 0) + 1 }
    }
    if (configTab) configTab.value = 'topology'
    Promise.resolve(router.replace({ path: '/configs', query: { tab: 'topology', process_id: processId } }))
      .catch(() => {})
  }

  return { gotoTopology }
}
