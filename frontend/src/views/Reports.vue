<script setup>
import { computed, ref } from 'vue'
import PageToolbar from '../components/PageToolbar.vue'
import ReportCandidatesPanel from './reports/ReportCandidatesPanel.vue'
import ReportJobsPanel from './reports/ReportJobsPanel.vue'
import ReportRulesPanel from './reports/ReportRulesPanel.vue'
import { useAuth } from '../stores/auth'

const { role } = useAuth()
// 报告规则为 admin 专属（脚本上传=服务器受信代码维护）；后端接口同样以 require_admin 收口
const isAdmin = computed(() => role.value === 'admin')
const activeTab = ref('generate')
</script>

<template>
  <div class="reports fade-up">
    <PageToolbar :title="$t('reports.title')" :subtitle="$t('reports.subtitle')" />

    <el-tabs v-model="activeTab" type="border-card" class="report-tabs">
      <el-tab-pane :label="$t('reports.tabGenerate')" name="generate" lazy>
        <ReportCandidatesPanel :active="activeTab === 'generate'" />
      </el-tab-pane>
      <el-tab-pane :label="$t('reports.tabJobs')" name="jobs" lazy>
        <ReportJobsPanel :active="activeTab === 'jobs'" />
      </el-tab-pane>
      <el-tab-pane v-if="isAdmin" :label="$t('reports.tabRules')" name="rules" lazy>
        <ReportRulesPanel />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
/* 高度链：页面 → Tabs → Tab 面板层层填充，表格在面板内弹性滚动 */
.reports { display: flex; flex-direction: column; gap: 16px; flex: 1 1 auto; min-height: 0; height: 100%; }
.report-tabs { background: #fff; border-radius: var(--app-card-radius); flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
.report-tabs :deep(.el-tabs__content) { flex: 1 1 auto; min-height: 0; padding: 16px 18px; overflow: hidden; }
.report-tabs :deep(.el-tab-pane) { height: 100%; }
</style>
