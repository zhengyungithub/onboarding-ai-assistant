import { useUserStore } from '@/stores/user'

/** 可见范围三态的展示文案 */
export const VISIBILITY_LABELS = {
  private: '仅自己',
  all: '全员共享',
  custom: '定向共享',
}

/**
 * 内容归属权限：
 * - 管理员/超管可管理所有人的内容；
 * - 普通用户只能管理自己创建的内容，被共享的内容仅可查看。
 * 可见范围（visibility）：private 仅自己 / all 全员共享 / custom 定向共享。
 */
export function useContentPermission() {
  const userStore = useUserStore()

  const isOwner = (row) => !!row?.user_id && row.user_id === userStore.user?.id

  /** 能否编辑/删除该行（本人或管理员） */
  const canEditRow = (row) => !!row && (userStore.isStaff() || isOwner(row))

  /** 是否是「别人共享给我的」内容（只读） */
  const isSharedRow = (row) => !!row?.is_shared && !isOwner(row)

  /** 该行的可见范围标签：all → 全员共享（绿）；custom → 定向共享（蓝）；私有不出标签 */
  const visTag = (row) => {
    if (!row) return null
    if (row.visibility === 'all') return { text: '共享', type: 'success' }
    if (row.visibility === 'custom') return { text: '定向共享', type: 'primary' }
    return null
  }

  /** 当前用户（staff）新建内容时的默认可见范围 */
  const defaultVisibility = () => (userStore.isStaff() ? 'all' : 'private')

  return { isOwner, canEditRow, isSharedRow, visTag, defaultVisibility }
}
