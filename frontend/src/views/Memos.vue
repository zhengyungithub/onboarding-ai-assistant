<template>
  <div class="memos-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>备忘录（待办事项）</span>
          <el-button type="primary" :icon="Plus" @click="openCreateDialog">新建备忘录</el-button>
        </div>
      </template>

      <!-- 统计卡片 -->
      <div class="stats-row">
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value">{{ stats.total }}</div>
          <div class="stat-label">全部</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" style="color: #f59e0b">{{ stats.pending }}</div>
          <div class="stat-label">待办</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" style="color: #10b981">{{ stats.completed }}</div>
          <div class="stat-label">已完成</div>
        </el-card>
        <el-card shadow="hover" class="stat-card">
          <div class="stat-value" style="color: #ef4444">{{ stats.high }}</div>
          <div class="stat-label">高优先级</div>
        </el-card>
      </div>

      <!-- 主体：左侧文件夹 + 右侧列表 -->
      <div class="body-row">
        <!-- 文件夹面板 -->
        <div class="folder-panel">
          <div class="folder-header">
            <span class="folder-title">文件夹</span>
            <el-button link type="primary" :icon="FolderAdd" @click="handleCreateFolderClick">新建</el-button>
          </div>
          <div
            class="folder-item"
            :class="{ active: selectedFolder === 'all' }"
            @click="handleSelectFolder('all')"
          >
            <el-icon><FolderOpened /></el-icon>
            <span class="folder-name">全部</span>
            <span class="folder-count">{{ stats.total }}</span>
          </div>
          <div
            class="folder-item"
            :class="{ active: selectedFolder === 'ungrouped' }"
            @click="handleSelectFolder('ungrouped')"
          >
            <el-icon><Files /></el-icon>
            <span class="folder-name">未分组</span>
            <span class="folder-count">{{ ungroupedCount }}</span>
          </div>
          <!-- 自建文件夹：树形（支持子文件夹） -->
          <el-tree
            v-if="folderTree.length"
            :data="folderTree"
            node-key="id"
            :props="{ label: 'name', children: 'children' }"
            default-expand-all
            :expand-on-click-node="false"
            class="folder-tree"
          >
            <template #default="{ data }">
              <div
                class="tree-node"
                :class="{ active: selectedFolder === data.id }"
                @click="handleSelectFolder(data.id)"
              >
                <el-icon><Folder /></el-icon>
                <span class="folder-name" :title="data.name">{{ data.name }}</span>
                <span class="folder-count">{{ subtreeCount(data.id) }}</span>
                <span class="folder-ops" @click.stop>
                  <el-icon class="op-icon" title="新建子文件夹" @click="handleCreateSubFolder(data)"><FolderAdd /></el-icon>
                  <el-icon class="op-icon" title="重命名" @click="handleRenameFolder(data)"><EditPen /></el-icon>
                  <el-icon class="op-icon danger" title="删除" @click="handleDeleteFolder(data)"><Delete /></el-icon>
                </span>
              </div>
            </template>
          </el-tree>
          <div v-else class="tree-empty">暂无文件夹，点「新建」创建</div>
        </div>

        <!-- 右侧：筛选 + 列表 + 分页 -->
        <div class="main-col">
      <!-- 筛选栏 -->
      <div class="filter-bar">
        <el-select v-model="filter.completed" placeholder="状态" clearable style="width: 120px" @change="handleFilterChange">
          <el-option label="待办" :value="false" />
          <el-option label="已完成" :value="true" />
        </el-select>
        <el-select v-model="filter.priority" placeholder="优先级" clearable style="width: 120px" @change="handleFilterChange">
          <el-option label="高" value="high" />
          <el-option label="中" value="medium" />
          <el-option label="低" value="low" />
        </el-select>
        <el-input
          v-model="filter.keyword"
          placeholder="搜索标题"
          clearable
          style="width: 200px"
          @keyup.enter="handleFilterChange"
          @clear="handleFilterChange"
        >
          <template #prefix><el-icon><Search /></el-icon></template>
        </el-input>
      </div>

      <!-- 备忘录列表 -->
      <div class="memo-list" v-loading="loading">
        <div v-if="memos.length === 0" class="empty-state">
          <el-icon :size="48" color="#c0c4cc"><Document /></el-icon>
          <p>暂无备忘录，点击「新建备忘录」开始记录</p>
        </div>
        <div
          v-for="memo in memos"
          :key="memo.id"
          class="memo-item"
          :class="{ 'is-completed': memo.is_completed }"
        >
          <div class="memo-left">
            <el-checkbox
              :model-value="memo.is_completed"
              @change="handleToggle(memo)"
            />
            <div class="memo-priority-dot" :class="`priority-${memo.priority}`" />
          </div>
          <div class="memo-content" @click="openEditDialog(memo)">
            <div class="memo-title">{{ memo.title }}</div>
            <div v-if="memo.content" class="memo-text">{{ memo.content }}</div>
            <div class="memo-meta">
              <el-tag size="small" :type="priorityType(memo.priority)" effect="plain">
                {{ priorityLabel(memo.priority) }}
              </el-tag>
              <el-tag
                v-if="selectedFolder === 'all' && folderName(memo.folder_id)"
                size="small"
                type="info"
                effect="plain"
              >
                <el-icon style="vertical-align: -2px; margin-right: 2px"><Folder /></el-icon>
                {{ folderName(memo.folder_id) }}
              </el-tag>
              <span v-if="memo.due_date" class="due-date">
                <el-icon><Clock /></el-icon>
                {{ formatDate(memo.due_date) }}
              </span>
              <span class="create-time">{{ formatDate(memo.created_at) }}</span>
            </div>
          </div>
          <div class="memo-actions">
            <el-button link type="primary" size="small" @click.stop="openEditDialog(memo)">编辑</el-button>
            <el-button link type="danger" size="small" @click.stop="handleDelete(memo)">删除</el-button>
          </div>
        </div>
      </div>

      <!-- 分页 -->
      <div class="pagination-bar" v-if="total > 0">
        <el-pagination
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :total="total"
          :page-sizes="[10, 20, 50]"
          layout="total, sizes, prev, pager, next, jumper"
          background
          @current-change="loadMemos"
          @size-change="handleSizeChange"
        />
      </div>
        </div>
      </div>
    </el-card>

    <!-- 新建/编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="editMode ? '编辑备忘录' : '新建备忘录'" width="560px">
      <el-form :model="form" label-width="80px">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" placeholder="输入备忘录标题" maxlength="200" show-word-limit />
        </el-form-item>
        <el-form-item label="文件夹">
          <el-tree-select
            v-model="form.folder_id"
            :data="folderSelectData"
            check-strictly
            default-expand-all
            :render-after-expand="false"
            placeholder="未分组"
            clearable
            style="width: 100%"
          />
        </el-form-item>
        <el-form-item label="内容">
          <el-input
            v-model="form.content"
            type="textarea"
            :rows="4"
            placeholder="详细内容（可选）"
          />
        </el-form-item>
        <el-form-item label="优先级">
          <el-radio-group v-model="form.priority">
            <el-radio value="high" label="high">
              <span style="color: #ef4444">●</span> 高
            </el-radio>
            <el-radio value="medium" label="medium">
              <span style="color: #f59e0b">●</span> 中
            </el-radio>
            <el-radio value="low" label="low">
              <span style="color: #10b981">●</span> 低
            </el-radio>
          </el-radio-group>
        </el-form-item>
        <el-form-item label="截止日期">
          <el-date-picker
            v-model="form.due_date"
            type="datetime"
            placeholder="选择截止时间"
            style="width: 100%"
            value-format="YYYY-MM-DDTHH:mm:ss"
          />
        </el-form-item>
        <el-form-item v-if="editMode" label="状态">
          <el-switch
            v-model="form.is_completed"
            active-text="已完成"
            inactive-text="待办"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="submitting" @click="handleSubmit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from 'vue'
