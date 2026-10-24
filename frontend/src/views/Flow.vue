<template>
  <div class="flow-page">
    <!-- 选择任务 -->
    <el-card class="flow-selector-card">
      <template #header><span>选择工作任务</span></template>
      <el-select v-model="selectedTaskId" placeholder="请选择任务" style="width: 300px" @change="onTaskChange">
        <el-option
          v-for="t in tasks"
          :key="t.id"
          :label="truncateLabel(taskLabel(t))"
          :value="t.id"
        >
          <el-tooltip
            :content="taskLabel(t)"
            placement="right"
            :show-after="300"
            :disabled="taskLabel(t).length <= TASK_LABEL_MAX"
          >
            <span class="task-option-label">{{ truncateLabel(taskLabel(t)) }}</span>
          </el-tooltip>
        </el-option>
      </el-select>
    </el-card>

    <div v-if="currentTask" class="flow-content">
      <!-- 左侧：流程节点列表 -->
      <el-card class="nodes-card">
        <template #header>
          <span>{{ currentTask.name }} — 流程步骤</span>
          <el-tag :type="statusType(currentTask.status)" size="small" style="margin-left: 8px">
            {{ statusLabel(currentTask.status) }}
          </el-tag>
        </template>
        <div class="nodes-steps">
          <div
            v-for="(p, idx) in currentTask.progress"
            :key="p.node_id"
            class="step-item"
            :class="{
              active: p.node_id === selectedProgress?.node_id,
              done: p.status === 'completed',
              current: p.node_id === currentTask.current_node_id,
            }"
            @click="selectNode(p)"
          >
            <div class="step-circle">{{ p.status === 'completed' ? '✓' : idx + 1 }}</div>
            <div class="step-info">
              <div class="step-name">
                <span>{{ p.node_name }}</span>
                <el-tag
                  v-if="p.flow_name"
                  size="small"
                  type="info"
                  effect="plain"
                  class="flow-tag"
                >
                  {{ p.flow_name }}
                </el-tag>
              </div>
              <div class="step-desc">{{ truncate(p.node_description, 30) }}</div>
              <div v-if="p.blocked_by && p.blocked_by.length" class="step-blocked">
                等待前置：{{ p.blocked_by.join('、') }}
              </div>
            </div>
            <el-tag v-if="p.is_gate" size="small" type="warning" effect="plain" style="margin-right: 4px">门禁</el-tag>
            <el-tag v-if="p.node_id === currentTask.current_node_id && p.status !== 'completed'" type="primary" size="small">当前</el-tag>
            <el-tag v-else-if="p.status === 'completed'" type="success" size="small">已完成</el-tag>
          </div>
        </div>
      </el-card>

      <!-- 右侧：选中节点详情 -->
      <el-card v-if="selectedProgress" class="detail-card">
        <template #header>
          <span>节点详情</span>
        </template>
        <div class="detail-body">
          <div class="detail-row">
            <span class="detail-label">节点名称：</span>
            <span class="detail-value">{{ selectedProgress.node_name }}</span>
          </div>
          <div class="detail-row">
            <span class="detail-label">完成状态：</span>
            <el-tag :type="progressTagType(selectedProgress.status)" size="small">
              {{ progressLabel(selectedProgress.status) }}
            </el-tag>
          </div>
          <div v-if="selectedProgress.is_gate || selectedProgress.owner_name || (selectedProgress.blocked_by && selectedProgress.blocked_by.length)" class="detail-row">
            <span class="detail-label">流程约束：</span>
            <span class="detail-value">
              <el-tag v-if="selectedProgress.is_gate" size="small" type="warning" effect="plain" style="margin-right: 6px">门禁节点</el-tag>
              <span v-if="selectedProgress.owner_name" class="constraint-text">负责人 {{ selectedProgress.owner_name }}</span>
              <span v-if="selectedProgress.blocked_by && selectedProgress.blocked_by.length" class="constraint-blocked">
                等待前置：{{ selectedProgress.blocked_by.join('、') }}
              </span>
            </span>
          </div>
          <div class="detail-desc">
            <span class="detail-label">操作说明：</span>
            <div class="desc-text">{{ selectedProgress.node_description || '暂无描述' }}</div>
          </div>
          <div v-if="selectedProgress.related_items && selectedProgress.related_items.length" class="detail-related">
            <span class="detail-label">关联知识条目：</span>
            <div class="related-list">
              <div v-for="item in selectedProgress.related_items" :key="item.id" class="related-item">
                <div class="related-item-title">📄 {{ item.title }}</div>
                <div v-if="item.steps && item.steps.length" class="related-item-block">
                  <div class="related-item-label">操作步骤：</div>
                  <ol class="related-item-steps">
                    <li v-for="(step, i) in item.steps" :key="i">{{ step }}</li>
                  </ol>
                </div>
              </div>
            </div>
          </div>

          <!-- 操作按钮 -->
          <div class="detail-actions">
            <el-button
              v-if="selectedProgress.status !== 'completed'"
              type="success"
              :loading="marking"
              :disabled="selectedProgress.node_id !== currentTask.current_node_id || selectedProgress.can_complete === false"
              @click="handleComplete"
            >
              标记当前节点完成
            </el-button>
            <el-button
              v-if="selectedProgress.status === 'completed'"
              type="warning"
              :loading="marking"
              @click="handleUncomplete"
            >
              取消完成
            </el-button>
            <span v-if="selectedProgress.status !== 'completed' && selectedProgress.blocked_by && selectedProgress.blocked_by.length" class="hint-text hint-blocked">
              前置未完成：{{ selectedProgress.blocked_by.join('、') }}
            </span>
            <span v-else-if="selectedProgress.status !== 'completed' && selectedProgress.node_id !== currentTask.current_node_id" class="hint-text">
              请先完成前面的节点
            </span>
          </div>
        </div>
      </el-card>
    </div>

    <el-empty v-else-if="!selectedTaskId" description="请先选择一个工作任务" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { listTasks, getTask, completeNode, incompleteNode } from '@/api/task'

