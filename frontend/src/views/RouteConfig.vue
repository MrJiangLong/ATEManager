<template>
  <div class="config-page fade-up">
    <PageToolbar :title="$t('configs.title')" :subtitle="$t('configs.subtitle')" />

    <el-tabs v-model="activeTab" type="border-card" class="config-tabs">
      <el-tab-pane :label="$t('configs.tabProcesses')" name="processes"><RouteProcesses /></el-tab-pane>
      <el-tab-pane :label="$t('configs.tabModels')" name="models"><RouteModels /></el-tab-pane>
      <el-tab-pane :label="$t('configs.tabStations')" name="stations"><RouteStations /></el-tab-pane>
      <el-tab-pane :label="$t('configs.tabTopology')" name="topology"><RouteTopology /></el-tab-pane>
      <el-tab-pane :label="$t('configs.tabItems')" name="items"><RouteItems /></el-tab-pane>
      <el-tab-pane :label="$t('configs.tabSnRegistry')" name="snRegistry"><RouteSnRegistry /></el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { provide, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import PageToolbar from '../components/PageToolbar.vue'
import RouteProcesses from './route-config/RouteProcesses.vue'
import RouteModels from './route-config/RouteModels.vue'
import RouteStations from './route-config/RouteStations.vue'
import RouteTopology from './route-config/RouteTopology.vue'
import RouteItems from './route-config/RouteItems.vue'
import RouteSnRegistry from './route-config/RouteSnRegistry.vue'

const route = useRoute()
/** 支持外部深链 ?tab=topology（机型页点流程名跳转过来） */
const TABS = ['processes', 'models', 'stations', 'topology', 'items', 'snRegistry']
const activeTab = ref(TABS.includes(route.query.tab) ? route.query.tab : 'processes')
/** 子页请求「打开某流程的工步拓扑」：nonce 保证同一流程重复点击也能触发（route.query 做不到） */
const topologyRequest = ref({
  pid: typeof route.query.process_id === 'string' ? route.query.process_id : '',
  nonce: 0,
})
/** 同一路由内 router.push 不会重挂组件：必须监听 query 才能切 Tab */
watch(
  () => route.query.tab,
  (tab) => {
    if (TABS.includes(tab)) activeTab.value = tab
  }
)
/** 下发当前 Tab 给各子页，供 useTabRefresh 在切回时刷新数据（组件常驻不重挂） */
provide('configTab', activeTab)
provide('topologyRequest', topologyRequest)
</script>

<style scoped>
.config-page { display: flex; flex-direction: column; gap: 16px; }
.config-tabs { background: #fff; border-radius: var(--app-card-radius); }
.config-tabs :deep(.el-tabs__content) { padding: 16px 18px; }
</style>
