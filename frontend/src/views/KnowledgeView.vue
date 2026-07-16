<template>
  <div class="kb-container" :class="[configStore.themeMode]">
    <!-- 顶栏 -->
    <header class="kb-topbar">
      <div class="kb-topbar-left">
        <n-button text class="icon-btn" @click="goBack" title="返回对话">
          <template #icon><n-icon :size="20"><ArrowBackOutline /></n-icon></template>
        </n-button>
        <span class="kb-title"><n-icon :size="18" style="vertical-align:-3px"><LibraryOutline /></n-icon> 我的知识库</span>
        <n-button v-if="kbStore.root" text class="icon-btn" @click="goGraph" title="知识图谱">
          <template #icon><n-icon :size="19"><GitNetworkOutline /></n-icon></template>
        </n-button>
      </div>
      <div class="kb-topbar-right">
        <!-- 语义搜索 -->
        <n-popover trigger="manual" :show="showResults" placement="bottom-end" :width="420" raw>
          <template #trigger>
            <n-input
              v-model:value="searchQuery"
              placeholder="语义搜索知识库…"
              size="small"
              clearable
              round
              style="width: 240px"
              :loading="searching"
              @keyup.enter="doSearch"
              @clear="clearSearch"
            >
              <template #prefix>
                <n-icon :size="15"><SearchOutline /></n-icon>
              </template>
            </n-input>
          </template>
          <div class="kb-search-results">
            <div v-if="searchHits.length === 0" class="kb-search-empty">
              未检索到相关内容。若未建立索引，请到「设置 → 知识库」重建索引。
            </div>
            <div
              v-for="(hit, i) in searchHits"
              :key="i"
              class="kb-search-item"
              @click="openHit(hit)"
            >
              <div class="kb-search-file">
                📄 {{ hit.file_path }}
                <span v-if="hit.heading_path" class="kb-search-heading"> · {{ hit.heading_path }}</span>
              </div>
              <div class="kb-search-snippet">{{ snippet(hit.content) }}</div>
            </div>
          </div>
        </n-popover>

        <n-text depth="3" class="kb-path" v-if="kbStore.root">{{ kbStore.root }}</n-text>

        <n-popconfirm @positive-click="runAutoTag" :disabled="autoTagRunning">
          <template #trigger>
            <n-button text class="icon-btn" :loading="autoTagRunning" title="自动标签：为知识库所有文件智能打标">
              <template #icon><n-icon :size="18"><PricetagsOutline /></n-icon></template>
            </n-button>
          </template>
          {{ autoTagRunning ? `打标中 ${autoTagProgress.current}/${autoTagProgress.total}…` : '为知识库全部文件自动生成标签？' }}
        </n-popconfirm>
      </div>
    </header>

    <!-- 主体：左树 + 右内容 -->
    <div class="kb-body">
      <KbTreePanel :width="treePanelResize.width.value" />

      <!-- 知识库树 → 内容 拖拽手柄 -->
      <div
        class="resize-handle"
        :class="{ active: treePanelResize.isDragging.value }"
        v-bind="treePanelResize.handleProps"
      ></div>

      <KbContentPanel v-if="kbStore.currentPath" :width="contentPanelResize.width.value" @close="kbStore.resetSelection()" />

      <!-- 知识库内容 → 引导区 拖拽手柄 -->
      <div
        v-if="kbStore.currentPath"
        class="resize-handle"
        :class="{ active: contentPanelResize.isDragging.value }"
        v-bind="contentPanelResize.handleProps"
      ></div>

      <!-- 未选中文件时的引导 -->
      <div v-else class="kb-guide">
        <n-scrollbar>
          <div class="kb-guide-inner">
            <h2>📚 我的知识库</h2>
            <p class="kb-guide-sub">从左侧选择一篇笔记查看，或参考下面的骨架搭建你的个人知识系统。</p>
            <MarkdownRender custom-id="kb-guide" :content="guideText" :final="true" />
          </div>
        </n-scrollbar>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { NButton, NIcon, NText, NScrollbar, NInput, NPopover, NPopconfirm, useMessage } from 'naive-ui'
