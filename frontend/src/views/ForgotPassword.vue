<template>
  <div class="forgot-container">
    <el-card class="forgot-card">
      <div class="forgot-header">
        <h2>重置密码</h2>
        <p>通过邮箱验证重置您的登录密码</p>
      </div>
      <el-form :model="form" label-width="0" @submit.prevent="handleSubmit">
        <el-form-item>
          <el-input v-model="form.username" placeholder="用户名" prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.email" placeholder="注册邮箱" prefix-icon="Message" size="large" />
        </el-form-item>
        <el-form-item>
          <div class="code-row">
            <el-input v-model="form.code" placeholder="验证码" prefix-icon="Key" size="large" />
            <el-button :loading="sendingCode" :disabled="countdown > 0" @click="sendCode">
              {{ countdown > 0 ? `${countdown}s 后重发` : '发送验证码' }}
            </el-button>
          </div>
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.newPassword" type="password" placeholder="新密码（至少6位）" prefix-icon="Lock" size="large" show-password />
        </el-form-item>
        <el-form-item>
          <el-input v-model="form.confirmPassword" type="password" placeholder="确认新密码" prefix-icon="Lock" size="large" show-password @keyup.enter="handleSubmit" />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="submitting" @click="handleSubmit">
          重置密码
        </el-button>
        <div class="back-link">
          <el-link @click="router.push('/login')">← 返回登录</el-link>
        </div>
      </el-form>
    </el-card>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { forgotPassword, resetPassword } from '@/api/auth'

const router = useRouter()
const sendingCode = ref(false)
const submitting = ref(false)
const countdown = ref(0)
let timer = null

const form = ref({
  username: '',
  email: '',
  code: '',
  newPassword: '',
  confirmPassword: '',
})

function startCountdown() {
  countdown.value = 60
  timer = setInterval(() => {
    countdown.value--
    if (countdown.value <= 0) {
      clearInterval(timer)
      timer = null
    }
  }, 1000)
}

async function sendCode() {
  if (!form.value.username || !form.value.email) {
    ElMessage.warning('请先输入用户名和邮箱')
    return
  }
  sendingCode.value = true
  try {
    await forgotPassword({ username: form.value.username, email: form.value.email })
    ElMessage.success('验证码已发送到您的邮箱')
    startCountdown()
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    sendingCode.value = false
  }
}

async function handleSubmit() {
  const { username, email, code, newPassword, confirmPassword } = form.value
  if (!username || !email || !code || !newPassword || !confirmPassword) {
    ElMessage.warning('请填写所有字段')
    return
  }
  if (newPassword !== confirmPassword) {
    ElMessage.warning('两次输入的密码不一致')
    return
  }
  if (newPassword.length < 6) {
    ElMessage.warning('密码至少 6 位')
    return
  }
  submitting.value = true
  try {
    await resetPassword({ username, email, code, new_password: newPassword })
    ElMessage.success('密码重置成功，请使用新密码登录')
    router.push('/login')
  } catch (e) {
    // 错误已在拦截器处理
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.forgot-container {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
}
.forgot-card {
  width: 420px;
  padding: 20px;
  border-radius: 16px;
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.15);
}
.forgot-header {
  text-align: center;
  margin-bottom: 24px;
}
.forgot-header h2 {
  font-size: 22px;
  color: #1f2937;
  margin-bottom: 8px;
}
.forgot-header p {
  color: #6b7280;
  font-size: 14px;
}
.code-row {
  display: flex;
  gap: 10px;
  width: 100%;
}
.code-row .el-input {
  flex: 1;
}
.back-link {
  text-align: center;
  margin-top: 12px;
}
</style>