import { Plus, Search, Document, Clock, Folder, FolderAdd, FolderOpened, Files, EditPen, Delete } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  listMemos, createMemo, updateMemo, toggleMemo, deleteMemo,
  listFolders, createFolder, renameFolder, deleteFolder,
} from '@/api/memo'

const loading = ref(false)
const submitting = ref(false)
const memos = ref([])
const dialogVisible = ref(false)
const editMode = ref(false)

// 分页
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)

// 统计（全量口径，不受筛选/分页影响）
const stats = ref({ total: 0, pending: 0, completed: 0, high: 0 })

// 文件夹：'all' 全部 / 'ungrouped' 未分组 / 文件夹 id
const folders = ref([])
const selectedFolder = ref('all')
const ungroupedCount = computed(
  () => Math.max(0, stats.value.total - folders.value.reduce((s, f) => s + f.memo_count, 0))
)

const filter = reactive({
  completed: null,
  priority: '',
  keyword: '',
})

const form = reactive({
  id: '',
  title: '',
  content: '',
  priority: 'medium',
  due_date: null,
  is_completed: false,
  folder_id: '',
})

function priorityType(p) {
  return { high: 'danger', medium: 'warning', low: 'success' }[p] || 'info'
}

function priorityLabel(p) {
  return { high: '高', medium: '中', low: '低' }[p] || p
}

