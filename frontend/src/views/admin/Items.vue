<template>
  <div class="items-page">
    <el-card>
      <template #header>
        <div class="card-header">
          <span>知识库管理</span>
          <el-button type="primary" :icon="Plus" @click="openCreateDialog">新建条目</el-button>
        </div>
      </template>

      <div class="filter-bar">
        <el-input v-model="keyword" placeholder="搜索标题" style="width: 200px" clearable @keyup.enter="loadItems" />
        <el-select v-model="statusFilter" placeholder="状态" clearable style="width: 120px" @change="loadItems">
          <el-option label="草稿" value="draft" />
          <el-option label="已发布" value="published" />
        </el-select>
        <el-select
          v-model="flowFilter"
          placeholder="关联流程"
          clearable
          filterable
          style="width: 180px"
          @change="loadItems"
        >
          <el-option
            v-for="f in flows"
            :key="f.id"
            :label="f.name"
            :value="f.id"
          />
        </el-select>
        <el-button @click="loadItems">查询</el-button>
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
        :data="items"
        v-loading="loading"
        stripe
        :row-class-name="rowClassName"
        @selection-change="handleSelectionChange"
      >
        <el-table-column type="selection" width="50" :selectable="canEditRow" />
        <el-table-column label="标题" min-width="200">
          <template #default="{ row }">
            <span>{{ truncate(row.title, 15) }}</span>
            <el-button v-if="row.title && row.title.length > 15" link type="primary" size="small" @click="showDesc(row.title)">查看</el-button>
            <el-tag v-if="visTag(row)" size="small" :type="visTag(row).type" effect="plain" style="margin-left: 6px">{{ visTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="system_or_material" label="系统/物料" width="150" />
        <el-table-column label="关联流程" width="180">
          <template #default="{ row }">
            <div class="flow-tags">
              <el-tag
                v-for="name in getFlowNames(row.related_flow)"
                :key="name"
                size="small"
                type="info"
                style="margin-right: 4px; margin-bottom: 2px"
              >
                {{ name }}
              </el-tag>
              <span v-if="getFlowNames(row.related_flow).length === 0">-</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.status === 'published' ? 'success' : 'info'" size="small">
              {{ row.status === 'published' ? '已发布' : '草稿' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="180">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <template v-if="canEditRow(row)">
              <el-button link type="primary" size="small" @click="openEditDialog(row)">编辑</el-button>
              <el-button v-if="row.status === 'draft'" link type="success" size="small" @click="handlePublish(row)">发布</el-button>
              <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
            </template>
            <span v-else class="readonly-hint" title="点击查看内容" @click="openViewDialog(row)">共享内容·只读</span>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :total="total"
        :page-sizes="[10, 20, 50]"
        layout="total, sizes, prev, pager, next"
        @size-change="loadItems"
        @current-change="loadItems"
        style="margin-top: 16px; justify-content: flex-end"
      />
    </el-card>

    <!-- 新建/编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑条目' : '新建条目'" width="600px">
      <el-form :model="form" label-width="100px">
        <el-form-item label="标题" required>
          <el-input v-model="form.title" />
        </el-form-item>
        <el-form-item label="步骤">
          <el-input
            v-model="stepsText"
            type="textarea"
            :rows="4"
            placeholder="每行一个步骤"
          />
        </el-form-item>
        <el-form-item label="系统/物料">
          <el-input v-model="form.system_or_material" />
        </el-form-item>
        <el-form-item label="注意事项">
          <el-input v-model="form.caution" type="textarea" :rows="3" />
        </el-form-item>
        <el-form-item label="关联流程">
          <el-select
            v-model="form.related_flow"
            multiple
            filterable
            clearable
            placeholder="可多选，也可留空"
            style="width: 100%"
          >
            <el-option
              v-for="f in flows"
              :key="f.id"
              :label="f.name"
              :value="f.id"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="状态">
          <el-radio-group v-model="form.status">
            <el-radio value="draft">草稿</el-radio>
            <el-radio value="published">发布</el-radio>
          </el-radio-group>
        </el-form-item>
        <VisibilityPicker v-model:visibility="form.visibility" v-model:shared-ids="form.shared_user_ids" />
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="handleSave">保存</el-button>
      </template>
    </el-dialog>

    <!-- 查看全文弹窗 -->
    <el-dialog v-model="descDialogVisible" title="完整标题" width="500px">
      <div class="desc-full-content">{{ descFullText }}</div>
    </el-dialog>

    <!-- 共享内容只读详情弹窗 -->
    <el-dialog v-model="viewDialogVisible" title="知识条目详情（只读）" width="620px">
      <el-descriptions :column="1" border label-width="90px">
        <el-descriptions-item label="标题" label-class-name="view-label">{{ viewRow?.title || '-' }}</el-descriptions-item>
        <el-descriptions-item label="步骤" label-class-name="view-label">
          <ol v-if="viewRow?.steps?.length" class="view-steps">
            <li v-for="(s, i) in viewRow.steps" :key="i">{{ s }}</li>
          </ol>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="系统/物料" label-class-name="view-label">{{ viewRow?.system_or_material || '-' }}</el-descriptions-item>
        <el-descriptions-item label="注意事项" label-class-name="view-label">{{ viewRow?.caution || '-' }}</el-descriptions-item>
        <el-descriptions-item label="关联流程" label-class-name="view-label">
          <template v-if="getFlowNames(viewRow?.related_flow).length">
            <el-tag
              v-for="name in getFlowNames(viewRow.related_flow)"
              :key="name"
              size="small"
              type="info"
              style="margin-right: 4px; margin-bottom: 2px"
            >{{ name }}</el-tag>
          </template>
          <span v-else>-</span>
        </el-descriptions-item>
        <el-descriptions-item label="状态" label-class-name="view-label">
          <el-tag :type="viewRow?.status === 'published' ? 'success' : 'info'" size="small">
            {{ viewRow?.status === 'published' ? '已发布' : '草稿' }}
          </el-tag>
        </el-descriptions-item>
      </el-descriptions>
      <template #footer>
        <el-button @click="viewDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, nextTick, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { listItems, getItem, createItem, updateItem, deleteItem, publishItem } from '@/api/knowledge'
import { listFlows } from '@/api/flow'
import { useContentPermission } from '@/composables/useContentPermission'
import VisibilityPicker from '@/components/VisibilityPicker.vue'

const { canEditRow, visTag, defaultVisibility } = useContentPermission()

const route = useRoute()

// 从其他页面（如 AI 问答引用来源）跳转过来时，要高亮的条目 ID
const highlightId = ref('')

function rowClassName({ row }) {
  return row.id === highlightId.value ? 'highlight-row' : ''
}

const flows = ref([])
const items = ref([])
const total = ref(0)
const loading = ref(false)
const page = ref(1)
const pageSize = ref(10)
const keyword = ref('')
const statusFilter = ref('')
const flowFilter = ref('')
const selectedRows = ref([])

function handleSelectionChange(rows) {
  selectedRows.value = rows
}

const dialogVisible = ref(false)
const editing = ref(false)
const saving = ref(false)
const form = ref({
  title: '',
  steps: [],
  system_or_material: '',
  caution: '',
  related_flow: [],
  status: 'draft',
  visibility: 'private',
  shared_user_ids: [],
})
const stepsText = ref('')

function formatDate(d) {
  return new Date(d).toLocaleString('zh-CN')
}

async function loadFlows() {
  const res = await listFlows()
  flows.value = res.flows
}

// 兼容旧数据：字符串转数组
function normalizeRelatedFlow(val) {
  if (!val) return []
  if (Array.isArray(val)) return val
  return String(val).split(',').filter(Boolean)
}

// 根据流程 ID 列表获取流程名称
function getFlowNames(relatedFlow) {
  const ids = normalizeRelatedFlow(relatedFlow)
  const nameMap = {}
  flows.value.forEach((f) => {
    nameMap[f.id] = f.name
  })
  return ids.map((id) => nameMap[id] || id).filter(Boolean)
}

async function loadItems() {
  loading.value = true
  try {
    const res = await listItems({
      keyword: keyword.value || undefined,
      status: statusFilter.value || undefined,
      related_flow: flowFilter.value || undefined,
      skip: (page.value - 1) * pageSize.value,
      limit: pageSize.value,
    })
    items.value = res.items
    total.value = res.total
  } finally {
    loading.value = false
  }
}

function openCreateDialog() {
  editing.value = false
  form.value = {
    title: '', steps: [], system_or_material: '', caution: '',
    related_flow: [], status: 'draft',
    visibility: defaultVisibility(), shared_user_ids: [],
  }
  stepsText.value = ''
  dialogVisible.value = true
}

function openEditDialog(row) {
  editing.value = true
  form.value = {
    ...row,
    related_flow: normalizeRelatedFlow(row.related_flow),
    visibility: row.visibility || 'private',
    shared_user_ids: row.shared_user_ids || [],
  }
  stepsText.value = (row.steps || []).join('\n')
  dialogVisible.value = true
}

async function handleSave() {
  if (!form.value.title.trim()) {
    ElMessage.warning('请输入标题')
    return
  }
  form.value.steps = stepsText.value.split('\n').map((s) => s.trim()).filter(Boolean)
  saving.value = true
  try {
    if (editing.value) {
      await updateItem(form.value.id, form.value)
      ElMessage.success('更新成功')
    } else {
      await createItem(form.value)
      ElMessage.success('创建成功')
    }
    dialogVisible.value = false
    loadItems()
  } finally {
    saving.value = false
  }
}

async function handlePublish(row) {
  try {
    await publishItem(row.id)
    ElMessage.success('已发布并向量化')
    loadItems()
  } catch (e) {
    ElMessage.error('发布失败，请检查 Qdrant 是否启动')
  }
}

async function handleDelete(row) {
  await ElMessageBox.confirm(`确定删除「${row.title}」吗？`, '确认', { type: 'warning' })
  try {
    await deleteItem(row.id)
    ElMessage.success('已删除')
    loadItems()
  } catch (e) {}
}

async function handleBatchDelete() {
  if (selectedRows.value.length === 0) return
  await ElMessageBox.confirm(
    `确定删除选中的 ${selectedRows.value.length} 条知识条目吗？`,
    '批量删除确认',
    { type: 'warning' }
  )
  try {
    await Promise.all(selectedRows.value.map((r) => deleteItem(r.id)))
    ElMessage.success(`已删除 ${selectedRows.value.length} 条`)
    selectedRows.value = []
    loadItems()
  } catch (e) {}
}

const descFullText = ref('')
const descDialogVisible = ref(false)

// 共享内容只读详情
const viewDialogVisible = ref(false)
const viewRow = ref(null)

function openViewDialog(row) {
  viewRow.value = row
  viewDialogVisible.value = true
}

function truncate(text, maxLen) {
  if (!text) return '-'
  return text.length > maxLen ? text.slice(0, maxLen) + '...' : text
}

function showDesc(text) {
  descFullText.value = text || ''
  descDialogVisible.value = true
}

onMounted(async () => {
  if (route.query.highlight) {
    highlightId.value = String(route.query.highlight)
  }
  await loadFlows()
  await loadItems()
  await locateHighlighted()
})

// 定位并高亮来自跳转参数的条目：若不在当前页，按标题搜索后重新加载
async function locateHighlighted() {
  if (!highlightId.value) return
  if (!items.value.some((i) => i.id === highlightId.value)) {
    try {
      const detail = await getItem(highlightId.value)
      keyword.value = detail.title || ''
      statusFilter.value = ''
      flowFilter.value = ''
      page.value = 1
      await loadItems()
    } catch (e) {
      highlightId.value = ''
      return
    }
  }
  nextTick(() => {
    const row = document.querySelector('.highlight-row')
    if (row) row.scrollIntoView({ behavior: 'smooth', block: 'center' })
    ElMessage.success('已定位到引用来源对应的条目')
  })
}
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}
.filter-bar {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
}
.readonly-hint {
  font-size: 12px;
  color: #9ca3af;
  cursor: pointer;
  user-select: none;
}
.readonly-hint:hover {
  color: #409eff;
  text-decoration: underline;
}
.view-steps {
  margin: 0;
  padding-left: 20px;
}
.view-steps li {
  margin-bottom: 4px;
  line-height: 1.6;
}
.items-page :deep(.el-table .highlight-row > td.el-table__cell) {
  background-color: #eef2ff !important;
}
</style>
