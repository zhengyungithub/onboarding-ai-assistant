<template>
  <!--
    免登录确认页：确认人往往没装这套系统，链接点开就能看、能表态。
    刻意不走 MainLayout / axios 拦截器——这里没有登录态，401 跳转逻辑不适用。
  -->
  <div class="public-page">
    <div class="brand">
      <span class="brand-dot"></span>
      <span>知途 · 材料确认</span>
    </div>

    <el-card v-if="state === 'loading'" shadow="never" class="card">
      <div class="loading-box">
        <el-icon class="is-loading" :size="22"><Loading /></el-icon>
        <span>正在打开材料…</span>
      </div>
    </el-card>

    <el-card v-else-if="state === 'error'" shadow="never" class="card">
      <el-result icon="warning" title="链接打不开" :sub-title="errorMessage">
        <template #extra>
          <p class="hint">可能原因：链接已过期、发起人已撤销，或免登录通道未开启。请联系发给你链接的同事重新发送。</p>
        </template>
      </el-result>
    </el-card>

    <template v-else>
      <el-card shadow="never" class="card">
        <div class="doc-head">
          <div>
            <div class="doc-title">
              {{ detail.filename }}
              <el-tag size="small" type="info" effect="plain">v{{ detail.version }}</el-tag>
            </div>
            <div class="doc-sub">
              {{ detail.requested_by_name }} 请你确认 · {{ formatTime(detail.created_at) }}
              <span v-if="detail.expires_at" class="expire">· 链接有效至 {{ formatTime(detail.expires_at) }}</span>
            </div>
          </div>
          <el-tag :type="statusType" effect="light">{{ statusLabel }}</el-tag>
        </div>

        <div v-if="detail.message" class="ask-box">
          <span class="ask-who">{{ detail.requested_by_name }}：</span>{{ detail.message }}
        </div>

        <div class="content-title">
          文档内容
          <span v-if="originalFailed && detail.content_truncated" class="truncated">（内容较长，仅显示前 2 万字）</span>
        </div>
        <!-- 原版式预览优先（Excel 网格 / Word 排版），失败回退提取文本 -->
        <div v-if="originalLoading" v-loading="true" class="doc-content original-loading"></div>
        <DocPreview
          v-else-if="originalBlob && !originalFailed"
          :blob="originalBlob"
          :filename="detail.filename"
          @error="originalFailed = true"
          @unsupported="originalFailed = true"
        />
        <div v-else>
          <el-alert
            v-if="originalFailed"
            type="info"
            :closable="false"
            class="preview-hint"
            title="原文件暂不可用，以下为系统提取的文本"
          />
          <div class="doc-content" v-html="rendered"></div>
        </div>

        <div class="download-row">
          <el-button link type="primary" @click="download">下载原文件（用 WPS 打开更准确）</el-button>
        </div>
      </el-card>

      <el-card v-if="detail.status === 'pending'" shadow="never" class="card action-card">
        <div class="action-title">这份材料没问题吗？</div>
        <p class="action-sub">确认后发起人就能看到「{{ detail.confirm_user_name }} 已确认」，不必再追问。</p>
        <el-input
          v-if="rejectMode"
          v-model="comment"
          type="textarea"
          :rows="3"
          maxlength="1000"
          show-word-limit
          placeholder="哪里不对、应该改成什么（写一句就够）"
          class="reject-input"
        />
        <div class="action-buttons">
          <template v-if="!rejectMode">
            <el-button size="large" @click="rejectMode = true">有问题，要改</el-button>
            <el-button size="large" type="primary" :loading="submitting" @click="submit('confirmed')">
              确认无误
            </el-button>
          </template>
          <template v-else>
            <el-button size="large" @click="rejectMode = false">取消</el-button>
            <el-button size="large" type="danger" :loading="submitting" @click="submit('rejected')">
              提交意见
            </el-button>
          </template>
        </div>
      </el-card>

      <el-card v-else shadow="never" class="card result-card">
        <div class="result-icon" :class="detail.status">
          <el-icon :size="26"><CircleCheckFilled v-if="detail.status === 'confirmed'" /><WarningFilled v-else /></el-icon>
        </div>
        <div class="result-title">
          {{ detail.status === 'confirmed' ? '已确认无误' : '已提交意见' }}
        </div>
        <div class="result-sub">
          {{ detail.status === 'confirmed'
            ? '发起人已经能看到你的确认结果，感谢配合。'
            : '发起人会按你的意见修改，改完可能再发一轮确认。' }}
          <span v-if="detail.resolved_at">（{{ formatTime(detail.resolved_at) }}）</span>
        </div>
        <div v-if="detail.comment" class="result-comment">{{ detail.comment }}</div>
      </el-card>
    </template>

    <div class="footer">此页面仅展示这一份材料，不会显示系统中的其他内容。</div>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import MarkdownIt from 'markdown-it'
import { Loading, CircleCheckFilled, WarningFilled } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import DocPreview from '@/components/DocPreview.vue'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })
const route = useRoute()
const token = route.params.token

const state = ref('loading')
const errorMessage = ref('')
const detail = ref(null)
const submitting = ref(false)
const rejectMode = ref(false)
const comment = ref('')

const rendered = computed(() => md.render(detail.value?.content || ''))

// 原版式预览：公开下载接口拉原始文件；失败回退提取文本
const originalBlob = ref(null)
const originalLoading = ref(false)
const originalFailed = ref(false)

