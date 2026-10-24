import request from './request'

// 流程列表
export function listFlows(params) {
  return request.get('/flows', { params })
}

// 创建流程
export function createFlow(data) {
  return request.post('/flows', data)
}

// 获取流程详情
export function getFlow(id) {
  return request.get(`/flows/${id}`)
}

// 更新流程
export function updateFlow(id, data) {
  return request.put(`/flows/${id}`, data)
}

// 删除流程
export function deleteFlow(id) {
  return request.delete(`/flows/${id}`)
}

// 添加节点
export function addNode(flowId, data) {
  return request.post(`/flows/${flowId}/nodes`, data)
}

// 更新节点
export function updateNode(flowId, nodeId, data) {
  return request.put(`/flows/${flowId}/nodes/${nodeId}`, data)
}

// 删除节点
export function deleteNode(flowId, nodeId) {
  return request.delete(`/flows/${flowId}/nodes/${nodeId}`)
}

// 下一步提醒
export function getNextStep(flowId, currentNodeId) {
  return request.post('/flows/next-step', { flow_id: flowId, current_node_id: currentNodeId })
}
