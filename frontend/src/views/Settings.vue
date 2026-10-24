<template>
  <el-card>
    <template #header>
      <span>API Key 配置</span>
    </template>
    <el-form :model="form" label-width="140px" style="max-width: 600px">
      <el-form-item label="LLM Base URL">
        <el-input v-model="form.llm_base_url" placeholder="https://api.deepseek.com" />
      </el-form-item>
      <el-form-item label="LLM API Key">
        <el-input v-model="form.llm_api_key" type="password" show-password placeholder="输入你的 LLM API Key" />
        <div class="tip">已配置：{{ apiKeys?.llm_api_key_configured ? '✅ 是' : '❌ 否' }}</div>
      </el-form-item>
      <el-form-item label="LLM 模型">
        <el-input v-model="form.llm_model" placeholder="deepseek-chat" />
      </el-form-item>
      <el-form-item label="Embedding API Key">
        <el-input v-model="form.embedding_api_key" type="password" show-password placeholder="输入 Embedding API Key" />
        <div class="tip">已配置：{{ apiKeys?.embedding_api_key_configured ? '✅ 是' : '❌ 否' }}</div>
      </el-form-item>
      <el-form-item label="Embedding 模型">
        <el-input v-model="form.embedding_model" placeholder="bge-small-zh-v1.5" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="handleSave">保存配置</el-button>
      </el-form-item>
    </el-form>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { getApiKeys, updateApiKeys } from '@/api/auth'

const form = ref({
  llm_base_url: '',
  llm_api_key: '',
  llm_model: '',
  embedding_api_key: '',
  embedding_model: '',
})
const apiKeys = ref(null)

onMounted(async () => {
  try {
    apiKeys.value = await getApiKeys()
    form.value.llm_base_url = apiKeys.value.llm_base_url || ''
    form.value.llm_model = apiKeys.value.llm_model || ''
    form.value.embedding_model = apiKeys.value.embedding_model || ''
  } catch (e) {}
})

async function handleSave() {
  try {
    const data = { ...form.value }
    // 空字符串表示清空，null 表示不修改
    Object.keys(data).forEach((k) => {
      if (data[k] === '') data[k] = null
    })
    apiKeys.value = await updateApiKeys(data)
    ElMessage.success('配置已保存')
  } catch (e) {}
}
</script>

<style scoped>
.tip {
  font-size: 12px;
  color: #6b7280;
  margin-top: 4px;
}
</style>
