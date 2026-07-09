<template>
  <div class="kb-sidecar">
    <div class="kb-sidecar-head">
      <span class="kb-sidecar-title">
        📝 附注笔记
        <n-text depth="3" style="font-size:0.72rem;font-weight:400">（可写 [[ 双链，纳入图谱）</n-text>
      </span>
      <n-space :size="6" align="center">
        <template v-if="editable">
          <n-button v-if="!editing" size="tiny" secondary @click="startEdit">
            <template #icon><n-icon><CreateOutline /></n-icon></template>
            {{ content ? '编辑' : '添加附注' }}
          </n-button>
          <template v-else>
            <n-button size="tiny" type="primary" :loading="saving" @click="save">保存</n-button>
            <n-button size="tiny" @click="cancelEdit">取消</n-button>
          </template>
        </template>
        <n-tag v-else size="tiny" type="warning">只读</n-tag>
      </n-space>
    </div>

    <!-- 编辑态：[[ 自动补全 -->
    <div v-if="editing" class="kb-sidecar-editor-wrap">
      <textarea
        ref="editorRef"
        v-model="editContent"
        class="kb-sidecar-textarea"
        placeholder="给该文件写点批注，输入 [[ 可链接其它笔记，让它进入双链图谱…"
        @input="onEditorInput"
        @keydown="onEditorKeydown"
        @scroll="closeSuggest"
        @blur="onEditorBlur"
      ></textarea>
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

    <!-- 预览态 -->
    <div v-else class="kb-sidecar-preview">
      <MarkdownRender
        v-if="content"
        :custom-id="`kb-sidecar-${sidecarPath}`"
        :content="renderedContent"
        :final="true"
      />
      <p v-else class="kb-sidecar-empty">
        暂无附注。点击「添加附注」为该文件写批注与 [[双链]]。
      </p>

      <!-- 反向链接 -->
      <div v-if="backlinks.length" class="kb-backlinks">
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
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted } from 'vue'
import { NButton, NIcon, NSpace, NTag, NText, useMessage } from 'naive-ui'
import { CreateOutline } from '@vicons/ionicons5'
import { MarkdownRender } from 'markstream-vue'
import 'markstream-vue/index.css'
import { useKnowledgeStore } from '@/stores/knowledge'
import {
  getSidecar, saveSidecar, getBacklinks, getNoteNames, type Backlink,
} from '@/api/knowledge'

// target：被附注的资源相对路径（如 04-资料/讲义.pdf）
const props = defineProps<{ target: string }>()

const message = useMessage()
const kbStore = useKnowledgeStore()

const sidecarPath = ref('')     // 附注笔记相对路径（<target>.md）
const content = ref('')         // 已保存的附注内容
const editable = ref(false)
const editing = ref(false)
const editContent = ref('')
const saving = ref(false)
const editorRef = ref<HTMLTextAreaElement | null>(null)

const noteNames = ref<string[]>([])
const backlinks = ref<Backlink[]>([])

async function load() {
  editing.value = false
  try {
    const data = await getSidecar(props.target)
    sidecarPath.value = data.path
    content.value = data.content || ''
    editable.value = data.editable
  } catch (e: any) {
    // 读取失败（如后端未更新导致 404）不应把面板锁死：
    // 回退到该文件本身的只读状态（公共基础才只读），并提示用户。
    sidecarPath.value = props.target + '.md'
    content.value = ''
    editable.value = !kbStore.isReadonly
    message.warning(e?.message || '附注接口不可用，请确认后端已重启')
  }
  loadBacklinks()
}

async function loadNoteNames() {
  try { noteNames.value = await getNoteNames() } catch { noteNames.value = [] }
}

async function loadBacklinks() {
  // 反链以资源本身为目标（其附注的链接已归并到资源节点）
  if (!props.target) { backlinks.value = []; return }
  try { backlinks.value = await getBacklinks(props.target) } catch { backlinks.value = [] }
}

// [[wikilink]] 解析为已存在笔记（与主编辑器一致）
function resolveWikiTarget(target: string): string | null {
  const t = target.split('#')[0].split('|')[0].trim()
  if (!t) return null
  const norm = t.replace(/\\/g, '/').replace(/^\.\//, '')
  const withMd = /\.(md|markdown)$/i.test(norm) ? norm : norm + '.md'
  if (noteNames.value.includes(withMd)) return withMd
  const stem = t.split('/').pop()!.replace(/\.(md|markdown)$/i, '').toLowerCase()
  const matches = noteNames.value.filter(
    n => n.split('/').pop()!.replace(/\.(md|markdown)$/i, '').toLowerCase() === stem
  )
  return matches[0] || null
}

const renderedContent = computed(() =>
  content.value.replace(
    /\[\[([^\[\]|#]+)(?:#[^\[\]|]+)?(?:\|([^\[\]]+))?\]\]/g,
    (_m, target: string, alias: string) => {
      const label = (alias || target).trim()
      const resolved = resolveWikiTarget(target)
      if (resolved) return `[${label}](#kblink:${encodeURIComponent(resolved)})`
      return `<span class="kb-wikilink-missing">${label}</span>`
    }
  )
)

async function openPath(path: string) {
  try { await kbStore.openFile(path, true) }
  catch (e: any) { message.error(e.message || '打开笔记失败') }
}

// ---------- [[ 自动补全 ----------
const suggest = ref<{ show: boolean; items: string[]; active: number; start: number }>({
  show: false, items: [], active: 0, start: -1,
})
const suggestStyle = ref<Record<string, string>>({ top: '8px', left: '8px' })

function closeSuggest() { suggest.value.show = false }
function onEditorBlur() { setTimeout(closeSuggest, 150) }

function onEditorInput() {
  const ta = editorRef.value
  if (!ta) return
  const pos = ta.selectionStart
  const before = editContent.value.slice(0, pos)
  const idx = before.lastIndexOf('[[')
  if (idx === -1 || before.slice(idx).includes(']]')) { closeSuggest(); return }
  const kw = before.slice(idx + 2).toLowerCase()
  if (kw.includes('\n')) { closeSuggest(); return }
  const items = noteNames.value.filter(n => n.toLowerCase().includes(kw)).slice(0, 8)
  suggest.value = { show: true, items, active: 0, start: idx }
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
  const name = item.replace(/\.(md|markdown)$/i, '')
  editContent.value =
    editContent.value.slice(0, start) + `[[${name}]]` + editContent.value.slice(pos)
  closeSuggest()
  nextTick(() => {
    const caret = start + name.length + 4
    ta.focus()
    ta.setSelectionRange(caret, caret)
  })
}

function startEdit() {
  editContent.value = content.value
  editing.value = true
  loadNoteNames()
}

function cancelEdit() { editing.value = false }

async function save() {
  saving.value = true
  try {
    await saveSidecar(props.target, editContent.value)
    content.value = editContent.value
    editing.value = false
    message.success('附注已保存')
    loadBacklinks()
  } catch (e: any) {
    message.error(e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

watch(() => props.target, load)
onMounted(() => { loadNoteNames(); load() })
</script>

<style scoped>
.kb-sidecar {
  border-top: var(--glass-border);
  margin-top: 20px;
  padding-top: 12px;
}
.kb-sidecar-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 10px;
  gap: 8px;
}
.kb-sidecar-title {
  font-size: 0.85rem;
  font-weight: 600;
  color: var(--text-primary);
  display: inline-flex;
  align-items: baseline;
  gap: 6px;
}
.kb-sidecar-editor-wrap { position: relative; }
.kb-sidecar-textarea {
  width: 100%;
  min-height: 160px;
  resize: vertical;
  border: var(--glass-border);
  border-radius: 8px;
  outline: none;
  padding: 12px 14px;
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 0.86rem;
  line-height: 1.6;
}
.kb-sidecar-preview { font-size: 0.9rem; }
.kb-sidecar-empty {
  color: var(--text-secondary);
  font-size: 0.82rem;
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

.kb-backlinks {
  margin-top: 20px;
  padding-top: 14px;
  border-top: var(--glass-border);
}
.kb-backlinks-title {
  font-size: 0.8rem;
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