import { ArrowBackOutline, LibraryOutline, SearchOutline, GitNetworkOutline, PricetagsOutline } from '@vicons/ionicons5'
import { MarkdownRender } from 'markstream-vue'
import 'markstream-vue/index.css'
import { useConfigStore } from '@/stores/config'
import { useKnowledgeStore } from '@/stores/knowledge'
import { useResizeHandle } from '@/composables/useResizeHandle'
import { kbSearch, type SearchHit } from '@/api/knowledge'
import KbTreePanel from '@/components/kb/KbTreePanel.vue'
import KbContentPanel from '@/components/kb/KbContentPanel.vue'

import { ref } from 'vue'

const router = useRouter()
const configStore = useConfigStore()
const kbStore = useKnowledgeStore()
const message = useMessage()

// 面板拖拽调整宽度（与 ChatWindow 共用 localStorage key）
const treePanelResize = useResizeHandle({
  minWidth: 150, maxWidth: 500, storageKey: 'panel-tree-width', initialWidth: 240, direction: 'right',
})
const contentPanelResize = useResizeHandle({
  minWidth: 240, maxWidth: 700, storageKey: 'panel-content-width', initialWidth: 420, direction: 'right',
})

// ---------- 语义搜索 ----------
const searchQuery = ref('')
const searchHits = ref<SearchHit[]>([])
const searching = ref(false)
const showResults = ref(false)

function snippet(text: string): string {
  const t = text.replace(/\s+/g, ' ').trim()
  return t.length > 120 ? t.slice(0, 120) + '…' : t
}

async function doSearch() {
  const q = searchQuery.value.trim()
  if (!q) return
  searching.value = true
  try {
    searchHits.value = await kbSearch(q, 8)
    showResults.value = true
  } catch (e: any) {
    message.error(e.message || '搜索失败')
    searchHits.value = []
    showResults.value = true
  } finally {
    searching.value = false
  }
}

function clearSearch() {
  searchHits.value = []
  showResults.value = false
}

// ---------- 自动标签 ----------
const autoTagRunning = ref(false)
const autoTagProgress = ref({ current: 0, total: 0 })

async function runAutoTag() {
  if (autoTagRunning.value) return
  autoTagRunning.value = true
  autoTagProgress.value = { current: 0, total: 0 }
  try {
    // 启动后台任务
    const resp = await fetch('/api/kb/tags/auto/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_path: '' }),
    })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: '启动失败' }))
      throw new Error(err.detail || '启动自动标签失败')
    }

    // 轮询进度
    let done = false
    while (!done) {
      await new Promise(r => setTimeout(r, 1000))
      try {
        const s = await fetch('/api/kb/tags/auto/status')
        const state = await s.json()
        autoTagProgress.value = { current: state.progress || 0, total: state.total || 0 }
        if (!state.running) {
          done = true
          if (state.error) {
            message.error(`自动标签失败：${state.error}`)
          } else if (state.result) {
            const r = state.result as any
            message.success(
              `自动标签完成！已为 ${r.tagged} 个文件打标（共扫描 ${r.total_files} 个文件）`
            )
          }
        }
      } catch {
        done = true
      }
    }
  } catch (e: any) {
    message.error(e.message || '自动标签失败')
  } finally {
    autoTagRunning.value = false
  }
}

async function openHit(hit: SearchHit) {
  showResults.value = false
  try {
    await kbStore.openFile(hit.file_path, true)
  } catch (e: any) {
    message.error(e.message || '打开笔记失败')
  }
}

