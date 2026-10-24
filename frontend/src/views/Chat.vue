<template>
  <div class="chat-container">
    <!-- 工具栏：多选模式开关 + 批量操作 -->
    <div class="chat-toolbar">
      <el-checkbox v-model="selectMode" @change="onSelectModeChange">
        <span style="font-size: 13px">删除对话</span>
      </el-checkbox>
      <template v-if="selectMode">
        <el-checkbox
          v-model="selectAll"
          :indeterminate="isIndeterminate"
          @change="onSelectAll"
          style="margin-left: 16px"
        >
          <span style="font-size: 13px">全选</span>
        </el-checkbox>
        <el-button
          type="danger"
          size="small"
          :disabled="selectedIds.length === 0"
          @click="handleBatchDelete"
          style="margin-left: 12px"
        >
          删除选中 ({{ selectedIds.length }})
        </el-button>
        <el-button size="small" @click="selectMode = false" style="margin-left: 8px">
          取消
        </el-button>
      </template>
      <!-- Agent 过程开关（默认关闭 → 与改造前完全一致；开启后走 Agent 路径并展示过程） -->
      <div v-if="!selectMode" class="agent-toggle">
        <el-switch v-model="agentMode" size="small" @change="onAgentModeChange" />
        <span class="agent-toggle-label">Agent 过程</span>
        <el-tooltip
          placement="bottom"
          content="开启后走 Agent 路径，可折叠查看「分诊 → 工具调用 → 引用校验」的完整过程；关闭时与原来完全一致"
        >
          <el-icon class="agent-toggle-help"><QuestionFilled /></el-icon>
        </el-tooltip>
      </div>
      <el-button
        v-if="!selectMode && chatStore.messages.length > 0"
        size="small"
        text
        type="danger"
        @click="handleClearAll"
        style="margin-left: auto"
      >
        清空全部对话
      </el-button>
    </div>

    <div class="chat-messages" ref="messagesContainer" v-loading="chatStore.loading">
      <!-- 空状态提示 -->
      <div v-if="chatStore.messages.length === 0" class="empty-state">
        <el-icon :size="48" color="#c0c4cc"><ChatDotRound /></el-icon>
        <p>输入问题开始对话，我会基于知识库回答</p>
      </div>

      <div
        v-for="msg in chatStore.messages"
        :key="msg.id"
        class="message-row"
        :class="[msg.role, { 'is-selected': selectedIds.includes(msg.id) }]"
      >
        <!-- 多选模式下的勾选框 -->
        <el-checkbox
          v-if="selectMode"
          :model-value="selectedIds.includes(msg.id)"
          @change="(val) => toggleSelect(msg.id, val)"
          class="select-checkbox"
        />

        <el-avatar :size="36" :icon="msg.role === 'user' ? User : ChatDotRound" :class="msg.role" />
        <div class="message-bubble">
          <div class="message-content" v-html="renderMarkdown(msg.content)"></div>

          <!-- Agent 过程折叠面板（仅开启 Agent 模式的那一轮回答才有） -->
          <el-collapse
            v-if="msg.role === 'assistant' && msg.trace && msg.trace.length"
            class="trace-panel"
          >
            <el-collapse-item name="trace">
              <template #title>
                <span class="trace-title">Agent 过程</span>
                <span v-if="toolCount(msg.trace)" class="trace-count">
                  {{ toolCount(msg.trace) }} 次工具调用
                </span>
              </template>
              <div class="trace-list">
                <div
                  v-for="(step, idx) in msg.trace"
                  :key="idx"
                  class="trace-item"
                  :class="'trace-' + step.kind"
                >
                  <span class="trace-dot"></span>
                  <div class="trace-body">
                    <template v-if="step.kind === 'intent'">
                      分诊为「{{ step.label }}」
                    </template>
                    <template v-else-if="step.kind === 'step'">{{ step.text }}</template>
                    <template v-else-if="step.kind === 'tool'">
                      调用 <code class="trace-tool-name">{{ step.tool }}</code>
                      <span class="trace-meta">
                        {{ step.ok ? '成功' : '未命中' }}
                        <template v-if="step.ms"> · {{ Math.round(step.ms) }} ms</template>
                      </span>
                      <div v-if="step.summary" class="trace-summary">{{ step.summary }}</div>
                    </template>
                    <template v-else-if="step.kind === 'citation'">
                      引用校验：剔除 {{ step.dropped || 0 }} 条无支撑来源
                      <span
                        v-if="step.coverage !== null && step.coverage !== undefined"
                        class="trace-meta"
                      >
                        · 硬信息支撑 {{ Math.round(step.coverage * 100) }}%
                      </span>
                    </template>
                    <template v-else-if="step.kind === 'fallback'">
                      已回落固定管线（Agent 本轮未产出结果）
                    </template>
                    <template v-else-if="step.kind === 'error'">
                      过程中出错：{{ step.message }}
                    </template>
                    <template v-else-if="step.kind === 'done'">
                      完成 · 共 {{ step.tool_calls || 0 }} 次工具调用
                      <span v-if="step.duration_ms" class="trace-meta">
                        · {{ step.duration_ms }} ms
                      </span>
                    </template>
                  </div>
                </div>
              </div>
            </el-collapse-item>
          </el-collapse>

          <div v-if="msg.references && msg.references.length" class="references">
            <div class="ref-title">
              引用来源：
              <span
                v-if="msg.citation_coverage !== null && msg.citation_coverage !== undefined"
                class="ref-coverage"
                :title="'回答中的数字、系统名等硬信息，有 ' + Math.round(msg.citation_coverage * 100) + '% 能在引用原文中找到'"
              >
                已核验 {{ Math.round(msg.citation_coverage * 100) }}%
              </span>
            </div>
            <el-tooltip
              v-for="ref in msg.references"
              :key="ref.item_id"
              :content="verifyTip(ref)"
              placement="top"
            >
              <el-tag
                size="small"
                effect="plain"
                class="ref-tag"
                :class="{
                  'ref-verified': ref.verified === true,
                  'ref-partial': ref.verified === 'partial',
                }"
                @click="goToItem(ref)"
              >
                <el-icon v-if="ref.verified === true" class="ref-icon"><CircleCheck /></el-icon>
                <el-icon v-else-if="ref.verified === 'partial'" class="ref-icon"><Warning /></el-icon>
                {{ ref.title }}
              </el-tag>
            </el-tooltip>
          </div>

          <!-- AI 回答的反馈按钮（仅助手消息且非多选模式时显示） -->
          <div v-if="msg.role === 'assistant' && !selectMode && msg.content" class="feedback-bar">
            <el-button
              size="small"
              text
              :type="msg.feedback === 'useful' ? 'success' : 'info'"
              :icon="CaretTop"
              @click="handleFeedback(msg, 'useful')"
            >
              {{ msg.feedback === 'useful' ? '已赞' : '有用' }}
            </el-button>
            <el-button
              size="small"
              text
              :type="msg.feedback === 'useless' ? 'danger' : 'info'"
              :icon="CaretBottom"
              @click="handleFeedback(msg, 'useless')"
            >
              {{ msg.feedback === 'useless' ? '已踩' : '没用' }}
            </el-button>
          </div>
        </div>
      </div>

      <!-- 流式加载中 -->
      <div v-if="loading" class="message-row assistant">
        <el-avatar :size="36" :icon="ChatDotRound" class="assistant" />
        <div class="message-bubble">
          <el-icon class="is-loading" :size="20"><Loading /></el-icon>
        </div>
      </div>
    </div>

    <div class="chat-input">
      <el-input
        v-model="input"
        type="textarea"
        :rows="2"
        placeholder="输入你的问题，按 Enter 发送，Shift+Enter 换行"
        @keydown.enter.exact.prevent="handleSend"
        :disabled="loading"
      />
      <el-button type="primary" :icon="Promotion" :loading="loading" @click="handleSend">发送</el-button>
    </div>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useChatStore } from '@/stores/chat'
