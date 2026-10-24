<template>
  <div class="confirm-page">
    <el-card shadow="never" class="list-card">
      <div class="page-head">
        <div class="head-text">
          <h3>待我确认</h3>
          <p class="sub">别人发来让你确认的材料都在这里。确认过的不再打扰，提过意见的会留在「有问题」里。</p>
        </div>
        <el-radio-group v-model="status" size="small" @change="load">
          <el-radio-button value="pending">待确认</el-radio-button>
          <el-radio-button value="confirmed">已确认</el-radio-button>
          <el-radio-button value="rejected">有问题</el-radio-button>
          <el-radio-button value="all">全部</el-radio-button>
        </el-radio-group>
      </div>

      <el-table
        v-loading="loading"
        :data="items"
        size="small"
        :empty-text="emptyText"
        style="width: 100%"
      >
        <el-table-column label="材料" min-width="240">
          <template #default="{ row }">
            <div class="doc-cell">
              <el-tag size="small" effect="plain">{{ row.file_type || 'file' }}</el-tag>
              <span class="doc-name" :title="row.filename">{{ row.filename }}</span>
              <el-tag size="small" type="info" effect="plain">v{{ row.version || 1 }}</el-tag>
            </div>
            <div class="doc-owner">来自 {{ row.owner_name || '未知' }}</div>
          </template>
        </el-table-column>
        <el-table-column label="发起人" width="120">
          <template #default="{ row }">{{ row.requested_by_name || '-' }}</template>
        </el-table-column>
        <el-table-column label="附言" min-width="200">
          <template #default="{ row }">
            <span v-if="row.message" class="msg">{{ row.message }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="发起时间" width="130">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="110">
          <template #default="{ row }">
            <el-tag size="small" :type="statusType(row.status)" effect="light">
              {{ statusLabel(row.status) }}
            </el-tag>
            <div v-if="row.first_viewed_at" class="viewed">已打开</div>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">
              {{ row.status === 'pending' ? '查看并确认' : '查看' }}
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 详情抽屉：先看内容，再表态 -->
    <el-drawer
      v-model="drawerVisible"
      :title="detail ? `${detail.filename}（v${detail.version}）` : '确认材料'"
      size="62%"
      class="confirm-drawer"
    >
      <div v-if="detail" v-loading="detailLoading" class="detail-body">
        <el-alert
          v-if="detail.message"
          type="info"
          :closable="false"
          class="ask-banner"
        >
          <template #title>
            <span class="banner-title">{{ detail.requested_by_name }} 请你确认：</span>
            <span>{{ detail.message }}</span>
          </template>
        </el-alert>

        <el-descriptions :column="2" border size="small" class="meta">
          <el-descriptions-item label="文档">{{ detail.filename }}</el-descriptions-item>
          <el-descriptions-item label="版本">v{{ detail.version }}（发起时确认的是 v{{ detail.doc_version }}）</el-descriptions-item>
          <el-descriptions-item label="归属">{{ detail.owner_name || '-' }}</el-descriptions-item>
          <el-descriptions-item label="发起时间">{{ formatTime(detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="当前状态" :span="2">
            <el-tag size="small" :type="statusType(detail.status)">{{ statusLabel(detail.status) }}</el-tag>
            <span v-if="detail.resolved_at" class="resolved">于 {{ formatTime(detail.resolved_at) }} 处理</span>
          </el-descriptions-item>
        </el-descriptions>

        <div v-if="detail.status === 'rejected' && detail.comment" class="comment-box">
          <div class="comment-title">你提出的意见</div>
          <div class="comment-text">{{ detail.comment }}</div>
        </div>

        <div class="content-title">
          文档内容
          <span v-if="originalFailed && detail.content_truncated" class="truncated">（内容较长，仅显示前 2 万字，完整内容请下载原文件）</span>
        </div>
        <!-- 原版式预览优先（Excel 网格 / Word 排版），失败或加载中回退提取文本 -->
        <div v-if="originalLoading" v-loading="true" class="doc-content original-loading"></div>
        <DocPreview
          v-else-if="originalBlob && !originalFailed"
          :blob="originalBlob"
          :filename="detail.filename"
          @error="originalFailed = true"
          @unsupported="originalFailed = true"
        />
        <div v-else-if="rendered">
          <el-alert
            v-if="originalFailed"
            type="info"
            :closable="false"
            class="preview-hint"
            title="原文件不可用（可能已丢失）或该格式不支持在线预览，以下为系统提取的文本"
          />
          <div class="doc-content markdown-body" v-html="rendered"></div>
        </div>
        <el-empty v-else description="文档内容为空" />

        <div v-if="detail.is_invitee && detail.status === 'pending'" class="actions">
          <el-button size="large" @click="openReject">有问题，要改</el-button>
          <el-button size="large" type="primary" :loading="submitting" @click="submit('confirmed')">
            确认无误
          </el-button>
        </div>
        <el-alert
          v-else-if="detail.status !== 'pending'"
          type="success"
          :closable="false"
          class="done-tip"
          :title="detail.status === 'confirmed' ? '你已经确认过这份材料，无需再处理' : '你已经提过意见，等发起人修改后再发起新一轮'"
        />
        <el-alert
          v-else
          type="info"
          :closable="false"
          class="done-tip"
          title="这是你发起的确认，可以在这里查看对方的处理进度"
        />
      </div>
    </el-drawer>

    <!-- 提意见：必须写清哪里要改，否则发起人没法处理 -->
    <el-dialog v-model="rejectVisible" title="需要调整的地方" width="480px">
      <p class="reject-hint">写一句就够，比如「人数应该是 4 人，漏了一个」。</p>
      <el-input
        v-model="rejectComment"
        type="textarea"
        :rows="4"
        maxlength="1000"
        show-word-limit
        placeholder="哪里不对、应该改成什么"
      />
      <template #footer>
        <el-button @click="rejectVisible = false">取消</el-button>
        <el-button type="danger" :loading="submitting" @click="submit('rejected')">提交意见</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import MarkdownIt from 'markdown-it'
import {
  listMyConfirmations,
  getConfirmation,
  respondConfirmation,
  fetchConfirmationBlob,
} from '@/api/confirmation'
import DocPreview from '@/components/DocPreview.vue'

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const status = ref('pending')
const items = ref([])
const loading = ref(false)

const drawerVisible = ref(false)
const detailLoading = ref(false)
const detail = ref(null)
const submitting = ref(false)

const rejectVisible = ref(false)
const rejectComment = ref('')

const emptyText = computed(() => {
  const map = {
    pending: '没有待确认的材料，清空了',
    confirmed: '还没有确认过的材料',
    rejected: '还没有提过意见',
    all: '没有和你相关的确认记录',
  }
  return map[status.value] || '暂无记录'
})

const rendered = computed(() => md.render(detail.value?.content || ''))

// 原版式预览：拉原始文件渲染；失败回退提取文本（不影响确认动作）
const originalBlob = ref(null)
const originalLoading = ref(false)
const originalFailed = ref(false)

async function loadOriginal(confId) {
  originalBlob.value = null
  originalFailed.value = false
  originalLoading.value = true
  try {
    originalBlob.value = await fetchConfirmationBlob(confId)
  } catch (e) {
    originalFailed.value = true
  } finally {
    originalLoading.value = false
  }
}

function statusType(s) {
  return { pending: 'warning', confirmed: 'success', rejected: 'danger', cancelled: 'info' }[s] || 'info'
}

function statusLabel(s) {
  return { pending: '待确认', confirmed: '已确认', rejected: '有问题', cancelled: '已撤销' }[s] || s
}

function formatTime(value) {
  if (!value) return '-'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function notifyChange() {
  // 侧边栏徽标靠这个事件刷新，避免为一个小数字上全局状态
  window.dispatchEvent(new CustomEvent('confirmations-changed'))
}

async function load() {
  loading.value = true
  try {
    const res = await listMyConfirmations({ status: status.value })
    items.value = res.items || []
  } finally {
    loading.value = false
  }
}

async function openDetail(row) {
  drawerVisible.value = true
  detail.value = { ...row }
  loadOriginal(row.id)
  detailLoading.value = true
  try {
    detail.value = await getConfirmation(row.id)
  } catch (e) {
    // 全局拦截器已提示
  } finally {
    detailLoading.value = false
  }
}

function openReject() {
  rejectComment.value = ''
  rejectVisible.value = true
}

async function submit(action) {
  if (!detail.value) return
  if (action === 'rejected' && !rejectComment.value.trim()) {
    ElMessage.warning('请写一句需要调整的地方')
    return
  }
  submitting.value = true
  try {
    const res = await respondConfirmation(
      detail.value.id,
      action,
      action === 'rejected' ? rejectComment.value.trim() : '',
    )
    rejectVisible.value = false
    ElMessage.success(action === 'confirmed' ? '已确认无误' : '意见已提交')
    detail.value = { ...detail.value, ...res }
    notifyChange()
    await load()
  } finally {
    submitting.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.confirm-page {
  max-width: 1180px;
  margin: 0 auto;
}
.list-card {
  border-radius: 12px;
}
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
}
.head-text h3 {
  margin: 0 0 4px;
  font-size: 17px;
  color: #1f2937;
}
.sub {
  margin: 0;
  font-size: 12px;
  color: #6b7280;
  line-height: 1.7;
}
.doc-cell {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}
.doc-name {
  font-weight: 500;
  color: #1f2937;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.doc-owner {
  margin-top: 2px;
  font-size: 12px;
  color: #9ca3af;
}
.msg {
  color: #374151;
}
.muted {
  color: #c0c4cc;
}
.viewed {
  margin-top: 2px;
  font-size: 11px;
  color: #9ca3af;
}
.detail-body {
  padding: 0 4px 16px;
}
.ask-banner {
  margin-bottom: 14px;
}
.banner-title {
  font-weight: 600;
}
.meta {
  margin-bottom: 16px;
}
.resolved {
  margin-left: 8px;
  font-size: 12px;
  color: #6b7280;
}
.comment-box {
  margin-bottom: 16px;
  padding: 10px 12px;
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 8px;
}
.comment-title {
  font-size: 12px;
  font-weight: 600;
  color: #b91c1c;
  margin-bottom: 4px;
}
.comment-text {
  font-size: 13px;
  color: #7f1d1d;
  line-height: 1.7;
  white-space: pre-wrap;
}
.content-title {
  font-size: 13px;
  font-weight: 600;
  color: #1f2937;
  margin-bottom: 8px;
}
.truncated {
  font-weight: 400;
  font-size: 12px;
  color: #b45309;
}
.doc-content {
  max-height: 52vh;
  overflow: auto;
  padding: 14px 16px;
  background: #fafafa;
  border: 1px solid #f0f0f0;
  border-radius: 8px;
  font-size: 13px;
  line-height: 1.8;
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
.actions {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
  margin-top: 20px;
  padding-top: 16px;
  border-top: 1px solid #f0f0f0;
}
.done-tip {
  margin-top: 16px;
}
.reject-hint {
  margin: 0 0 10px;
  font-size: 12px;
  color: #6b7280;
}
</style>
