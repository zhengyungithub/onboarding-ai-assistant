import request from './request'

// 登录
export function login(data) {
  return request.post('/auth/login', data)
}

// 获取当前用户
export function getMe() {
  return request.get('/auth/me')
}

// 获取 API Key 配置
export function getApiKeys() {
  return request.get('/auth/api-keys')
}

// 更新 API Key 配置
export function updateApiKeys(data) {
  return request.put('/auth/api-keys', data)
}

// ===== 管理员用户管理 =====

// 获取用户列表（仅管理员）
export function listUsers() {
  return request.get('/auth/users')
}

// 创建用户（仅管理员）
export function createUser(data) {
  return request.post('/auth/users', data)
}

// 更新用户（仅管理员）
export function updateUser(userId, data) {
  return request.put(`/auth/users/${userId}`, data)
}

// 删除用户（仅管理员）
export function deleteUser(userId) {
  return request.delete(`/auth/users/${userId}`)
}

// ===== 忘记密码 =====

// 发送重置密码验证码
export function forgotPassword(data) {
  return request.post('/auth/forgot-password', data)
}

// 重置密码
export function resetPassword(data) {
  return request.post('/auth/reset-password', data)
}
