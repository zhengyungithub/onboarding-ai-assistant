import { defineStore } from 'pinia'
import { ref } from 'vue'
import {
  getChatMessages,
  batchDeleteMessages,
  clearAllMessages,
  setMessageFeedback,
} from '@/api/chatMessage'

export const useChatStore = defineStore('chat', () => {
  // 当前用户的对话历史（从后端加载，按用户隔离）
  const messages = ref([])
  const loading = ref(false)

  // 从后端加载当前用户的对话历史
  async function loadMessages() {
    loading.value = true
    try {
      const res = await getChatMessages({ limit: 200 })
      messages.value = res.items || []
    } catch (e) {
      // 错误已在拦截器处理
    } finally {
      loading.value = false
    }
  }

  // 本地临时添加消息（用于流式显示占位）
  function addLocalMessage(msg) {
    const message = {
      id: 'temp_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8),
      role: msg.role,
      content: msg.content || '',
      references: msg.references || [],
      feedback: null,
      ...msg,
    }
    messages.value.push(message)
    return message
  }

  // 更新本地消息（流式过程中实时更新内容）
  function updateLocalMessage(id, updates) {
    const msg = messages.value.find((m) => m.id === id)
    if (msg) {
      Object.assign(msg, updates)
    }
  }

  // 批量删除对话消息（同步后端）
  async function batchDelete(ids) {
    if (!ids || ids.length === 0) return 0
    const res = await batchDeleteMessages(ids)
    messages.value = messages.value.filter((m) => !ids.includes(m.id))
    return res.deleted
  }

  // 清空当前用户所有对话（同步后端）
  async function clearAll() {
    const res = await clearAllMessages()
    messages.value = []
    return res.deleted
  }

  // 对 AI 回答进行反馈（有用/没用）
  async function setFeedback(messageId, feedback, reason = null) {
    const res = await setMessageFeedback(messageId, feedback, reason)
    const msg = messages.value.find((m) => m.id === messageId)
    if (msg) {
      msg.feedback = res.feedback
      msg.feedback_reason = res.feedback_reason ?? null
    }
    return res
  }

  return {
    messages,
    loading,
    loadMessages,
    addLocalMessage,
    updateLocalMessage,
    batchDelete,
    clearAll,
    setFeedback,
  }
})
