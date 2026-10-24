import request from './request'

// 任务列表
export function listTasks(params) {
  return request.get('/tasks', { params })
}

// 到期视图：逾期 / 今日 / 未来 N 天（合并任务与备忘录）
export function getAgenda(params) {
  return request.get('/tasks/agenda', { params })
}

// 创建任务
export function createTask(data) {
  return request.post('/tasks', data)
}

// 任务详情
export function getTask(id) {
  return request.get(`/tasks/${id}`)
}

// 更新任务
export function updateTask(id, data) {
  return request.put(`/tasks/${id}`, data)
}

// 删除任务
export function deleteTask(id) {
  return request.delete(`/tasks/${id}`)
}

// 标记节点完成（门禁节点需要传 remark 作为过闸说明）
export function completeNode(taskId, nodeId, payload = null) {
  return request.post(`/tasks/${taskId}/nodes/${nodeId}/complete`, payload)
}

// 标记节点未完成
export function incompleteNode(taskId, nodeId) {
  return request.post(`/tasks/${taskId}/nodes/${nodeId}/incomplete`)
}

// 记一笔进展（只追加，不覆盖历史）
export function addProgressLog(taskId, data) {
  return request.post(`/tasks/${taskId}/logs`, data)
}

// 查询进展流水（按时间倒序，可按节点过滤）
export function listProgressLogs(taskId, params) {
  return request.get(`/tasks/${taskId}/logs`, { params })
}
