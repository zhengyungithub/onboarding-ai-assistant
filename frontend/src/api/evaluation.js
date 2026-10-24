import request from './request'

// 测试用例列表
export function listCases(params) {
  return request.get('/evaluation/cases', { params })
}

// 创建测试用例
export function createCase(data) {
  return request.post('/evaluation/cases', data)
}

// 删除测试用例
export function deleteCase(id) {
  return request.delete(`/evaluation/cases/${id}`)
}

// 一键评测
export function runEvaluation() {
  return request.post('/evaluation/run')
}

// 获取统计
export function getStats() {
  return request.get('/evaluation/stats')
}

// 提交反馈
export function submitFeedback(data) {
  return request.post('/evaluation/feedback', data)
}

// 待处理负反馈列表（被标记「没用」且尚未转成测试用例）
export function listPendingFeedback() {
  return request.get('/evaluation/pending-feedback')
}

// 把一条负反馈转为测试用例
export function feedbackToCase(data) {
  return request.post('/evaluation/feedback-to-case', data)
}

// 引用质量统计（改造 3 Verifier 的度量出口）
// scope: me=只看自己；all=全站（仅超管）
export function getCitationStats(params) {
  return request.get('/evaluation/citation-stats', { params })
}
