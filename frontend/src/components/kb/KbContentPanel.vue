<template>
  <div class="kb-content-panel" :style="{ width: (props.width ?? 420) + 'px' }">
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
        <n-tag v-else-if="kbStore.isReadonly" size="small" type="warning">
          <template #icon><n-icon><LockClosedOutline /></n-icon></template>
          只读（公共基础）
        </n-tag>
        <n-tag v-else size="small" type="info">只读</n-tag>
        <!-- 图片/PDF：用系统默认程序打开 -->
        <n-button
          v-if="isViewable"
          text size="small"
          @click="openWithDefaultApp"
          title="用系统默认程序打开"
        >
          <template #icon><n-icon :size="18"><OpenOutline /></n-icon></template>
        </n-button>
        <n-button text size="small" @click="emit('close')" title="关闭">
          <template #icon><n-icon :size="18"><CloseOutline /></n-icon></template>
        </n-button>
      </n-space>
    </div>
    <!-- 标签行 -->
    <div v-if="kbStore.currentPath" class="kb-tags-row">
      <n-space :size="4" align="center">
        <n-tag
          v-for="tag in currentTags"
          :key="tag.id"
          :color="{ color: tag.color, textColor: '#fff' }"
          size="small"
          closable
          @close="removeFileTag(tag)"
        >{{ tag.name }}</n-tag>
        <n-button text size="tiny" @click="showTagInput = !showTagInput" title="添加标签">
          <template #icon><n-icon :size="14"><AddOutline /></n-icon></template>
        </n-button>
        <n-input
          v-if="showTagInput"
          v-model:value="newTagName"
          size="tiny"
          placeholder="标签名"
          style="width:80px"
          @keydown.enter="addTag"
          @blur="addTag"
        />
      </n-space>
    </div>
    <div class="kb-content-scroll">
      <!-- 编辑模式 -->
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
        <ul v-if="suggest.show" class="kb-suggest" :style="suggestStyle">
          <li v-for="(item, i) in suggest.items" :key="item" :class="{ active: i === suggest.active }" @mousedown.prevent="applySuggest(item)">{{ item }}</li>
          <li v-if="suggest.items.length === 0" class="kb-suggest-empty">无匹配笔记</li>
        </ul>
      </div>
      <!-- 预览模式 -->
      <div v-else class="kb-preview">
        <div v-if="kbStore.fileFormat === 'image'" class="kb-image-view">
          <img :src="kbStore.rawUrl" :alt="kbStore.currentPath" @click="zoomed = true" title="点击放大" />
        </div>
        <object v-else-if="kbStore.fileFormat === 'pdf'" class="kb-pdf-view" :data="kbStore.rawUrl" type="application/pdf" :title="kbStore.currentPath">
          <p>浏览器不支持内嵌 PDF 预览，<a :href="kbStore.rawUrl" target="_blank">点此在新标签页打开</a></p>
        </object>
        <MarkdownRender v-else-if="isMarkdown" :custom-id="`kb-${kbStore.currentPath}`" :content="renderedContent" :final="true" />
        <pre v-else class="kb-plain">{{ kbStore.fileContent }}</pre>
      </div>
    </div>

    <!-- 底部固定面板：标签 + 附注/反向链接 -->
    <div v-if="kbStore.currentPath" class="kb-bottom-panel">
      <!-- 标签栏 -->
      <div class="kb-tags-bar">
        <span class="kb-tags-label">🏷️</span>
        <n-tag v-for="(tag, i) in fileTags" :key="i" size="small" closable @on-close="() => removeTag(i)" type="info">
          {{ tag }}
        </n-tag>
        <n-input
          v-if="addingTag"
          ref="tagInputRef"
          v-model:value="newTag"
          size="tiny"
          placeholder="输入标签（不含#）"
          style="width: 120px"
          @keydown.enter="confirmAddTag"
          @blur="confirmAddTag"
        />
        <n-button v-else size="tiny" secondary type="info" @click="startAddTag">+ 标签</n-button>
        <span v-if="!kbStore.isEditable" style="font-size:0.7rem;color:var(--text-secondary);margin-left:4px">（只读）</span>
      </div>
      <!-- 附注笔记（仅非 md 需要） -->
      <KbSidecarPanel v-if="showSidecar" :target="kbStore.currentPath" />
      <!-- 反向链接（仅 md） -->
      <div v-if="isMarkdown && backlinks.length" class="kb-backlinks">
        <div class="kb-backlinks-title">🔗 反向链接（{{ backlinks.length }}）</div>
        <div v-for="bl in backlinks" :key="bl.file_path" class="kb-backlink-item" @click="openPath(bl.file_path)">{{ bl.file_path }}</div>
      </div>
    </div>

    <!-- 图片放大灯箱 -->
    <Teleport to="body">
      <div v-if="zoomed" class="kb-lightbox" @click="zoomed = false">
        <img :src="kbStore.rawUrl" :alt="kbStore.currentPath" @click.stop />
        <n-button class="kb-lightbox-close" circle @click="zoomed = false">
          <template #icon><n-icon :size="20"><CloseOutline /></n-icon></template>
        </n-button>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { NButton, NIcon, NSpace, NTag, NInput, useMessage } from 'naive-ui'