import { chatStream, chatAgentStream } from '@/api/chat'
import {
  User, ChatDotRound, Loading, Promotion,
  CaretTop, CaretBottom, CircleCheck, Warning, QuestionFilled,
} from '@element-plus/icons-vue'
import { ElMessageBox, ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import 'highlight.js/styles/github.css'

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true,
  highlight: (str, lang) => {
    const hljs = require('highlight.js')
    if (lang && hljs.getLanguage(lang)) {
      try {
        return '<pre class="hljs"><code>' + hljs.highlight(str, { language: lang }).value + '</code></pre>'
      } catch (e) {}
    }
    return '<pre class="hljs"><code>' + md.utils.escapeHtml(str) + '</code></pre>'
  },
})

const userStore = useUserStore()
const chatStore = useChatStore()
const router = useRouter()
const input = ref('')
const loading = ref(false)
const messagesContainer = ref(null)

// 点击引用来源 → 按来源类型跳转到对应管理页并高亮
// document → 资料入库；flow_node → 流程配置；knowledge_item → 知识库管理
const DOC_EXT_RE = /\.(docx?|xlsx?|pdf|txt|md|pptx?)$/i
function goToItem(reference) {
  if (!reference?.item_id) return
  // 兼容旧消息（引用没有 source_type）：按标题后缀猜测
  const type =
    reference.source_type ||
    (DOC_EXT_RE.test(reference.title || '') ? 'document' : 'knowledge_item')
  if (type === 'document') {
    router.push({ path: '/admin/ingest', query: { highlight: reference.item_id } })
  } else if (type === 'flow_node') {
    router.push({ path: '/admin/flows', query: { highlight: reference.item_id } })
  } else {
    router.push({ path: '/admin/items', query: { highlight: reference.item_id } })
  }
}

