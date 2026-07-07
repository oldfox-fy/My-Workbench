import { createRouter, createWebHashHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/chat'
  },
  {
    path: '/chat',
    component: () => import('@/views/ChatWindow.vue')
  },
  {
    path: '/chat/:id',
    name: 'chat',
    component: () => import('@/views/ChatWindow.vue')
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/KnowledgeView.vue')
  },
  {
    path: '/knowledge/graph',
    name: 'knowledge-graph',
    component: () => import('@/views/KbGraphView.vue')
  },
]

const router = createRouter({
  history: createWebHashHistory(),
  routes
})
export default router