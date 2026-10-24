<template>
  <div class="ingest-page">
    <el-card class="ledger-card">
      <template #header>
        <div class="card-header">
          <span>资料入库 — 文档台账</span>
          <div class="header-actions">
            <el-upload
              :show-file-list="false"
              :before-upload="handleUpload"
              accept=".doc,.docx,.xls,.xlsx,.pdf,.txt,.md"
            >
              <el-button type="primary" :icon="Upload">上传文档</el-button>
            </el-upload>
            <el-button :icon="EditPen" @click="openPasteDialog">粘贴文本抽取</el-button>
          </div>
        </div>
      </template>

      <el-alert
        title="上传的文档会保存到本地数据库，可在台账中管理。点击「AI 抽取」将文档内容抽取为结构化知识条目，也可仅存档不抽取。"
        type="info"
        :closable="false"
        style="margin-bottom: 16px"
      />

      <div class="filter-bar">
        <el-input
          v-model="keyword"
          placeholder="搜索文件名"
          style="width: 220px"
          clearable
          @keyup.enter="loadDocs"
        />
        <el-button @click="loadDocs">查询</el-button>
        <el-button
          type="danger"
          plain
          :disabled="selectedRows.length === 0"
          @click="handleBatchDelete"
        >
          批量删除 ({{ selectedRows.length }})
        </el-button>
      </div>

      <el-table
        :data="docs"
        v-loading="loading"
        stripe
        :row-class-name="rowClassName"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="50" :selectable="canEditRow" />
        <el-table-column label="文件名" min-width="240" show-overflow-tooltip>
          <template #default="{ row }">
            {{ row.filename }}
            <el-tag v-if="visTag(row)" size="small" :type="visTag(row).type" effect="plain" style="margin-left: 6px">{{ visTag(row).text }}</el-tag>
            <el-tag v-if="row.task_id" size="small" type="primary" effect="plain" style="margin-left: 6px">
              {{ taskName(row.task_id) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="版本" width="110">
          <template #default="{ row }">
            <span class="ver-tag-wrap" @click="openVersions(row)">
              <el-tag
                size="small"
                :type="row.is_latest === false ? 'info' : 'success'"
                :effect="row.is_latest === false ? 'plain' : 'light'"
              >
                v{{ row.version || 1 }}
              </el-tag>
              <span v-if="row.is_latest === false" class="ver-superseded">已替代</span>
            </span>
          </template>
        </el-table-column>
        <el-table-column prop="file_type" label="类型" width="80">
          <template #default="{ row }">
            <el-tag size="small" effect="plain">{{ row.file_type }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="大小" width="100">
          <template #default="{ row }">{{ formatSize(row.file_size) }}</template>
        </el-table-column>
        <el-table-column label="上传时间" width="180">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="row.status === 'extracted' ? 'success' : 'info'" size="small">
              {{ row.status === 'extracted' ? `已抽取 ${row.extracted_count} 条` : '已存档' }}
            </el-tag>
          </template>
        </el-table-column>
        <!-- 确认清单：一眼看出「谁还没看过」，点开就是发起/追加确认的入口 -->
        <el-table-column label="确认" width="132">
          <template #default="{ row }">
            <el-tag
              v-if="row.confirm_summary && row.confirm_summary.total"
              size="small"
              :type="confirmTagType(row.confirm_summary)"
              effect="light"
              class="confirm-tag"
              @click="openConfirmDrawer(row)"
            >
              {{ confirmText(row.confirm_summary) }}
            </el-tag>
            <el-button
              v-else-if="canEditRow(row)"
              link
              type="primary"
              size="small"
              @click="openConfirmDialog(row)"
            >+ 发起确认</el-button>
            <span v-else class="confirm-none">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="350" fixed="right">
          <template #default="{ row }">
            <template v-if="canEditRow(row)">
              <el-button link type="primary" size="small" @click="handleExtract(row)">AI 抽取</el-button>
              <el-button link type="primary" size="small" @click="openVisibilityDialog(row)">共享设置</el-button>
              <el-button link type="primary" size="small" @click="openTaskDialog(row)">关联任务</el-button>
            </template>
            <el-button link type="info" size="small" @click="handleViewContent(row)">查看内容</el-button>
            <el-button link type="warning" size="small" @click="handleDownload(row)">下载</el-button>
            <el-button v-if="canEditRow(row)" link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        @size-change="loadDocs"
        @current-change="loadDocs"
        style="margin-top: 16px; justify-content: flex-end"
      />
    </el-card>

    <!-- 查看文档内容弹窗（Markdown 格式化渲染 + 缩放 + 放大） -->
    <el-dialog
      v-model="contentDialogVisible"
      :title="currentDoc?.filename"
      :width="contentMaximized ? '80vw' : '900px'"
      :top="contentMaximized ? '2vh' : '5vh'"
      class="content-dialog"
      @opened="onContentDialogOpened"
      @closed="onContentDialogClosed"
    >
      <template #header>
        <div class="dialog-header">
          <span class="dialog-title">{{ currentDoc?.filename }}</span>
          <div class="dialog-actions">
            <el-button :icon="Download" size="small" @click="handleDownload(currentDoc)">
              下载原文件
            </el-button>
            <el-button
              :icon="contentMaximized ? Close : FullScreen"
              size="small"
              @click="toggleMaximize"
              :title="contentMaximized ? '还原' : '放大（占屏幕75%）'"
            >
              {{ contentMaximized ? '还原' : '放大' }}
            </el-button>
          </div>
        </div>
      </template>
      <div class="content-viewer" :class="{ 'is-maximized': contentMaximized }">
        <div v-if="!originalBlob || previewFailed" class="zoom-toolbar">
          <el-button class="zoom-btn" :icon="ZoomOut" size="small" @click="zoomOut" />
          <span class="zoom-percent">{{ Math.round(zoom * 100) }}%</span>
          <el-button class="zoom-btn" :icon="ZoomIn" size="small" @click="zoomIn" />
          <el-button class="zoom-btn" :icon="Aim" size="small" @click="zoomReset" title="重置" />
        </div>
        <!-- 原版式预览（Excel 网格 / Word 排版 / PDF / 文本），失败或无原始文件时回退提取文本 -->
        <DocPreview
          v-if="originalBlob && !previewFailed"
          :blob="originalBlob"
          :filename="currentDoc?.filename || ''"
          @error="previewFailed = true"
          @unsupported="previewFailed = true"
        />
        <div v-else-if="renderedContent" class="fallback-wrap">
          <el-alert
            v-if="previewFailed"
            type="info"
            :closable="false"
            class="preview-hint"
            title="原文件不可用（可能已丢失）或该格式不支持在线预览，以下为系统提取的文本，表格结构可能有偏差"
          />
          <div
            class="markdown-body"
            :style="{ transform: `scale(${zoom})`, transformOrigin: 'top left' }"
            v-html="renderedContent"
          ></div>
        </div>
        <el-empty v-else description="文档解析内容为空" />
      </div>
    </el-dialog>

    <!-- AI 抽取结果校对弹窗 -->
    <el-dialog
      v-model="extractDialogVisible"
      :title="`AI 抽取结果 — ${currentDoc?.filename || ''}`"
      width="780px"
      top="5vh"
    >
      <el-alert
        title="请检查以下 AI 抽取的条目，可编辑修改后批量入库"
        type="warning"
        :closable="false"
        style="margin-bottom: 16px"
      />
      <div class="extracted-list">
        <div v-for="(item, idx) in extractedItems" :key="idx" class="extracted-item">
          <div class="item-header">
            <span class="item-index">条目 {{ idx + 1 }}</span>
            <el-button link type="danger" size="small" @click="removeItem(idx)">删除</el-button>
          </div>
          <el-form label-width="100px" class="item-form">
            <el-form-item label="标题">
              <el-input v-model="item.title" />
            </el-form-item>
            <el-form-item label="步骤">
              <el-input
                v-model="item.stepsText"
                type="textarea"
                :rows="3"
                placeholder="每行一个步骤"
              />
            </el-form-item>
            <el-form-item label="系统/物料">
              <el-input v-model="item.system_or_material" />
            </el-form-item>
            <el-form-item label="注意事项">
              <el-input v-model="item.caution" type="textarea" :rows="2" />
            </el-form-item>
            <el-form-item label="关联流程">
              <el-input v-model="item.related_flow" />
            </el-form-item>
            <el-form-item label="状态">
              <el-radio-group v-model="item.status">
                <el-radio value="draft">草稿</el-radio>
                <el-radio value="published">直接发布</el-radio>
              </el-radio-group>
            </el-form-item>
          </el-form>
        </div>
      </div>
      <div v-if="extractedItems.length === 0" class="empty-state">
        <el-empty description="没有抽取到条目" />
      </div>
      <template #footer>
        <el-button @click="extractDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :icon="Check"
          :loading="saving"
          :disabled="extractedItems.length === 0"
          @click="handleBatchCreate"
        >
          确认入库（{{ extractedItems.length }} 条）
        </el-button>
      </template>
    </el-dialog>

    <!-- 粘贴文本抽取弹窗 -->
    <el-dialog v-model="pasteDialogVisible" title="粘贴文本抽取" width="700px">
      <el-input
        v-model="rawText"
        type="textarea"
        :rows="10"
        placeholder="在此粘贴原始资料文本..."
        show-word-limit
        :maxlength="50000"
      />
      <template #footer>
        <el-button @click="pasteDialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          :icon="MagicStick"
          :loading="extracting"
          :disabled="!rawText.trim()"
          @click="handlePasteExtract"
        >
          AI 抽取
        </el-button>
      </template>
    </el-dialog>

    <!-- 可见范围设置弹窗（上传前 / 共享设置共用） -->
    <el-dialog
      v-model="visDialogVisible"
      :title="editingDoc ? `共享设置 — ${editingDoc.filename}` : '上传前设置可见范围'"
      width="560px"
    >
      <el-form label-width="90px">
        <VisibilityPicker v-model:visibility="visForm.visibility" v-model:shared-ids="visForm.shared_user_ids" />
        <el-form-item v-if="!editingDoc" label="关联任务">
          <el-select
            v-model="visForm.task_id"
            clearable
            filterable
            placeholder="可选，把材料挂到某个任务下"
            style="width: 100%"
          >
            <el-option v-for="t in taskOptions" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="cancelVisibilityDialog">取消</el-button>
        <el-button type="primary" :loading="visSaving" @click="confirmVisibilityDialog">
          {{ editingDoc ? '保存' : '开始上传' }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 历史版本抽屉（版本链由旧到新） -->
    <el-drawer v-model="versionsVisible" title="历史版本" size="480px">
      <div v-loading="versionsLoading" class="versions-body">
        <el-alert
          title="同名文件上传会自动生成新版本，旧版保留可下载，检索与任务详情只认最新版。"
          type="info"
          :closable="false"
          style="margin-bottom: 16px"
        />
        <el-timeline v-if="versions.length">
          <el-timeline-item
            v-for="(v, idx) in versions"
            :key="v.id"
            :timestamp="formatDate(v.created_at)"
            placement="top"
            :type="v.is_latest ? 'success' : 'info'"
            :hollow="!v.is_latest"
          >
            <div class="ver-item">
              <div class="ver-head">
                <el-tag size="small" :type="v.is_latest ? 'success' : 'info'" :effect="v.is_latest ? 'light' : 'plain'">
                  v{{ v.version }}
                </el-tag>
                <span v-if="v.is_latest" class="ver-current">当前最新</span>
                <span v-else class="ver-old">已被 v{{ versions[idx + 1] ? versions[idx + 1].version : '?' }} 替代</span>
              </div>
              <div class="ver-meta">
                {{ formatSize(v.file_size) }} · {{ v.file_type }}
                · {{ v.status === 'extracted' ? `已抽取 ${v.extracted_count} 条` : '已存档' }}
              </div>
              <div class="ver-actions">
                <el-button link type="primary" size="small" @click="handleDownload(v)">下载</el-button>
                <el-button link type="info" size="small" @click="viewVersionContent(v)">查看内容</el-button>
              </div>
            </div>
          </el-timeline-item>
        </el-timeline>
        <el-empty v-else description="暂无版本记录" :image-size="70" />
      </div>
    </el-drawer>

    <!-- 关联任务弹窗 -->
    <el-dialog v-model="taskDialogVisible" :title="`关联任务 — ${taskDoc?.filename || ''}`" width="480px">
      <el-form label-width="80px">
        <el-form-item label="任务">
          <el-select
            v-model="taskForm.task_id"
            clearable
            filterable
            placeholder="选择要关联的任务（可清空解除）"
            style="width: 100%"
          >
            <el-option v-for="t in taskOptions" :key="t.id" :label="t.name" :value="t.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <div class="task-tip">关联后，任务详情页会聚合显示这份材料的最新版。</div>
      <template #footer>
        <el-button @click="taskDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="taskSaving" @click="confirmTaskDialog">保存</el-button>
      </template>
    </el-dialog>

    <!-- 发起确认：选人 + 附言，提交后直接给出每人的免登录链接 -->
    <el-dialog
      v-model="confirmDialogVisible"
      :title="`发起确认 — ${confirmDoc?.filename || ''}`"
      width="520px"
    >
      <el-form label-width="76px">
        <el-form-item label="确认人" required>
          <el-select
            v-model="confirmForm.user_ids"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择需要确认这份材料的同事"
            style="width: 100%"
          >
            <el-option v-for="u in contacts" :key="u.id" :label="u.username" :value="u.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="附言">
          <el-input
            v-model="confirmForm.message"
            type="textarea"
            :rows="2"
            maxlength="200"
            show-word-limit
            placeholder="如：这版加了新岗位，麻烦看一下（可选）"
          />
        </el-form-item>
      </el-form>

      <!-- 提交后展示结果：谁是新邀请、谁是之前没处理的、每人的链接 -->
      <div v-if="confirmResult" class="confirm-result">
        <el-alert
          type="success"
          :closable="false"
          :title="`已发起：新增 ${confirmResult.created} 人，${confirmResult.reused} 人此前已在等待处理`"
        />
        <div v-if="!confirmResult.public_enabled" class="link-tip">
          免登录通道未开启，确认人需要登录系统在「待我确认」里处理。
          若要给不装系统的同事发链接，请在 .env 设置
          <code>DOC_CONFIRM_PUBLIC_ENABLED=true</code> 后重启后端。
        </div>
        <div v-else class="link-list">
          <div v-for="it in confirmResult.items" :key="it.id" class="link-row">
            <span class="link-name">{{ it.user_name }}</span>
            <el-input :model-value="publicLink(it.token)" readonly size="small" class="link-input" />
            <el-button link type="primary" size="small" @click="copyLink(it.token)">复制</el-button>
          </div>
        </div>
      </div>

      <template #footer>
        <el-button @click="confirmDialogVisible = false">{{ confirmResult ? '关闭' : '取消' }}</el-button>
        <el-button
          v-if="!confirmResult"
          type="primary"
          :loading="confirmSubmitting"
          @click="submitConfirmRequest"
        >发起确认</el-button>
      </template>
    </el-dialog>

    <!-- 确认进度：谁确认了、谁提了意见、谁还没看 -->
    <el-drawer v-model="confirmDrawerVisible" title="确认进度" size="520px">
      <div v-if="confirmProgress" class="progress-body">
        <el-alert
          v-if="confirmProgress.is_latest === false"
          type="warning"
          :closable="false"
          show-icon
          title="这是历史版本：这里的确认结果不代表最新版的结论，建议在最新版上重新发起确认。"
          style="margin-bottom: 12px"
        />
        <div class="progress-head">
          <div class="progress-title">{{ confirmProgress.filename }}</div>
          <div class="progress-sub">
            v{{ confirmProgress.version }} ·
            {{ confirmProgress.summary.total }} 人参与 ·
            已确认 {{ confirmProgress.summary.confirmed }}
          </div>
          <el-progress
            :percentage="confirmPercent"
            :status="confirmProgress.summary.all_confirmed ? 'success' : undefined"
            :stroke-width="8"
          />
        </div>

        <div class="progress-list">
          <div v-for="it in confirmProgress.items" :key="it.id" class="progress-row">
            <div class="row-main">
              <span class="row-name">{{ it.user_name }}</span>
              <el-tag size="small" :type="confirmStatusType(it.status)" effect="light">
                {{ confirmStatusLabel(it.status) }}
              </el-tag>
              <span v-if="it.rounds > 1" class="row-rounds">第 {{ it.rounds }} 轮</span>
              <span v-if="!it.first_viewed_at && it.status === 'pending'" class="row-unseen">未打开</span>
            </div>
            <div v-if="it.comment" class="row-comment">{{ it.comment }}</div>
            <div class="row-meta">
              <span>{{ formatDate(it.created_at) }} 发起</span>
              <span v-if="it.resolved_at">· {{ formatDate(it.resolved_at) }} 处理</span>
              <template v-if="it.status === 'pending'">
                <el-button
                  v-if="confirmProgress.public_enabled && it.token_alive"
                  link
                  type="primary"
                  size="small"
                  @click="copyExistingLink(it.id)"
                >复制链接</el-button>
                <el-button link type="danger" size="small" @click="revokeConfirm(it)">撤销</el-button>
              </template>
            </div>
          </div>
          <el-empty v-if="!confirmProgress.items.length" description="还没有发起过确认" :image-size="70" />
        </div>

        <div class="progress-foot">
          <el-button type="primary" plain @click="openConfirmDialog(confirmDoc)">追加确认人</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { ref, computed, nextTick, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Upload, EditPen, MagicStick, Check, Download, ZoomIn, ZoomOut, Aim, FullScreen, Close } from '@element-plus/icons-vue'
import MarkdownIt from 'markdown-it'
import {
  listDocuments,
  deleteDocument,
  extractDocument,
  markExtracted,
  downloadDocument,
  fetchDocumentBlob,
  getDocument,
  listDocumentVersions,
  bindDocumentTask,
} from '@/api/sourceDocuments'
import { listTasks } from '@/api/task'
import { extractText, batchCreate } from '@/api/ingest'
import {
  requestConfirmations,
  getDocumentConfirmations,
  listConfirmContacts,
  revokeConfirmation,
  issueConfirmationLink,
} from '@/api/confirmation'
import { updateDocumentVisibility } from '@/api/sourceDocuments'
import { useUploadStore } from '@/stores/upload'
import { useUserStore } from '@/stores/user'
import { useContentPermission } from '@/composables/useContentPermission'
import VisibilityPicker from '@/components/VisibilityPicker.vue'
import DocPreview from '@/components/DocPreview.vue'

const uploadStore = useUploadStore()
const userStore = useUserStore()
const { canEditRow, visTag } = useContentPermission()

const route = useRoute()

// 从其他页面（如 AI 问答引用来源）跳转过来时，要高亮的文档 ID
const highlightId = ref('')

function rowClassName({ row }) {
  return row.id === highlightId.value ? 'highlight-row' : ''
}

const md = new MarkdownIt({
  html: true,
  linkify: true,
  typographer: true,
  breaks: true,
})

const docs = ref([])
const total = ref(0)
const loading = ref(false)
const extracting = ref(false)
const saving = ref(false)
const page = ref(1)
const pageSize = ref(10)
const keyword = ref('')
const selectedRows = ref([])

function handleSelectionChange(rows) {
  selectedRows.value = rows
}

// 文档内容查看
const contentDialogVisible = ref(false)
const contentText = ref('')
const currentDoc = ref(null)
// 原版式预览（原始文件流），失败时回退提取文本
const originalBlob = ref(null)
const previewFailed = ref(false)

// 缩放控制
const zoom = ref(1)
const ZOOM_MIN = 0.3
const ZOOM_MAX = 3
const ZOOM_STEP = 0.1

// 弹窗放大控制
const contentMaximized = ref(false)
function toggleMaximize() {
  contentMaximized.value = !contentMaximized.value
}

function zoomIn() {
  zoom.value = Math.min(ZOOM_MAX, zoom.value + ZOOM_STEP)
}
function zoomOut() {
  zoom.value = Math.max(ZOOM_MIN, zoom.value - ZOOM_STEP)
}
function zoomReset() {
  zoom.value = 1
}
function onContentDialogOpened() {
  zoom.value = 1
  contentMaximized.value = false
}
function onContentDialogClosed() {
  zoom.value = 1
  contentMaximized.value = false
  resetPreviewState()
}

// AI 抽取结果
const extractDialogVisible = ref(false)
const extractedItems = ref([])

// 粘贴文本
const pasteDialogVisible = ref(false)
const rawText = ref('')

function formatSize(bytes) {
  if (!bytes) return '0 B'
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / 1024 / 1024).toFixed(2) + ' MB'
}

function formatDate(d) {
  if (!d) return ''
  return new Date(d).toLocaleString('zh-CN')
}

async function loadDocs() {
  loading.value = true
  try {
    const res = await listDocuments({
      keyword: keyword.value || undefined,
      skip: (page.value - 1) * pageSize.value,
      limit: pageSize.value,
    })
    docs.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

async function handleUpload(file) {
  // 管理员/超管：先设置可见范围再上传；普通用户：直接上传（后端强制仅自己）
  if (userStore.isStaff()) {
    pendingFile.value = file
    editingDoc.value = null
    visForm.value = { visibility: 'all', shared_user_ids: [], task_id: null }
    visDialogVisible.value = true
  } else {
    doUpload(file)
  }
  return false // 阻止 el-upload 自动上传
}

function doUpload(file, extra = {}) {
  // 后台异步上传：带进度条、可切页、完成后铃铛 + 通知提示（见 stores/upload.js 与 MainLayout）
  uploadStore
    .startUpload(file, extra)
    .then((res) => {
      // 无论用户当前在哪个页面，成功后都刷新台账数据
      loadDocs()
      // 升版是"静默发生"的，必须明确告知，否则用户会以为同名文件被覆盖了
      if (res && res.is_new_version) {
        ElMessage.info(`「${res.filename}」已保存为第 ${res.version} 版，旧版仍保留`)
      }
    })
    .catch(() => {})
}

// ===== 可见范围设置弹窗（上传前 / 已有文档共享设置） =====
const visDialogVisible = ref(false)
const visSaving = ref(false)
const pendingFile = ref(null)   // 待上传的文件（上传前模式）
const editingDoc = ref(null)    // 待修改的文档（共享设置模式）
const visForm = ref({ visibility: 'all', shared_user_ids: [], task_id: null })

// ===== 任务下拉（关联任务 + 文件名旁的任务标签） =====
const taskOptions = ref([])

async function loadTaskOptions() {
  try {
    const res = await listTasks()
    taskOptions.value = res.tasks || []
  } catch (e) {
    // 任务列表加载失败不影响台账主流程
  }
}

function taskName(id) {
  const t = taskOptions.value.find((x) => x.id === id)
  return t ? t.name : '已关联任务'
}

// ===== 关联任务弹窗 =====
const taskDialogVisible = ref(false)
const taskSaving = ref(false)
const taskDoc = ref(null)
const taskForm = ref({ task_id: null })

function openTaskDialog(row) {
  taskDoc.value = row
  taskForm.value = { task_id: row.task_id || null }
  taskDialogVisible.value = true
}

async function confirmTaskDialog() {
  if (!taskDoc.value) return
  taskSaving.value = true
  try {
    await bindDocumentTask(taskDoc.value.id, taskForm.value.task_id || null)
    ElMessage.success(taskForm.value.task_id ? '已关联到任务' : '已解除任务关联')
    taskDialogVisible.value = false
    loadDocs()
  } catch (e) {
    ElMessage.error('保存失败')
  } finally {
    taskSaving.value = false
  }
}

// ===== 历史版本抽屉 =====
const versionsVisible = ref(false)
const versionsLoading = ref(false)
const versions = ref([])

async function openVersions(row) {
  versionsVisible.value = true
  versionsLoading.value = true
  versions.value = []
  try {
    const res = await listDocumentVersions(row.id)
    versions.value = res.versions || []
  } catch (e) {
    ElMessage.error('版本记录加载失败')
  } finally {
    versionsLoading.value = false
  }
}

async function viewVersionContent(v) {
  try {
    const doc = await getDocument(v.id)
    currentDoc.value = doc
    contentText.value = doc.content || ''
    resetPreviewState()
    contentDialogVisible.value = true
    loadOriginalBlob(v.id)
  } catch (e) {
    ElMessage.error('内容加载失败')
  }
}

function openVisibilityDialog(row) {
  editingDoc.value = row
  pendingFile.value = null
  visForm.value = {
    visibility: row.visibility || 'private',
    shared_user_ids: row.shared_user_ids || [],
    task_id: row.task_id || null,
  }
  visDialogVisible.value = true
}

function cancelVisibilityDialog() {
  visDialogVisible.value = false
  pendingFile.value = null
  editingDoc.value = null
}

async function confirmVisibilityDialog() {
  if (visForm.value.visibility === 'custom' && visForm.value.shared_user_ids.length === 0) {
    ElMessage.warning('已选择「指定用户」，请至少选择一个用户，或改回「仅自己」')
    return
  }
  visSaving.value = true
  try {
    if (editingDoc.value) {
      await updateDocumentVisibility(
        editingDoc.value.id,
        visForm.value.visibility,
        visForm.value.shared_user_ids
      )
      ElMessage.success('可见范围已更新')
      visDialogVisible.value = false
      loadDocs()
    } else if (pendingFile.value) {
      const file = pendingFile.value
      visDialogVisible.value = false
      doUpload(file, {
        visibility: visForm.value.visibility,
        shared_user_ids: JSON.stringify(visForm.value.shared_user_ids || []),
        task_id: visForm.value.task_id || undefined,
      })
    }
    pendingFile.value = null
    editingDoc.value = null
  } finally {
    visSaving.value = false
  }
}

function handleViewContent(row) {
  currentDoc.value = row
  contentText.value = row.content || ''
  resetPreviewState()
  contentDialogVisible.value = true
  loadOriginalBlob(row.id)
}

// 拉取原始文件做原版式预览；失败不阻塞弹窗，静默回退提取文本
async function loadOriginalBlob(docId) {
  originalBlob.value = null
  previewFailed.value = false
  try {
    originalBlob.value = await fetchDocumentBlob(docId)
  } catch (e) {
    previewFailed.value = true
  }
}

function resetPreviewState() {
  originalBlob.value = null
  previewFailed.value = false
}

// 将解析内容渲染为 HTML（支持标题、列表、表格等格式）
// 图片 URL 追加 token，因为 <img> 标签无法携带 Authorization header
const renderedContent = computed(() => {
  if (!contentText.value.trim()) return ''
  const token = localStorage.getItem('token') || ''
  // 给 /api/source-documents/.../images/... 的图片 URL 追加 ?token=xxx
  let text = contentText.value
  if (token) {
    text = text.replace(
      /(\/api\/source-documents\/[^)\s]+\/images\/[^)\s]+)/g,
      (match) => `${match}?token=${encodeURIComponent(token)}`
    )
  }
  return md.render(text)
})

async function handleDownload(row) {
  try {
    const blob = await downloadDocument(row.id)
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = row.filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    window.URL.revokeObjectURL(url)
    ElMessage.success('文件下载中，可用 WPS 打开')
  } catch (e) {
    ElMessage.error('下载失败')
  }
}

async function handleExtract(row) {
  currentDoc.value = row
  extracting.value = true
  extractedItems.value = []
  extractDialogVisible.value = true
  try {
    const res = await extractDocument(row.id)
    extractedItems.value = (res.items || []).map((item) => ({
      ...item,
      stepsText: (item.steps || []).join('\n'),
      status: 'draft',
    }))
    if (extractedItems.value.length === 0) {
      ElMessage.warning('未抽取到有效条目')
    } else {
      ElMessage.success(`抽取到 ${extractedItems.value.length} 条条目，请校对`)
    }
  } catch (e) {
    const detail = e?.response?.data?.detail || ''
    ElMessage.error(detail ? `抽取失败：${detail}` : '抽取失败，请检查 API Key 配置')
    extractDialogVisible.value = false
  } finally {
    extracting.value = false
  }
}

function removeItem(idx) {
  extractedItems.value.splice(idx, 1)
}

async function handleBatchCreate() {
  const items = extractedItems.value.map((item) => ({
    title: item.title,
    steps: (item.stepsText || '').split('\n').map((s) => s.trim()).filter(Boolean),
    system_or_material: item.system_or_material,
    caution: item.caution,
    related_flow: item.related_flow,
    status: item.status,
  }))

  saving.value = true
  try {
    const res = await batchCreate(items)
    // 若从文档抽取，标记文档为已抽取
    if (currentDoc.value) {
      try {
        await markExtracted(currentDoc.value.id, res.count)
      } catch (e) {
        // 标记失败不影响入库结果
      }
    }
    ElMessage.success(`成功入库 ${res.count} 条条目`)
    extractDialogVisible.value = false
    loadDocs()
  } catch (e) {
    ElMessage.error('入库失败')
  } finally {
    saving.value = false
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除「${row.filename}」吗？`, '确认', { type: 'warning' })
  try {
    await deleteDocument(row.id)
    ElMessage.success('已删除')
    loadDocs()
  } catch (e) {}
}

async function handleBatchDelete() {
  if (selectedRows.value.length === 0) return
  await ElMessageBox.confirm(
    `确定删除选中的 ${selectedRows.value.length} 个文档吗？`,
    '批量删除确认',
    { type: 'warning' }
  )
  try {
    await Promise.all(selectedRows.value.map((r) => deleteDocument(r.id)))
    ElMessage.success(`已删除 ${selectedRows.value.length} 个文档`)
    selectedRows.value = []
    loadDocs()
  } catch (e) {}
}

function openPasteDialog() {
  rawText.value = ''
  currentDoc.value = null  // 粘贴文本模式不属于某个文档，清空当前文档引用
  pasteDialogVisible.value = true
}

async function handlePasteExtract() {
  extracting.value = true
  try {
    const res = await extractText(rawText.value)
    extractedItems.value = (res.items || []).map((item) => ({
      ...item,
      stepsText: (item.steps || []).join('\n'),
      status: 'draft',
    }))
    pasteDialogVisible.value = false
    if (extractedItems.value.length === 0) {
      ElMessage.warning('未抽取到有效条目，请检查资料内容')
    } else {
      ElMessage.success(`抽取到 ${extractedItems.value.length} 条条目，请校对`)
      extractDialogVisible.value = true
    }
  } catch (e) {
    const detail = e?.response?.data?.detail || ''
    ElMessage.error(detail ? `抽取失败：${detail}` : '抽取失败，请检查 API Key 配置')
  } finally {
    extracting.value = false
  }
}

// ===== 文档确认清单（迭代 4）=====
const confirmDialogVisible = ref(false)
const confirmDrawerVisible = ref(false)
const confirmDoc = ref(null)
const confirmForm = ref({ user_ids: [], message: '' })
const confirmSubmitting = ref(false)
const confirmResult = ref(null)
const confirmProgress = ref(null)
const contacts = ref([])

function confirmText(s) {
  if (s.all_confirmed) return `${s.confirmed}/${s.total} 已确认`
  if (s.rejected) return `${s.rejected} 人有问题`
  return `${s.confirmed}/${s.total} 已确认`
}

function confirmTagType(s) {
  if (s.all_confirmed) return 'success'
  if (s.rejected) return 'danger'
  return 'warning'
}

const confirmPercent = computed(() => {
  const s = confirmProgress.value?.summary
  if (!s || !s.total) return 0
  return Math.round((s.confirmed / s.total) * 100)
})

function confirmStatusLabel(s) {
  return { pending: '待确认', confirmed: '已确认', rejected: '有问题', cancelled: '已撤销' }[s] || s
}

function confirmStatusType(s) {
  return { pending: 'warning', confirmed: 'success', rejected: 'danger', cancelled: 'info' }[s] || 'info'
}

async function loadContacts() {
  try {
    const res = await listConfirmContacts()
    contacts.value = res.items || []
  } catch (e) {
    contacts.value = []
  }
}

function openConfirmDialog(row) {
  if (!row) return
  confirmDoc.value = row
  confirmForm.value = { user_ids: [], message: '' }
  confirmResult.value = null
  confirmDialogVisible.value = true
  if (!contacts.value.length) loadContacts()
}

function publicLink(token) {
  if (!token) return ''
  return `${window.location.origin}/public/confirm/${token}`
}

async function copyText(text) {
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('链接已复制')
  } catch (e) {
    // 非 https 环境下 clipboard 不可用，退回到临时 textarea
    const ta = document.createElement('textarea')
    ta.value = text
    ta.style.position = 'fixed'
    ta.style.opacity = '0'
    document.body.appendChild(ta)
    ta.select()
    document.execCommand('copy')
    document.body.removeChild(ta)
    ElMessage.success('链接已复制')
  }
}

function copyLink(token) {
  copyText(publicLink(token))
}

async function submitConfirmRequest() {
  if (!confirmForm.value.user_ids.length) {
    ElMessage.warning('请选择至少一位确认人')
    return
  }
  confirmSubmitting.value = true
  try {
    const res = await requestConfirmations(
      confirmDoc.value.id,
      confirmForm.value.user_ids,
      confirmForm.value.message,
    )
    confirmResult.value = res
    ElMessage.success('已发起确认')
    await loadDocs()
    if (confirmDrawerVisible.value) await refreshProgress()
  } finally {
    confirmSubmitting.value = false
  }
}

async function refreshProgress() {
  if (!confirmDoc.value) return
  try {
    confirmProgress.value = await getDocumentConfirmations(confirmDoc.value.id)
  } catch (e) {
    confirmProgress.value = null
  }
}

async function openConfirmDrawer(row) {
  confirmDoc.value = row
  confirmProgress.value = null
  confirmDrawerVisible.value = true
  await refreshProgress()
}

async function copyExistingLink(confId) {
  try {
    const res = await issueConfirmationLink(confId)
    copyText(publicLink(res.token))
  } catch (e) {
    // 全局拦截器已提示（如免登录通道未开启）
  }
}

async function revokeConfirm(item) {
  try {
    await ElMessageBox.confirm(
      `撤销后「${item.user_name}」将不再需要确认这份材料，确定吗？`,
      '撤销确认',
      { type: 'warning', confirmButtonText: '撤销', cancelButtonText: '取消' },
    )
  } catch (e) {
    return
  }
  await revokeConfirmation(item.id)
  ElMessage.success('已撤销')
  await refreshProgress()
  await loadDocs()
}

onMounted(async () => {
  if (route.query.highlight) {
    highlightId.value = String(route.query.highlight)
  }
  loadTaskOptions()
  await loadDocs()
  await locateHighlighted()
})

// 定位并高亮来自跳转参数的文档：若不在当前页，按文件名搜索后重新加载
async function locateHighlighted() {
  if (!highlightId.value) return
  if (!docs.value.some((d) => d.id === highlightId.value)) {
    try {
      const detail = await getDocument(highlightId.value)
      keyword.value = detail.filename || ''
      page.value = 1
      await loadDocs()
    } catch (e) {
      highlightId.value = ''
      return
    }
  }

  // 引用的可能是升版前的历史文档（列表默认只显示最新版）→ 退回高亮同文件名的最新版
  let tip = '已定位到引用来源对应的文档'
  if (!docs.value.some((d) => d.id === highlightId.value)) {
    const sameName = docs.value.find((d) => d.filename === keyword.value)
    if (!sameName) {
      highlightId.value = ''
      ElMessage.warning('未找到对应的文档（可能已被删除）')
      return
    }
    highlightId.value = sameName.id
    tip = '该引用来自历史版本，已定位到它的最新版'
  }

  nextTick(() => {
    const row = document.querySelector('.highlight-row')
    if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' })
    ElMessage.success(tip)
  })
}
</script>

<style scoped>
.ingest-page {
  max-width: 1100px;
  margin: 0 auto;
}
.ledger-card {
  border-radius: 12px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.header-actions {
  display: flex;
  gap: 12px;
}
.filter-bar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.extracted-list {
  display: flex;
  flex-direction: column;
  gap: 16px;
  max-height: 60vh;
  overflow-y: auto;
}
.extracted-item {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  padding: 16px;
  background: #fafafa;
}
.item-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}
.item-index {
  font-weight: 600;
  color: #6366f1;
}
.item-form {
  margin-top: 8px;
}
.empty-state {
  padding: 40px 0;
}
.ingest-page :deep(.el-table .highlight-row > td.el-table__cell) {
  background-color: #eef2ff !important;
}
/* 版本列 */
.ver-tag-wrap {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  cursor: pointer;
}
.ver-superseded {
  font-size: 12px;
  color: #909399;
}
/* 历史版本抽屉 */
.versions-body {
  min-height: 200px;
}
.ver-item {
  padding: 4px 0 8px;
}
.ver-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.ver-current {
  font-size: 12px;
  color: #67c23a;
}
.ver-old {
  font-size: 12px;
  color: #909399;
}
.ver-meta {
  margin-top: 6px;
  font-size: 12px;
  color: #909399;
}
.ver-actions {
  margin-top: 6px;
}
.task-tip {
  font-size: 12px;
  color: #909399;
  padding-left: 80px;
}
.dialog-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}
.dialog-title {
  font-size: 16px;
  font-weight: 600;
}
.dialog-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

/* 文档查看器容器 */
.content-viewer {
  position: relative;
  max-height: 72vh;
  overflow: auto;
  padding: 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fff;
}
/* 放大状态：占屏幕更大高度 */
.content-viewer.is-maximized {
  max-height: 88vh;
}
/* 放大状态时弹窗内边距收紧，内容区更大 */
.content-dialog :deep(.el-dialog__body) {
  padding: 16px 20px;
}

/* 缩放工具栏（浏览器风格） */
.zoom-toolbar {
  position: sticky;
  top: 0;
  z-index: 10;
  display: flex;
  align-items: center;
  gap: 2px;
  margin-bottom: 12px;
  padding: 4px 8px;
  background: rgba(255, 255, 255, 0.95);
  border-radius: 8px;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.1);
  width: fit-content;
}
.zoom-toolbar .zoom-btn {
  padding: 4px 8px;
  border: none;
  background: transparent;
}
.zoom-toolbar .zoom-btn:hover {
  background: #f3f4f6;
}
.zoom-percent {
  display: inline-block;
  min-width: 44px;
  text-align: center;
  font-size: 12px;
  color: #4b5563;
  font-weight: 500;
}

/* Markdown 文档渲染样式（类似 WPS 阅读视图） */
.markdown-body {
  padding: 8px 4px;
  line-height: 1.8;
  color: #1f2937;
  font-size: 14px;
  transform-origin: top left;
  transition: transform 0.15s ease;
}
.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 20px 0 12px;
  font-weight: 600;
  color: #111827;
  border-bottom: 1px solid #e5e7eb;
  padding-bottom: 6px;
}
.markdown-body :deep(h1) { font-size: 22px; }
.markdown-body :deep(h2) { font-size: 19px; }
.markdown-body :deep(h3) { font-size: 16px; }
.markdown-body :deep(p) {
  margin: 8px 0;
}
.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 8px 0;
  padding-left: 24px;
}
.markdown-body :deep(li) {
  margin: 4px 0;
}
.markdown-body :deep(table) {
  border-collapse: collapse;
  width: 100%;
  margin: 12px 0;
  font-size: 13px;
}
.markdown-body :deep(th),
.markdown-body :deep(td) {
  border: 1px solid #d1d5db;
  padding: 6px 10px;
  text-align: left;
}
.markdown-body :deep(th) {
  background: #f3f4f6;
  font-weight: 600;
}
.markdown-body :deep(tr:nth-child(even)) {
  background: #fafafa;
}
.markdown-body :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
  margin: 12px 0;
  cursor: zoom-in;
}
.markdown-body :deep(p:has(img)) {
  text-align: center;
}
.markdown-body :deep(blockquote) {
  border-left: 4px solid #6366f1;
  padding: 8px 12px;
  margin: 12px 0;
  background: #f5f3ff;
  color: #4b5563;
}
.markdown-body :deep(code) {
  background: #f3f4f6;
  padding: 2px 6px;
  border-radius: 4px;
  font-size: 13px;
}
.markdown-body :deep(pre) {
  background: #1f2937;
  color: #e5e7eb;
  padding: 12px;
  border-radius: 8px;
  overflow-x: auto;
}
.fallback-wrap {
  width: 100%;
}
.preview-hint {
  margin-bottom: 10px;
}
.markdown-body :deep(pre code) {
  background: transparent;
  color: inherit;
}

/* ---------- 确认清单 ---------- */
.confirm-tag {
  cursor: pointer;
}
.confirm-none {
  color: #c0c4cc;
}
.confirm-result {
  margin-top: 6px;
}
.link-tip {
  margin-top: 10px;
  padding: 10px 12px;
  background: #fffbeb;
  border: 1px solid #fde68a;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.7;
  color: #92400e;
}
.link-tip code {
  background: #fef3c7;
  padding: 1px 5px;
  border-radius: 4px;
}
.link-list {
  margin-top: 10px;
}
.link-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.link-name {
  width: 88px;
  flex-shrink: 0;
  font-size: 12px;
  color: #374151;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.link-input {
  flex: 1;
}
.progress-body {
  padding: 0 4px;
}
.progress-head {
  padding-bottom: 12px;
  border-bottom: 1px solid #f3f4f6;
}
.progress-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2937;
  word-break: break-all;
}
.progress-sub {
  margin: 4px 0 10px;
  font-size: 12px;
  color: #6b7280;
}
.progress-list {
  padding: 8px 0;
}
.progress-row {
  padding: 10px 0;
  border-bottom: 1px dashed #f3f4f6;
}
.row-main {
  display: flex;
  align-items: center;
  gap: 8px;
}
.row-name {
  font-size: 13px;
  font-weight: 500;
  color: #1f2937;
}
.row-rounds,
.row-unseen {
  font-size: 11px;
  color: #9ca3af;
}
.row-unseen {
  color: #b45309;
}
.row-comment {
  margin-top: 6px;
  padding: 6px 10px;
  background: #fef2f2;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.7;
  color: #b91c1c;
}
.row-meta {
  margin-top: 6px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  color: #9ca3af;
}
.progress-foot {
  padding-top: 12px;
  border-top: 1px solid #f3f4f6;
}
</style>