// 引用核验徽标（改造 3）：true=已核验 / "partial"=部分支持 / 无标记=未校验
function verifyTip(reference) {
  if (reference?.verified === true) return '已核验：该来源原文确实支撑了回答内容'
  if (reference?.verified === 'partial') return '部分支持：回答只用到该来源的一部分内容'
  if (reference?.verified === false) return '未采用：该来源未被回答引用'
  return '未校验'
}

// ---------------------------------------------------------------------------
// Agent 过程模式（前端入口）
// 默认关闭 → 完全走原 /api/chat/stream，行为与改造前一字不差；
// 开启 → 走 /api/chat/agent/stream，多出「分诊 / 工具调用 / 引用校验」过程事件，
//        并在回答下方给出可折叠的过程面板。开关记在 localStorage，刷新后保持。
// ---------------------------------------------------------------------------
const AGENT_MODE_KEY = 'hr-ai:agent-trace-enabled'
const agentMode = ref(false)
try {
  agentMode.value = localStorage.getItem(AGENT_MODE_KEY) === '1'
} catch (e) {}

function onAgentModeChange(val) {
  try {
    localStorage.setItem(AGENT_MODE_KEY, val ? '1' : '0')
  } catch (e) {}
  if (val) {
    ElMessage.info('已开启 Agent 过程，本轮回答将展示分诊、工具调用与引用校验')
  }
}

// 过程条目中的工具调用次数（折叠标题的摘要）
function toolCount(trace) {
  if (!Array.isArray(trace)) return 0
  return trace.filter((s) => s && s.kind === 'tool').length
}

// 多选模式
const selectMode = ref(false)
const selectedIds = ref([])

const selectAll = computed({
  get: () => selectedIds.value.length > 0 && selectedIds.value.length === chatStore.messages.length,
  set: (val) => {},
})
const isIndeterminate = computed(
  () => selectedIds.value.length > 0 && selectedIds.value.length < chatStore.messages.length
)

onMounted(async () => {
  await chatStore.loadMessages()
  nextTick(() => scrollToBottom())
})

// 排版修复：LLM 有时把「二、xxx」章节标题或「1. 」列表项直接接在上一句末尾，
// Markdown 会把它们挤进同一段。这里在渲染前补换行（跳过代码块）。
// 三条守卫：① 断点只认句末标点，避免把「步骤一、确认人数」这种正文误拆；
// ② 标题常写成 **二、xxx**，断行要落在 ** 之前，否则会把加粗标记拆散；
// ③ 有序列表前面的 ** 属于上一句的收尾加粗，要留在原位。
const CN_SECTION = '[一二三四五六七八九十]{1,3}、'
const SENTENCE_END = '[。！？；：…”」』）)!?;:]'

