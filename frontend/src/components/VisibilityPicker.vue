<template>
  <template v-if="userStore.isStaff()">
    <el-form-item label="可见范围">
      <el-radio-group :model-value="visibility" @update:model-value="onVisibilityChange">
        <el-radio value="private">仅自己</el-radio>
        <el-radio value="all">全员共享</el-radio>
        <el-radio value="custom">指定用户</el-radio>
      </el-radio-group>
    </el-form-item>
    <el-form-item v-if="visibility === 'custom'" label="共享给">
      <div class="share-picker">
        <el-select
          :model-value="sharedIds"
          multiple
          filterable
          placeholder="选择可见的用户（可多选）"
          style="width: 100%"
          :loading="loading"
          @update:model-value="onIdsChange"
        >
          <el-option v-for="u in userOptions" :key="u.id" :label="userLabel(u)" :value="u.id" />
        </el-select>
        <div class="share-tip">被选中的用户可查看该内容（只读）；管理员/超管始终全局可见。</div>
      </div>
    </el-form-item>
  </template>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useUserStore } from '@/stores/user'
import { listUsers } from '@/api/auth'

const props = defineProps({
  visibility: { type: String, default: 'all' },
  sharedIds: { type: Array, default: () => [] },
})
const emit = defineEmits(['update:visibility', 'update:sharedIds'])

const userStore = useUserStore()
const loading = ref(false)
const users = ref([])

const ROLE_TEXT = { user: '普通用户', admin: '管理员', super_admin: '超管' }
const userLabel = (u) => `${u.username}（${ROLE_TEXT[u.role] || u.role}）`

// 排除自己（对自己共享没有意义），其余用户/管理员都可选
const userOptions = computed(() =>
  users.value.filter((u) => u.id !== userStore.user?.id)
)

function onVisibilityChange(v) {
  emit('update:visibility', v)
  if (v !== 'custom') emit('update:sharedIds', [])
}

function onIdsChange(ids) {
  emit('update:sharedIds', ids)
}

onMounted(async () => {
  if (!userStore.isStaff()) return
  loading.value = true
  try {
    const list = await listUsers()
    users.value = Array.isArray(list) ? list : list.users || []
  } catch (e) {
    users.value = []
  } finally {
    loading.value = false
  }
})
</script>

<style scoped>
.share-picker { width: 100%; }
.share-tip { font-size: 12px; color: #909399; line-height: 1.6; margin-top: 4px; }
</style>
