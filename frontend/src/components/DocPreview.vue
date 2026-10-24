<template>
  <div class="doc-preview">
    <!-- Excel / WPS 表格：网格视图（列头 A/B/C + 行号 + 多 sheet 标签） -->
    <template v-if="mode === 'sheet'">
      <div class="sheet-tabs" v-if="sheetNames.length > 1">
        <button
          v-for="name in sheetNames"
          :key="name"
          class="sheet-tab"
          :class="{ active: name === activeSheet }"
          @click="switchSheet(name)"
        >{{ name }}</button>
      </div>
      <div class="grid-wrap" ref="gridWrap">
        <table class="grid-table" v-if="gridReady">
          <thead>
            <tr>
              <th class="corner"></th>
              <th v-for="c in gridCols" :key="c" class="col-head">{{ c }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, r) in gridRows" :key="r">
              <td class="row-head">{{ r + 1 }}</td>
              <td
                v-for="(_, c) in row"
                :key="c"
                class="grid-cell"
                :class="{ 'is-empty': isEmpty(row[c]) }"
                :rowspan="rowSpan(r, c)"
                :colspan="colSpan(r, c)"
                v-show="!hiddenByMerge(r, c)"
              >{{ display(row[c]) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- Word：docx-preview 按原版式分页渲染 -->
    <div v-else-if="mode === 'word'" class="word-wrap" ref="wordWrap"></div>

    <!-- PDF：浏览器自带查看器 -->
    <iframe v-else-if="mode === 'pdf'" class="pdf-frame" :src="objectUrl" title="PDF 预览"></iframe>

    <!-- 文本 / Markdown -->
    <div v-else-if="mode === 'text'" class="markdown-body text-body" v-html="renderedText"></div>

    <el-empty v-else description="该格式暂不支持在线预览，请下载原文件查看" />
  </div>
</template>

<script setup>
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import * as XLSX from 'xlsx'
import { ElMessage } from 'element-plus'

const props = defineProps({
  blob: { type: Object, default: null }, // 原始文件 Blob
  filename: { type: String, default: '' },
})
const emit = defineEmits(['unsupported', 'error'])

const md = new MarkdownIt({ html: false, linkify: true, breaks: true })

const mode = ref('none')
const objectUrl = ref('')
const renderedText = ref('')

// ---- 表格网格状态 ----
const sheetNames = ref([])
const activeSheet = ref('')
const gridRows = ref([])
const gridCols = ref([])
const merges = ref([]) // [{s:{r,c},e:{r,c}}]
const gridReady = ref(false)

const MAX_ROWS = 500 // 预览上限，避免大表卡死
const MAX_COLS = 60

function ext() {
  const m = (props.filename || '').toLowerCase().match(/\.([a-z0-9]+)$/)
  return m ? m[1] : ''
}

function pickMode() {
  const e = ext()
  if (['xlsx', 'xls', 'csv'].includes(e)) return 'sheet'
  if (e === 'docx') return 'word'
  if (e === 'pdf') return 'pdf'
  if (['txt', 'md'].includes(e)) return 'text'
  return 'none'
}

// ---- Excel ----
let workbook = null

function colLetter(idx) {
  let s = ''
  let n = idx
  while (n >= 0) {
    s = String.fromCharCode((n % 26) + 65) + s
    n = Math.floor(n / 26) - 1
  }
  return s
}

async function renderSheet() {
  const buf = await props.blob.arrayBuffer()
  workbook = XLSX.read(buf, { type: 'array', cellDates: true })
  sheetNames.value = workbook.SheetNames
  activeSheet.value = workbook.SheetNames[0]
  drawSheet()
}

function drawSheet() {
  const ws = workbook.Sheets[activeSheet.value]
  if (!ws) return
  // !ref 决定范围；!merges 是合并单元格
  const ref = ws['!ref'] || 'A1'
  const range = XLSX.utils.decode_range(ref)
  const endRow = Math.min(range.e.r, MAX_ROWS - 1)
  const endCol = Math.min(range.e.c, MAX_COLS - 1)

  const rows = []
  for (let r = range.s.r; r <= endRow; r++) {
    const row = []
    for (let c = range.s.c; c <= endCol; c++) {
      const cell = ws[XLSX.utils.encode_cell({ r, c })]
      row.push(cell || null)
    }
    rows.push(row)
  }
  const ms = (ws['!merges'] || []).filter(
    (m) => m.e.r <= endRow && m.e.c <= endCol && m.s.r >= range.s.r && m.s.c >= range.s.c
  ).map((m) => ({
    r1: m.s.r - range.s.r, c1: m.s.c - range.s.c,
    r2: m.e.r - range.s.r, c2: m.e.c - range.s.c,
  }))
  gridRows.value = rows
  gridCols.value = Array.from({ length: endCol - range.s.c + 1 }, (_, i) => colLetter(range.s.c + i))
  merges.value = ms
  gridReady.value = true
}

function switchSheet(name) {
  activeSheet.value = name
  if (workbook) drawSheet()
}

function mergeAt(r, c) {
  return merges.value.find((m) => r >= m.r1 && r <= m.r2 && c >= m.c1 && c <= m.c2)
}
function rowSpan(r, c) {
  const m = mergeAt(r, c)
  return m && m.r1 === r && m.c1 === c ? m.r2 - m.r1 + 1 : 1
}
function colSpan(r, c) {
  const m = mergeAt(r, c)
  return m && m.r1 === r && m.c1 === c ? m.c2 - m.c1 + 1 : 1
}
function hiddenByMerge(r, c) {
  const m = mergeAt(r, c)
  return m && !(m.r1 === r && m.c1 === c)
}
function isEmpty(cell) {
  return !cell || (cell.v === undefined || cell.v === null || cell.v === '')
}
function display(cell) {
  if (!cell) return ''
  if (cell.t === 'd' && cell.v instanceof Date) {
    const p = (n) => String(n).padStart(2, '0')
    return `${cell.v.getFullYear()}-${p(cell.v.getMonth() + 1)}-${p(cell.v.getDate())}`
  }
  return cell.w !== undefined ? cell.w : cell.v ?? ''
}

// ---- Word ----
async function renderWord() {
  const docx = await import('docx-preview')
  await nextTick()
  const container = wordWrap.value
  container.innerHTML = ''
  await docx.renderAsync(props.blob, container, null, {
    inWrapper: true,
    ignoreWidth: false,
    ignoreHeight: false,
    breakPages: true,
  })
}

const wordWrap = ref(null)

// ---- 文本 / Markdown ----
async function renderText() {
  const text = await props.blob.text()
  const e = ext()
  renderedText.value = e === 'md' ? md.render(text) : `<pre class="plain-text">${escapeHtml(text)}</pre>`
}

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

async function render() {
  if (!props.blob) {
    mode.value = 'none'
    return
  }
  const m = pickMode()
  mode.value = m
  try {
    if (m === 'sheet') {
      await renderSheet()
    } else if (m === 'word') {
      await renderWord()
    } else if (m === 'pdf') {
      objectUrl.value = URL.createObjectURL(props.blob)
    } else if (m === 'text') {
      await renderText()
    } else {
      emit('unsupported')
    }
  } catch (err) {
    console.warn('DocPreview render failed, fallback to extracted text:', err)
    ElMessage.warning('原版式解析失败，已回退为提取文本预览')
    emit('error', err)
  }
}

watch(() => props.blob, (b) => {
  if (objectUrl.value) {
    URL.revokeObjectURL(objectUrl.value)
    objectUrl.value = ''
  }
  gridReady.value = false
  merges.value = []
  renderedText.value = ''
  workbook = null
  if (b) render()
  else mode.value = 'none'
}, { immediate: true })

onBeforeUnmount(() => {
  if (objectUrl.value) URL.revokeObjectURL(objectUrl.value)
  workbook = null
})

defineExpose({ switchSheet })
</script>

<style scoped>
.doc-preview {
  width: 100%;
  overflow: hidden;
}

/* ---- 表格网格 ---- */
.sheet-tabs {
  display: flex;
  gap: 2px;
  margin-bottom: 8px;
  border-bottom: 1px solid var(--el-border-color-light);
  padding-bottom: 4px;
  flex-wrap: wrap;
}
.sheet-tab {
  border: none;
  background: transparent;
  padding: 4px 14px;
  font-size: 13px;
  cursor: pointer;
  border-radius: 4px 4px 0 0;
  color: var(--el-text-color-regular);
}
.sheet-tab.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-weight: 600;
}
.grid-wrap {
  overflow: auto;
  max-height: 62vh;
  border: 1px solid var(--el-border-color-light);
}
.grid-table {
  border-collapse: collapse;
  font-size: 12px;
  table-layout: auto;
  min-width: 100%;
}
.grid-table th,
.grid-table td {
  border: 1px solid var(--el-border-color);
  padding: 4px 8px;
  white-space: nowrap;
  max-width: 260px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.col-head,
.row-head,
.corner {
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-weight: 600;
  text-align: center;
  position: sticky;
  font-size: 11px;
  min-width: 28px;
}
.col-head { top: 0; z-index: 2; }
.row-head { left: 0; z-index: 1; }
.corner { left: 0; top: 0; z-index: 3; }
.grid-cell { background: #fff; }
.grid-cell.is-empty { color: transparent; }

/* ---- Word ---- */
.word-wrap {
  max-height: 68vh;
  overflow: auto;
  background: var(--el-fill-color-lighter);
  padding: 12px 0;
}
.word-wrap :deep(.docx-wrapper) {
  background: transparent;
  padding: 8px;
}
.word-wrap :deep(.docx-wrapper > section.docx) {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.12);
  margin-bottom: 16px;
}

/* ---- PDF / 文本 ---- */
.pdf-frame {
  width: 100%;
  height: 68vh;
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
}
.text-body { max-height: 68vh; overflow: auto; }
.text-body :deep(.plain-text) {
  white-space: pre-wrap;
  word-break: break-all;
  font-family: inherit;
  margin: 0;
  line-height: 1.7;
}
</style>
