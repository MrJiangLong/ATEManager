<template>
  <el-header height="60px" class="header">
    <div class="header-left">
      <el-button link class="collapse-btn" :icon="collapsed ? Expand : Fold" @click="$emit('toggle')" />

      <span class="header-title">{{ currentTitle }}</span>
    </div>

    <div class="header-right">
      <el-dropdown trigger="click" @command="onLang">
        <span class="lang-btn">
          <el-icon><Switch /></el-icon>
          <span>{{ langLabel }}</span>
          <el-icon class="el-icon--right"><ArrowDown /></el-icon>
        </span>
        <template #dropdown>
          <el-dropdown-menu>
            <el-dropdown-item command="zh-CN" :class="{ 'lang-active': locale === 'zh-CN' }">
              <el-icon v-if="locale === 'zh-CN'"><Check /></el-icon>{{ $t('layout.langZh') }}
            </el-dropdown-item>
            <el-dropdown-item command="en" :class="{ 'lang-active': locale === 'en' }">
              <el-icon v-if="locale === 'en'"><Check /></el-icon>{{ $t('layout.langEn') }}
            </el-dropdown-item>
          </el-dropdown-menu>
        </template>
      </el-dropdown>

      <el-tag type="info" effect="plain" class="date-tag">
        <el-icon><Calendar /></el-icon>
        {{ today }}
      </el-tag>

      <!-- 已登录：用户信息 + 下拉（修改密码 / 退出） -->
      <div v-if="isLoggedIn" class="user-box">
        <el-avatar :size="34" class="avatar">{{ avatarText }}</el-avatar>
        <el-dropdown trigger="click" @command="$emit('user-command', $event)">
          <div class="user-meta user-meta-btn">
            <span class="user-name">{{ userName }}</span>
            <span class="user-role">{{ userRole }}</span>
          </div>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="changePassword">
                <el-icon><Key /></el-icon>{{ $t('auth.changePassword') }}
              </el-dropdown-item>
              <el-dropdown-item command="logout" divided>
                <el-icon><SwitchButton /></el-icon>{{ $t('auth.logout') }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
    </div>
  </el-header>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { ArrowDown, Calendar, Check, Expand, Fold, Key, Switch, SwitchButton } from '@element-plus/icons-vue'
import { setLocale } from '../i18n'
import { useAuth } from '../stores/auth'

defineProps({
  collapsed: { type: Boolean, default: false },
})
defineEmits(['toggle', 'user-command'])

const route = useRoute()
const { t, locale } = useI18n()
const auth = useAuth()
const isLoggedIn = auth.isLoggedIn

/* 顶栏左：当前页面标题（来自路由 meta.titleKey） */
const currentTitle = computed(() => (route.meta?.titleKey ? t(route.meta.titleKey) : ''))

/* 顶栏右：长格式日期（与 AppSidebar 收起态保持一致：语言感知） */
const today = computed(() => {
  const tag = locale.value === 'en' ? 'en-US' : 'zh-CN'
  return new Date().toLocaleDateString(tag, {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    weekday: 'long',
  })
})

const langLabel = computed(() => (locale.value === 'en' ? t('layout.langEn') : t('layout.langZh')))

/* 用户信息：直接读全局会话，避免父组件透传 ref */
const avatarText = computed(() => {
  const name = auth.state.user?.full_name || auth.state.user?.username || ''
  return name ? name.slice(0, 1).toUpperCase() : 'U'
})
const userName = computed(() => auth.state.user?.full_name || auth.state.user?.username || '-')
const userRole = computed(() => (auth.state.user?.is_admin ? t('layout.roleAdmin') : t('layout.roleUser')))

function onLang(value) {
  setLocale(value)
}
</script>

<style scoped>
.header {
  background: #fff;
  border-bottom: 1px solid #eef1f7;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 24px;
}

.header-left { display: flex; align-items: center; gap: 12px; min-width: 0; }
.collapse-btn { font-size: 18px; color: #8b98b5; padding: 4px; }
.collapse-btn:hover { color: var(--app-primary); }

.header-title {
  font-size: 16px;
  font-weight: 600;
  color: var(--app-text, #1f2b4d);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.header-right { display: flex; align-items: center; gap: 18px; }

.lang-btn {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 12px;
  border-radius: 8px;
  border: 1px solid #e6eaf2;
  color: #4a5678;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
  outline: none;
}
.lang-btn:hover {
  border-color: var(--app-primary);
  color: var(--app-primary);
  background: #f0f5ff;
}

.date-tag {
  display: flex;
  align-items: center;
  gap: 5px;
  border-radius: 20px;
  padding: 0 14px;
  height: 32px;
  font-size: 12.5px;
  color: #5c6b8c;
}

.user-box { display: flex; align-items: center; gap: 10px; }
.user-meta-btn {
  display: flex;
  flex-direction: column;
  line-height: 1.3;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 8px;
  transition: all 0.2s;
  outline: none;
}
.user-meta-btn:hover { background: #f0f5ff; }
.user-name { font-size: 13.5px; color: #3d4a6b; font-weight: 500; }
.user-role { font-size: 10.5px; color: #a0abc2; letter-spacing: 1px; }

.avatar {
  background: linear-gradient(135deg, #2f6bff, #7aa2ff);
  font-size: 13px;
  color: #fff;
  box-shadow: 0 3px 10px rgba(47, 107, 255, 0.35);
}

@media (max-width: 900px) {
  .user-meta-btn { display: none; }
}
</style>
