<template>
  <div class="users-page">
    <div class="page-header">
      <h2>用户管理</h2>
      <div class="header-actions">
        <el-button
          type="danger"
          plain
          :disabled="selectedRows.length === 0"
          @click="handleBatchDelete"
        >
          批量删除 ({{ selectedRows.length }})
        </el-button>
        <el-button type="primary" @click="showCreateDialog = true">
          <el-icon><Plus /></el-icon>
          添加用户
        </el-button>
      </div>
    </div>

    <!-- 用户列表 -->
    <el-table
      :data="users"
      v-loading="loading"
      border
      stripe
      style="width: 100%"
      @selection-change="handleSelectionChange"
    >
      <el-table-column type="selection" width="50" :selectable="canSelect" />
      <el-table-column prop="username" label="用户名" width="200" />
      <el-table-column prop="email" label="邮箱" min-width="200">
        <template #default="{ row }">
          {{ row.email || '—' }}
        </template>
      </el-table-column>
      <el-table-column prop="role" label="角色" width="120">
        <template #default="{ row }">
          <el-tag :type="roleTagType(row.role)">
            {{ roleLabel(row.role) }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="created_at" label="创建时间" min-width="180">
        <template #default="{ row }">
          {{ row.created_at ? new Date(row.created_at).toLocaleString('zh-CN') : '—' }}
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button
            type="primary"
            size="small"
            :disabled="!canEdit(row)"
            @click="openEditDialog(row)"
          >
            编辑
          </el-button>
          <el-button
            v-if="canDelete(row)"
            type="danger"
            size="small"
            @click="handleDelete(row)"
          >
            删除
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 创建用户对话框 -->
    <el-dialog v-model="showCreateDialog" title="添加用户" width="480px">
      <el-form :model="createForm" label-width="80px">
        <el-form-item label="用户名" required>
          <el-input v-model="createForm.username" placeholder="3-50 个字符" />
        </el-form-item>
        <el-form-item label="密码" required>
          <el-input v-model="createForm.password" type="password" placeholder="至少 6 位" show-password />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="createForm.email" placeholder="可选" />
        </el-form-item>
        <el-form-item label="角色" required>
          <el-select v-model="createForm.role" style="width: 100%">
            <el-option
              v-for="opt in assignableRoleOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <div class="role-tip">{{ roleTip(createForm.role) }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showCreateDialog = false">取消</el-button>
        <el-button type="primary" :loading="creating" @click="handleCreate">确认添加</el-button>
      </template>
    </el-dialog>

    <!-- 编辑用户对话框 -->
    <el-dialog v-model="showEditDialog" title="编辑用户" width="480px">
      <el-form :model="editForm" label-width="80px">
        <el-form-item label="用户名">
          <el-input v-model="editForm.username" placeholder="3-50 个字符" />
        </el-form-item>
        <el-form-item label="新密码">
          <el-input
            v-model="editForm.password"
            type="password"
            placeholder="留空则不修改密码"
            show-password
          />
        </el-form-item>
        <el-form-item label="邮箱">
          <el-input v-model="editForm.email" placeholder="可选" />
        </el-form-item>
        <el-form-item label="角色">
          <el-select
            v-model="editForm.role"
            style="width: 100%"
            :disabled="!canChangeEditRole"
          >
            <el-option
              v-for="opt in editRoleOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
          <div class="role-tip">{{ canChangeEditRole ? roleTip(editForm.role) : '仅超级管理员可调整管理员角色' }}</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="showEditDialog = false">取消</el-button>
        <el-button type="primary" :loading="editing" @click="handleEdit">保存修改</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus } from '@element-plus/icons-vue'
import { listUsers, createUser, updateUser, deleteUser } from '@/api/auth'
import { useUserStore } from '@/stores/user'

const userStore = useUserStore()

const ROLE_LABELS = { user: '普通用户', admin: '管理员', super_admin: '超级管理员' }
const ROLE_TIPS = {
  user: '普通用户：使用 AI 问答、流程步骤、任务与备忘录，并检索管理员共享的知识。',
  admin: '管理员：维护知识库 / 资料 / 流程 / 评测数据，可管理普通用户，创建的内容全员共享。',
  super_admin: '超级管理员：系统唯一账号，额外拥有角色分配与管理员管理能力，不可删除或降级。',
}

const roleLabel = (role) => ROLE_LABELS[role] || role
const roleTip = (role) => ROLE_TIPS[role] || ''
const roleTagType = (role) => (role === 'super_admin' ? 'danger' : role === 'admin' ? 'warning' : 'info')

// 可分配角色：超管可建管理员，管理员只能建普通用户
const assignableRoleOptions = computed(() => {
  const options = [{ label: '普通用户', value: 'user' }]
  if (userStore.isSuperAdmin()) options.push({ label: '管理员', value: 'admin' })
  return options
})