const tasks = ref([])
const selectedTaskId = ref('')
const currentTask = ref(null)
const selectedProgress = ref(null)
const marking = ref(false)

function statusLabel(s) {
  return { in_progress: '进行中', completed: '已完成', paused: '已暂停' }[s] || s
}

function statusType(s) {
  return { in_progress: 'primary', completed: 'success', paused: 'info' }[s] || 'info'
}

function progressLabel(s) {
  return { in_progress: '进行中', completed: '已完成', pending: '未开始' }[s] || s
}

function progressTagType(s) {
  return { in_progress: 'primary', completed: 'success', pending: 'info' }[s] || 'info'
}

function truncate(text, maxLen) {
  if (!text) return '无描述'
  return text.length > maxLen ? text.slice(0, maxLen) + '...' : text
}

// 任务下拉命名规则：「任务名称（关联流程1、关联流程2）」
const TASK_LABEL_MAX = 20

function taskLabel(t) {
  const flows = t.flow_names && t.flow_names.length ? t.flow_names : (t.flow_name ? [t.flow_name] : [])
  return flows.length ? `${t.name}（${flows.join('、')}）` : t.name
}

function truncateLabel(text) {
  return text.length > TASK_LABEL_MAX ? text.slice(0, TASK_LABEL_MAX) + '…' : text
}

async function loadTasks() {
  const res = await listTasks({ status: 'in_progress' })
  tasks.value = res.tasks
}

async function onTaskChange(taskId) {
  if (!taskId) {
    currentTask.value = null
    selectedProgress.value = null
    return
  }
  const res = await getTask(taskId)
  currentTask.value = res
  // 默认选中当前节点
  const current = res.progress?.find((p) => p.node_id === res.current_node_id)
  selectedProgress.value = current || res.progress?.[0] || null
}

function selectNode(p) {
  selectedProgress.value = p
}