import { CloseOutline, CreateOutline, LockClosedOutline, OpenOutline, AddOutline } from '@vicons/ionicons5'
import { MarkdownRender } from 'markstream-vue'
import 'markstream-vue/index.css'
import { useKnowledgeStore } from '@/stores/knowledge'
import { getBacklinks, getNoteNames, type Backlink } from '@/api/knowledge'
import KbSidecarPanel from '@/components/kb/KbSidecarPanel.vue'

const props = defineProps<{ width?: number }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const message = useMessage()
const kbStore = useKnowledgeStore()

const editing = ref(false)

// 标签
const currentTags = ref<{ id: number; name: string; color: string }[]>([])
const showTagInput = ref(false)
const newTagName = ref('')

async function loadTags() {
  if (!kbStore.currentPath) return
  try {
    const resp = await fetch(`/api/kb/files/tags?file_path=${encodeURIComponent(kbStore.currentPath)}`)
    if (resp.ok) currentTags.value = await resp.json()
  } catch { /* ignore */ }
}

async function addTag() {
  const name = newTagName.value.trim()
  newTagName.value = ''
  showTagInput.value = false
  if (!name || !kbStore.currentPath) return

  const newTags = [...currentTags.value.map(t => t.name), name]
  await fetch(`/api/kb/files/tags?file_path=${encodeURIComponent(kbStore.currentPath)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tags: newTags }),
  })
  await loadTags()
}

async function removeFileTag(tag: { id: number; name: string }) {
  const newTags = currentTags.value.filter(t => t.id !== tag.id).map(t => t.name)
  await fetch(`/api/kb/files/tags?file_path=${encodeURIComponent(kbStore.currentPath)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tags: newTags }),
  })
  await loadTags()
}

watch(() => kbStore.currentPath, () => {
  if (kbStore.currentPath) loadTags()
}, { immediate: true })
const editContent = ref('')
const saving = ref(false)
const editorRef = ref<HTMLTextAreaElement | null>(null)

const isMarkdown = computed(() => /\.(md|markdown)$/i.test(kbStore.currentPath))

// 图片/PDF 可用系统程序打开 + 灯箱放大
const isViewable = computed(() => kbStore.fileFormat === 'image' || kbStore.fileFormat === 'pdf')
const zoomed = ref(false)

// 非 md 资源（pdf/图片/docx/二进制等）显示附注面板，供其接入双链图谱
// md 笔记本身可直接写 [[]]，无需附注
const showSidecar = computed(() =>
  !!kbStore.currentPath && !isMarkdown.value
)

async function openWithDefaultApp() {
  const path = kbStore.absPath
  if (!path) return
  try {
    const api = (window as any).pywebview?.api
    if (api?.open_with_default_app) {
      const res = await api.open_with_default_app(path)
      if (res && res.success === false) message.error(res.error || '打开失败')
    } else {
      message.warning('用系统程序打开仅支持桌面环境')
    }
  } catch (e: any) {
    message.error(e?.message || '打开失败')
  }
}