function normalizeMarkdownLayout(text) {
  const t = (text || '').replace(/\r\n/g, '\n')
  const parts = t.split(/(```[\s\S]*?```)/g)
  return parts
    .map((seg, i) => {
      if (i % 2 === 1) return seg // 代码块原样保留
      let s = seg

      // ① 章节标题（一、二、…）：接在句末标点之后时补成独立段落，** 随标题一起换行
      s = s.replace(new RegExp(`(${SENTENCE_END})(\\*{0,2})(${CN_SECTION})`, 'g'), '$1\n\n$2$3')

      // ② 有序列表项（1. 2. …）：接在句末标点（或上一句收尾的 **）之后时补换行
      s = s.replace(new RegExp(`(${SENTENCE_END}|\\*\\*)(\\*{0,2})(\\d{1,2}\\. )`, 'g'), '$1$2\n\n$3')

      // ③ 缩进的子列表（「   - xxx」）被挤在上一行末尾时补换行
      s = s.replace(/([^\n])([ \t]{2,})([*\-] )/g, '$1\n$2$3')

      return s
    })
    .join('')
}

function renderMarkdown(text) {
  return md.render(normalizeMarkdownLayout(text))
}

function scrollToBottom() {
  nextTick(() => {
    if (messagesContainer.value) {
      messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
    }
  })
}

function onSelectModeChange(val) {
  if (!val) {
    selectedIds.value = []
  }
}

function toggleSelect(id, val) {
  if (val) {
    if (!selectedIds.value.includes(id)) {
      selectedIds.value.push(id)
    }
  } else {
    selectedIds.value = selectedIds.value.filter((i) => i !== id)
  }
}

function onSelectAll(val) {
  if (val) {
    selectedIds.value = chatStore.messages.map((m) => m.id)
  } else {
    selectedIds.value = []
  }
}

async function handleSend() {
  const question = input.value.trim()
  if (!question || loading.value) return

  // 添加用户消息占位
  chatStore.addLocalMessage({ role: 'user', content: question })
  input.value = ''
  loading.value = true
  scrollToBottom()

  // 添加助手消息占位
  const assistantMsg = chatStore.addLocalMessage({ role: 'assistant', content: '', references: [] })

  // 开启「Agent 过程」走 Agent 路径；否则下面这段与改造前完全一致
  if (agentMode.value) {
    await runAgentStream(question, assistantMsg)
    return
  }

  let firstChunk = true
  let fullContent = ''

  await chatStream(
    question,
    userStore.token,
    (data) => {
      if (firstChunk) {
        firstChunk = false
        try {
          const parsed = JSON.parse(data)
          if (parsed.references) {
            chatStore.updateLocalMessage(assistantMsg.id, { references: parsed.references })
          } else {
            fullContent += data
          }
        } catch (e) {
          fullContent += data
        }
      } else {
        fullContent += data
      }
      chatStore.updateLocalMessage(assistantMsg.id, { content: fullContent })
      scrollToBottom()
    },
    () => {
      loading.value = false
      // 流式结束，重新从后端加载（替换临时占位消息为带真实 id 的消息）
      chatStore.loadMessages()
    },
    (err) => {
      chatStore.updateLocalMessage(assistantMsg.id, { content: '出错了：' + err.message })
      loading.value = false
    }
  )
}

// ---------------------------------------------------------------------------
// Agent 事件流（仅在开启「Agent 过程」时使用）
// 过程事件在正文生成前一次性下发，正文随后逐段流出；
// 引用会发两次：首包是候选引用（用户立刻能看到来源），生成结束后是校验过滤后的版本。
// ---------------------------------------------------------------------------
async function runAgentStream(question, assistantMsg) {
  let fullContent = ''
  const trace = []

  const pushTrace = (step) => {
    trace.push(step)
    chatStore.updateLocalMessage(assistantMsg.id, { trace: [...trace] })
  }

  await chatAgentStream(question, userStore.token, {
    onTrace(step) {
      pushTrace(step)
      scrollToBottom()
    },
    onText(text) {
      fullContent += text
      chatStore.updateLocalMessage(assistantMsg.id, { content: fullContent })
      scrollToBottom()
    },
    onReferences(refs) {
      if (refs && refs.length) {
        chatStore.updateLocalMessage(assistantMsg.id, { references: refs })
      }
    },
    onCitationCheck(evt) {
      pushTrace({
        kind: 'citation',
        dropped: evt.dropped || 0,
        decision: evt.decision,
        coverage: evt.coverage,
      })
      chatStore.updateLocalMessage(assistantMsg.id, { citation_coverage: evt.coverage })
    },
    async onDone() {
      loading.value = false
      // 与固定管线一致：流结束后重拉消息，把临时占位替换为带真实 id 的消息
      await chatStore.loadMessages()
      attachTraceToLatest(trace)
    },
    onError(err) {
      chatStore.updateLocalMessage(assistantMsg.id, { content: '出错了：' + err.message })
      loading.value = false
    },
  })
}