const guideText = `知识库使用指南 —— 你可以用这些功能来构建个人知识系统：

- **📝 笔记编辑**：完整 Markdown 语法支持，所见即所得。输入 \`[[\` 可快速链接其他笔记，支持自动补全。
- **🔗 双向链接**：用 \`[[笔记名]]\` 在笔记间建立连接，知识不再孤立。反向链接面板帮你追踪"谁提到了这篇笔记"。
- **🌐 知识图谱**：可视化检视笔记关系网络 —— 哪些笔记紧密聚集、哪些是孤岛、哪些链接尚未创建，一目了然。
- **🔍 语义搜索**：配置 Embedding 后，用自然语言搜索笔记内容，按语义相关性而非关键词匹配排序。
- **🏷️ 标签分类**：为笔记打标签，从另一个维度组织信息，与双链网络互为补充。
- **📎 附件与附注**：支持图片、PDF 等文件管理。非文本文件可添加附注笔记，同样参与双链网络。
- **🔒 公共基础**：将通用参考资料放入「公共基础」目录，自动设为只读保护，避免误改。

> 建议从最小可用版本开始：先写 3–5 篇核心笔记，用 \`[[双链]]\` 连起来，逐步扩展。让结构从内容中自然生长，而非预先设计完美骨架。`

function goBack() {
  router.push('/chat')
}

function goGraph() {
  router.push('/knowledge/graph')
}

onMounted(async () => {
  await kbStore.loadRoot()
  // 若后端未设置但本地存有路径，尝试恢复
  if (!kbStore.root) {
    const saved = localStorage.getItem('kbPath')
    if (saved) await kbStore.setRoot(saved).catch(() => {})
  }
  if (kbStore.root) await kbStore.loadTree()
})
</script>

<style scoped>
.kb-container {
  display: flex;
  flex-direction: column;
  height: 100vh;
  background: var(--bg-secondary);
  color: var(--text-primary);
  font-family: 'Inter', 'Segoe UI', sans-serif;
  overflow: hidden;
}

/* 顶栏 */
.kb-topbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  height: 48px;
  padding: 0 20px;
  background: var(--glass-bg);
  backdrop-filter: blur(8px);
  border-bottom: var(--glass-border);
  flex-shrink: 0;
}
.kb-topbar-left { display: flex; align-items: center; gap: 10px; }
.kb-topbar-right { display: flex; align-items: center; gap: 12px; }
.kb-title {
  font-size: 1.05rem;
  font-weight: 700;
  background: var(--accent-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.kb-path {
  font-size: 0.75rem;
  max-width: 380px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.icon-btn {
  width: 32px; height: 32px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 50%;
  color: var(--text-primary);
  transition: background 0.2s;
}
.icon-btn:hover { background: rgba(74, 124, 247, 0.2); box-shadow: var(--shadow-glow); }

/* 主体 */
.kb-body { flex: 1; display: flex; overflow: hidden; }

/* 拖拽手柄 */
.resize-handle {
  width: 4px;
  flex-shrink: 0;
  cursor: col-resize;
  background: transparent;
  transition: background 0.15s;
  z-index: 10;
  user-select: none;
}
.resize-handle:hover,
.resize-handle.active {
  background: var(--accent-color, #4a7cf7);
}

/* 引导页 */
.kb-guide { flex: 1; overflow: hidden; }
.kb-guide-inner {
  max-width: 720px;
  margin: 0 auto;
  padding: 40px 32px;
}
.kb-guide-inner h2 { margin-bottom: 8px; }
.kb-guide-sub { color: var(--text-secondary); margin-bottom: 24px; }

/* 语义搜索结果 */
.kb-search-results {
  max-height: 420px;
  overflow-y: auto;
  background: var(--bg-secondary, #1e1e1e);
  border: var(--glass-border);
  border-radius: 10px;
  padding: 6px;
}
.kb-search-empty {
  padding: 16px;
  font-size: 0.8rem;
  color: var(--text-secondary);
  text-align: center;
}
.kb-search-item {
  padding: 8px 10px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.15s;
}
.kb-search-item:hover { background: rgba(74, 124, 247, 0.15); }
.kb-search-file {
  font-size: 0.78rem;
  font-weight: 600;
  color: var(--text-primary);
  margin-bottom: 3px;
  word-break: break-all;
}
.kb-search-heading { font-weight: 400; color: var(--text-secondary); }
.kb-search-snippet {
  font-size: 0.75rem;
  color: var(--text-secondary);
  line-height: 1.4;
}
</style>
