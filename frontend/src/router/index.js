import { createRouter, createWebHistory } from 'vue-router'

import Layout from '../layout/Layout.vue'
// 从而冻结一个过期的 store 引用
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
          meta: { titleKey: 'menu.configs', roles: ['admin'] },
        },
        {
          path: 'users',
          name: 'Users',
          component: () => import('../views/Users.vue'),
          meta: { titleKey: 'menu.users', roles: ['admin'] },
        },
      ],
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

router.beforeEach(async (to) => {
  if (to.meta.public) return
  const { state, isLoggedIn, refresh } = useAuth()
  if (state.token && !state.checked) {
    await refresh()
  }
  if (!isLoggedIn.value) {
    return { path: '/login', query: { redirect: to.fullPath } }
  }
  // 页面级角色守卫（按钮显隐只是体验层，真正的边界在后端 require_role）
  if (to.meta.roles && !to.meta.roles.includes(state.user?.role)) {
    return { path: '/dashboard' }
  }
})

router.afterEach((to) => {
  applyRouteTitle(to)
})

export default router