// 重拉消息后，把本轮过程记录挂回最新一条助手消息
// （过程只在本次流式期间存在于内存，刷新后不可见；完整轨迹在后端 agent_runs 表里）
function attachTraceToLatest(trace) {
  if (!trace.length) return
  const list = chatStore.messages
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].role === 'assistant') {
      list[i].trace = [...trace]
      break
    }
  }
}

async function handleBatchDelete() {
  if (selectedIds.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `确定删除选中的 ${selectedIds.value.length} 条对话吗？`,
      '批量删除',
      { type: 'warning' }
    )
    await chatStore.batchDelete(selectedIds.value)
    ElMessage.success('已删除')
    selectedIds.value = []
    selectMode.value = false
  } catch (e) {}
}

async function handleClearAll() {
  try {
    await ElMessageBox.confirm('确定清空所有对话记录吗？', '提示', { type: 'warning' })
    await chatStore.clearAll()
    ElMessage.success('已清空')
  } catch (e) {}
}

async function handleFeedback(msg, feedback) {
  // 临时消息（id 以 temp_ 开头）不能反馈
  if (msg.id.startsWith('temp_')) {
    ElMessage.warning('请等待回答完成后再反馈')
    return
  }
  // 再次点击相同反馈则取消
  if (msg.feedback === feedback) {
    try {
      await chatStore.setFeedback(msg.id, null)
      ElMessage.success('已取消反馈')
    } catch (e) {}
    return
  }
  // 点「没用」：弹窗让用户填写原因（选填），原因会同步到评测页的待处理负反馈
  if (feedback === 'useless') {
    let reason = ''
    try {
      const { value } = await ElMessageBox.prompt(
        '这条回答哪里没用？您的反馈会帮助管理员优化知识库（可不填）',
        '反馈「没用」原因',
        {
          inputType: 'textarea',
          inputPlaceholder: '例如：漏了部分内容 / 引用了不相关的资料 / 答非所问 / 内容过时……',
          confirmButtonText: '提交反馈',
          cancelButtonText: '取消',
          inputValidator: (v) => (v || '').length <= 500 || '原因最多 500 字',
        },
      )
      reason = value || ''
    } catch (e) {
      return // 用户取消
    }
    try {
      await chatStore.setFeedback(msg.id, 'useless', reason)
      ElMessage.success('感谢反馈，我们会持续改进')
    } catch (e) {}
    return
  }
  try {
    await chatStore.setFeedback(msg.id, 'useful')
    ElMessage.success('感谢反馈：有用')
  } catch (e) {}
}
</script>

