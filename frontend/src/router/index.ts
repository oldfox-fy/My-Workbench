import { createRouter, createWebHashHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { noAuth: true },
  },
  {
    path: '/register',
    name: 'register',
    component: () => import('@/views/RegisterView.vue'),
    meta: { noAuth: true },
  },
  {
    path: '/',
    redirect: '/chat',
  },
  {
    path: '/chat',
    component: () => import('@/views/ChatWindow.vue'),
  },
  {
    path: '/chat/:id',
    name: 'chat',
    component: () => import('@/views/ChatWindow.vue'),
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/KnowledgeView.vue'),
  },
  {
    path: '/knowledge/graph',
    name: 'knowledge-graph',
    component: () => import('@/views/KbGraphView.vue'),
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes,
})

// ── 路由守卫：登录校验 ──
router.beforeEach(async (to, _from, next) => {
  // 动态导入 auth store（避免循环依赖）
  const { useAuthStore } = await import('@/stores/auth')
  const authStore = useAuthStore()

  // 首次导航时初始化
  if (!authStore.initialized) {
    await authStore.init()
  }

  // 免登录页面
  if (to.meta.noAuth) {
    if (authStore.isLoggedIn && to.path !== '/login') {
      next('/chat')
    } else {
      next()
    }
    return
  }

  // 需要登录
  if (!authStore.isLoggedIn) {
    next('/login')
    return
  }

  next()
})

export default router
