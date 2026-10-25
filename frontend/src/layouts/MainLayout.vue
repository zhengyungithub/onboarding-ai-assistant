<template>
  <el-container class="main-layout">
    <el-aside :width="collapsed ? '64px' : '220px'" class="sidebar">
      <div class="logo">
        <el-icon :size="24" color="#6366f1"><ChatDotRound /></el-icon>
        <span v-if="!collapsed">知途 · 业务问答</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        background-color="transparent"
        text-color="#4b5563"
        active-text-color="#6366f1"
        :collapse="collapsed"
        :collapse-transition="false"
      >
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/">
          <el-icon><ChatDotRound /></el-icon>
          <span>AI 问答</span>
        </el-menu-item>
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/flow">
          <el-icon><Guide /></el-icon>
          <span>流程下一步</span>
        </el-menu-item>
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/tasks">
          <el-icon><List /></el-icon>
          <span>我的任务</span>
        </el-menu-item>
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/confirmations">
          <el-icon><Stamp /></el-icon>
          <template #title>
            <span class="menu-label">
              待我确认
              <span v-if="pendingCount > 0" class="menu-badge">
                {{ pendingCount > 99 ? '99+' : pendingCount }}
              </span>
            </span>
          </template>
        </el-menu-item>
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/memos">
          <el-icon><Memo /></el-icon>
          <span>备忘录</span>
        </el-menu-item>
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/admin/items">
          <el-icon><Collection /></el-icon>
          <span>知识库管理</span>
        </el-menu-item>
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/admin/ingest">
          <el-icon><Upload /></el-icon>
          <span>资料入库</span>
        </el-menu-item>
        <el-menu-item v-if="!userStore.isSuperAdmin()" index="/admin/flows">
          <el-icon><Connection /></el-icon>
          <span>流程配置</span>
        </el-menu-item>
        <el-menu-item v-if="userStore.isSuperAdmin()" index="/settings">
          <el-icon><Setting /></el-icon>
          <span>API Key 配置</span>
        </el-menu-item>

        <el-sub-menu v-if="userStore.isSuperAdmin()" index="admin">
          <template #title>
            <el-icon><OfficeBuilding /></el-icon>
            <span>后台管理</span>
          </template>
          <el-menu-item index="/admin/evaluate">评测数据</el-menu-item>
          <el-menu-item index="/admin/users">用户管理</el-menu-item>
        </el-sub-menu>
      </el-menu>
    </el-aside>

    <el-container>
      <el-header class="header">
        <div class="header-left">
          <el-tooltip :content="collapsed ? '展开侧边栏' : '收起侧边栏'" placement="bottom">
            <el-button text class="collapse-btn" @click="collapsed = !collapsed">
              <el-icon :size="20">
                <Fold v-if="!collapsed" />
                <Expand v-else />
              </el-icon>
            </el-button>
          </el-tooltip>
          <div class="header-title">{{ currentTitle }}</div>
        </div>
        <div class="header-right">
          <el-popover placement="bottom-end" :width="340" trigger="click" @show="onBellOpen">
            <template #reference>
              <span class="bell-wrap">
                <el-badge is-dot :hidden="!uploadStore.hasUnseenResult" class="bell-badge">
                  <el-icon :size="20" class="bell-icon"><Bell /></el-icon>
                </el-badge>
              </span>
            </template>
            <div class="bell-panel">
              <div class="bell-panel-header">
                <span>上传动态</span>
                <el-button
                  v-if="uploadStore.finishedTasks.length"
                  link
                  type="primary"
                  size="small"
                  @click="uploadStore.clearFinished()"
                >
                  清除记录
                </el-button>
              </div>
              <template v-if="uploadStore.tasks.length">
                <div v-for="t in uploadStore.tasks" :key="t.id" class="bell-task">
                  <el-icon :size="18" :class="['bell-task-icon', t.status]">
                    <Loading v-if="t.status === 'uploading'" />
                    <CircleCheckFilled v-else-if="t.status === 'success'" />
                    <CircleCloseFilled v-else />
                  </el-icon>
                  <div class="bell-task-body">
                    <div class="bell-task-name">{{ t.filename }}</div>
                    <div v-if="t.status === 'uploading'" class="bell-task-status">
                      <el-progress :percentage="t.progress" :stroke-width="6" :show-text="false" class="bell-progress" />
                      <span>{{ t.progress }}%</span>
                    </div>
                    <div v-else-if="t.status === 'success'" class="bell-task-status ok">上传成功，已存入台账</div>
                    <div v-else class="bell-task-status fail">{{ t.error }}</div>
                  </div>
                </div>
              </template>
              <el-empty v-else description="暂无上传记录" :image-size="60" />
            </div>
          </el-popover>
          <el-dropdown @command="handleCommand">
          <span class="user-info">
            <el-avatar :size="32" :icon="UserFilled" />
            <span class="username">{{ userStore.user?.username }}</span>
            <el-tag v-if="userStore.isSuperAdmin()" size="small" type="danger" effect="plain">
              {{ userStore.roleLabel() }}
            </el-tag>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item v-if="userStore.isSuperAdmin()" command="settings">API Key 配置</el-dropdown-item>
              <el-dropdown-item command="logout" divided>退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
        </div>
      </el-header>

      <el-main class="main-content">
        <router-view v-slot="{ Component }">
          <transition name="fade" mode="out-in">
            <component :is="Component" />
          </transition>
        </router-view>
      </el-main>
    </el-container>

    <!-- 全局悬浮上传进度条（跨页面显示，上传在后台持续） -->
    <transition name="tray-slide">
      <div v-if="uploadStore.uploadingCount > 0" class="upload-tray">
        <div class="upload-tray-title">
          <span class="upload-tray-emoji">📤</span>
          正在上传 {{ uploadStore.uploadingCount }} 个文档
        </div>
        <div v-for="t in uploadStore.activeTasks" :key="t.id" class="upload-tray-task">
          <div class="upload-tray-name">{{ t.filename }}</div>
          <div class="upload-tray-track">
            <div class="upload-tray-fill" :style="{ width: t.progress + '%' }"></div>
          </div>
          <div class="upload-tray-pct">{{ t.progress }}%</div>
        </div>
        <div class="upload-tray-hint">
          💡 您可以继续操作其他功能，后台会继续上传，上传成功后会进行提示~
        </div>
      </div>
    </transition>
  </el-container>
