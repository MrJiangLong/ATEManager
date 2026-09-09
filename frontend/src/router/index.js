import { createRouter, createWebHistory } from 'vue-router'

import Layout from '../layout/Layout.vue'
// Note: do NOT call useAuth() here. The auth factory is invoked later inside
// navigation guards so it runs after `app.use(router)` and after any future
// migration to Pinia. Calling it at module top-level would freeze a stale
// reference and trigger warnings in strict mode.
import { useAuth } from '../stores/auth'
import { applyRouteTitle } from '../utils/title'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/login',
      name: 'Login',
      component: () => import('../views/Login.vue'),
      meta: { titleKey: 'auth.login', public: true },
    },
    {
      path: '/',
      component: Layout,
      redirect: '/dashboard',
      children: [
        {
          path: 'dashboard',
          name: 'Dashboard',
          component: () => import('../views/Dashboard.vue'),
          meta: { titleKey: 'menu.dashboard' },
        },
        {
          path: 'products',
          name: 'Products',
          component: () => import('../views/Products.vue'),
          meta: { titleKey: 'menu.products' },
        },
        {
          // 深链入口：由在制品列表跳转，不出现在侧边栏
          path: 'trace/:sn',
          name: 'Trace',
          component: () => import('../views/Trace.vue'),
          meta: { titleKey: 'menu.trace' },
        },
        {
          path: 'records',
          name: 'Records',
          component: () => import('../views/Records.vue'),
          meta: { titleKey: 'menu.records' },
        },
        {
          path: 'repairs',
          name: 'Repairs',
          component: () => import('../views/Repairs.vue'),
          meta: { titleKey: 'menu.repairs' },
        },
        {
          path: 'clients',
          name: 'Clients',
          component: () => import('../views/Clients.vue'),
          meta: { titleKey: 'menu.clients' },
        },
        {
          path: 'sessions',
          name: 'Sessions',
          component: () => import('../views/Sessions.vue'),
          meta: { titleKey: 'menu.sessions' },
        },
        {
          path: 'configs',
          name: 'RouteConfig',
          component: () => import('../views/RouteConfig.vue'),
          meta: { titleKey: 'menu.configs' },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

// 管理端所有页面均需登录；未登录跳转登录页并携带回跳地址
router.beforeEach(async (to) => {
  if (to.meta.public) return
  // Lazy-resolve the auth store so we don't capture a singleton before the
  // app has finished registering plugins.
  const { state, isLoggedIn, refresh } = useAuth()
  if (state.token && !state.checked) {
    await refresh()
  }
  if (!isLoggedIn.value) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
})

router.afterEach((to) => {
  applyRouteTitle(to)
})

export default router
