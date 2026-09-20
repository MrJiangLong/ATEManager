import { createApp } from 'vue'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import i18n from './i18n'
import { setUnauthorizedHandler } from './api'
import { useAuth } from './stores/auth'
import { ElMessage } from 'element-plus'
import './styles/index.css'

const app = createApp(App)

setUnauthorizedHandler(() => {
  if (router.currentRoute.value.path.startsWith('/login')) {
    return
  }
  const { logout } = useAuth()
  logout()
  ElMessage.warning(i18n.global.t('auth.sessionExpired'))
  router.push({ path: '/login', query: { redirect: router.currentRoute.value.fullPath } })
})

app.use(router)
app.use(i18n)
app.use(ElementPlus)
for (const [name, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(name, component)
}

app.mount('#app')

