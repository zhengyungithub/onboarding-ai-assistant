import { defineStore } from 'pinia'
import { ref } from 'vue'
import { login as loginApi, getMe } from '@/api/auth'

export const useUserStore = defineStore('user', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user = ref(JSON.parse(localStorage.getItem('user') || 'null'))

  async function login(username, password) {
    const res = await loginApi({ username, password })
    token.value = res.access_token
    user.value = res.user
    localStorage.setItem('token', res.access_token)
    localStorage.setItem('user', JSON.stringify(res.user))
    return res
  }

  async function fetchUser() {
    const res = await getMe()
    user.value = res
    localStorage.setItem('user', JSON.stringify(res))
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  // 角色：user 普通用户 / admin 管理员 / super_admin 超管
  const isAdmin = () => user.value?.role === 'admin' || user.value?.role === 'super_admin'
  const isStaff = () => isAdmin()
  const isSuperAdmin = () => user.value?.role === 'super_admin'
  const roleLabel = () =>
    ({ super_admin: '超级管理员', admin: '管理员', user: '普通用户' })[user.value?.role] || ''

  return { token, user, login, fetchUser, logout, isAdmin, isStaff, isSuperAdmin, roleLabel }
})
