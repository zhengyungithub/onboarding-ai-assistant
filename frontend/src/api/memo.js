import request from './request'

// 备忘录列表
export function listMemos(params) {
  return request.get('/memos', { params })
}

// 创建备忘录
export function createMemo(data) {
  return request.post('/memos', data)
}

// 更新备忘录
export function updateMemo(id, data) {
  return request.put(`/memos/${id}`, data)
}

// 切换完成状态
export function toggleMemo(id) {
  return request.patch(`/memos/${id}/toggle`)
}

// 删除备忘录
export function deleteMemo(id) {
  return request.delete(`/memos/${id}`)
}

// ---------- 文件夹 ----------

// 文件夹列表
export function listFolders() {
  return request.get('/memo-folders')
}

// 新建文件夹
export function createFolder(data) {
  return request.post('/memo-folders', data)
}

// 重命名文件夹
export function renameFolder(id, data) {
  return request.put(`/memo-folders/${id}`, data)
}

// 删除文件夹（其中备忘录移入未分组）
export function deleteFolder(id) {
  return request.delete(`/memo-folders/${id}`)
}
