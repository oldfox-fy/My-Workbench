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
      </div>
    </header>

    <!-- 主体：左树 + 右内容 -->
    <div class="kb-body">
      <KbTreePanel />

      <KbContentPanel v-if="kbStore.currentPath" @close="kbStore.resetSelection()" />

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
import { NButton, NIcon, NText, NScrollbar, NInput, NPopover, useMessage } from 'naive-ui'
import { ArrowBackOutline, LibraryOutline, SearchOutline, GitNetworkOutline } from '@vicons/ionicons5'
import { MarkdownRender } from 'markstream-vue'
import 'markstream-vue/index.css'
import { useConfigStore } from '@/stores/config'
import { useKnowledgeStore } from '@/stores/knowledge'
import { kbSearch, type SearchHit } from '@/api/knowledge'
import KbTreePanel from '@/components/kb/KbTreePanel.vue'
import KbContentPanel from '@/components/kb/KbContentPanel.vue'

import { ref } from 'vue'

const router = useRouter()
const configStore = useConfigStore()
const kbStore = useKnowledgeStore()
const message = useMessage()

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

async function openHit(hit: SearchHit) {
  showResults.value = false
  try {
    await kbStore.openFile(hit.file_path, true)
  } catch (e: any) {
    message.error(e.message || '打开笔记失败')
  }
}

const guideText = `个人知识系统骨架（可参考手动搭建）：

- **01-输入系统**：世界进入我 —— 输入记录、来源材料、讨论记录、输入索引、回看与再读
- **02-沉淀系统**：我消化世界 —— 灵感与问题库、洞见库、知识卡片库、我的资产库、概念定义库、人生主题地图、个人档案库
- **03-行动系统**：我作用于世界 —— 目标与方向、行动计划、项目与实验、作品与发布、反馈与复盘、机会与成果、行动档案
- **04-能力系统**：我用什么能力做 —— 能力总览、系统级能力、输入/沉淀/行动能力、跨系统流程、能力孵化池、能力注册表、能力统计
- **90-系统底座**：系统怎么稳定运行 —— 系统总览、规则中心、索引与搜索、模板中心、记忆回流、系统演进日志、自动化脚本、系统审计与清理

> 建议从最小可用版本开始：只保留最近 7 天真正会用到的目录，其余先空着。`

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
