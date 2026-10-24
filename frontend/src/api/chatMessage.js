import request from './request'

// 获取对话历史
export function getChatMessages(params) {
  return request.get('/chat/messages', { params })
}

// 批量删除对话消息
export function batchDeleteMessages(ids) {
  return request.delete('/chat/messages', { data: { ids } })
}

// 清空所有对话
export function clearAllMessages() {
  return request.delete('/chat/messages/all')
}

// 对 AI 回答反馈（有用/没用），点「没用」时可附带原因
export function setMessageFeedback(messageId, feedback, reason = null) {
  return request.patch(`/chat/messages/${messageId}/feedback`, { feedback, reason })
}