// 文件夹树（parent_id 层级；后端已保证无环、最多 3 层）
const folderTree = computed(() => {
  const nodes = folders.value.map((f) => ({ ...f, children: [] }))
  const byId = {}
  nodes.forEach((n) => { byId[n.id] = n })
  const roots = []
  nodes.forEach((n) => {
    if (n.parent_id && byId[n.parent_id]) byId[n.parent_id].children.push(n)
    else roots.push(n)
  })
  return roots
})

function findTreeNode(id) {
  const walk = (nodes) => {
    for (const n of nodes) {
      if (n.id === id) return n
      const found = walk(n.children)
      if (found) return found
    }
    return null
  }
  return walk(folderTree.value)
}

// 子树备忘录总数（直接 + 所有子孙文件夹，与列表聚合口径一致）
function subtreeCount(id) {
  const node = findTreeNode(id)
  if (!node) return 0
  const sum = (n) => n.memo_count + n.children.reduce((s, c) => s + sum(c), 0)
  return sum(node)
}

// id → 完整路径（如「工作/校招」），用于标签与树形下拉显示
const folderPathMap = computed(() => {
  const map = {}
  const walk = (nodes, prefix) => {
    for (const n of nodes) {
      const path = prefix ? `${prefix}/${n.name}` : n.name
      map[n.id] = path
      walk(n.children, path)
    }
  }
  walk(folderTree.value, '')
  return map
})

function folderName(id) {
  return (id && folderPathMap.value[id]) || ''
}

// 弹窗文件夹下拉的数据（树形，未分组作为首项）
const folderSelectData = computed(() => {
  const toNode = (n) => ({
    value: n.id,
    label: folderPathMap.value[n.id] || n.name,
    children: (n.children || []).map(toNode),
  })
  return [{ value: '', label: '未分组' }, ...folderTree.value.map(toNode)]
})

function formatDate(dateStr) {
  if (!dateStr) return ''
  const d = new Date(dateStr)
  const pad = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}

async function loadMemos() {
  loading.value = true
  try {
    const params = {
      skip: (page.value - 1) * pageSize.value,
      limit: pageSize.value,
    }
    if (filter.completed !== null && filter.completed !== '') {
      params.is_completed = filter.completed
    }
    if (filter.priority) {
      params.priority = filter.priority
    }
    if (filter.keyword) {
      params.keyword = filter.keyword
    }
    if (selectedFolder.value === 'ungrouped') {
      params.ungrouped = true
    } else if (selectedFolder.value !== 'all') {
      params.folder_id = selectedFolder.value
    }
    const res = await listMemos(params)
    memos.value = res.items || []
    total.value = res.total || 0
    // 当前页超出总页数（如删除/筛选后数据变少），回退到最后一页
    const maxPage = Math.max(1, Math.ceil(total.value / pageSize.value))
    if (page.value > maxPage) {
      page.value = maxPage
      loading.value = false
      return loadMemos()
    }
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    loading.value = false
  }
}

// 筛选条件变化 → 回到第 1 页重新加载
function handleFilterChange() {
  page.value = 1
  loadMemos()
}

// 每页条数变化 → 回到第 1 页
function handleSizeChange() {
  page.value = 1
  loadMemos()
}

// 统计卡片：拉全量数据计算（与筛选/分页无关）
async function loadStats() {
  try {
    const res = await listMemos({ skip: 0, limit: 500 })
    const all = res.items || []
    const completed = all.filter((m) => m.is_completed).length
    stats.value = {
      total: res.total || all.length,
      pending: (res.total || all.length) - completed,
      completed,
      high: all.filter((m) => m.priority === 'high' && !m.is_completed).length,
    }
  } catch (e) {
    // 错误已在拦截器处理
  }
}

function reloadAll() {
  loadMemos()
  loadStats()
  loadFolders()
}

// ---------- 文件夹 ----------

async function loadFolders() {
  try {
    const res = await listFolders()
    folders.value = res.items || []
  } catch (e) {
    // 错误已在拦截器处理
  }
}

function handleSelectFolder(id) {
  selectedFolder.value = id
  page.value = 1
  loadMemos()
}

