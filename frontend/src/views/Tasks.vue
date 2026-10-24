<template>
  <div class="tasks-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>我的任务</span>
          <el-button type="primary" :icon="Plus" @click="openCreateDialog">新建任务</el-button>
        </div>
      </template>

      <div class="agenda-bar">
        <el-popover
          v-for="card in agendaCards"
          :key="card.key"
          placement="bottom-start"
          :width="330"
          trigger="click"
        >
          <template #reference>
            <div class="agenda-card" :class="card.cls">
              <div class="agenda-num">{{ agenda.counts[card.key] }}</div>
              <div class="agenda-label">{{ card.label }}</div>
            </div>
          </template>
          <div class="agenda-list">
            <div
              v-for="it in agenda[card.key]"
              :key="it.type + '-' + it.id"
              class="agenda-item"
            >
              <el-tag size="small" effect="plain" :type="it.type === 'task' ? 'primary' : 'info'">
                {{ it.type === 'task' ? '任务' : '备忘' }}
              </el-tag>
              <span class="agenda-title" :title="it.title">{{ it.title }}</span>
              <span class="agenda-date">{{ formatDue(it.due_date) }}</span>
            </div>
            <el-empty v-if="!agenda[card.key].length" description="暂无" :image-size="48" />
          </div>
        </el-popover>
      </div>

      <div class="filter-bar">
        <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 120px" @change="loadTasks">
          <el-option label="进行中" value="in_progress" />
          <el-option label="已完成" value="completed" />
          <el-option label="已暂停" value="paused" />
        </el-select>
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
        :data="tasks"
        v-loading="loading"
        stripe
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="50" />
        <el-table-column prop="name" label="任务名称" min-width="200" />
        <el-table-column label="所属流程" min-width="220">
          <template #default="{ row }">
            <el-tag
              v-for="name in row.flow_names || [row.flow_name]"
              :key="name"
              size="small"
              type="info"
              style="margin-right: 4px; margin-bottom: 2px"
            >
              {{ name }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="到期" width="120">
          <template #default="{ row }">
            <el-tag
              v-if="row.due_date"
              size="small"
              :type="dueMeta(row.due_date, row.status).type"
              :effect="row.status === 'completed' ? 'plain' : 'light'"
            >
              {{ dueMeta(row.due_date, row.status).text }}
            </el-tag>
            <span v-else class="muted">-</span>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openDetail(row)">查看详情</el-button>
            <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建任务弹窗 -->
    <el-dialog v-model="createVisible" title="新建任务" width="500px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item label="任务名称" required>
          <el-input v-model="createForm.name" placeholder="如：2026届校招" />
        </el-form-item>
        <el-form-item label="流程模板" required>
          <el-select
            v-model="createForm.flow_ids"
            multiple
            filterable
            collapse-tags
            collapse-tags-tooltip
            placeholder="选择一个或多个流程模板"
            style="width: 100%"
          >
            <el-option v-for="f in flows" :key="f.id" :label="f.name" :value="f.id" />
          </el-select>
          <!-- 已选流程排序列表 -->
          <div v-if="createForm.flow_ids && createForm.flow_ids.length > 0" class="flow-order-list">
            <div class="flow-order-title">
              执行顺序（从上到下），可调整：
            </div>
            <div
              v-for="(fid, idx) in createForm.flow_ids"
              :key="fid"
              class="flow-order-item"
            >
              <span class="flow-order-num">{{ idx + 1 }}</span>
              <span class="flow-order-name">{{ getFlowName(fid) }}</span>
              <div class="flow-order-actions">
                <el-button
                  link
                  size="small"
                  :disabled="idx === 0"
                  @click="moveFlowUp(idx)"
                >↑ 上移</el-button>
                <el-button
                  link
                  size="small"
                  :disabled="idx === createForm.flow_ids.length - 1"
                  @click="moveFlowDown(idx)"
                >下移 ↓</el-button>
                <el-button
                  link
                  type="danger"
                  size="small"
                  @click="removeFlow(idx)"
                >移除</el-button>
              </div>
            </div>
          </div>
        </el-form-item>
        <el-form-item label="截止时间">
          <el-date-picker
            v-model="createForm.due_date"
            type="datetime"
            placeholder="可选，用于到期提醒"
            value-format="YYYY-MM-DDTHH:mm:ss"
            style="width: 100%"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">创建</el-button>
      </template>
    </el-dialog>

    <!-- 任务详情弹窗 -->
    <el-dialog
      v-model="detailVisible"
      :title="detail ? detail.name : '任务详情'"
      width="800px"
      custom-class="task-detail-dialog"
    >
      <div v-if="detail" class="task-detail">
        <el-descriptions :column="2" border size="small" style="margin-bottom: 16px">
          <el-descriptions-item label="所属流程">
            <el-tag
              v-for="name in (detail.flow_names && detail.flow_names.length ? detail.flow_names : [detail.flow_name])"
              :key="name"
              size="small"
              type="info"
              style="margin-right: 4px"
            >
              {{ name }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusType(detail.status)" size="small">{{ statusLabel(detail.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="截止时间">
            <span :class="{ 'overdue-text': isOverdue(detail) }">
              {{ detail.due_date ? formatDue(detail.due_date) : '-' }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatDate(detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="流程描述" :span="2">{{ detail.flow_description || '-' }}</el-descriptions-item>
        </el-descriptions>

        <!-- 三件套概览：这个活「做到哪、材料齐不齐、依据在哪」一屏看完 -->
        <div class="overview-bar">
          <div class="ov-item">
            <span class="ov-num">{{ overview.done }}/{{ overview.nodes }}</span>
            <span class="ov-label">节点完成</span>
          </div>
          <div class="ov-item">
            <span class="ov-num">{{ overview.docs }}</span>
            <span class="ov-label">
              份材料
              <em v-if="overview.pendingDocs" class="ov-warn">（{{ overview.pendingDocs }} 份待确认）</em>
            </span>
          </div>
          <div class="ov-item">
            <span class="ov-num">{{ overview.items }}</span>
            <span class="ov-label">条关联知识点</span>
          </div>
        </div>

        <h4>相关材料 <span class="doc-hint">（资料台账中关联到本任务的最新版）</span></h4>
        <div v-loading="docsLoading" class="task-docs">
          <div v-for="d in taskDocs" :key="d.id" class="task-doc-item">
            <el-tag size="small" effect="plain">{{ d.file_type || 'file' }}</el-tag>
            <span class="doc-name" :title="d.filename">{{ d.filename }}</span>
            <el-tag size="small" type="success" effect="plain">v{{ d.version || 1 }}</el-tag>
            <el-tag
              v-if="d.confirm_summary && d.confirm_summary.total"
              size="small"
              :type="confirmTagType(d.confirm_summary)"
              effect="light"
            >
              {{ confirmTagText(d.confirm_summary) }}
            </el-tag>
            <span class="doc-time">{{ formatDate(d.created_at) }}</span>
            <el-button link type="primary" size="small" @click="downloadDoc(d)">下载</el-button>
          </div>
          <el-empty
            v-if="!taskDocs.length"
            description="还没有关联材料，可在「资料入库」里给文档关联任务"
            :image-size="52"
          />
        </div>

        <h4>流程节点进度</h4>
        <div class="progress-list">
          <template v-for="(group, gIdx) in groupedProgress" :key="group.flow_name">
            <!-- 流程分组标题 -->
            <div class="flow-group-title">
              <el-tag size="small" type="warning" effect="plain">流程 {{ gIdx + 1 }}</el-tag>
              <span class="flow-group-name">{{ group.flow_name }}</span>
              <span class="flow-group-count">（{{ group.nodes.length }} 个节点）</span>
            </div>
            <div
              v-for="(p, idx) in group.nodes"
              :key="p.node_id"
              class="progress-item-wrap"
              :class="{ 'is-current': p.node_id === detail.current_node_id }"
            >
              <div class="progress-item">
                <div class="progress-left">
                  <div class="progress-circle" :class="circleClass(p.status)">
                    {{ p.status === 'completed' ? '✓' : globalSeq(p) }}
                  </div>
                  <div class="progress-info">
                    <div class="progress-name">{{ p.node_name }}</div>
                    <div class="progress-desc">{{ p.node_description || '无描述' }}</div>
                    <div v-if="p.blocked_by && p.blocked_by.length" class="node-blocked">
                      等待前置：{{ p.blocked_by.join('、') }}
                    </div>
                    <div v-else-if="p.is_gate || p.owner_name" class="node-meta">
                      <el-tag v-if="p.is_gate" size="small" type="warning" effect="plain">门禁</el-tag>
                      <span v-if="p.owner_name" class="node-owner">负责人 {{ p.owner_name }}</span>
                    </div>
                  </div>
                </div>
                <div class="progress-right">
                  <el-tag :type="statusType(p.status)" size="small" effect="plain">
                    {{ nodeStatusLabel(p.status) }}
                  </el-tag>
                  <div class="node-actions">
                    <el-button
                      v-if="p.status !== 'completed'"
                      type="success"
                      size="small"
                      :disabled="(p.node_id !== detail.current_node_id && detail.status !== 'in_progress') || p.can_complete === false"
                      @click="handleComplete(p)"
                    >
                      标记完成
                    </el-button>
                    <el-button
                      v-if="p.status === 'completed'"
                      type="warning"
                      size="small"
                      @click="handleIncomplete(p)"
                    >
                      标记未完成
                    </el-button>
                  </div>
                </div>
              </div>

              <!-- 进展流水：默认露出最近 3 条，被打断后回来一眼看到做到哪 -->
              <div class="node-logs">
                <div v-for="lg in (p.recent_logs || [])" :key="lg.id" class="log-line">
                  <span class="log-time">{{ formatLogTime(lg.created_at) }}</span>
                  <el-tag
                    v-if="lg.kind !== 'manual'"
                    size="small"
                    effect="plain"
                    :type="lg.kind === 'complete' ? 'success' : 'warning'"
                  >
                    {{ lg.kind === 'complete' ? '完成' : '回退' }}
                  </el-tag>
                  <span class="log-note">{{ lg.note }}</span>
                </div>

                <div class="log-actions">
                  <el-button
                    v-if="p.log_count > (p.recent_logs || []).length"
                    link
                    type="primary"
                    size="small"
                    @click="openAllLogs(p)"
                  >
                    查看全部 {{ p.log_count }} 条
                  </el-button>
                  <el-button
                    v-if="logEditing !== p.node_id"
                    link
                    size="small"
                    class="log-add-btn"
                    @click="startLog(p)"
                  >
                    ＋ 记一笔
                  </el-button>
                </div>

                <div v-if="logEditing === p.node_id" class="log-editor">
                  <el-input
                    v-model="logText"
                    size="small"
                    maxlength="500"
                    show-word-limit
                    placeholder="做到哪了？卡在谁那里？回车保存"
                    @keyup.enter="submitLog(p)"
                  />
                  <div class="log-editor-btns">
                    <el-button size="small" type="primary" :loading="logSaving" @click="submitLog(p)">
                      保存
                    </el-button>
                    <el-button size="small" @click="cancelLog">取消</el-button>
                  </div>
                </div>
              </div>
            </div>
          </template>
        </div>
      </div>
    </el-dialog>

    <!-- 某节点的全部进展 -->
    <el-dialog v-model="allLogsVisible" :title="allLogsTitle" width="560px">
      <div v-loading="allLogsLoading" class="all-logs">
        <el-timeline v-if="allLogs.length">
          <el-timeline-item
            v-for="lg in allLogs"
            :key="lg.id"
            :timestamp="formatDate(lg.created_at)"
            placement="top"
            :type="lg.kind === 'complete' ? 'success' : (lg.kind === 'incomplete' ? 'warning' : 'primary')"
          >
            <div class="all-log-row">
              <el-tag
                v-if="lg.kind !== 'manual'"
                size="small"
                effect="plain"
                :type="lg.kind === 'complete' ? 'success' : 'warning'"
              >
                {{ lg.kind === 'complete' ? '完成' : '回退' }}
              </el-tag>
              <span>{{ lg.note }}</span>
            </div>
          </el-timeline-item>
        </el-timeline>
        <el-empty v-else description="还没有进展记录" :image-size="60" />
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted, nextTick } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import {
  listTasks,
  getAgenda,
  createTask,
  getTask,
  deleteTask,
  completeNode,
  incompleteNode,
  addProgressLog,
  listProgressLogs,
} from '@/api/task'
import { listFlows } from '@/api/flow'
import { listDocuments, downloadDocument } from '@/api/sourceDocuments'

const tasks = ref([])
const loading = ref(false)
const statusFilter = ref('')
const selectedRows = ref([])

// 到期视图：逾期 / 今日 / 未来 7 天（任务 + 备忘录合并）
const agendaCards = [
  { key: 'overdue', label: '已逾期', cls: 'is-overdue' },
  { key: 'today', label: '今日到期', cls: 'is-today' },
  { key: 'this_week', label: '未来 7 天', cls: 'is-week' },
]
const agenda = ref({
  overdue: [],
  today: [],
  this_week: [],
  counts: { overdue: 0, today: 0, this_week: 0 },
})

function handleSelectionChange(rows) {
  selectedRows.value = rows
}

const createVisible = ref(false)
const creating = ref(false)
const createForm = ref({ name: '', flow_ids: [], due_date: null })
const flows = ref([])

const detailVisible = ref(false)
const detail = ref(null)

// 进展流水：一次只开一个「记一笔」编辑框，避免弹窗里出现一排输入框
const logEditing = ref('')
const logText = ref('')
const logSaving = ref(false)

// 某节点的全部进展
const allLogsVisible = ref(false)
const allLogs = ref([])
const allLogsNode = ref(null)
const allLogsLoading = ref(false)
const allLogsTitle = computed(() =>
  allLogsNode.value ? `「${allLogsNode.value.node_name}」的全部进展` : '全部进展'
)

// 任务关联的材料（资料台账里 task_id 指向本任务的最新版文档）
const taskDocs = ref([])
const docsLoading = ref(false)

async function loadTaskDocs(taskId) {
  docsLoading.value = true
  taskDocs.value = []
  try {
    const res = await listDocuments({ task_id: taskId, limit: 50 })
    taskDocs.value = res.items || []
  } catch (e) {
    // 材料聚合失败不影响任务详情主流程
  } finally {
    docsLoading.value = false
  }
}

// 三件套概览：节点进度 / 材料（含确认状态）/ 关联知识点
const overview = computed(() => {
  const nodes = detail.value?.progress || []
  const itemIds = new Set()
  nodes.forEach((n) => (n.related_items || []).forEach((i) => itemIds.add(i.id)))
  return {
    done: nodes.filter((n) => n.status === 'completed').length,
    nodes: nodes.length,
    docs: taskDocs.value.length,
    pendingDocs: taskDocs.value.filter((d) => (d.confirm_summary?.pending || 0) > 0).length,
    items: itemIds.size,
  }
})

function confirmTagType(s) {
  if (s.all_confirmed) return 'success'
  if (s.rejected) return 'danger'
  return 'warning'
}

function confirmTagText(s) {
  if (s.all_confirmed) return '已全部确认'
  if (s.rejected) return `${s.rejected} 人有问题`
  return `确认 ${s.confirmed}/${s.total}`
}

async function downloadDoc(d) {
  try {
    const blob = await downloadDocument(d.id)
    const url = window.URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = d.filename
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    window.URL.revokeObjectURL(url)
  } catch (e) {
    ElMessage.error('下载失败')
  }
}

// 按 node_order 排序后的节点进度，序号用数组位置 + 1
const sortedProgress = computed(() => {
  if (!detail.value || !detail.value.progress) return []
  return [...detail.value.progress].sort((a, b) => (a.node_order ?? 0) - (b.node_order ?? 0))
})

// 节点全局连续序号（跨流程分组累计，1..N）
const globalSeqMap = computed(() => {
  const m = new Map()
  let n = 0
  for (const g of groupedProgress.value) {
    for (const p of g.nodes) {
      n += 1
      m.set(p.node_id, n)
    }
  }
  return m
})

function globalSeq(p) {
  return globalSeqMap.value.get(p.node_id) ?? 1
}

// 按流程分组的进度，保持流程顺序
const groupedProgress = computed(() => {
  if (!detail.value || !detail.value.progress) return []
  const flowOrder = detail.value.flow_names || [detail.value.flow_name]
  const groups = []
  const map = {}
  for (const p of sortedProgress.value) {
    const fname = p.flow_name || '未分组'
    if (!map[fname]) {
      map[fname] = { flow_name: fname, nodes: [] }
      groups.push(map[fname])
    }
    map[fname].nodes.push(p)
  }
  // 按 flow_names 顺序排序
  groups.sort((a, b) => {
    const ia = flowOrder.indexOf(a.flow_name)
    const ib = flowOrder.indexOf(b.flow_name)
    if (ia === -1 && ib === -1) return 0
    if (ia === -1) return 1
    if (ib === -1) return -1
    return ia - ib
  })
  return groups
})

function formatDate(d) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN', { hour12: false })
}

// 简版到期时间：10-25 18:00
function formatDue(d) {
  if (!d) return '-'
  const dt = new Date(d)
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  const hh = String(dt.getHours()).padStart(2, '0')
  const mi = String(dt.getMinutes()).padStart(2, '0')
  return `${mm}-${dd} ${hh}:${mi}`
}

// 日志时间：今天只显示时分，昨天带前缀，更早显示日期（时间线里最常看的是"刚才"）
function formatLogTime(d) {
  if (!d) return '-'
  const dt = new Date(d)
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const day = new Date(dt.getFullYear(), dt.getMonth(), dt.getDate())
  const hm = `${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`
  const diff = Math.round((today - day) / 86400000)
  if (diff === 0) return hm
  if (diff === 1) return `昨天 ${hm}`
  const mm = String(dt.getMonth() + 1).padStart(2, '0')
  const dd = String(dt.getDate()).padStart(2, '0')
  return `${mm}-${dd} ${hm}`
}

// 到期状态：逾期 / 今天 / 明天 / 日期
function dueMeta(d, status) {
  if (!d) return { text: '-', type: 'info' }
  const due = new Date(d)
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  const dueDay = new Date(due.getFullYear(), due.getMonth(), due.getDate())
  const diff = Math.round((dueDay - today) / 86400000)

  if (status === 'completed') return { text: formatDue(d), type: 'info' }
  if (diff < 0) return { text: `逾期 ${-diff} 天`, type: 'danger' }
  if (diff === 0) return { text: '今天', type: 'warning' }
  if (diff === 1) return { text: '明天', type: 'primary' }
  return { text: formatDue(d), type: 'info' }
}

function isOverdue(task) {
  if (!task || !task.due_date || task.status === 'completed') return false
  const due = new Date(task.due_date)
  const now = new Date()
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate())
  return due < today
}

function statusLabel(s) {
  return { in_progress: '进行中', completed: '已完成', paused: '已暂停' }[s] || s
}

function statusType(s) {
  return { in_progress: 'primary', completed: 'success', paused: 'info', pending: 'info' }[s] || 'info'
}

function nodeStatusLabel(s) {
  return { pending: '未开始', in_progress: '进行中', completed: '已完成', skipped: '已跳过' }[s] || s
}

function circleClass(s) {
  if (s === 'completed') return 'done'
  if (s === 'in_progress') return 'active'
  return ''
}

async function loadAgenda() {
  try {
    agenda.value = await getAgenda({ days: 7 })
  } catch (e) {
    // 到期视图失败不影响任务列表主流程
  }
}

async function loadTasks() {
  loading.value = true
  try {
    const res = await listTasks({ status: statusFilter.value || undefined })
    tasks.value = res.tasks
  } finally {
    loading.value = false
  }
  // 任务增删改后到期视图需同步刷新，统一挂在这里避免遗漏
  loadAgenda()
}

async function loadFlows() {
  const res = await listFlows()
  flows.value = res.flows
}

function openCreateDialog() {
  createForm.value = { name: '', flow_ids: [], due_date: null }
  createVisible.value = true
}

function getFlowName(fid) {
  const f = flows.value.find((x) => x.id === fid)
  return f ? f.name : fid
}

function moveFlowUp(idx) {
  if (idx <= 0) return
  const arr = [...createForm.value.flow_ids]
  ;[arr[idx - 1], arr[idx]] = [arr[idx], arr[idx - 1]]
  createForm.value.flow_ids = arr
}

function moveFlowDown(idx) {
  const arr = createForm.value.flow_ids
  if (idx >= arr.length - 1) return
  ;[arr[idx], arr[idx + 1]] = [arr[idx + 1], arr[idx]]
  createForm.value.flow_ids = [...arr]
}

function removeFlow(idx) {
  const arr = [...createForm.value.flow_ids]
  arr.splice(idx, 1)
  createForm.value.flow_ids = arr
}

async function handleCreate() {
  if (!createForm.value.name.trim()) { ElMessage.warning('请输入任务名称'); return }
  if (!createForm.value.flow_ids || createForm.value.flow_ids.length === 0) { ElMessage.warning('请至少选择一个流程模板'); return }
  creating.value = true
  try {
    await createTask(createForm.value)
    ElMessage.success('任务创建成功')
    createVisible.value = false
    loadTasks()
  } finally {
    creating.value = false
  }
}

async function openDetail(row) {
  const res = await getTask(row.id)
  detail.value = res
  logEditing.value = ''
  logText.value = ''
  detailVisible.value = true
  loadTaskDocs(row.id)
}

// ===== 进展流水 =====

async function startLog(p) {
  logEditing.value = p.node_id
  logText.value = ''
  await nextTick()
  const el = document.querySelector('.log-editor input')
  if (el) el.focus()
}

function cancelLog() {
  logEditing.value = ''
  logText.value = ''
}

async function submitLog(p) {
  const text = (logText.value || '').trim()
  if (!text) {
    ElMessage.warning('写点什么再保存')
    return
  }
  logSaving.value = true
  try {
    await addProgressLog(detail.value.id, { node_id: p.node_id, note: text })
    logEditing.value = ''
    logText.value = ''
    // 重拉详情而不是本地拼接：节点顺序、状态、计数都以服务端为准
    detail.value = await getTask(detail.value.id)
    ElMessage.success('已记下')
  } catch (e) {
    // 错误提示由响应拦截器统一给出
  } finally {
    logSaving.value = false
  }
}

async function openAllLogs(p) {
  allLogsNode.value = p
  allLogsVisible.value = true
  allLogsLoading.value = true
  allLogs.value = []
  try {
    const res = await listProgressLogs(detail.value.id, { node_id: p.node_id, limit: 200 })
    allLogs.value = res.logs || []
  } finally {
    allLogsLoading.value = false
  }
}

async function handleComplete(p) {
  // 门禁节点：先让用户填过闸说明（后端要求 ≥5 字，前端先拦一道，避免白跑一次 409）
  let payload = null
  if (p.is_gate) {
    try {
      const { value } = await ElMessageBox.prompt(
        `「${p.node_name}」是门禁节点，过闸前请说明依据（至少 5 个字）：`,
        '过闸说明',
        {
          confirmButtonText: '确认完成',
          cancelButtonText: '取消',
          inputPlaceholder: '例如：已与张老师确认场地，可发布行程',
          inputValidator: (v) => (v && v.trim().length >= 5) || '至少填写 5 个字',
        }
      )
      payload = { remark: value.trim() }
    } catch (e) {
      return // 用户取消
    }
  }
  try {
    const res = await completeNode(detail.value.id, p.node_id, payload)
    detail.value = res
    ElMessage.success(`节点「${p.node_name}」已完成`)
    loadTasks()
  } catch (e) {
    // 被前置挡住 / 状态已变化：刷新一次详情，让阻塞信息与按钮状态回到真实值
    if (detail.value?.id) {
      try { detail.value = await getTask(detail.value.id) } catch (_) {}
    }
  }
}

async function handleIncomplete(p) {
  try {
    const res = await incompleteNode(detail.value.id, p.node_id)
    detail.value = res
    ElMessage.info(`节点「${p.node_name}」已标记为未完成`)
    loadTasks()
  } catch (e) {}
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除任务「${row.name}」吗？`, '确认', { type: 'warning' })
  try {
    await deleteTask(row.id)
    ElMessage.success('已删除')
    loadTasks()
  } catch (e) {}
}

async function handleBatchDelete() {
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `确定删除选中的 ${selectedRows.value.length} 个任务吗？`,
      '批量删除确认',
      { type: 'warning' }
    )
  } catch (e) {
    return // 用户取消
  }
  try {
    await Promise.all(selectedRows.value.map((r) => deleteTask(r.id)))
    ElMessage.success(`已删除 ${selectedRows.value.length} 个任务`)
    selectedRows.value = []
    loadTasks()
  } catch (e) {}
}

onMounted(() => {
  loadTasks()
  loadFlows()
})
</script>

<style scoped>
/* 任务详情弹窗：节点进度列表区域独立滚动 */
:deep(.task-detail-dialog) .el-dialog__body {
  padding-top: 10px;
}
.progress-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 55vh;
  overflow-y: auto;
  padding-right: 8px;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.filter-bar {
  margin-bottom: 16px;
}
/* 到期视图卡片 */
.agenda-bar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.agenda-card {
  flex: 1;
  padding: 12px 16px;
  border-radius: 10px;
  background: #f5f7fa;
  border-left: 3px solid #dcdfe6;
  cursor: pointer;
  transition: background 0.2s;
}
.agenda-card:hover { background: #eef1f6; }
.agenda-card.is-overdue { border-left-color: #f56c6c; }
.agenda-card.is-today { border-left-color: #e6a23c; }
.agenda-card.is-week { border-left-color: #409eff; }
.agenda-num { font-size: 22px; font-weight: 500; line-height: 1.2; }
.agenda-card.is-overdue .agenda-num { color: #f56c6c; }
.agenda-card.is-today .agenda-num { color: #e6a23c; }
.agenda-label { font-size: 12px; color: #909399; margin-top: 2px; }
.agenda-list { max-height: 300px; overflow-y: auto; }
.agenda-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px solid #f0f0f0;
}
.agenda-item:last-child { border-bottom: none; }
.agenda-title {
  flex: 1;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.agenda-date { font-size: 12px; color: #909399; white-space: nowrap; }
.overdue-text { color: #f56c6c; }
.muted { color: #c0c4cc; }
/* 任务关联材料 */
.doc-hint {
  font-size: 12px;
  font-weight: 400;
  color: #909399;
}
.task-docs {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 8px;
  max-height: 180px;
  overflow-y: auto;
}
/* 三件套概览 */
.overview-bar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
.ov-item {
  flex: 1;
  padding: 10px 14px;
  background: linear-gradient(160deg, #f8fafc, #eef2ff);
  border: 1px solid #e8ecf7;
  border-radius: 10px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.ov-num {
  font-size: 18px;
  font-weight: 700;
  color: #4338ca;
  line-height: 1.2;
}
.ov-label {
  font-size: 12px;
  color: #6b7280;
}
.ov-warn {
  font-style: normal;
  color: #b45309;
}
.task-doc-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 6px;
  background: #fafafa;
}
.task-doc-item .doc-name {
  flex: 1;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-doc-item .doc-time {
  font-size: 12px;
  color: #909399;
  white-space: nowrap;
}
/* 流程排序列表 */
.flow-order-list {
  margin-top: 12px;
  padding: 10px;
  background: #f5f7fa;
  border-radius: 6px;
}
.flow-order-title {
  font-size: 12px;
  color: #909399;
  margin-bottom: 8px;
}
.flow-order-item {
  display: flex;
  align-items: center;
  padding: 6px 10px;
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  margin-bottom: 6px;
}
.flow-order-num {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  background: #409eff;
  color: #fff;
  font-size: 12px;
  margin-right: 10px;
}
.flow-order-name {
  flex: 1;
  font-size: 14px;
}
.flow-order-actions {
  display: flex;
  gap: 8px;
}
/* 流程分组标题 */
.flow-group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  margin-bottom: 4px;
  padding: 6px 10px;
  background: #fdf6ec;
  border-left: 3px solid #e6a23c;
  border-radius: 4px;
}
.flow-group-name {
  font-weight: 600;
  font-size: 14px;
}
.flow-group-count {
  font-size: 12px;
  color: #909399;
}
.progress-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
/* 节点卡片：外层承载边框与高亮，内层只负责左右布局，日志挂在下方 */
.progress-item-wrap {
  display: flex;
  flex-direction: column;
  border-radius: 8px;
  border: 1px solid #e5e7eb;
  background: #f9fafb;
  overflow: hidden;
}
.progress-item-wrap.is-current {
  border-color: #6366f1;
  background: #eef2ff;
}
.progress-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
}
/* 进展流水 */
.node-logs {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 16px 10px 60px;
}
.log-line {
  display: flex;
  align-items: baseline;
  gap: 6px;
  font-size: 12px;
  line-height: 1.5;
}
.log-time {
  color: #9ca3af;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.log-note {
  color: #4b5563;
  word-break: break-word;
}
.log-actions {
  display: flex;
  gap: 10px;
  margin-top: 2px;
}
.log-add-btn { color: #909399; }
.log-editor {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}
.log-editor-btns {
  display: flex;
  gap: 6px;
}
/* 全部进展时间线 */
.all-logs {
  max-height: 60vh;
  overflow-y: auto;
  padding-right: 6px;
}
.all-log-row {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
}
.progress-left {
  display: flex;
  align-items: center;
  gap: 12px;
}
.progress-circle {
  width: 32px;
  height: 32px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: 600;
  background: #e5e7eb;
  color: #4b5563;
  font-size: 13px;
}
.progress-circle.active { background: #6366f1; color: #fff; }
.progress-circle.done { background: #10b981; color: #fff; }
.progress-name { font-weight: 600; font-size: 14px; }
.progress-desc { font-size: 12px; color: #6b7280; margin-top: 2px; }
/* 前置未完成：直接说明在等谁，避免用户对着灰按钮猜原因 */
.node-blocked { font-size: 12px; color: #b45309; margin-top: 4px; }
.node-meta { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
.node-owner { font-size: 12px; color: #9ca3af; }
.progress-right {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}
.node-actions { display: flex; gap: 6px; }
</style>
