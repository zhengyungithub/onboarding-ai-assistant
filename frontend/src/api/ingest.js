import request from './request'

// 结构化抽取
export function extractText(rawText) {
  return request.post('/ingest/extract', { raw_text: rawText })
}

// 批量入库
export function batchCreate(items) {
  return request.post('/ingest/batch', { items })
}