async function handleCreateFolder(parent = null) {
  // 后端限制最多 3 层：若父文件夹已在第 3 层，提前提示
  if (parent) {
    const depthOf = (target) => {
      const walk = (nodes, level) => {
        for (const n of nodes) {
          if (n.id === target.id) return level
          const found = walk(n.children, level + 1)
          if (found) return found
        }
        return 0
      }
      return walk(folderTree.value, 1)
    }
    if (depthOf(parent) >= 3) {
      ElMessage.warning('文件夹最多支持 3 层，无法再在其下创建子文件夹')
      return
    }
  }
  try {
    const { value } = await ElMessageBox.prompt(
      parent ? `将在「${parent.name}」下创建子文件夹` : '请输入文件夹名称',
      parent ? '新建子文件夹' : '新建文件夹',
      {
        inputPattern: /\S+/,
        inputErrorMessage: '名称不能为空',
        inputValidator: (v) => (v && v.trim().length <= 50) || '名称不超过 50 个字符',
      }
    )
    await createFolder({ name: value.trim(), parent_id: parent ? parent.id : null })
    ElMessage.success('文件夹已创建')
    loadFolders()
  } catch (e) {
    // 取消或报错（报错已由拦截器提示）
  }
}

function handleCreateSubFolder(folder) {
  handleCreateFolder(folder)
}

// 顶部「新建」按钮：当前选中了某个具体文件夹 → 在其下新建子文件夹；否则新建顶级文件夹
function handleCreateFolderClick() {
  if (selectedFolder.value !== 'all' && selectedFolder.value !== 'ungrouped') {
    const node = findTreeNode(selectedFolder.value)
    if (node) {
      handleCreateFolder(node)
      return
    }
  }
  handleCreateFolder(null)
}

async function handleRenameFolder(folder) {
  try {
    const { value } = await ElMessageBox.prompt('请输入新的文件夹名称', '重命名文件夹', {
      inputValue: folder.name,
      inputPattern: /\S+/,
      inputErrorMessage: '名称不能为空',
      inputValidator: (v) => (v && v.trim().length <= 50) || '名称不超过 50 个字符',
    })
    await renameFolder(folder.id, { name: value.trim() })
    ElMessage.success('已重命名')
    loadFolders()
  } catch (e) {
    // 取消或报错
  }
}

async function handleDeleteFolder(folder) {
  // 统计子文件夹数量，删除提示要说清后果
  const subIds = []
  const collect = (n) => { n.children.forEach((c) => { subIds.push(c.id); collect(c) }) }
  const node = findTreeNode(folder.id)
  if (node) collect(node)
  try {
    await ElMessageBox.confirm(
      `确定删除文件夹「${folder.name}」吗？其中的 ${folder.memo_count} 条备忘录将移入「未分组」` +
        (subIds.length ? `，${subIds.length} 个子文件夹将上移一级` : '') +
        '，不会被删除。',
      '提示',
      { type: 'warning' }
    )
    await deleteFolder(folder.id)
    ElMessage.success('文件夹已删除')
    // 若当前选中的是被删文件夹或其子孙，回退到「全部」
    if (selectedFolder.value === folder.id || subIds.includes(selectedFolder.value)) {
      selectedFolder.value = 'all'
      page.value = 1
    }
    loadFolders()
    loadMemos()
  } catch (e) {
    // 取消或报错
  }
}

function openCreateDialog() {
  editMode.value = false
  form.id = ''
  form.title = ''
  form.content = ''
  form.priority = 'medium'
  form.due_date = null
  form.is_completed = false
  // 当前正选中的是某个文件夹 → 默认新建到该文件夹
  form.folder_id = selectedFolder.value !== 'all' && selectedFolder.value !== 'ungrouped'
    ? selectedFolder.value
    : ''
  dialogVisible.value = true
}

function openEditDialog(memo) {
  editMode.value = true
  form.id = memo.id
  form.title = memo.title
  form.content = memo.content || ''
  form.priority = memo.priority
  form.due_date = memo.due_date ? memo.due_date.slice(0, 19) : null
  form.is_completed = memo.is_completed
  form.folder_id = memo.folder_id || ''
  dialogVisible.value = true
}

async function handleSubmit() {
  if (!form.title.trim()) {
    ElMessage.warning('请输入标题')
    return
  }
  submitting.value = true
  try {
    const data = {
      title: form.title.trim(),
      content: form.content || null,
      priority: form.priority,
      due_date: form.due_date,
      folder_id: form.folder_id || null,
    }
    if (editMode.value) {
      if (form.is_completed !== undefined) {
        data.is_completed = form.is_completed
      }
      await updateMemo(form.id, data)
      ElMessage.success('已更新')
    } else {
      await createMemo(data)
      ElMessage.success('已创建')
    }
    dialogVisible.value = false
    reloadAll()
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    submitting.value = false
  }
}

async function handleToggle(memo) {
  try {
    await toggleMemo(memo.id)
    memo.is_completed = !memo.is_completed
    ElMessage.success(memo.is_completed ? '已完成' : '已恢复为待办')
    loadStats()
  } catch (e) {
    // 错误已在拦截器处理
  }
}

