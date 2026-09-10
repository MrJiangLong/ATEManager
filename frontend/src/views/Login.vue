<template>
  <div class="login-page">
    <div class="bg-decor decor-1"></div>
    <div class="bg-decor decor-2"></div>
    <div class="bg-decor decor-3"></div>

    <div class="login-box">
      <div class="brand-panel">
        <div class="brand-logo">
          <AppLogoMark :size="48" />
          <span class="brand-name">{{ $t('app.name') }}</span>
        </div>
        <p class="brand-sub">{{ $t('auth.brandSub') }}</p>
        <ul class="brand-features">
          <li v-for="f in features" :key="f.key" class="feature-item">
            <el-icon color="#7aa2ff"><Check /></el-icon>
            <span>{{ f.text }}</span>
          </li>
        </ul>
        <div class="brand-footer">{{ $t('auth.brandFooter') }}</div>
      </div>

      <div class="form-panel">
        <div class="form-top">
          <el-dropdown trigger="click" @command="changeLang">
            <span class="lang-btn">
              <el-icon><Switch /></el-icon>
              <span>{{ langLabel }}</span>
              <el-icon class="el-icon--right"><ArrowDown /></el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="zh-CN" :class="{ 'lang-active': locale === 'zh-CN' }">
                  <el-icon v-if="locale === 'zh-CN'"><Check /></el-icon>简体中文
                </el-dropdown-item>
                <el-dropdown-item command="en" :class="{ 'lang-active': locale === 'en' }">
                  <el-icon v-if="locale === 'en'"><Check /></el-icon>English
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
        </div>

        <div class="form-body">
          <h1 class="form-title">{{ $t('auth.welcomeBack') }}</h1>
          <p class="form-sub">{{ $t('auth.welcomeBackDesc') }}</p>

          <el-form ref="formRef" :model="form" :rules="rules" size="large" @keyup.enter="handleLogin">
            <el-form-item prop="username">
              <el-input
                v-model="form.username"
                :prefix-icon="User"
                :placeholder="$t('auth.usernamePlaceholder')"
                autocomplete="username"
              />
            </el-form-item>
            <el-form-item prop="password">
              <el-input
                v-model="form.password"
                type="password"
                show-password
                :prefix-icon="Lock"
                :placeholder="$t('auth.passwordPlaceholder')"
                autocomplete="current-password"
              />
            </el-form-item>
            <el-button
              type="primary"
              class="login-btn"
              :loading="loading"
              @click="handleLogin"
            >
              {{ loading ? $t('auth.loggingIn') : $t('auth.login') }}
            </el-button>
          </el-form>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowDown, Check, Lock, Switch, User } from '@element-plus/icons-vue'
import { useI18n } from 'vue-i18n'
import { setLocale } from '../i18n'
import { useAuth } from '../stores/auth'
import AppLogoMark from '../components/AppLogoMark.vue'

const router = useRouter()
const route = useRoute()
const { t, locale } = useI18n()
const auth = useAuth()

const formRef = ref(null)
const loading = ref(false)
const form = reactive({ username: '', password: '' })

const rules = {
  username: [{ required: true, message: () => t('auth.usernameRequired'), trigger: 'blur' }],
  password: [{ required: true, message: () => t('auth.passwordRequired'), trigger: 'blur' }],
}

const langLabel = computed(() => (locale.value === 'en' ? 'English' : '简体中文'))
const features = computed(() => [
  { key: 'f1', text: t('auth.feature1') },
  { key: 'f2', text: t('auth.feature2') },
  { key: 'f3', text: t('auth.feature3') },
])

function changeLang(value) {
  setLocale(value)
}

onMounted(() => {
  if (auth.state.token && !auth.state.user) {
    auth.logout()
  }
})

function safeRedirect(raw) {
  if (typeof raw !== 'string') return '/dashboard'
  if (!raw.startsWith('/') || raw.startsWith('//')) return '/dashboard'
  return raw
}

