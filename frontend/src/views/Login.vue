<template>
  <div class="login-page">
    <!-- 左侧品牌区 -->
    <div class="brand-pane">
      <div class="brand-inner">
        <div class="brand-logo">
          <span class="logo-mark">知</span>
          <span class="logo-text">知途</span>
        </div>
        <h1 class="brand-title">新员工的问题，<br />来这里问。</h1>
        <p class="brand-sub">
          制度、流程、材料、考勤——入职路上遇到的每个疑问，<br class="wide-only" />
          知途都帮你找到有出处的答案。
        </p>
        <div class="brand-illustration">
          <img :src="illustration" alt="欢迎来到新的旅程" />
        </div>
        <div class="brand-points">
          <div class="point"><span class="dot"></span>答案带引用来源，可追溯</div>
          <div class="point"><span class="dot"></span>流程节点卡住时，边问边做</div>
          <div class="point"><span class="dot"></span>材料确认与进展，一站看清</div>
        </div>
      </div>
    </div>

    <!-- 右侧表单区 -->
    <div class="form-pane">
      <div class="form-box">
        <div class="login-header">
          <h2>欢迎回来</h2>
          <p>请使用管理员分配的账号登录</p>
        </div>
        <el-form :model="loginForm" label-width="0" @submit.prevent="handleLogin">
          <el-form-item>
            <el-input v-model="loginForm.username" name="username" autocomplete="username" placeholder="用户名" prefix-icon="User" size="large" />
          </el-form-item>
          <el-form-item>
            <el-input v-model="loginForm.password" name="password" type="password" autocomplete="current-password" placeholder="密码" prefix-icon="Lock" size="large" show-password @keyup.enter="handleLogin" />
          </el-form-item>
          <el-button type="primary" size="large" class="login-btn" :loading="loading" @click="handleLogin">
            登 录
          </el-button>
          <div class="forgot-link">
            <el-link type="primary" :underline="false" @click="goForgot">忘记密码？</el-link>
          </div>
        </el-form>
      </div>
      <div class="form-footer">知途 · 新员工入职 AI 问答助手</div>
    </div>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useUserStore } from '@/stores/user'
import illustration from '@/assets/login-illustration.svg'

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
.login-page {
  height: 100vh;
  display: flex;
  background: #f8fafc;
}

/* ===== 左侧品牌区 ===== */
.brand-pane {
  flex: 1.15;
  background: linear-gradient(160deg, #eef2ff 0%, #e0e7ff 45%, #ede9fe 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 48px;
}
.brand-inner {
  max-width: 480px;
  width: 100%;
  color: #1f2937;
}
.brand-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 40px;
}
.logo-mark {
  width: 40px;
  height: 40px;
  border-radius: 11px;
  background: linear-gradient(135deg, #6366f1, #8b5cf6);
  color: #fff;
  font-weight: 700;
  font-size: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 12px rgba(99, 102, 241, 0.35);
}
.logo-text {
  font-size: 20px;
  font-weight: 700;
  color: #312e81;
  letter-spacing: 2px;
}
.brand-title {
  font-size: 32px;
  line-height: 1.4;
  font-weight: 700;
  color: #1e1b4b;
  margin-bottom: 14px;
}
.brand-sub {
  font-size: 14.5px;
  line-height: 1.9;
  color: #4b5563;
  margin-bottom: 28px;
}
.brand-illustration {
  display: flex;
  justify-content: center;
  margin-bottom: 30px;
}
.brand-illustration img {
  width: min(360px, 80%);
  height: auto;
  color: #6366f1; /* unDraw 插图主色走 currentColor */
  filter: drop-shadow(0 10px 24px rgba(99, 102, 241, 0.18));
}
.brand-points {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.point {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 13.5px;
  color: #374151;
}
.dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #6366f1;
  flex-shrink: 0;
}

/* ===== 右侧表单区 ===== */
.form-pane {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px 32px;
  background: #ffffff;
}
.form-box {
  width: 100%;
  max-width: 360px;
}
.login-header {
  text-align: center;
  margin-bottom: 28px;
}
.login-header h2 {
  font-size: 24px;
  color: #111827;
  margin-bottom: 8px;
}
.login-header p {
  color: #9ca3af;
  font-size: 14px;
}
.login-btn {
  width: 100%;
  margin-top: 4px;
  letter-spacing: 6px;
}
.forgot-link {
  text-align: right;
  margin-top: 10px;
}
.form-footer {
  margin-top: 40px;
  font-size: 12.5px;
  color: #cbd5e1;
}

/* ===== 窄屏适配：隐藏品牌区 ===== */
@media (max-width: 860px) {
  .brand-pane { display: none; }
  .login-page { background: #fff; }
}
@media (max-width: 1100px) {
  .wide-only { display: none; }
}
</style>
