import request from './request'

// ---- 文档维度：发起确认 / 查看进度 ----

// 对一份文档发起确认（user_ids 为确认人，message 为附言）
export function requestConfirmations(docId, userIds, message = '') {
  return request.post(`/source-documents/${docId}/confirmations`, {
    user_ids: userIds,
    message,
  })
}

// 文档的确认进度明细（含 summary 与每人状态）
export function getDocumentConfirmations(docId) {
  return request.get(`/source-documents/${docId}/confirmations`)
}

// ---- 确认人维度 ----

// 待我确认（status: pending / confirmed / rejected / all）
export function listMyConfirmations(params = {}) {
  return request.get('/confirmations/mine', { params })
}

// 待我确认条数（侧边栏徽标）
export function getPendingCount() {
  return request.get('/confirmations/pending-count')
}

// 可邀请确认的人（排除自己）
export function listConfirmContacts() {
  return request.get('/confirmations/contacts')
}

// 确认记录详情（含文档正文预览）
export function getConfirmation(id) {
  return request.get(`/confirmations/${id}`)
}

// 下载确认对应的原始文件（原版式预览也走这里，权限：被确认人/发起人/管理员）
export function downloadConfirmationDocument(id) {
  return request.get(`/confirmations/${id}/download`, { responseType: 'blob' })
}

// 原版式预览取文件流：静默失败，由页面降级为提取文本
export function fetchConfirmationBlob(id) {
  return request.get(`/confirmations/${id}/download`, { responseType: 'blob', silent: true })
}

// 提交确认结果：confirmed / rejected
export function respondConfirmation(id, action, comment = '') {
  return request.post(`/confirmations/${id}/respond`, { action, comment })
}

// 撤销一条待确认（发起人）
export function revokeConfirmation(id) {
  return request.post(`/confirmations/${id}/revoke`)
}

// 取回/补发免登录确认链接（发起人）
export function issueConfirmationLink(id) {
  return request.post(`/confirmations/${id}/link`)
}
