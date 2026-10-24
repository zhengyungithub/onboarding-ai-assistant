import request from './request'

// 知识条目列表
export function listItems(params) {
  return request.get('/items', { params })
}

// 创建条目
export function createItem(data) {
  return request.post('/items', data)
}

// 获取条目详情
export function getItem(id) {
  return request.get(`/items/${id}`)
}

// 更新条目
export function updateItem(id, data) {
  return request.put(`/items/${id}`, data)
}

// 删除条目
export function deleteItem(id) {
  return request.delete(`/items/${id}`)
}

// 发布条目
export function publishItem(id) {
  return request.post(`/items/${id}/publish`)
}

// 文档解析（首次需加载 Docling 模型，超时设为 300s）
export function parseDocument(file) {
  const formData = new FormData()
  formData.append('file', file)
  return request.post('/documents/parse', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000,
  })
}
