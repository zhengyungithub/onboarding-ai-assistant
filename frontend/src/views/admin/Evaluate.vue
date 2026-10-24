<template>
  <div class="evaluate-page">
    <!-- 统计卡片 -->
    <el-row :gutter="16" class="stats-row">
      <el-col :span="4">
        <el-card class="stat-card">
          <div class="stat-value">{{ stats.total_items }}</div>
          <div class="stat-label">知识条目总数</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card class="stat-card">
          <div class="stat-value">{{ stats.published_items }}</div>
          <div class="stat-label">已发布条目</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card class="stat-card">
          <div class="stat-value">{{ stats.total_cases }}</div>
          <div class="stat-label">测试用例数</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card class="stat-card">
          <div class="stat-value">{{ stats.latest_hit_rate !== null ? (stats.latest_hit_rate * 100).toFixed(1) + '%' : '—' }}</div>
          <div class="stat-label">最近命中率</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card class="stat-card">
          <div class="stat-value green">{{ stats.feedback_helpful }}</div>
          <div class="stat-label">有用反馈</div>
        </el-card>
      </el-col>
      <el-col :span="4">
        <el-card class="stat-card">
          <div class="stat-value red">{{ stats.feedback_unhelpful }}</div>
          <div class="stat-label">没用反馈</div>
          <div v-if="pendingFeedback.length" class="stat-hint">{{ pendingFeedback.length }} 条待处理</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 评测操作 -->
    <el-card class="action-card">
      <template #header>
        <div class="card-header">
          <span>一键评测</span>
          <el-button type="primary" :loading="running" @click="handleRun" :disabled="!stats.total_cases">运行评测</el-button>
        </div>
      </template>
      <div v-if="evalResult" class="eval-result">
        <el-result
          :icon="evalResult.hit_rate >= 0.8 ? 'success' : 'warning'"
          :title="`命中率：${(evalResult.hit_rate * 100).toFixed(1)}%`"
          :sub-title="`${evalResult.hit_count} / ${evalResult.total_cases} 条命中`"
        />
      </div>
    </el-card>

    <!-- 评测详情 -->
    <el-card v-if="evalResult?.details?.length" class="details-card">
      <template #header><span>评测详情</span></template>
      <el-table :data="evalResult.details" stripe>
        <el-table-column label="问题" prop="question" min-width="200" />
        <el-table-column label="期望条目" min-width="180">
          <template #default="{ row }">
            <el-tag
              v-for="(t, i) in (row.expected_item_titles?.length ? row.expected_item_titles : [row.expected_item_title])"
              :key="i"
              size="small"
              effect="plain"
              :type="isExpectedMatched(row, i) ? 'success' : 'info'"
              style="margin: 2px"
            >{{ t }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="命中" width="110">
          <template #default="{ row }">
            <el-tag :type="row.hit ? 'success' : 'danger'" size="small">{{ row.hit ? '✓ 命中' : '✗ 未命中' }}</el-tag>
            <div v-if="(row.expected_count || 1) > 1" class="hit-frac">{{ row.matched_count || 0 }}/{{ row.expected_count }} 条</div>
          </template>
        </el-table-column>
        <el-table-column label="实际检索结果" min-width="300">
          <template #default="{ row }">
            <div v-if="row.actual_items?.length">
              <el-tag v-for="(item, i) in row.actual_items" :key="i" size="small" :type="expectedIdsOf(row).includes(item.id) ? 'success' : 'info'" style="margin: 2px">
                {{ item.title }} ({{ item.score }})
              </el-tag>
            </div>
            <span v-else-if="row.error" class="error-text">{{ row.error }}</span>
            <span v-else>无结果</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 引用质量（改造 3 Verifier 的度量出口） -->
    <el-card class="citation-card" v-loading="loadingCitation">
      <template #header>
        <div class="card-header">
          <div class="header-left">
            <span>引用质量</span>
            <el-tag size="small" effect="plain" type="info">{{ citationScopeLabel }}</el-tag>
          </div>
          <el-button size="small" :icon="Refresh" @click="loadCitation">刷新</el-button>
        </div>
      </template>
      <div class="citation-tip">引用校验（Verifier）逐条核对回答里的数字、天数、系统名等硬信息能否在引用原文中找到，并把「答非所用」的来源剔除。<strong>引用支撑率</strong> = 保留 ÷（保留 + 剔除），它不依赖回答是否含硬信息，是判断「引用不相关」的主指标。校验结果<strong>只在 Agent 接管（AGENT_MODE=on）时为每条回答写入</strong>，shadow 模式下仅后台试跑留下少量样本。</div>
      <el-row :gutter="16" class="citation-metrics">
        <el-col :span="6">
          <div class="metric">
            <div class="metric-value" :class="rateClass(citation.citation_support_rate)">{{ pct(citation.citation_support_rate) }}</div>
            <div class="metric-label">引用支撑率（主指标）</div>
            <div class="metric-sub">{{ citation.kept_references }} 保留 / {{ citation.dropped_references }} 剔除</div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="metric">
            <div class="metric-value">{{ pct(citation.avg_coverage) }}</div>
            <div class="metric-label">硬信息覆盖率（辅助）</div>
            <div class="metric-sub">回答不含硬信息时为空</div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="metric">
            <div class="metric-value" :class="rateClass(citation.pass_rate)">{{ pct(citation.pass_rate) }}</div>
            <div class="metric-label">校验通过率</div>
            <div class="metric-sub">覆盖率 ≥ 85% 视为通过</div>
          </div>
        </el-col>
        <el-col :span="6">
          <div class="metric">
            <div class="metric-value">{{ citation.verified }}<span class="metric-unit"> / {{ citation.total }}</span></div>
            <div class="metric-label">已校验回答数</div>
            <div class="metric-sub">分母为最近 200 条 AI 回答</div>
          </div>
        </el-col>
      </el-row>
      <el-table :data="citation.recent" stripe size="small">
        <el-table-column label="时间" width="150">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="支撑率" width="100">
          <template #default="{ row }">
            <span :class="rateTextClass(row.support_rate)">{{ pct(row.support_rate) }}</span>
          </template>
        </el-table-column>
        <el-table-column label="硬信息覆盖率" width="120">
          <template #default="{ row }">{{ pct(row.coverage) }}</template>
        </el-table-column>
        <el-table-column prop="references" label="保留引用" width="90" />
        <el-table-column prop="partial" label="部分支撑" width="90" />
        <el-table-column prop="dropped" label="剔除引用" width="90" />
        <template #empty>
          <div class="empty-text">暂无校验数据 —— 把 AGENT_MODE 调成 on 并对话几轮后回来看</div>
        </template>
      </el-table>
    </el-card>

    <!-- 待处理负反馈 -->
    <el-card class="pending-card">
      <template #header>
        <div class="card-header">
          <div class="header-left">
            <span>待处理负反馈</span>
            <el-tag v-if="pendingFeedback.length" type="danger" size="small" effect="plain">
              共 {{ pendingFeedback.length }} 条
            </el-tag>
          </div>
          <el-button size="small" :icon="Refresh" @click="loadPending">刷新</el-button>
        </div>
      </template>
      <div class="pending-tip">
        用户点了「没用」的回答会汇总到这里。确认某条确实需要优化后，点「转为测试用例」把它沉淀进评测集——由你指定「正确答案应该是哪条」，系统不做自动推断。
      </div>
      <el-table :data="pendingFeedback" v-loading="loadingPending" stripe size="small">
        <el-table-column prop="question" label="用户提问" min-width="180" show-overflow-tooltip />
        <el-table-column prop="answer" label="AI 回答" min-width="220" show-overflow-tooltip />
        <el-table-column label="反馈原因" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span v-if="row.reason">{{ row.reason }}</span>
            <span v-else class="muted-text">未填写</span>
          </template>
        </el-table-column>
        <el-table-column label="当时引用来源" min-width="200">
          <template #default="{ row }">
            <template v-if="row.references?.length">
              <el-tag
                v-for="(ref, i) in row.references"
                :key="i"
                size="small"
                type="info"
                effect="plain"
                style="margin: 2px"
              >
                {{ ref.title }}
              </el-tag>
            </template>
            <span v-else class="muted-text">无引用</span>
          </template>
        </el-table-column>
        <el-table-column prop="username" label="反馈人" width="110" />
        <el-table-column label="反馈时间" width="150">
          <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="130">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openFeedbackDialog(row)">转为测试用例</el-button>
          </template>
        </el-table-column>
        <template #empty>
          <div class="empty-text">暂无待处理负反馈</div>
        </template>
      </el-table>
    </el-card>

    <!-- 测试用例管理 -->
    <el-card class="cases-card">
      <template #header>
        <div class="card-header">
          <span>测试用例管理</span>
          <el-button type="primary" size="small" :icon="Plus" @click="openCaseDialog">添加用例</el-button>
        </div>
      </template>
      <el-table :data="cases" v-loading="loadingCases" stripe size="small">
        <el-table-column prop="question" label="问题" min-width="250" />
        <el-table-column label="期望条目" min-width="180">
          <template #default="{ row }">
            <el-tag
              v-for="(t, i) in row.expected_item_titles"
              :key="i"
              size="small"
              effect="plain"
              style="margin: 2px"
            >{{ t }}</el-tag>
            <span v-if="!row.expected_item_titles?.length" class="muted-text">{{ row.expected_item_title }}</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="80">
          <template #default="{ row }">
            <el-tag :type="row.enabled ? 'success' : 'info'" size="small">{{ row.enabled ? '启用' : '禁用' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="来源" width="80">
          <template #default="{ row }">
            <el-tag v-if="row.source_message_id" type="warning" size="small" effect="plain">负反馈</el-tag>
            <span v-else class="muted-text">手工</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="80">
          <template #default="{ row }">
            <el-button link type="danger" size="small" @click="handleDeleteCase(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 添加用例弹窗 -->
    <el-dialog v-model="caseDialogVisible" title="添加测试用例" width="500px">
      <el-form :model="caseForm" label-width="80px">
        <el-form-item label="问题" required>
          <el-input v-model="caseForm.question" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="期望条目" required>
          <el-select
            v-model="caseForm.expected_item_ids"
            multiple
            collapse-tags
            collapse-tags-tooltip
            filterable
            placeholder="可多选；全部命中才算通过"
            style="width: 100%"
          >
            <el-option v-for="item in publishedItems" :key="item.id" :label="item.title" :value="item.id" />
          </el-select>
          <div class="form-hint">选多个时：AI 引用里必须包含全部所选条目，才算这道题通过。</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="caseDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSaveCase">保存</el-button>
      </template>
    </el-dialog>

    <!-- 负反馈转测试用例弹窗 -->
    <el-dialog v-model="fbDialogVisible" title="负反馈转测试用例" width="560px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        title="请确认识别出的问题，并指定「正确答案应该是哪条」知识条目。转换后该反馈不再出现在待处理列表。"
        class="fb-alert"
      />
      <el-form :model="fbForm" label-width="90px" class="fb-form">
        <el-form-item label="用户提问" required>
          <el-input v-model="fbForm.question" type="textarea" :rows="2" placeholder="若为空请手动填写" />
        </el-form-item>
        <el-form-item label="AI 原回答">
          <div class="fb-answer">{{ fbAnswer || '（无）' }}</div>
        </el-form-item>
        <el-form-item label="期望条目" required>
          <el-select
            v-model="fbForm.expected_item_ids"
            multiple
            collapse-tags
            collapse-tags-tooltip
            filterable
            placeholder="可多选；全部命中才算通过"
            style="width: 100%"
          >
            <el-option v-for="item in publishedItems" :key="item.id" :label="item.title" :value="item.id" />
          </el-select>
          <div class="form-hint">这题涉及多个知识点就都选上——AI 引用必须覆盖全部所选，才算答全。</div>
        </el-form-item>
        <el-form-item v-if="selectableRefs.length" label="引用速选">
          <div class="fb-refs">
            <el-tag
              v-for="ref in selectableRefs"
              :key="ref.item_id"
              size="small"
              :type="fbForm.expected_item_ids.includes(ref.item_id) ? 'primary' : 'info'"
              effect="plain"
              class="fb-ref-tag"
              @click="toggleFbRef(ref.item_id)"
            >
              {{ ref.title }}
            </el-tag>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fbDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="savingFb" @click="handleSaveFeedbackCase">确认转为用例</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { getStats, listCases, createCase, deleteCase, runEvaluation, listPendingFeedback, feedbackToCase, getCitationStats } from '@/api/evaluation'
import { listItems } from '@/api/knowledge'
import { useUserStore } from '@/stores/user'

const stats = ref({ total_items: 0, published_items: 0, total_cases: 0, latest_hit_rate: null, feedback_helpful: 0, feedback_unhelpful: 0 })
const cases = ref([])
const loadingCases = ref(false)
const running = ref(false)
const evalResult = ref(null)

const caseDialogVisible = ref(false)
const caseForm = ref({ question: '', expected_item_ids: [] })
const publishedItems = ref([])

const pendingFeedback = ref([])
const loadingPending = ref(false)
const fbDialogVisible = ref(false)
const fbForm = ref({ message_id: '', question: '', expected_item_ids: [] })
const fbAnswer = ref('')
const fbRefs = ref([])
const savingFb = ref(false)

// ===== 引用质量（改造 3 Verifier 度量） =====
const userStore = useUserStore()
const loadingCitation = ref(false)
const citation = ref({
  total: 0,
  verified: 0,
  citation_support_rate: null,
  avg_coverage: null,
  pass_rate: null,
  degraded_rate: null,
  kept_references: 0,
  dropped_references: 0,
  recent: [],
})
// scope=all 仅超管可看；管理员退化为只看自己
const citationScope = computed(() => (userStore.isSuperAdmin() ? 'all' : 'me'))
const citationScopeLabel = computed(() => (citationScope.value === 'all' ? '全站口径' : '仅本人'))

function pct(v) {
  if (v === null || v === undefined) return '—'
  return (v * 100).toFixed(1) + '%'
}

function rateClass(v) {
  if (v === null || v === undefined) return ''
  if (v >= 0.85) return 'good'
  if (v >= 0.55) return 'warn'
  return 'bad'
}

function rateTextClass(v) {
  if (v === null || v === undefined) return ''
  if (v >= 0.85) return 'text-good'
  if (v >= 0.55) return 'text-warn'
  return 'text-bad'
}

async function loadCitation() {
  loadingCitation.value = true
  try {
    citation.value = await getCitationStats({ scope: citationScope.value, limit: 200 })
  } catch (e) {
    ElMessage.error('引用质量数据加载失败')
  } finally {
    loadingCitation.value = false
  }
}

// 引用来源里能直接作为「期望条目」的（即已发布的知识条目），去重后供一键点选
const selectableRefs = computed(() => {
  const ids = new Set(publishedItems.value.map((i) => i.id))
  const out = []
  const seen = new Set()
  for (const r of fbRefs.value) {
    if (ids.has(r.item_id) && !seen.has(r.item_id)) {
      seen.add(r.item_id)
      out.push(r)
    }
  }
  return out
})

function formatTime(t) {
  if (!t) return '—'
  const d = new Date(t)
  const p = (n) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

async function loadPublishedItems(force = false) {
  if (!force && publishedItems.value.length) return
  const res = await listItems({ status: 'published', limit: 200 })
  publishedItems.value = res.items
}

async function loadStats() {
  stats.value = await getStats()
}

async function loadCases() {
  loadingCases.value = true
  try {
    const res = await listCases()
    cases.value = res.cases
  } finally { loadingCases.value = false }
}

async function openCaseDialog() {
  caseForm.value = { question: '', expected_item_ids: [] }
  await loadPublishedItems()
  caseDialogVisible.value = true
}

async function handleSaveCase() {
  if (!caseForm.value.question.trim() || !caseForm.value.expected_item_ids.length) {
    ElMessage.warning('请填写问题并至少选择一个期望条目')
    return
  }
  const titles = caseForm.value.expected_item_ids.map(
    (id) => publishedItems.value.find(i => i.id === id)?.title || ''
  )
  await createCase({
    question: caseForm.value.question,
    expected_item_ids: caseForm.value.expected_item_ids,
    expected_item_title: titles[0] || '',
  })
  ElMessage.success('用例已创建')
  caseDialogVisible.value = false
  loadCases()
  loadStats()
}

async function handleDeleteCase(row) {
  await ElMessageBox.confirm('确定删除该测试用例？', '确认', { type: 'warning' })
  await deleteCase(row.id)
  ElMessage.success('已删除')
  loadCases()
  loadStats()
}

async function handleRun() {
  running.value = true
  try {
    evalResult.value = await runEvaluation()
    ElMessage.success('评测完成')
    loadStats()
  } catch (e) {
    ElMessage.error('评测失败，请检查 API Key 和 Qdrant 配置')
  } finally {
    running.value = false
  }
}

async function loadPending() {
  loadingPending.value = true
  try {
    const res = await listPendingFeedback()
    pendingFeedback.value = res.items
  } finally {
    loadingPending.value = false
  }
}

async function openFeedbackDialog(row) {
  await loadPublishedItems()
  fbForm.value = { message_id: row.message_id, question: row.question || '', expected_item_ids: [] }
  fbAnswer.value = row.answer || ''
  fbRefs.value = row.references || []
  // 若当时的引用里恰好有已发布条目，默认选中第一条，管理员可按需多选
  const first = selectableRefs.value[0]
  if (first) fbForm.value.expected_item_ids = [first.item_id]
  fbDialogVisible.value = true
}

// 引用速选支持多选切换
function toggleFbRef(itemId) {
  const list = fbForm.value.expected_item_ids
  const i = list.indexOf(itemId)
  if (i >= 0) list.splice(i, 1)
  else list.push(itemId)
}

// 评测详情：某道题的期望条目 id 列表（兼容老数据）
function expectedIdsOf(row) {
  return row.expected_item_ids?.length ? row.expected_item_ids : [row.expected_item_id].filter(Boolean)
}

// 评测详情：第 i 个期望条目是否被命中（决定标签颜色）
function isExpectedMatched(row, i) {
  const ids = expectedIdsOf(row)
  const actual = (row.actual_items || []).map((x) => x.id)
  return actual.includes(ids[i])
}

async function handleSaveFeedbackCase() {
  if (!fbForm.value.question.trim()) {
    ElMessage.warning('请填写用户提问')
    return
  }
  if (!fbForm.value.expected_item_ids.length) {
    ElMessage.warning('请至少选择一个期望条目')
    return
  }
  savingFb.value = true
  try {
    await feedbackToCase({ ...fbForm.value })
    ElMessage.success('已转为测试用例，可在下方用例列表查看')
    fbDialogVisible.value = false
    loadPending()
    loadCases()
    loadStats()
  } finally {
    savingFb.value = false
  }
}

onMounted(() => {
  loadStats()
  loadCases()
  loadPending()
  loadCitation()
})
</script>

<style scoped>
.evaluate-page { display: flex; flex-direction: column; gap: 16px; }
.stats-row { margin-bottom: 4px; }
.stat-card { text-align: center; border-radius: 12px; }
.stat-value { font-size: 28px; font-weight: 700; color: #6366f1; }
.stat-value.green { color: #10b981; }
.stat-value.red { color: #ef4444; }
.stat-label { font-size: 12px; color: #6b7280; margin-top: 4px; }
.stat-hint { font-size: 12px; color: #ef4444; margin-top: 2px; }
.action-card { border-radius: 12px; }
.card-header { display: flex; justify-content: space-between; align-items: center; }
.header-left { display: flex; align-items: center; gap: 8px; }
.details-card { border-radius: 12px; }
.pending-card { border-radius: 12px; }
.cases-card { border-radius: 12px; }
.citation-card { border-radius: 12px; }
.citation-tip {
  font-size: 12px;
  color: #6b7280;
  line-height: 1.7;
  background: #f9fafb;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.citation-tip strong { color: #374151; font-weight: 500; }
.citation-metrics { margin-bottom: 12px; }
.metric {
  border: 1px solid #eef0f4;
  border-radius: 10px;
  padding: 12px 8px;
  text-align: center;
  height: 100%;
  box-sizing: border-box;
}
.metric-value { font-size: 24px; font-weight: 700; color: #111827; line-height: 1.2; }
.metric-value.good { color: #10b981; }
.metric-value.warn { color: #f59e0b; }
.metric-value.bad { color: #ef4444; }
.metric-unit { font-size: 14px; font-weight: 400; color: #9ca3af; }
.metric-label { font-size: 12px; color: #374151; margin-top: 4px; }
.metric-sub { font-size: 11px; color: #9ca3af; margin-top: 2px; }
.text-good { color: #10b981; }
.text-warn { color: #f59e0b; }
.text-bad { color: #ef4444; }
.error-text { font-size: 12px; color: #ef4444; }
.muted-text { font-size: 12px; color: #9ca3af; }
.form-hint { font-size: 12px; color: #6b7280; line-height: 1.5; margin-top: 4px; }
.hit-frac { font-size: 11px; color: #6b7280; margin-top: 2px; }
.empty-text { color: #9ca3af; font-size: 13px; padding: 12px 0; }
.pending-tip {
  font-size: 12px;
  color: #6b7280;
  line-height: 1.7;
  background: #f9fafb;
  border-radius: 8px;
  padding: 10px 12px;
  margin-bottom: 12px;
}
.fb-alert { margin-bottom: 16px; }
.fb-form { margin-top: 4px; }
.fb-answer {
  width: 100%;
  max-height: 120px;
  overflow-y: auto;
  font-size: 13px;
  color: #4b5563;
  line-height: 1.6;
  background: #f9fafb;
  border-radius: 6px;
  padding: 8px 10px;
  white-space: pre-wrap;
  word-break: break-word;
}
.fb-refs { display: flex; flex-wrap: wrap; }
.fb-ref-tag { cursor: pointer; margin: 2px 6px 2px 0; }
</style>
