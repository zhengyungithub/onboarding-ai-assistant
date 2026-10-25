import { createRouter, createWebHistory } from 'vue-router'
import { useUserStore } from '@/stores/user'

const routes = [
  {
    path: '/login',
    name: 'Login',
    component: () => import('@/views/Login.vue'),
    meta: { requiresAuth: false },
  },
  {
    path: '/forgot-password',
    name: 'ForgotPassword',
    component: () => import('@/views/ForgotPassword.vue'),
    meta: { requiresAuth: false },
  },
  {
    // 免登录确认页：确认人通常没有账号，链接点开即用（不带主布局）
    path: '/public/confirm/:token',
    name: 'PublicConfirm',
    component: () => import('@/views/PublicConfirm.vue'),
    meta: { requiresAuth: false, title: '材料确认' },
  },
  {
    path: '/',
    component: () => import('@/layouts/MainLayout.vue'),
    meta: { requiresAuth: true },
    children: [
      {
        path: '',
        name: 'Chat',
        component: () => import('@/views/Chat.vue'),
        meta: { title: 'AI 问答' },
      },
      {
        path: 'flow',
        name: 'Flow',
        component: () => import('@/views/Flow.vue'),
        meta: { title: '流程下一步' },
      },
      {
        path: 'tasks',
        name: 'Tasks',
        component: () => import('@/views/Tasks.vue'),
        meta: { title: '我的任务' },
      },
      {
        path: 'confirmations',
        name: 'Confirmations',
        component: () => import('@/views/Confirmations.vue'),
        meta: { title: '待我确认' },
      },
      {
        path: 'memos',
        name: 'Memos',
        component: () => import('@/views/Memos.vue'),
        meta: { title: '备忘录' },
      },
      {
        path: 'settings',
        name: 'Settings',
        component: () => import('@/views/Settings.vue'),
        meta: { title: 'API Key 配置', requiresStaff: true },
      },
      // 知识库管理、资料入库、流程配置（所有用户可访问，数据按 user_id 隔离）
      {
        path: 'admin/items',
        name: 'AdminItems',
        component: () => import('@/views/admin/Items.vue'),
        meta: { title: '知识库管理' },
      },
      {
        path: 'admin/ingest',
        name: 'AdminIngest',
        component: () => import('@/views/admin/Ingest.vue'),
        meta: { title: '资料入库' },
      },
      {
        path: 'admin/flows',
        name: 'AdminFlows',
        component: () => import('@/views/admin/Flows.vue'),
        meta: { title: '流程配置' },
      },
      // 后台路由（仅管理员及以上：管理员 / 超管）
      {
        path: 'admin/evaluate',
        name: 'AdminEvaluate',
        component: () => import('@/views/admin/Evaluate.vue'),
        meta: { title: '评测数据', requiresStaff: true },
      },
      {
        path: 'admin/users',
        name: 'AdminUsers',
        component: () => import('@/views/admin/Users.vue'),
        meta: { title: '用户管理', requiresStaff: true },
      },
    ],
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

// 全局路由守卫
router.beforeEach((to, from, next) => {
  const userStore = useUserStore()
  const token = userStore.token

  if (to.meta.requiresAuth !== false && !token) {
    // 需要登录但未登录
    next({ path: '/login' })
  } else if (to.path === '/login' && token) {
    // 已登录访问登录页
    next({ path: '/' })
  } else if (to.meta.requiresStaff && !userStore.isStaff()) {
    // 非管理员访问后台
    next({ path: '/' })
  } else if (to.path === '/' && userStore.isSuperAdmin()) {
    // 两级角色：超管没有业务菜单，落地页为用户管理
    next({ path: '/admin/users' })
  } else {
    next()
  }
})

export default router
