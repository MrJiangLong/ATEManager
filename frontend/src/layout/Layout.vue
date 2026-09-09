<template>
  <el-container class="layout">
    <AppSidebar :collapsed="collapsed" :version="version" />
    <el-container class="main-wrap" direction="vertical">
      <AppHeader :collapsed="collapsed" @toggle="toggleCollapse" @user-command="handleUserCommand" />

      <el-main class="content">
        <router-view v-slot="{ Component }">
          <transition name="fade-slide" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>

    <ChangePasswordDialog v-model="pwdVisible" />
  </el-container>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ElMessage } from 'element-plus'
import api from '../api'
import { useAuth } from '../stores/auth'
import AppHeader from './AppHeader.vue'
import AppSidebar from './AppSidebar.vue'
import ChangePasswordDialog from './ChangePasswordDialog.vue'

const router = useRouter()
const { t } = useI18n()
const auth = useAuth()
const version = ref('--')

const collapsed = ref(localStorage.getItem('sidebar-collapsed') === '1')

function toggleCollapse() {
  collapsed.value = !collapsed.value
  localStorage.setItem('sidebar-collapsed', collapsed.value ? '1' : '0')
}

onMounted(async () => {
  try {
    const res = await api.get('/health')
    if (res.data?.version) version.value = res.data.version
  } catch {
    /* 健康检查失败时保留占位符 */
  }
})

const pwdVisible = ref(false)

function handleUserCommand(command) {
  if (command === 'logout') {
    auth.logout()
    ElMessage.success(t('auth.logoutSuccess'))
    router.push('/login')
  } else if (command === 'changePassword') {
    pwdVisible.value = true
  }
}
</script>

<style scoped>
.layout { height: 100%; }
.content {
  padding: 20px 24px;
  overflow-y: auto;
  background: var(--app-content-bg, #f5f6fa);
}
.fade-slide-enter-active,
.fade-slide-leave-active { transition: opacity 0.18s ease, transform 0.18s ease; }
.fade-slide-enter-from { opacity: 0; transform: translateY(6px); }
.fade-slide-leave-to { opacity: 0; transform: translateY(-6px); }
</style>
