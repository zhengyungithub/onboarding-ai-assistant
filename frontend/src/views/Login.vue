<template>
  <div class="login-container">
    <el-card class="login-card">
      <div class="login-header">
        <h2>智图 · 入职 AI 问答助手</h2>
        <p>新员工的问题，来这里问 — 请使用管理员分配的账号登录</p>
      </div>
      <el-form :model="loginForm" label-width="0" @submit.prevent="handleLogin">
        <el-form-item>
          <el-input v-model="loginForm.username" name="username" autocomplete="username" placeholder="用户名" prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="loginForm.password" name="password" type="password" autocomplete="current-password" placeholder="密码" prefix-icon="Lock" size="large" show-password @keyup.enter="handleLogin" />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="handleLogin">
          登录
        </el-button>
        <div class="forgot-link">
          <el-link type="primary" :underline="false" @click="goForgot">忘记密码？</el-link>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'

const router = useRouter()
const userStore = useUserStore()
const loading = ref(false)

const loginForm = ref({ username: '', password: '' })

async function handleLogin() {
  if (!loginForm.value.username || !loginForm.value.password) {
    ElMessage.warning('请输入用户名和密码')
    return
  }
  loading.value = true
  try {
    await userStore.login(loginForm.value.username, loginForm.value.password)
    ElMessage.success('登录成功')
    router.push('/')
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    loading.value = false
  }
}

function goForgot() {
  router.push('/forgot-password')
}
</script>

<style scoped>
.login-container {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.login-card {
  width: 420px;
  padding: 20px;
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.15);
}
.login-header {
  text-align: center;
  margin-bottom: 24px;
}
.login-header h2 {
  font-size: 22px;
  color: #1f2937;
  margin-bottom: 8px;
}
.login-header p {
  color: #6b7280;
  font-size: 14px;
}
.forgot-link {
  text-align: right;
  margin-top: 8px;
}
</style>