// 编辑时可选角色：超管可改角色；管理员编辑普通用户时角色锁定
const editRoleOptions = computed(() => {
  const options = [{ label: '普通用户', value: 'user' }]
  if (userStore.isSuperAdmin()) options.push({ label: '管理员', value: 'admin' })
  if (editForm.value.role === 'super_admin') options.unshift({ label: '超级管理员', value: 'super_admin' })
  return options
})

const canChangeEditRole = computed(() => {
  const target = users.value.find((u) => u.id === editForm.value.id)
  if (!target || !userStore.isSuperAdmin()) return false
  if (target.role === 'super_admin') return false // 超管账号不可改角色
  if (target.id === userStore.user?.id) return false // 不能改自己的角色
  return true
})

// 超管账号不可被编辑；管理员不能编辑管理员
function canEdit(row) {
  if (row.role === 'super_admin') return row.id === userStore.user?.id
  if (row.id === userStore.user?.id) return true
  if (userStore.isSuperAdmin()) return true
  return row.role === 'user'
}

// 超管账号不可删除；不能删自己；管理员不能删管理员
function canDelete(row) {
  if (row.role === 'super_admin') return false
  if (row.id === userStore.user?.id) return false
  return canEdit(row)
}

const users = ref([])
const loading = ref(false)
const selectedRows = ref([])
const showCreateDialog = ref(false)
const showEditDialog = ref(false)
const creating = ref(false)
const editing = ref(false)

function handleSelectionChange(rows) {
  selectedRows.value = rows
}

// 超管、管理员（仅超管可操作）、当前登录用户不可被勾选删除
function canSelect(row) {
  return canDelete(row)
}

const createForm = ref({
  username: '',
  password: '',
  email: '',
  role: 'user',
})

const editForm = ref({
  id: '',
  username: '',
  password: '',
  email: '',
  role: 'user',
})

async function fetchUsers() {
  loading.value = true
  try {
    const res = await listUsers()
    users.value = res
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    loading.value = false
  }
}

async function handleCreate() {
  if (!createForm.value.username || !createForm.value.password) {
    ElMessage.warning('请填写用户名和密码')
    return
  }
  if (createForm.value.username.length < 3) {
    ElMessage.warning('用户名至少 3 个字符')
    return
  }
  if (createForm.value.password.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  creating.value = true
  try {
    await createUser(createForm.value)
    ElMessage.success('用户创建成功')
    showCreateDialog.value = false
    createForm.value = { username: '', password: '', email: '', role: 'user' }
    await fetchUsers()
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    creating.value = false
  }
}

function openEditDialog(row) {
  editForm.value = {
    id: row.id,
    username: row.username,
    password: '',
    email: row.email || '',
    role: row.role,
  }
  showEditDialog.value = true
}

async function handleEdit() {
  if (editForm.value.username && editForm.value.username.length < 3) {
    ElMessage.warning('用户名至少 3 个字符')
    return
  }
  if (editForm.value.password && editForm.value.password.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  editing.value = true
  try {
    const data = {}
    if (editForm.value.username) data.username = editForm.value.username
    if (editForm.value.password) data.password = editForm.value.password
    data.email = editForm.value.email || null
    data.role = editForm.value.role
    await updateUser(editForm.value.id, data)
    ElMessage.success('用户信息已更新')
    showEditDialog.value = false
    await fetchUsers()
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    editing.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除用户「${row.username}」吗？该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }
    )
    await deleteUser(row.id)
    ElMessage.success('删除成功')
    await fetchUsers()
  } catch (e) {
    // 取消或错误
  }
}

async function handleBatchDelete() {
  if (selectedRows.value.length === 0) return
  try {
    await ElMessageBox.confirm(
      `确定删除选中的 ${selectedRows.value.length} 个用户吗？该操作不可恢复。`,
      '批量删除确认',
      { type: 'warning', confirmButtonText: '确认删除', cancelButtonText: '取消' }
    )
  } catch (e) {
    return // 用户取消
  }
  const results = await Promise.allSettled(selectedRows.value.map((r) => deleteUser(r.id)))
  const okCount = results.filter((r) => r.status === 'fulfilled').length
  const failCount = results.length - okCount
  if (okCount > 0) ElMessage.success(`已删除 ${okCount} 个用户`)
  if (failCount > 0) ElMessage.warning(`${failCount} 个用户删除失败`)
  selectedRows.value = []
  await fetchUsers()
}

onMounted(() => {
  fetchUsers()
})
</script>

<style scoped>
.users-page {
  padding: 20px;
}
.page-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 20px;
}
.page-header h2 {
  margin: 0;
  font-size: 20px;
  color: #1f2937;
}
.header-actions {
  display: flex;
  align-items: center;
  gap: 12px;
}
.role-tip {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: #6b7280;
}
</style>