async function loadOriginal() {
  originalBlob.value = null
  originalFailed.value = false
  originalLoading.value = true
  try {
    const res = await fetch(`/api/public/confirmations/${token}/download`)
    if (!res.ok) throw new Error(`status ${res.status}`)
    originalBlob.value = await res.blob()
  } catch (e) {
    originalFailed.value = true
  } finally {
    originalLoading.value = false
  }
}

const statusLabel = computed(() => {
  const s = detail.value?.status
  return { pending: '待你确认', confirmed: '已确认', rejected: '已提意见', cancelled: '已撤销' }[s] || s
})
const statusType = computed(() => {
  const s = detail.value?.status
  return { pending: 'warning', confirmed: 'success', rejected: 'danger' }[s] || 'info'
})

function formatTime(value) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  let data = {}
  try {
    data = await res.json()
  } catch (e) {
    data = {}
  }
  if (!res.ok) {
    const err = new Error(data.detail || `请求失败（${res.status}）`)
    err.status = res.status
    throw err
  }
  return data
}

async function load() {
  try {
    detail.value = await api(`/api/public/confirmations/${token}`)
    state.value = 'ok'
    loadOriginal()
  } catch (e) {
    state.value = 'error'
    errorMessage.value = e.status === 410
      ? '这个链接已过期，需要发起人重新发送。'
      : (e.message || '链接无效')
  }
}

async function submit(action) {
  if (action === 'rejected' && !comment.value.trim()) {
    ElMessage.warning('请写一句需要调整的地方')
    return
  }
  submitting.value = true
  try {
    const res = await api(`/api/public/confirmations/${token}/respond`, {
      method: 'POST',
      body: JSON.stringify({ action, comment: action === 'rejected' ? comment.value.trim() : '' }),
    })
    detail.value = { ...detail.value, ...res }
    rejectMode.value = false
    ElMessage.success(action === 'confirmed' ? '已确认无误' : '意见已提交')
    window.scrollTo({ top: 0, behavior: 'smooth' })
  } catch (e) {
    ElMessage.error(e.message || '提交失败，请稍后重试')
  } finally {
    submitting.value = false
  }
}

function download() {
  window.open(`/api/public/confirmations/${token}/download`, '_blank')
}

onMounted(load)
</script>

<style scoped>
.public-page {
  min-height: 100vh;
  background: linear-gradient(160deg, #f8fafc 0%, #eef2ff 100%);
  padding: 28px 16px 40px;
  display: flex;
  flex-direction: column;
  align-items: center;
}
.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: #6366f1;
  font-weight: 600;
  margin-bottom: 16px;
}
.brand-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #6366f1;
}
.card {
  width: 100%;
  max-width: 820px;
  border-radius: 14px;
  border: 1px solid #eef0f4;
  margin-bottom: 14px;
}
.loading-box {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 40px 0;
  color: #6b7280;
  font-size: 13px;
}
.doc-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
  padding-bottom: 14px;
  border-bottom: 1px solid #f3f4f6;
}
.doc-title {
  font-size: 17px;
  font-weight: 600;
  color: #1f2937;
  display: flex;
  align-items: center;
  gap: 6px;
  word-break: break-all;
}
.doc-sub {
  margin-top: 6px;
  font-size: 12px;
  color: #6b7280;
}
.expire {
  color: #b45309;
}
.ask-box {
  margin-top: 14px;
  padding: 10px 12px;
  background: #eef2ff;
  border: 1px solid #e0e7ff;
  border-radius: 8px;
  font-size: 13px;
  color: #3730a3;
  line-height: 1.7;
}
.ask-who {
  font-weight: 600;
}
.content-title {
  margin: 18px 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: #1f2937;
}
.truncated {
  font-weight: 400;
  font-size: 12px;
  color: #b45309;
}
.doc-content {
  max-height: 56vh;
  overflow: auto;
  padding: 14px 16px;
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.85;
  color: #1f2937;
}
.preview-hint {
  margin-bottom: 10px;
}
.doc-content.original-loading {
  min-height: 200px;
}
.doc-content :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
}
.doc-content :deep(th),
.doc-content :deep(td) {
  border: 1px solid #e5e7eb;
  padding: 6px 10px;
}
.doc-content :deep(th) {
  background: #f3f4f6;
}
.download-row {
  margin-top: 10px;
  text-align: right;
}
.action-card {
  border-color: #e0e7ff;
}
.action-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2937;
}
.action-sub {
  margin: 6px 0 14px;
  font-size: 12px;
  color: #6b7280;
}
.reject-input {
  margin-bottom: 12px;
}
.action-buttons {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
.result-card {
  text-align: center;
  padding: 8px 0;
}
.result-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 46px;
  height: 46px;
  border-radius: 50%;
  margin-bottom: 8px;
}
.result-icon.confirmed {
  color: #16a34a;
  background: #dcfce7;
}
.result-icon.rejected {
  color: #d97706;
  background: #fef3c7;
}
.result-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2937;
}
.result-sub {
  margin-top: 6px;
  font-size: 13px;
  color: #6b7280;
  line-height: 1.7;
}
.result-comment {
  margin: 14px auto 0;
  max-width: 520px;
  padding: 10px 12px;
  background: #fef3c7;
  border-radius: 8px;
  font-size: 13px;
  color: #92400e;
  text-align: left;
  line-height: 1.7;
}
.hint {
  margin: 0;
  font-size: 12px;
  color: #9ca3af;
  line-height: 1.8;
  max-width: 460px;
}
.footer {
  margin-top: 4px;
  font-size: 11px;
  color: #9ca3af;
}
</style>
