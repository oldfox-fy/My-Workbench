import { ref, onUnmounted, type Ref } from 'vue'

export interface ResizeHandleOptions {
  /** 最小宽度 (px) */
  minWidth: number
  /** 最大宽度 (px) */
  maxWidth: number
  /** localStorage 持久化 key */
  storageKey: string
  /** 初始宽度 (px) */
  initialWidth: number
  /** 拖拽方向：'left' 表示向左拖减小宽度，'right' 表示向右拖增大宽度 */
  direction: 'left' | 'right'
}

export interface ResizeHandleReturn {
  /** 是否正在拖拽 */
  isDragging: Ref<boolean>
  /** 当前宽度 (px) */
  width: Ref<number>
  /** 绑定到拖拽手柄 DOM 元素的 props */
  handleProps: {
    onMousedown: (e: MouseEvent) => void
  }
}

/**
 * 面板拖拽调整宽度的通用 composable。
 *
 * 用法：
 * ```ts
 * const panel = useResizeHandle({
 *   minWidth: 160, maxWidth: 500,
 *   storageKey: 'panel-tree-width',
 *   initialWidth: 240,
 *   direction: 'right',
 * })
 * // 模板中：
 * // <div :style="{ width: panel.width.value + 'px' }">...</div>
 * // <div v-bind="panel.handleProps" class="resize-handle" :class="{ active: panel.isDragging.value }" />
 * ```
 */
export function useResizeHandle(options: ResizeHandleOptions): ResizeHandleReturn {
  const { minWidth, maxWidth, storageKey, initialWidth, direction } = options

  // 从 localStorage 恢复上次保存的宽度
  const saved = localStorage.getItem(storageKey)
  const parsed = saved ? parseInt(saved, 10) : NaN
  const startWidth = !isNaN(parsed) ? Math.min(maxWidth, Math.max(minWidth, parsed)) : initialWidth

  const width = ref<number>(startWidth)
  const isDragging = ref(false)

  let startX = 0
  let startWidthVal = 0

  function onMousedown(e: MouseEvent) {
    e.preventDefault()
    isDragging.value = true
    startX = e.clientX
    startWidthVal = width.value

    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'

    document.addEventListener('mousemove', onMousemove)
    document.addEventListener('mouseup', onMouseup)
  }

  function onMousemove(e: MouseEvent) {
    if (!isDragging.value) return

    const delta = direction === 'right' ? e.clientX - startX : startX - e.clientX
    const newWidth = Math.min(maxWidth, Math.max(minWidth, startWidthVal + delta))
    width.value = newWidth
  }

  function onMouseup() {
    isDragging.value = false
    document.body.style.userSelect = ''
    document.body.style.cursor = ''

    // 持久化到 localStorage
    localStorage.setItem(storageKey, String(width.value))

    document.removeEventListener('mousemove', onMousemove)
    document.removeEventListener('mouseup', onMouseup)
  }

  onUnmounted(() => {
    document.removeEventListener('mousemove', onMousemove)
    document.removeEventListener('mouseup', onMouseup)
    document.body.style.userSelect = ''
    document.body.style.cursor = ''
  })

  return {
    isDragging,
    width,
    handleProps: { onMousedown },
  }
}