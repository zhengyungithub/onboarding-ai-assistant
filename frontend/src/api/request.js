import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'

const request = axios.create({
  baseURL: '/api',
  timeout: 30000,
})

// 请求拦截器：附加 Token
request.interceptors.request.use(
  (config) => {
    const token = localStorage.getItem('token')
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error) => Promise.reject(error)
)

// 会话过期提示去重（并发多个请求同时 401 时只提示一次）
let sessionExpiredShown = false

// 响应拦截器：统一错误处理
request.interceptors.response.use(
  (response) => response.data,
  (error) => {
    const status = error.response?.status
    const detail = error.response?.data?.detail
    const url = error.config?.url || ''
    // 登录接口的 401 是「用户名或密码错误」，不属于会话过期
    const isLoginUrl = url.includes('/auth/login')

    if (status === 401 && isLoginUrl) {
      ElMessage.error(detail || '用户名或密码错误')
    } else if (status === 401) {
      if (!sessionExpiredShown) {
        sessionExpiredShown = true
        ElMessage.error('登录已过期，请重新登录')
        setTimeout(() => { sessionExpiredShown = false }, 2000)
      }
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      if (router.currentRoute.value.path !== '/login') {
        // 整页跳转，确保 Pinia 内存中的用户态一并重置
        window.location.replace('/login')
      }
    } else {
      // 预览类请求允许静默失败（config.silent），由页面自己降级提示，不打扰用户
      if (!error.config?.silent) {
        ElMessage.error(detail || '请求失败')
      }
    }
    return Promise.reject(error)
  }
)

export default request