async function handleDelete(memo) {
  try {
    await ElMessageBox.confirm(`确定删除「${memo.title}」吗？`, '提示', { type: 'warning' })
    await deleteMemo(memo.id)
    ElMessage.success('已删除')
    reloadAll()
  } catch (e) {}
}

onMounted(() => {
  reloadAll()
})
</script>

<style scoped>
.memos-page {
  max-width: 1400px;
  margin: 0 auto;
}
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 16px;
  font-weight: 600;
}
.stats-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 16px;
  margin-bottom: 16px;
}
.stat-card {
  text-align: center;
}
.stat-value {
  font-size: 28px;
  font-weight: 700;
  color: #6366f1;
}
.stat-label {
  font-size: 13px;
  color: #6b7280;
  margin-top: 4px;
}
.body-row {
  display: flex;
  gap: 16px;
  align-items: flex-start;
}
.folder-panel {
  width: 210px;
  flex-shrink: 0;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  padding: 8px;
  background: #f9fafb;
}
.folder-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 4px 6px 8px;
  border-bottom: 1px solid #e5e7eb;
  margin-bottom: 6px;
}
.folder-title {
  font-size: 13px;
  font-weight: 600;
  color: #374151;
}
.folder-item {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 7px 8px;
  border-radius: 6px;
  cursor: pointer;
  color: #4b5563;
  font-size: 13px;
  transition: all 0.15s;
  position: relative;
}
.folder-item:hover {
  background: #eef2ff;
}
.folder-item.active {
  background: #e0e7ff;
  color: #4338ca;
  font-weight: 600;
}
.folder-item .el-icon {
  flex-shrink: 0;
}
.folder-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.folder-count {
  font-size: 12px;
  color: #9ca3af;
  flex-shrink: 0;
}
.folder-ops {
  display: none;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
}
.folder-item:hover .folder-ops {
  display: inline-flex;
}
.folder-item:hover .folder-count {
  display: none;
}
/* 树节点 hover 同样显示操作图标（新建子文件夹/重命名/删除） */
.folder-tree :deep(.el-tree-node__content:hover) .folder-ops {
  display: inline-flex;
}
.folder-tree :deep(.el-tree-node__content:hover) .folder-count {
  display: none;
}
.op-icon {
  font-size: 14px;
  color: #6b7280;
}
.op-icon:hover {
  color: #4f46e5;
}
.op-icon.danger:hover {
  color: #ef4444;
}
/* 文件夹树（el-tree）样式对齐面板 */
.folder-tree {
  background: transparent;
  --el-tree-node-content-height: 30px;
  font-size: 13px;
}
.folder-tree :deep(.el-tree-node__content) {
  border-radius: 6px;
  padding-right: 4px;
}
.folder-tree :deep(.el-tree-node__content:hover) {
  background: #eef2ff;
}
.tree-node {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
  color: #4b5563;
}
.tree-node.active {
  color: #4338ca;
  font-weight: 600;
}
.tree-node .el-icon {
  flex-shrink: 0;
}
.tree-empty {
  font-size: 12px;
  color: #9ca3af;
  text-align: center;
  padding: 16px 0;
}
.main-col {
  flex: 1;
  min-width: 0;
}
.filter-bar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.memo-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 200px;
}
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 200px;
  gap: 12px;
  color: #9ca3af;
}
.memo-item {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  padding: 16px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fff;
  transition: all 0.2s;
}
.memo-item:hover {
  border-color: #c7d2fe;
  box-shadow: 0 2px 8px rgba(99, 102, 241, 0.08);
}
.memo-item.is-completed {
  opacity: 0.6;
  background: #f9fafb;
}
.memo-item.is-completed .memo-title {
  text-decoration: line-through;
}
.memo-left {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-top: 2px;
}
.memo-priority-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.priority-high { background: #ef4444; }
.priority-medium { background: #f59e0b; }
.priority-low { background: #10b981; }
.memo-content {
  flex: 1;
  cursor: pointer;
  min-width: 0;
}
.memo-title {
  font-size: 15px;
  font-weight: 600;
  color: #1f2937;
  margin-bottom: 4px;
}
.memo-text {
  font-size: 13px;
  color: #6b7280;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  margin-bottom: 8px;
}
.memo-meta {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.due-date {
  font-size: 12px;
  color: #ef4444;
  display: flex;
  align-items: center;
  gap: 4px;
}
.create-time {
  font-size: 12px;
  color: #9ca3af;
}
.memo-actions {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex-shrink: 0;
}
.pagination-bar {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