async function handleComplete() {
  if (!selectedProgress.value) return
  const nodeId = selectedProgress.value.node_id

  // 门禁节点：先收过闸说明，用户取消就不发请求（后端也会校验，这里只是少跑一次 409）
  let payload = null
  if (selectedProgress.value.is_gate) {
    try {
      const { value } = await ElMessageBox.prompt(
        `「${selectedProgress.value.node_name}」是门禁节点，过闸前请说明依据（至少 5 个字）：`,
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
  } else {
    await ElMessageBox.confirm(`确定标记「${selectedProgress.value.node_name}」为已完成吗？`, '确认', { type: 'warning' })
  }

  marking.value = true
  try {
    const res = await completeNode(currentTask.value.id, nodeId, payload)
    currentTask.value = res
    // 选中下一个节点（新的 current_node）
    const next = res.progress?.find((p) => p.node_id === res.current_node_id)
    selectedProgress.value = next || null
    if (res.status === 'completed') {
      ElMessage.success('🎉 任务已全部完成！')
    } else {
      ElMessage.success('节点已完成，已推进到下一步')
    }
  } catch (e) {
    // 409（前置未完成 / 门禁缺说明）已由请求层提示，这里只把详情拉回真实状态，
    // 否则按钮和「等待前置」还停在上一次的快照上，用户会以为是页面坏了
    if (e.response?.status === 409 && currentTask.value?.id) {
      try {
        const fresh = await getTask(currentTask.value.id)
        currentTask.value = fresh
        selectedProgress.value = fresh.progress?.find((p) => p.node_id === nodeId) || selectedProgress.value
      } catch (_) {}
    } else if (!e.response) {
      ElMessage.error('操作失败')
    }
  } finally {
    marking.value = false
  }
}

async function handleUncomplete() {
  if (!selectedProgress.value) return
  const nodeId = selectedProgress.value.node_id
  await ElMessageBox.confirm(`确定取消「${selectedProgress.value.node_name}」的完成状态吗？将回退到该节点。`, '确认', { type: 'warning' })
  marking.value = true
  try {
    const res = await incompleteNode(currentTask.value.id, nodeId)
    currentTask.value = res
    selectedProgress.value = res.progress?.find((p) => p.node_id === nodeId) || null
    ElMessage.success('已回退到该节点')
  } catch (e) {
    ElMessage.error('操作失败')
  } finally {
    marking.value = false
  }
}

onMounted(loadTasks)
</script>

<style scoped>
.flow-page { max-width: 1200px; margin: 0 auto; }
.task-option-label {
  display: inline-block;
  max-width: 280px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  vertical-align: middle;
}
.flow-selector-card { margin-bottom: 16px; border-radius: 12px; }
.flow-content { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.nodes-card { border-radius: 12px; }
.detail-card { border-radius: 12px; }
.nodes-steps { display: flex; flex-direction: column; gap: 8px; max-height: 65vh; overflow-y: auto; padding-right: 4px; }
.step-item {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px; border-radius: 8px; cursor: pointer;
  border: 2px solid transparent; transition: all 0.2s;
}
.step-item:hover { background: #f3f4f6; }
.step-item.active { border-color: #f97316; background: #fff7ed; }
.step-item.done { opacity: 0.6; }
.step-item.current { border-color: #f97316; }
.step-circle {
  width: 32px; height: 32px; border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  font-weight: 600; background: #e5e7eb; color: #4b5563; flex-shrink: 0;
}
.step-item.active .step-circle,
.step-item.current .step-circle { background: #f97316; color: #fff; }
.step-item.done .step-circle { background: #10b981; color: #fff; }
.step-info { flex: 1; min-width: 0; }
.step-name { font-weight: 600; font-size: 14px; display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.flow-tag { flex-shrink: 0; color: #16a34a; border-color: #bbf7d0; background: #f0fdf4; }
.step-desc { font-size: 12px; color: #6b7280; margin-top: 2px; }
/* 前置未完成：给出"在等谁"，而不是只让按钮变灰 */
.step-blocked { font-size: 12px; color: #b45309; margin-top: 2px; }
.constraint-text { font-size: 12px; color: #6b7280; margin-right: 8px; }
.constraint-blocked { font-size: 12px; color: #b45309; }
.hint-blocked { color: #b45309; }

.detail-body { padding: 8px 0; }
.detail-row { display: flex; align-items: center; margin-bottom: 16px; }
.detail-label { font-weight: 600; color: #374151; min-width: 80px; flex-shrink: 0; }
.detail-value { color: #1f2937; }
.detail-desc { margin-bottom: 16px; }
.desc-text {
  margin-top: 8px; padding: 12px; background: #f9fafb;
  border-radius: 8px; line-height: 1.8; color: #1f2937; white-space: pre-wrap;
}
.detail-related { margin-bottom: 16px; }
.related-list { margin-top: 8px; display: flex; flex-direction: column; gap: 12px; }
.related-item {
  padding: 12px; background: #f9fafb; border-radius: 8px;
  border-left: 3px solid #f97316;
}
.related-item-title { font-weight: 600; color: #ea580c; margin-bottom: 8px; }
.related-item-block { margin-top: 8px; }
.related-item-label { font-size: 12px; color: #6b7280; font-weight: 600; margin-bottom: 4px; }
.related-item-steps { margin: 0; padding-left: 20px; color: #1f2937; line-height: 1.8; }
.related-item-text { color: #1f2937; line-height: 1.6; }
.related-item-text.caution { color: #d97706; }
.detail-actions { margin-top: 24px; display: flex; align-items: center; gap: 12px; }
.hint-text { font-size: 12px; color: #9ca3af; }
</style>
