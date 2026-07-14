<template>
  <div class="ppt-preview-card">
    <!-- 头部：标题 + 页数 + 主题 + 下载按钮 -->
    <div class="ppt-preview-header" @click="collapsed = !collapsed">
      <span class="ppt-icon">📊</span>
      <span class="ppt-title">PPT 预览：{{ data.title }}</span>
      <span class="ppt-badge">{{ data.slide_count }} 页</span>
      <span class="ppt-theme-badge">{{ data.theme_label || data.theme }}</span>
      <span class="ppt-toggle">{{ collapsed ? '▶' : '▼' }}</span>
    </div>

    <!-- 幻灯片缩略图横向滚动条 -->
    <div v-if="!collapsed" class="ppt-slides-strip">
      <div
        v-for="(slide, i) in data.slides"
        :key="i"
        class="ppt-slide-thumb"
        :class="'slide-type-' + (slide.type || 'content')"
      >
        <div class="slide-number">{{ i + 1 }}</div>
        <div class="slide-preview-area">
          <div class="slide-thumb-type">{{ typeLabel(slide.type) }}</div>
          <div class="slide-thumb-title">{{ slide.title || '无标题' }}</div>
          <div class="slide-thumb-content" v-if="slide.content_preview">
            {{ slide.content_preview }}
          </div>
        </div>
      </div>
    </div>

    <!-- 底部操作栏 -->
    <div v-if="!collapsed" class="ppt-preview-footer">
      <a
        class="ppt-download-btn"
        :href="fullDownloadUrl"
        :download="data.filename"
        @click="handleDownload"
      >
        ⬇ 下载 PPTX
      </a>
      <span class="ppt-footer-hint">点击上方卡片可浏览每页内容概览</span>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { isRunningInPyWebView } from '@/utils/common'

interface SlidePreview {
  title: string
  content_preview: string
  type: string
}

interface PptPreviewData {
  file: string
  filename: string
  title: string
  slide_count: number
  theme: string
  theme_label?: string
  slides: SlidePreview[]
}

const props = defineProps<{
  node: {
    type: string
    text?: string
  }
}>()

const collapsed = ref(false)

const data = computed<PptPreviewData>(() => {
  try {
    if (props.node.text) {
      return JSON.parse(props.node.text)
    }
  } catch {
    // fall through
  }
  return {
    file: '',
    filename: 'presentation.pptx',
    title: '演示文稿',
    slide_count: 0,
    theme: 'blue',
    slides: [],
  }
})

const fullDownloadUrl = computed(() => {
  const url = data.value.file
  if (!url) return ''
  if (url.startsWith('/')) {
    return url
  }
  return url
})

function typeLabel(type: string): string {
  const labels: Record<string, string> = {
    cover: '封面',
    toc: '目录',
    content: '内容',
    ending: '结尾',
  }
  return labels[type] || type
}

function handleDownload(e: MouseEvent) {
  const url = fullDownloadUrl.value
  if (!url) return

  if (isRunningInPyWebView()) {
    e.preventDefault()
    const fullUrl = url.startsWith('/') ? window.location.origin + url : url
    if (window.pywebview?.api?.download_file) {
      window.pywebview.api.download_file(fullUrl, data.value.filename || 'presentation.pptx')
    }
  }
}
</script>

<style scoped>
.ppt-preview-card {
  border: 1px solid var(--border-color);
  border-radius: 10px;
  background: var(--bg-secondary);
  overflow: hidden;
  margin: 12px 0;
  transition: border-color 0.2s;
}

.ppt-preview-card:hover {
  border-color: var(--primary-color, #6366f1);
}

/* ── 头部 ── */
.ppt-preview-header {
  display: flex;
  align-items: center;
  padding: 12px 16px;
  cursor: pointer;
  user-select: none;
  gap: 10px;
  transition: background 0.15s;
}
.ppt-preview-header:hover {
  background: rgba(99, 102, 241, 0.04);
}

.ppt-icon { font-size: 18px; flex-shrink: 0; }
.ppt-title {
  font-weight: 600;
  font-size: 14px;
  color: var(--text-primary);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ppt-badge {
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 12px;
  background: rgba(99, 102, 241, 0.12);
  color: var(--primary-color, #6366f1);
  flex-shrink: 0;
}
.ppt-theme-badge {
  font-size: 11px;
  padding: 2px 8px;
  border-radius: 10px;
  background: var(--bg-tertiary);
  color: var(--text-secondary);
  flex-shrink: 0;
}
.ppt-toggle {
  font-size: 12px;
  color: var(--text-tertiary);
  flex-shrink: 0;
}

/* ── 幻灯片缩略图横向滚动 ── */
.ppt-slides-strip {
  display: flex;
  gap: 12px;
  overflow-x: auto;
  padding: 8px 16px 16px;
  scroll-behavior: smooth;
  -webkit-overflow-scrolling: touch;
}
.ppt-slides-strip::-webkit-scrollbar {
  height: 4px;
}
.ppt-slides-strip::-webkit-scrollbar-thumb {
  background: var(--border-color);
  border-radius: 2px;
}
.ppt-slides-strip::-webkit-scrollbar-thumb:hover {
  background: var(--text-tertiary);
}

/* ── 单张幻灯片卡片 ── */
.ppt-slide-thumb {
  flex-shrink: 0;
  width: 180px;
  min-height: 120px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  background: var(--bg-primary);
  position: relative;
  overflow: hidden;
  transition: box-shadow 0.2s, transform 0.2s;
  cursor: default;
}
.ppt-slide-thumb:hover {
  box-shadow: 0 2px 12px rgba(0, 0, 0, 0.1);
  transform: translateY(-2px);
}

.slide-number {
  position: absolute;
  top: 4px;
  right: 6px;
  font-size: 10px;
  color: var(--text-tertiary);
  font-weight: 600;
}

.slide-preview-area {
  padding: 14px 10px 10px;
  height: 100%;
  display: flex;
  flex-direction: column;
}

.slide-thumb-type {
  font-size: 10px;
  text-transform: uppercase;
  color: var(--primary-color, #6366f1);
  font-weight: 600;
  margin-bottom: 4px;
  letter-spacing: 0.5px;
}

.slide-thumb-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--text-primary);
  line-height: 1.3;
  margin-bottom: 6px;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.slide-thumb-content {
  font-size: 11px;
  color: var(--text-secondary);
  line-height: 1.4;
  flex: 1;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

/* ── 类型特定样式 ── */
.slide-type-cover .slide-thumb-title {
  font-size: 16px;
  text-align: center;
  -webkit-line-clamp: 3;
}
.slide-type-ending .slide-thumb-title {
  font-size: 15px;
  text-align: center;
}

/* ── 底部 ── */
.ppt-preview-footer {
  display: flex;
  align-items: center;
  padding: 10px 16px;
  border-top: 1px solid var(--border-color);
  gap: 16px;
}

.ppt-download-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 6px 16px;
  border-radius: 6px;
  background: var(--primary-color, #6366f1);
  color: #fff;
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
  cursor: pointer;
  transition: background 0.2s;
}
.ppt-download-btn:hover {
  background: #4f46e5;
}

.ppt-footer-hint {
  font-size: 12px;
  color: var(--text-tertiary);
}

/* ── 亮色/暗色适配 ── */
/*
 组件本身使用 CSS 变量（var(--bg-primary), var(--text-primary) 等），
 跟随 Naive UI 的主题模式自动适配。
*/
</style>
