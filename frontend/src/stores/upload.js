import { defineStore } from 'pinia'
import { ElNotification } from 'element-plus'
import request from '@/api/request'

/**
 * 全局上传仓库：上传请求独立于页面组件存在，
 * 用户切换页面后上传依然继续，完成后通过铃铛红点 + 通知提示。
 */
export const useUploadStore = defineStore('upload', {
  state: () => ({
    // { id, filename, progress, status: 'uploading'|'success'|'error', error, finishedAt }
    tasks: [],
    // 有已完成（成功或失败）但用户尚未查看的结果 → 铃铛显示红点
    hasUnseenResult: false,
  }),
  getters: {
    uploadingCount: (s) => s.tasks.filter((t) => t.status === 'uploading').length,
    activeTasks: (s) => s.tasks.filter((t) => t.status === 'uploading'),
    finishedTasks: (s) => s.tasks.filter((t) => t.status !== 'uploading'),
  },
  actions: {
    startUpload(file, extra = {}) {
      const task = {
        id: Date.now() + '-' + Math.random().toString(36).slice(2, 8),
        filename: file.name,
        progress: 0,
        status: 'uploading',
        error: '',
        finishedAt: null,
      }
      this.tasks.unshift(task)

      const formData = new FormData()
      formData.append('file', file)
      // 附加可见范围等元数据（visibility / shared_user_ids 等）
      Object.entries(extra).forEach(([k, v]) => {
        if (v !== undefined && v !== null) formData.append(k, v)
      })
      return request
        .post('/source-documents/upload', formData, {
          headers: { 'Content-Type': 'multipart/form-data' },
          timeout: 300000, // Docling 首次加载模型耗时较长
          onUploadProgress: (e) => {
            if (e.total) {
              task.progress = Math.min(99, Math.round((e.loaded / e.total) * 100))
            }
          },
        })
        .then((res) => {
          task.progress = 100
          task.status = 'success'
          task.finishedAt = Date.now()
          this.hasUnseenResult = true
          ElNotification({
            title: '上传成功 🎉',
            message: `「${file.name}」已存入资料台账`,
            type: 'success',
            duration: 4500,
          })
          return res
        })
        .catch((e) => {
          task.status = 'error'
          task.error = e?.response?.data?.detail || '上传失败，请重试'
          task.finishedAt = Date.now()
          this.hasUnseenResult = true
          ElNotification({
            title: '上传失败 😿',
            message: `「${file.name}」${task.error}`,
            type: 'error',
            duration: 6000,
          })
          throw e
        })
    },
    markAllSeen() {
      this.hasUnseenResult = false
    },
    clearFinished() {
      this.tasks = this.tasks.filter((t) => t.status === 'uploading')
    },
  },
})