<style scoped>
.chat-container {
  display: flex;
  flex-direction: column;
  height: calc(100vh - 120px);
  background: #fff;
  border-radius: 12px;
  overflow: hidden;
}
.chat-toolbar {
  display: flex;
  align-items: center;
  padding: 10px 24px;
  border-bottom: 1px solid #f0f0f0;
  background: #fafafa;
}
.agent-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-left: 20px;
  font-size: 13px;
  color: #6b7280;
}
.agent-toggle-help {
  color: #9ca3af;
  cursor: help;
}
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 24px;
  display: flex;
  flex-direction: column;
  gap: 20px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  gap: 16px;
  color: #9ca3af;
}
.empty-state p {
  font-size: 14px;
}
.message-row {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 8px;
  border-radius: 8px;
  transition: background 0.2s;
}
.message-row.is-selected {
  background: #eef2ff;
}
.message-row.user {
  flex-direction: row-reverse;
}
.select-checkbox {
  align-self: center;
}
.message-row .user {
  background: #6366f1;
  color: #fff;
}
.message-row .assistant {
  background: #f3f4f6;
  color: #1f2937;
}
.message-bubble {
  max-width: 70%;
  padding: 12px 16px;
  border-radius: 12px;
  line-height: 1.6;
}
.message-row.user .message-bubble {
  background: #6366f1;
  color: #fff;
}
.message-row.assistant .message-bubble {
  background: #f3f4f6;
  color: #1f2937;
}
/* 回答正文（Markdown 渲染结果）排版：段落/列表间距统一，避免默认 1em 段距把回答拉得很散 */
.message-content {
  line-height: 1.7;
  word-break: break-word;
}
.message-content :deep(p) {
  margin: 0 0 8px;
}
.message-content :deep(p:last-child) {
  margin-bottom: 0;
}
.message-content :deep(ol),
.message-content :deep(ul) {
  margin: 4px 0 8px;
  padding-left: 22px;
}
.message-content :deep(ol:last-child),
.message-content :deep(ul:last-child) {
  margin-bottom: 0;
}
.message-content :deep(li) {
  margin: 3px 0;
}
/* 列表被拆成「松散列表」后，每项会被包一层 <p>，这里把多余段距收掉 */
.message-content :deep(li > p) {
  margin: 0 0 4px;
}
.message-content :deep(li > p:last-child) {
  margin-bottom: 0;
}
.message-content :deep(strong) {
  font-weight: 600;
}
.message-content :deep(pre) {
  margin: 8px 0;
  padding: 10px 12px;
  background: #1f2937;
  color: #f3f4f6;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 13px;
}
.message-content :deep(code) {
  font-family: 'JetBrains Mono', Consolas, Monaco, monospace;
  font-size: 0.92em;
}
.message-content :deep(p > code),
.message-content :deep(li > code) {
  padding: 1px 5px;
  background: #e5e7eb;
  border-radius: 4px;
}
.message-content :deep(table) {
  margin: 8px 0;
  border-collapse: collapse;
  font-size: 13px;
}
.message-content :deep(th),
.message-content :deep(td) {
  padding: 6px 10px;
  border: 1px solid #e5e7eb;
}
.message-content :deep(th) {
  background: #eef2ff;
  font-weight: 600;
}
/* Agent 过程折叠面板（只出现在开启「Agent 过程」时的那一轮回答上） */
.trace-panel {
  margin-top: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
  overflow: hidden;
}
.trace-panel :deep(.el-collapse-item__header) {
  height: auto;
  padding: 6px 12px;
  font-size: 12px;
  line-height: 1.6;
  border-bottom: none;
}
.trace-panel :deep(.el-collapse-item__wrap) {
  border-bottom: none;
}
.trace-panel :deep(.el-collapse-item__content) {
  padding: 4px 12px 12px;
}
.trace-title {
  font-size: 12px;
  color: #4b5563;
}
.trace-count {
  margin-left: 8px;
  font-size: 12px;
  color: #9ca3af;
}
.trace-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.trace-item {
  display: flex;
  gap: 8px;
  font-size: 12px;
  color: #4b5563;
  line-height: 1.6;
}
.trace-dot {
  flex-shrink: 0;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #d1d5db;
  margin-top: 6px;
}
.trace-tool .trace-dot {
  background: #6366f1;
}
.trace-citation .trace-dot {
  background: #67c23a;
}
.trace-fallback .trace-dot,
.trace-error .trace-dot {
  background: #e6a23c;
}
.trace-body {
  flex: 1;
  min-width: 0;
}
.trace-tool-name {
  padding: 1px 4px;
  border-radius: 3px;
  background: #f3f4f6;
  font-size: 11px;
  color: #6366f1;
}
.trace-meta {
  color: #9ca3af;
}
.trace-summary {
  color: #9ca3af;
  word-break: break-all;
}
.references {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid #e5e7eb;
}
.ref-title {
  font-size: 12px;
  color: #6b7280;
  margin-bottom: 8px;
}
.ref-tag {
  margin-right: 8px;
  margin-bottom: 4px;
  cursor: pointer;
  transition: all 0.2s;
}
.ref-tag:hover {
  color: #6366f1;
  border-color: #6366f1;
  background: #eef2ff;
}
/* 引用核验徽标：已核验=绿、部分支持=橙（颜色只做区分，不改变原有点击跳转行为） */
.ref-tag.ref-verified {
  border-color: #67c23a;
}
.ref-tag.ref-partial {
  border-color: #e6a23c;
}
.ref-icon {
  margin-right: 2px;
  vertical-align: -2px;
}
.ref-coverage {
  margin-left: 6px;
  font-size: 12px;
  font-weight: normal;
  color: #67c23a;
}
.feedback-bar {
  margin-top: 10px;
  display: flex;
  gap: 8px;
}
.chat-input {
  display: flex;
  gap: 12px;
  padding: 16px 24px;
  border-top: 1px solid #e5e7eb;
  background: #fafafa;
}
.chat-input :deep(.el-textarea__inner) {
  border-radius: 8px;
}
</style>