</template>

<script setup>
import { ref, computed, onMounted, onBeforeUnmount, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'
import { useUploadStore } from '@/stores/upload'
import { getPendingCount } from '@/api/confirmation'
import {
  UserFilled, ChatDotRound, Guide, List, Setting, OfficeBuilding, Collection, Upload,
  Connection, Memo, Fold, Expand, Bell, Loading, CircleCheckFilled, CircleCloseFilled, Stamp,
} from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const userStore = useUserStore()
const uploadStore = useUploadStore()
const collapsed = ref(false)

// 待我确认条数（侧边栏徽标）：按路由切换刷新，别的页面处理完会广播事件
const pendingCount = ref(0)

async function loadPendingCount() {
  try {
    const res = await getPendingCount()
    pendingCount.value = res.count || 0
  } catch (e) {
    // 徽标失败不该影响主流程
  }
}

onMounted(() => {
  loadPendingCount()
  window.addEventListener('confirmations-changed', loadPendingCount)
})
onBeforeUnmount(() => {
  window.removeEventListener('confirmations-changed', loadPendingCount)
})
watch(() => route.path, loadPendingCount)

function onBellOpen() {
  // 打开面板即视为已查看，红点熄灭
  uploadStore.markAllSeen()
}

const activeMenu = computed(() => route.path)
const currentTitle = computed(() => route.meta?.title || '知途 · 业务问答')

function handleCommand(command) {
  if (command === 'logout') {
    userStore.logout()
    router.push('/login')
  } else if (command === 'settings') {
    router.push('/settings')
  }
}
</script>

<style scoped>
.main-layout {
  height: 100vh;
}
.sidebar {
  background-color: #fff;
  border-right: 1px solid #e5e7eb;
  transition: width 0.2s ease;
  overflow-x: hidden;
}
.sidebar :deep(.el-menu) {
  border-right: none;
}
.menu-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.menu-badge {
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: #ef4444;
  color: #fff;
  font-size: 11px;
  line-height: 18px;
  text-align: center;
}
.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
  color: #1f2937;
  border-bottom: 1px solid #f3f4f6;
  white-space: nowrap;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}.collapse-btn {
  padding: 8px;
  font-size: 18px;
  color: #4b5563;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 16px;
}

