<template>
  <div class="kb-content-panel">
    <div class="kb-content-header">
      <span class="kb-file-name">{{ kbStore.currentPath }}</span>
      <n-space :size="8" align="center">
        <template v-if="kbStore.isEditable">
          <n-button v-if="!editing" size="small" secondary @click="startEdit">
            <template #icon><n-icon><CreateOutline /></n-icon></template>
            编辑
          </n-button>
          <template v-else>
            <n-button size="small" type="primary" :loading="saving" @click="save">保存</n-button>
            <n-button size="small" @click="cancelEdit">取消</n-button>
          </template>
        </template>
        <n-tag v-else size="small" type="warning">只读</n-tag>
        <n-button text size="small" @click="emit('close')" title="关闭">
          <template #icon><n-icon :size="18"><CloseOutline /></n-icon></template>
        </n-button>
      </n-space>
    </div>
    <div class="kb-content-scroll">
      <!-- 编辑模式：支持 [[ 双链自动补全 -->
      <div v-if="editing" class="kb-editor-wrap">
        <textarea
          ref="editorRef"
          v-model="editContent"
          class="kb-editor-textarea"
          placeholder="在此输入内容（支持 Markdown，输入 [[ 可链接其它笔记）"
          @input="onEditorInput"
          @keydown="onEditorKeydown"
          @scroll="closeSuggest"
          @blur="onEditorBlur"
        ></textarea>
        <!-- [[ 补全下拉 -->
        <ul v-if="suggest.show" class="kb-suggest" :style="suggestStyle">
          <li
            v-for="(item, i) in suggest.items"
            :key="item"
            :class="{ active: i === suggest.active }"
            @mousedown.prevent="applySuggest(item)"
          >{{ item }}</li>
          <li v-if="suggest.items.length === 0" class="kb-suggest-empty">无匹配笔记</li>
        </ul>
      </div>
      <!-- 预览模式 -->
      <div v-else class="kb-preview">
        <MarkdownRender
          v-if="isMarkdown"
          :custom-id="`kb-${kbStore.currentPath}`"
          :content="renderedContent"
          :final="true"
        />
        <pre v-else class="kb-plain">{{ kbStore.fileContent }}</pre>

        <!-- 反向链接面板 -->
        <div v-if="isMarkdown && backlinks.length" class="kb-backlinks">
          <div class="kb-backlinks-title">🔗 反向链接（{{ backlinks.length }}）</div>
          <div
            v-for="bl in backlinks"
            :key="bl.file_path"
            class="kb-backlink-item"
            @click="openPath(bl.file_path)"
          >{{ bl.file_path }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { NButton, NIcon, NSpace, NTag, useMessage } from 'naive-ui'
import { CloseOutline, CreateOutline } from '@vicons/ionicons5'
import { MarkdownRender } from 'markstream-vue'
import 'markstream-vue/index.css'
import { useKnowledgeStore } from '@/stores/knowledge'
import { getBacklinks, getNoteNames, type Backlink } from '@/api/knowledge'

const emit = defineEmits<{ (e: 'close'): void }>()

const message = useMessage()
const kbStore = useKnowledgeStore()

const editing = ref(false)
const editContent = ref('')
const saving = ref(false)
const editorRef = ref<HTMLTextAreaElement | null>(null)

const isMarkdown = computed(() => /\.(md|markdown)$/i.test(kbStore.currentPath))

// ---------- 双链：笔记名索引（用于补全与跳转解析） ----------
const noteNames = ref<string[]>([])
const backlinks = ref<Backlink[]>([])

async function loadNoteNames() {
  try { noteNames.value = await getNoteNames() } catch { noteNames.value = [] }
}

async function loadBacklinks() {
  if (!isMarkdown.value || !kbStore.currentPath) { backlinks.value = []; return }
  try { backlinks.value = await getBacklinks(kbStore.currentPath) } catch { backlinks.value = [] }
}

// 把 [[目标]] 解析为已存在笔记的相对路径（文件名匹配，兼容 Obsidian）
function resolveWikiTarget(target: string): string | null {
  const t = target.split('#')[0].split('|')[0].trim()
  if (!t) return null
  const norm = t.replace(/\\/g, '/').replace(/^\.\//, '')
  const withMd = /\.(md|markdown)$/i.test(norm) ? norm : norm + '.md'
  // 直接路径匹配
  if (noteNames.value.includes(withMd)) return withMd
  // 文件名 stem 匹配
  const stem = t.split('/').pop()!.replace(/\.(md|markdown)$/i, '').toLowerCase()
  const matches = noteNames.value.filter(
    n => n.split('/').pop()!.replace(/\.(md|markdown)$/i, '').toLowerCase() === stem
  )
  return matches[0] || null
}

// 预览渲染：把 [[wikilink]] 转成可点击的锚点（未匹配的标灰）
const renderedContent = computed(() => {
  if (!isMarkdown.value) return kbStore.fileContent
  return kbStore.fileContent.replace(
    /\[\[([^\[\]|#]+)(?:#[^\[\]|]+)?(?:\|([^\[\]]+))?\]\]/g,
    (_m, target: string, alias: string) => {
      const label = (alias || target).trim()
      const resolved = resolveWikiTarget(target)
      if (resolved) {
        return `[${label}](#kblink:${encodeURIComponent(resolved)})`
      }
      return `<span class="kb-wikilink-missing">${label}</span>`
    }
  )
})

// 拦截预览区内 wikilink 点击
function onPreviewClick(e: MouseEvent) {
  const a = (e.target as HTMLElement).closest('a') as HTMLAnchorElement | null
  if (!a) return
  const href = a.getAttribute('href') || ''
  if (href.startsWith('#kblink:')) {
    e.preventDefault()
    openPath(decodeURIComponent(href.slice('#kblink:'.length)))
  }
}

async function openPath(path: string) {
  try {
    await kbStore.openFile(path, true)
  } catch (e: any) {
    message.error(e.message || '打开笔记失败')
  }
}

// ---------- [[ 自动补全 ----------
const suggest = ref<{ show: boolean; items: string[]; active: number; start: number }>({
  show: false, items: [], active: 0, start: -1,
})
const suggestStyle = ref<Record<string, string>>({})

function closeSuggest() { suggest.value.show = false }
function onEditorBlur() { setTimeout(closeSuggest, 150) }

function onEditorInput() {
  const ta = editorRef.value
  if (!ta) return
  const pos = ta.selectionStart
  const before = editContent.value.slice(0, pos)
  // 找最近的 [[ 且其后没有 ]]
  const idx = before.lastIndexOf('[[')
  if (idx === -1 || before.slice(idx).includes(']]')) { closeSuggest(); return }
  const kw = before.slice(idx + 2).toLowerCase()
  if (kw.includes('\n')) { closeSuggest(); return }
  const items = noteNames.value
    .filter(n => n.toLowerCase().includes(kw))
    .slice(0, 8)
  suggest.value = { show: true, items, active: 0, start: idx }
  positionSuggest()
}

function positionSuggest() {
  // 简单定位：放在编辑器左上偏移，避免复杂的光标测量
  suggestStyle.value = { top: '8px', left: '8px' }
}

function onEditorKeydown(e: KeyboardEvent) {
  if (!suggest.value.show) return
  if (e.key === 'ArrowDown') {
    e.preventDefault()
    suggest.value.active = (suggest.value.active + 1) % suggest.value.items.length
  } else if (e.key === 'ArrowUp') {
    e.preventDefault()
    suggest.value.active = (suggest.value.active - 1 + suggest.value.items.length) % suggest.value.items.length
  } else if (e.key === 'Enter' && suggest.value.items.length) {
    e.preventDefault()
    applySuggest(suggest.value.items[suggest.value.active])
  } else if (e.key === 'Escape') {
    closeSuggest()
  }
}

function applySuggest(item: string) {
  const ta = editorRef.value
  if (!ta) return
  const pos = ta.selectionStart
  const start = suggest.value.start
  // 用不含扩展名的名字作为链接目标（Obsidian 风格）
  const name = item.replace(/\.(md|markdown)$/i, '')
  const newText =
    editContent.value.slice(0, start) + `[[${name}]]` + editContent.value.slice(pos)
  editContent.value = newText
  closeSuggest()
  nextTick(() => {
    const caret = start + name.length + 4
    ta.focus()
    ta.setSelectionRange(caret, caret)
  })
}

// 切换文件时退出编辑态、刷新反链
watch(() => kbStore.currentPath, () => {
  editing.value = false
  loadBacklinks()
})

function startEdit() {
  editContent.value = kbStore.fileContent
  editing.value = true
  loadNoteNames()
}

function cancelEdit() {
  editing.value = false
}

async function save() {
  saving.value = true
  try {
    await kbStore.saveFile(kbStore.currentPath, editContent.value)
    kbStore.fileContent = editContent.value
    editing.value = false
    message.success('已保存')
    loadBacklinks()
  } catch (e: any) {
    message.error(e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  loadNoteNames()
  loadBacklinks()
  // 事件委托：预览区 wikilink 点击
  document.addEventListener('click', onPreviewClick, true)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onPreviewClick, true)
})
</script>

<style scoped>
.kb-content-panel {
  width: 420px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--bg-secondary);
  border-right: var(--glass-border);
}
.kb-content-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  border-bottom: var(--glass-border);
  flex-shrink: 0;
  gap: 8px;
}
.kb-file-name {
  font-size: 0.9rem;
  font-weight: 600;
  color: var(--text-primary);
  word-break: break-all;
}
.kb-content-scroll { flex: 1; overflow: hidden; display: flex; }

/* 编辑器 + 补全 */
.kb-editor-wrap { flex: 1; position: relative; display: flex; }
.kb-editor-textarea {
  flex: 1;
  width: 100%;
  height: 100%;
  resize: none;
  border: none;
  outline: none;
  padding: 16px 20px;
  background: transparent;
  color: var(--text-primary);
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 0.9rem;
  line-height: 1.6;
}
.kb-suggest {
  position: absolute;
  z-index: 20;
  min-width: 220px;
  max-height: 260px;
  overflow-y: auto;
  margin: 0;
  padding: 4px;
  list-style: none;
  background: var(--bg-secondary, #1e1e1e);
  border: var(--glass-border);
  border-radius: 8px;
  box-shadow: 0 6px 24px rgba(0, 0, 0, 0.3);
}
.kb-suggest li {
  padding: 5px 10px;
  border-radius: 6px;
  font-size: 0.8rem;
  cursor: pointer;
  color: var(--text-primary);
  word-break: break-all;
}
.kb-suggest li.active,
.kb-suggest li:hover { background: rgba(74, 124, 247, 0.2); }
.kb-suggest-empty { color: var(--text-secondary); cursor: default; }

.kb-preview {
  flex: 1;
  overflow-y: auto;
  padding: 20px 24px;
}
.kb-plain {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 0.88rem;
  line-height: 1.6;
  color: var(--text-primary);
}

/* 反向链接 */
.kb-backlinks {
  margin-top: 28px;
  padding-top: 16px;
  border-top: var(--glass-border);
}
.kb-backlinks-title {
  font-size: 0.82rem;
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 8px;
}
.kb-backlink-item {
  font-size: 0.8rem;
  color: var(--accent-color, #4a7cf7);
  padding: 4px 8px;
  border-radius: 6px;
  cursor: pointer;
  word-break: break-all;
}
.kb-backlink-item:hover { background: rgba(74, 124, 247, 0.15); }

:deep(.kb-wikilink-missing) {
  color: var(--text-secondary);
  text-decoration: underline dotted;
  opacity: 0.7;
}
</style>