// 切换文件时关闭灯箱
watch(() => kbStore.currentPath, () => { zoomed.value = false })

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

// ---------- 标签 ----------
const fileTags = ref<string[]>([])
const addingTag = ref(false)
const newTag = ref('')
const tagInputRef = ref<InstanceType<typeof NInput> | null>(null)

// 从文件内容解析已有标签
function parseTags() {
  if (!kbStore.currentPath) { fileTags.value = []; return }
  const content = kbStore.fileContent
  const tags = new Set<string>()
  // 匹配 #中文标签 或 #english_tag 或 #混合-标签
  const re = /(?:^|\s)#([\w一-鿿/-]+)/g
  let m
  while ((m = re.exec(content)) !== null) {
    tags.add(m[1])
  }
  fileTags.value = Array.from(tags)
}

// 切换文件时重新解析
watch(() => kbStore.currentPath, () => {
  editing.value = false
  loadBacklinks()
})
// 文件内容变化时重新解析标签
watch(() => kbStore.fileContent, () => {
  parseTags()
})

// 标签增删
function startAddTag() {
  addingTag.value = true
  newTag.value = ''
  nextTick(() => tagInputRef.value?.focus())
}

async function confirmAddTag() {
  const tag = newTag.value.trim()
  addingTag.value = false
  if (!tag || fileTags.value.includes(tag)) return
  fileTags.value.push(tag)
  await saveTagsToFile()
}

async function removeTag(idx: number) {
  fileTags.value.splice(idx, 1)
  await saveTagsToFile()
}

async function saveTagsToFile() {
  // 将标签写回文件内容：在内容末尾追加或替换已有的 #标签 行
  try {
    if (!kbStore.isEditable) {
      message.warning('该文件不可编辑，标签无法保存')
      // 恢复
      parseTags()
      return
    }
    let content = kbStore.fileContent
    // 移除内容中所有已有的 #标签
    content = content.replace(/(?:^|\s)#([\w一-鿿/-]+)/g, '').replace(/\n{3,}/g, '\n\n').trim()
    // 在末尾追加标签
    if (fileTags.value.length > 0) {
      content += '\n\n' + fileTags.value.map(t => `#${t}`).join(' ')
    }
    await kbStore.saveFile(kbStore.currentPath, content)
    kbStore.fileContent = content
    message.success('标签已保存')
  } catch (e: any) {
    message.error(e.message || '保存标签失败')
    parseTags()
  }
}

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
  parseTags()
  // 事件委托：预览区 wikilink 点击
  document.addEventListener('click', onPreviewClick, true)
})

onBeforeUnmount(() => {
  document.removeEventListener('click', onPreviewClick, true)
})
</script>

<style scoped>
.kb-content-panel {
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

/* 图片内嵌查看 */
.kb-image-view {
  display: flex;
  justify-content: center;
  align-items: flex-start;
}
.kb-image-view img {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.2);
  cursor: zoom-in;
}

/* 图片放大灯箱 */
.kb-lightbox {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: rgba(0, 0, 0, 0.82);
  cursor: zoom-out;
}
.kb-lightbox img {
  max-width: 92vw;
  max-height: 92vh;
  object-fit: contain;
  border-radius: 6px;
  box-shadow: 0 8px 40px rgba(0, 0, 0, 0.5);
  cursor: default;
}
.kb-lightbox-close {
  position: fixed;
  top: 20px;
  right: 24px;
}

/* PDF 内嵌查看：铺满预览区 */
.kb-pdf-view {
  width: 100%;
  height: 100%;
  min-height: 70vh;
  border: none;
  border-radius: 8px;
  background: #fff;
}

/* 底部固定面板 */
.kb-bottom-panel {
  flex-shrink: 0;
  border-top: var(--glass-border);
  max-height: 45%;
  overflow-y: auto;
}

/* 标签栏 */
.kb-tags-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 16px;
  flex-wrap: wrap;
  border-bottom: var(--glass-border);
}
.kb-tags-label { font-size: 0.85rem; }

/* 反向链接（底部面板内） */
.kb-backlinks {
  padding: 12px 16px;
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