/* ---------- 铃铛 ---------- */
.bell-wrap {
  display: flex;
  align-items: center;
  cursor: pointer;
  padding: 4px;
}
.bell-icon {
  color: #6b7280;
  transition: color 0.2s, transform 0.2s;
}
.bell-wrap:hover .bell-icon {
  color: #f97316;
  transform: rotate(12deg);
}
.bell-badge :deep(.el-badge__content.is-dot) {
  width: 9px;
  height: 9px;
  right: calc(4px - 50%);
  top: 2px;
  background: #ef4444;
  box-shadow: 0 0 0 2px #fff;
}
.bell-panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-weight: 600;
  color: #1f2937;
  padding-bottom: 8px;
  border-bottom: 1px solid #f3f4f6;
  margin-bottom: 8px;
}
.bell-task {
  display: flex;
  gap: 8px;
  padding: 8px 0;
  border-bottom: 1px dashed #f3f4f6;
}
.bell-task:last-child { border-bottom: none; }
.bell-task-icon.uploading { color: #409eff; animation: bell-spin 1.2s linear infinite; }
.bell-task-icon.success { color: #22c55e; }
.bell-task-icon.error { color: #ef4444; }
@keyframes bell-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}
.bell-task-body { flex: 1; min-width: 0; }
.bell-task-name {
  font-size: 13px;
  color: #1f2937;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.bell-task-status {
  font-size: 12px;
  color: #6b7280;
  margin-top: 4px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.bell-task-status.ok { color: #16a34a; }
.bell-task-status.fail { color: #dc2626; }
.bell-progress { flex: 1; }

/* ---------- 全局悬浮上传进度条（清新可爱风） ---------- */
.upload-tray {
  position: fixed;
  right: 24px;
  bottom: 24px;
  width: 320px;
  background: linear-gradient(160deg, #fdfcf7 0%, #f0fdf4 60%, #ecfeff 100%);
  border: 1px solid #bbf7d0;
  border-radius: 16px;
  box-shadow: 0 8px 28px rgba(34, 197, 94, 0.18);
  padding: 14px 16px;
  z-index: 2000;
}
.upload-tray-title {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 600;
  font-size: 14px;
  color: #166534;
}
.upload-tray-emoji {
  display: inline-block;
  animation: emoji-bounce 1s ease-in-out infinite;
}
@keyframes emoji-bounce {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-4px); }
}
.upload-tray-task {
  margin-top: 10px;
}
.upload-tray-name {
  font-size: 12px;
  color: #374151;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.upload-tray-track {
  margin-top: 5px;
  height: 10px;
  background: #ffffff;
  border: 1px solid #d1fae5;
  border-radius: 999px;
  overflow: hidden;
}
.upload-tray-fill {
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, #86efac, #67e8f9, #86efac);
  background-size: 200% 100%;
  animation: tray-flow 1.2s linear infinite;
  transition: width 0.2s ease;
}
@keyframes tray-flow {
  from { background-position: 0% 0; }
  to { background-position: -200% 0; }
}
.upload-tray-pct {
  margin-top: 3px;
  font-size: 11px;
  color: #059669;
  text-align: right;
}
.upload-tray-hint {
  margin-top: 10px;
  padding-top: 8px;
  border-top: 1px dashed #bbf7d0;
  font-size: 12px;
  line-height: 1.6;
  color: #6b7280;
}
.tray-slide-enter-active, .tray-slide-leave-active {
  transition: all 0.3s ease;
}
.tray-slide-enter-from, .tray-slide-leave-to {
  opacity: 0;
  transform: translateY(24px);
}
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background-color: #fff;
  border-bottom: 1px solid #e5e7eb;
  padding: 0 24px;
}
.header-title {
  font-size: 16px;
  font-weight: 600;
  color: #1f2937;
}
.user-info {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
}
.username {
  font-size: 14px;
  color: #374151;
}
.main-content {
  background-color: #f5f7fa;
  padding: 24px;
}
.fade-enter-active, .fade-leave-active {
  transition: opacity 0.2s ease;
}
.fade-enter-from, .fade-leave-to {
  opacity: 0;
}
</style>