async function handleLogin() {
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  if (loading.value) return
  loading.value = true
  try {
    const user = await auth.login(form.username.trim(), form.password)
    ElMessage.success(`${t('auth.loginSuccess')}，${user.full_name || user.username}`)
    router.push(safeRedirect(route.query.redirect))
  } catch (err) {
    ElMessage.error(err.message || t('auth.wrongCredentials'))
  } finally {
    loading.value = false
  }
}
</script>

<style scoped>
.login-page {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
  background: linear-gradient(135deg, #0e1b3a 0%, #14274f 45%, #1c3a7d 100%);
}

.bg-decor {
  position: absolute;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(90, 139, 255, 0.22), transparent 70%);
  filter: blur(2px);
  animation: float 9s ease-in-out infinite;
}

.decor-1 {
  width: 420px;
  height: 420px;
  top: -120px;
  left: -100px;
}

.decor-2 {
  width: 320px;
  height: 320px;
  bottom: -100px;
  right: -80px;
  animation-delay: 2.5s;
}

.decor-3 {
  width: 180px;
  height: 180px;
  top: 55%;
  left: 62%;
  animation-delay: 5s;
}

@keyframes float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-24px); }
}

.login-box {
  position: relative;
  z-index: 1;
  display: flex;
  width: 880px;
  max-width: 92vw;
  min-height: 540px;
  border-radius: 20px;
  overflow: hidden;
  background: transparent;
  box-shadow: 0 24px 60px rgba(4, 14, 40, 0.45);
}

/* ===== 左侧品牌区 ===== */
.brand-panel {
  width: 44%;
  background: linear-gradient(160deg, #16295c 0%, #1c3a7d 55%, #24519e 100%);
  color: #fff;
  padding: 46px 38px;
  display: flex;
  flex-direction: column;
  position: relative;
  border-radius: 20px 0 0 20px;
}

.brand-panel::after {
  content: '';
  position: absolute;
  inset: 0;
  background: radial-gradient(500px 260px at 100% 0%, rgba(90, 139, 255, 0.25), transparent 70%);
  pointer-events: none;
}

.brand-logo {
  display: flex;
  align-items: center;
  gap: 12px;
}

.brand-name {
  font-size: 19px;
  font-weight: 700;
  letter-spacing: 1px;
  white-space: nowrap;
}

.brand-sub {
  margin-top: 22px;
  font-size: 14px;
  color: #b6c6ec;
  line-height: 1.7;
}

.brand-features {
  list-style: none;
  padding: 0;
  margin: 28px 0 0;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.feature-item {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13.5px;
  color: #dbe6ff;
}

.brand-footer {
  margin-top: auto;
  font-size: 12px;
  color: #7f93c2;
}

/* ===== 右侧登录区 ===== */
.form-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 44px 64px 56px;
  background: #fff;
  border-radius: 0 20px 20px 0;
}

.form-top {
  display: flex;
  justify-content: flex-end;
}

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

.form-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  justify-content: center;
  /* 限宽让输入框不撑满整个右侧面板，与左品牌区视觉重量更平衡 */
  max-width: 360px;
  width: 100%;
  margin: 0 auto;
  padding: 24px 0;
}

.form-title {
  font-size: 26px;
  font-weight: 700;
  color: var(--app-text-main, #1a2540);
  margin: 0;
}

.form-sub {
  margin: 12px 0 36px;
  font-size: 13.5px;
  color: #8a96b0;
  line-height: 1.6;
}

/* 表单项之间留出更明显的呼吸空间，避免表单看起来"挤" */
.form-body :deep(.el-form-item) {
  margin-bottom: 22px;
}
.form-body :deep(.el-form-item:last-of-type) {
  margin-bottom: 32px;
}

.login-btn {
  width: 100%;
  height: 44px;
  font-size: 15px;
  letter-spacing: 2px;
  border-radius: 10px;
}

.lang-active {
  color: var(--app-primary);
  font-weight: 600;
}

@media (max-width: 720px) {
  .brand-panel {
    display: none;
  }
}
</style>
