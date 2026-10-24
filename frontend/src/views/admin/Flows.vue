<template>
  <div class="flows-admin-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>流程配置</span>
          <div class="header-actions">
            <el-button
              type="danger"
              plain
              size="small"
              :disabled="selectedRows.length === 0"
              @click="handleBatchDelete"
            >
              批量删除 ({{ selectedRows.length }})
            </el-button>
            <el-button type="primary" :icon="Plus" @click="openCreateDialog">新建流程</el-button>
          </div>
        </div>
      </template>

      <el-table
        :data="flows"
        v-loading="loading"
        stripe
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="50" :selectable="canEditRow" />
        <el-table-column label="流程名称" min-width="200">
          <template #default="{ row }">
            <span>{{ truncate(row.name, 15) }}</span>
            <el-button v-if="row.name && row.name.length > 15" link type="primary" size="small" @click="showDesc(row.name)">查看</el-button>
            <el-tag v-if="visTag(row)" size="small" :type="visTag(row).type" effect="plain" style="margin-left: 6px">{{ visTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="描述" min-width="250">
          <template #default="{ row }">
            <span>{{ truncate(row.description, 20) }}</span>
            <el-button v-if="row.description && row.description.length > 20" link type="primary" size="small" @click="showDesc(row.description)">查看</el-button>
          </template>
        </el-table-column>
        <el-table-column label="节点数" width="80">
          <template #default="{ row }">{{ row.nodes?.length || 0 }}</template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <template v-if="canEditRow(row)">
              <el-button link type="primary" size="small" @click="openEditDialog(row)">编辑</el-button>
              <el-button link type="primary" size="small" @click="openNodesDialog(row)">管理节点</el-button>
              <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
            </template>
            <el-button v-else link type="info" size="small" @click="openNodesDialog(row)">查看节点</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 新建/编辑流程弹窗 -->
    <el-dialog v-model="flowDialogVisible" :title="editing ? '编辑流程' : '新建流程'" width="500px">
      <el-form :model="flowForm" label-width="80px">
        <el-form-item label="名称" required>
          <el-input v-model="flowForm.name" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="flowForm.description" type="textarea" :rows="3" />
        </el-form-item>
        <VisibilityPicker v-model:visibility="flowForm.visibility" v-model:shared-ids="flowForm.shared_user_ids" />
      </el-form>
      <template #footer>
        <el-button @click="flowDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSaveFlow">保存</el-button>
      </template>
    </el-dialog>

    <!-- 节点管理弹窗 -->
    <el-dialog v-model="nodesDialogVisible" :title="nodesDialogEditable ? '管理节点' : '查看节点'" width="75%" class="nodes-dialog">
      <div v-if="currentFlow" class="nodes-manager">
        <h4>{{ currentFlow.name }} — 节点列表</h4>
        <el-button v-if="nodesDialogEditable" type="primary" size="small" :icon="Plus" style="margin-bottom: 12px" @click="openNodeDialog()">添加节点</el-button>
        <el-table :data="currentFlowNodes" stripe size="small" max-height="55vh" :row-class-name="nodeRowClassName">
          <el-table-column label="顺序" width="60" prop="order" />
          <el-table-column prop="name" label="节点名称" width="140" />
          <el-table-column label="工作流" width="190">
            <template #default="{ row }">
              <el-tag v-if="row.is_gate" size="small" type="warning" effect="plain" style="margin-right: 4px">门禁</el-tag>
              <el-tag v-if="row.depends_on && row.depends_on.length" size="small" type="info" effect="plain" style="margin-right: 4px">
                前置 {{ row.depends_on.length }}
              </el-tag>
              <span v-if="row.owner_name" class="wf-owner">{{ row.owner_name }}</span>
              <span v-else class="wf-owner muted">未指定负责人</span>
            </template>
          </el-table-column>
          <el-table-column label="描述" min-width="200">
            <template #default="{ row }">
              <span>{{ truncate(row.description, 20) }}</span>
              <el-button v-if="row.description && row.description.length > 20" link type="primary" size="small" @click="showDesc(row.description)">查看</el-button>
            </template>
          </el-table-column>
          <el-table-column prop="created_at" label="创建时间" width="160">
            <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="操作" width="150">
            <template #default="{ row }">
              <template v-if="nodesDialogEditable">
                <el-button link size="small" @click="openNodeDialog(row)">编辑</el-button>
                <el-button link type="danger" size="small" @click="handleDeleteNode(row)">删除</el-button>
              </template>
              <span v-else class="readonly-hint">共享流程·只读</span>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </el-dialog>

    <!-- 节点编辑弹窗 -->
    <el-dialog v-model="nodeDialogVisible" :title="editingNode ? '编辑节点' : '添加节点'" width="500px">
      <el-form :model="nodeForm" label-width="80px">
        <el-form-item label="名称" required>
          <el-input v-model="nodeForm.name" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="nodeForm.description" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="顺序">
          <el-input-number v-model="nodeForm.order" :min="1" />
          <span class="order-hint">填几就排第几位，其余节点自动顺延</span>
        </el-form-item>
        <el-form-item label="关联条目">
          <el-select v-model="nodeForm.related_item_ids" multiple filterable placeholder="选择关联知识条目" style="width: 100%">
            <el-option v-for="item in allItems" :key="item.id" :label="item.title" :value="item.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="前置节点">
          <el-select v-model="nodeForm.depends_on" multiple filterable placeholder="不选表示无前置" style="width: 100%">
            <el-option v-for="n in dependsOnOptions" :key="n.id" :label="`${n.order}. ${n.name}`" :value="n.id" />
          </el-select>
          <span class="order-hint">前置未完成的节点不能标记完成；只能选本流程内的节点</span>
        </el-form-item>
        <el-form-item label="负责人">
          <el-select
            v-model="nodeForm.owner_id"
            clearable
            filterable
            placeholder="不指定（仅做责任标注）"
            style="width: 100%"
            :disabled="!canPickOwner"
          >
            <el-option v-for="u in allUsers" :key="u.id" :label="u.username" :value="u.id" />
          </el-select>
          <span v-if="!canPickOwner" class="order-hint">只有管理员能指定负责人</span>
        </el-form-item>
        <el-form-item label="门禁节点">
          <el-switch v-model="nodeForm.is_gate" />
          <span class="order-hint">开启后，完成时必须填写过闸说明（至少 5 个字）</span>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="nodeDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingNode" @click="handleSaveNode">保存</el-button>
      </template>
    </el-dialog>

    <!-- 查看完整描述弹窗 -->
    <el-dialog v-model="descDialogVisible" title="完整描述" width="600px">
      <div class="desc-full-content">{{ descFullText }}</div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { listFlows, createFlow, updateFlow, deleteFlow, getFlow, addNode, updateNode, deleteNode } from '@/api/flow'
import { listItems } from '@/api/knowledge'
import { listUsers } from '@/api/auth'
import { useUserStore } from '@/stores/user'
import { useContentPermission } from '@/composables/useContentPermission'
import VisibilityPicker from '@/components/VisibilityPicker.vue'

const { canEditRow, visTag, defaultVisibility } = useContentPermission()

const route = useRoute()

// 从其他页面（如 AI 问答引用来源）跳转过来时，要高亮的流程节点 ID
const highlightNodeId = ref('')

function nodeRowClassName({ row }) {
  return row.id === highlightNodeId.value ? 'highlight-row' : ''
}

// 节点弹窗是否可编辑（共享流程对普通用户只读）
const nodesDialogEditable = computed(() => canEditRow(currentFlow.value))

const flows = ref([])
const loading = ref(false)
const selectedRows = ref([])

function handleSelectionChange(rows) {
  selectedRows.value = rows
}
const flowDialogVisible = ref(false)
const editing = ref(false)
const saving = ref(false)
const flowForm = ref({ name: '', description: '', visibility: 'private', shared_user_ids: [] })

const nodesDialogVisible = ref(false)
const currentFlow = ref(null)

const nodeDialogVisible = ref(false)
const editingNode = ref(null)
const savingNode = ref(false)
const nodeForm = ref({ name: '', description: '', order: 0, related_item_ids: [], depends_on: [], owner_id: null, is_gate: false })
const allItems = ref([])

// 前置节点候选：只允许同流程内、且不能是节点自己
const dependsOnOptions = computed(() =>
  currentFlowNodes.value.filter((n) => !editingNode.value || n.id !== editingNode.value.id)
)

// 负责人候选：只有管理员能拉用户列表（普通用户接口会 403，直接不请求）
const userStore = useUserStore()
const canPickOwner = computed(() => userStore.isStaff())
const allUsers = ref([])

async function loadUsers() {
  if (!canPickOwner.value) return
  try {
    allUsers.value = await listUsers()
  } catch (e) {
    allUsers.value = []
  }
}

// 选择关联条目后，自动用第一个条目的标题填充节点名称（仅当名称为空时）
watch(
  () => nodeForm.value.related_item_ids,
  (newIds) => {
    if (newIds && newIds.length > 0 && !nodeForm.value.name.trim()) {
      const firstItem = allItems.value.find((i) => i.id === newIds[0])
      if (firstItem) {
        nodeForm.value.name = firstItem.title
      }
    }
  }
)

const currentFlowNodes = ref([])

// 查看完整描述
const descDialogVisible = ref(false)
const descFullText = ref('')

function truncate(text, maxLen) {
  if (!text) return '-'
  return text.length > maxLen ? text.slice(0, maxLen) + '...' : text
}

function showDesc(text) {
  descFullText.value = text || ''
  descDialogVisible.value = true
}

function formatDate(d) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN', { hour12: false })
}

async function loadFlows() {
  loading.value = true
  try {
    const res = await listFlows()
    flows.value = res.flows
  } finally { loading.value = false }
}

function openCreateDialog() {
  editing.value = false
  flowForm.value = {
    name: '', description: '',
    visibility: defaultVisibility(), shared_user_ids: [],
  }
  flowDialogVisible.value = true
}

function openEditDialog(row) {
  editing.value = true
  flowForm.value = {
    ...row,
    visibility: row.visibility || 'private',
    shared_user_ids: row.shared_user_ids || [],
  }
  flowDialogVisible.value = true
}

async function handleSaveFlow() {
  if (!flowForm.value.name.trim()) { ElMessage.warning('请输入名称'); return }
  saving.value = true
  try {
    const payload = {
      name: flowForm.value.name,
      description: flowForm.value.description,
      visibility: flowForm.value.visibility || 'private',
      shared_user_ids: flowForm.value.shared_user_ids || [],
    }
    if (editing.value) {
      await updateFlow(flowForm.value.id, payload)
      ElMessage.success('更新成功')
    } else {
      await createFlow(payload)
      ElMessage.success('创建成功')
    }
    flowDialogVisible.value = false
    loadFlows()
  } finally { saving.value = false }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除「${row.name}」吗？`, '确认', { type: 'warning' })
  await deleteFlow(row.id)
  ElMessage.success('已删除')
  loadFlows()
}

async function handleBatchDelete() {
  if (selectedRows.value.length === 0) return
  await ElMessageBox.confirm(
    `确定删除选中的 ${selectedRows.value.length} 个流程吗？`,
    '批量删除确认',
    { type: 'warning' }
  )
  await Promise.all(selectedRows.value.map((r) => deleteFlow(r.id)))
  ElMessage.success(`已删除 ${selectedRows.value.length} 个流程`)
  selectedRows.value = []
  loadFlows()
}

async function openNodesDialog(row) {
  const res = await getFlow(row.id)
  currentFlow.value = res
  currentFlowNodes.value = [...(res.nodes || [])].sort((a, b) => a.order - b.order)
  // 加载知识条目供选择，只显示关联了当前流程的条目
  const itemsRes = await listItems({ status: 'published', related_flow: row.id, limit: 200 })
  allItems.value = itemsRes.items
  if (!allUsers.value.length) loadUsers()
  nodesDialogVisible.value = true
}

function openNodeDialog(node = null) {
  if (node) {
    editingNode.value = node
    nodeForm.value = {
      ...node,
      depends_on: node.depends_on ? [...node.depends_on] : [],
      owner_id: node.owner_id || null,
      is_gate: !!node.is_gate,
    }
  } else {
    editingNode.value = null
    const maxOrder = currentFlowNodes.value.length > 0
      ? Math.max(...currentFlowNodes.value.map((n) => n.order ?? 0))
      : -1
    nodeForm.value = {
      name: '', description: '', order: maxOrder + 1,
      related_item_ids: [], depends_on: [], owner_id: null, is_gate: false,
    }
  }
  nodeDialogVisible.value = true
}

async function handleSaveNode() {
  if (!nodeForm.value.name.trim()) { ElMessage.warning('请输入名称'); return }
  savingNode.value = true
  try {
    if (editingNode.value) {
      await updateNode(currentFlow.value.id, editingNode.value.id, nodeForm.value)
    } else {
      await addNode(currentFlow.value.id, nodeForm.value)
    }
    ElMessage.success('保存成功')
    nodeDialogVisible.value = false
    // 刷新节点列表
    const res = await getFlow(currentFlow.value.id)
    currentFlow.value = res
    currentFlowNodes.value = [...(res.nodes || [])].sort((a, b) => a.order - b.order)
    // 同步刷新外层流程列表的节点数
    loadFlows()
  } finally { savingNode.value = false }
}

async function handleDeleteNode(node) {
  await ElMessageBox.confirm(`确定删除节点「${node.name}」吗？`, '确认', { type: 'warning' })
  await deleteNode(currentFlow.value.id, node.id)
  ElMessage.success('已删除')
  const res = await getFlow(currentFlow.value.id)
  currentFlow.value = res
  currentFlowNodes.value = [...(res.nodes || [])].sort((a, b) => a.order - b.order)
  // 同步刷新外层流程列表的节点数
  loadFlows()
}

onMounted(async () => {
  await loadFlows()
  await locateHighlightedNode()
})

// 定位并高亮来自跳转参数的流程节点：找到包含该节点的流程，打开节点弹窗并高亮
async function locateHighlightedNode() {
  const nodeId = route.query.highlight ? String(route.query.highlight) : ''
  if (!nodeId) return
  highlightNodeId.value = nodeId
  const flow = flows.value.find((f) => (f.nodes || []).some((n) => n.id === nodeId))
  if (!flow) {
    highlightNodeId.value = ''
    return
  }
  await openNodesDialog(flow)
  nextTick(() => {
    const row = document.querySelector('.highlight-row')
    if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' })
    ElMessage.success('已定位到引用来源对应的流程节点')
  })
}
</script>

<style scoped>
.card-header { display: flex; justify-content: space-between; align-items: center; }
.order-hint { margin-left: 12px; font-size: 12px; color: #9ca3af; }
.readonly-hint { font-size: 12px; color: #9ca3af; }
.wf-owner { font-size: 12px; color: #374151; }
.wf-owner.muted { color: #9ca3af; }
.desc-full-content {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.6;
  font-size: 14px;
}
/* 管理节点弹窗：内容过多时弹窗整体可滚动，保证不超出屏幕 */
:deep(.nodes-dialog .el-dialog__body) {
  max-height: calc(90vh - 120px);
  overflow-y: auto;
}
/* 引用来源跳转过来的节点高亮 */
:deep(.el-table .highlight-row > td.el-table__cell) {
  background-color: #eef2ff !important;
}
</style>
