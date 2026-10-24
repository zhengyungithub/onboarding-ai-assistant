import request from './request'

// 上传文档到台账（保存到本地数据库）
export function uploadDocument(file) {
  const formData = new FormData()
  formData.append('file', file)
  return request.post('/source-documents/upload', formData, {
    headers: { 'Content-Type': 'multipart/form-data' },
    timeout: 300000, // Docling 首次加载模型耗时较长
  })
}

// 获取文档台账列表
export function listDocuments(params) {
  return request.get('/source-documents', { params })
}

// 获取单个文档详情（含解析内容）
export function getDocument(id) {
  return request.get(`/source-documents/${id}`)
}

// 删除文档
export function deleteDocument(id) {
  return request.delete(`/source-documents/${id}`)
}

// 对文档执行 AI 抽取
export function extractDocument(id) {
  return request.post(`/source-documents/${id}/extract`)
}

// 标记文档为已抽取（入库成功后调用）
export function markExtracted(id, count) {
  return request.post(`/source-documents/${id}/mark-extracted`, { count })
}

// 修改文档可见范围（private 仅自己 | all 全员共享 | custom 定向共享）
export function updateDocumentVisibility(id, visibility, sharedUserIds = []) {
  return request.patch(`/source-documents/${id}/visibility`, {
    visibility,
    shared_user_ids: sharedUserIds,
  })
}

// 下载原始文件（返回 blob，前端触发下载）
export function downloadDocument(id) {
  return request.get(`/source-documents/${id}/download`, { responseType: 'blob' })
}

// 原版式预览取文件流：静默失败（页面自己降级为提取文本），不弹「请求失败」
export function fetchDocumentBlob(id) {
  return request.get(`/source-documents/${id}/download`, { responseType: 'blob', silent: true })
}

// 获取该文档所在版本链的全部版本（由旧到新）
export function listDocumentVersions(id) {
  return request.get(`/source-documents/${id}/versions`)
}

// 关联/解除文档与任务的绑定（taskId 传 null 表示解除）
export function bindDocumentTask(id, taskId) {
  return request.patch(`/source-documents/${id}/task`, { task_id: taskId })
}
