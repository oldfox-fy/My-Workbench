<template>
  <!-- 服务端生成/上传文件：渲染成带「下载」按钮的文件卡片 -->
  <span v-if="isServerFile" class="file-card" :class="fileTypeClass">
    <img v-if="iconSrc" class="file-card-icon" :src="iconSrc" alt="" />
    <span v-else class="file-card-icon file-card-emoji">📄</span>
    <span class="file-card-name" :title="displayName">{{ displayName }}</span>
    <button class="file-card-btn download" type="button" @click.stop.prevent="download">
      ⬇ 下载
    </button>
  </span>

  <!-- 本地文件路径：打开 / 复制路径 -->
  <span v-else-if="isLocal" class="file-card local" :class="fileTypeClass">
    <img v-if="iconSrc" class="file-card-icon" :src="iconSrc" alt="" />
    <span v-else class="file-card-icon file-card-emoji">📄</span>
    <span class="file-card-name" :title="localPath">{{ displayName }}</span>
    <button v-if="inPywebview" class="file-card-btn open" type="button" @click.stop.prevent="openLocal">
      打开
    </button>
    <button class="file-card-btn copy" type="button" @click.stop.prevent="copyPath">
      复制路径
    </button>
  </span>

  <!-- 普通链接（网页 / 引用等）：保持原有链接行为 -->
  <a
    v-else
    :href="finalHref"
    :class="['link-node', fileTypeClass]"
    target="_blank"
    @click="handleSmartClick"
  >
    <slot>{{ node.text }}</slot>
  </a>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useMessage } from 'naive-ui'
import {
  isLocalFilePath,
  isRunningInPyWebView,
  isServerFileUrl,
  downloadFile,
  extractFileName,
  copyToClipboard,
} from '@/utils/common'


const props = defineProps<{
  node: {
    type: 'link'
    href?: string
    text?: string
    loading?: boolean
    alt?: string
  }
}>()

const message = useMessage()

// 文件扩展名 → 图标 svg 映射（public/svg 目录下）
const iconMap: [string[], string][] = [
  [['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg'], 'image'],
  [['pdf'], 'pdf'],
  [['doc', 'docx'], 'word'],
  [['xls', 'xlsx'], 'excel'],
  [['ppt', 'pptx'], 'ppt'],
  [['txt', 'md', 'rtf'], 'text'],
  [['zip', 'rar', '7z', 'tar', 'gz'], 'zip'],
]

// 文件类型 → 样式 class（保留旧有的 ::before 图标兼容）
const typeMap = [
  { extensions: ['jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'svg'], class: 'file-image' },
  { extensions: ['pdf'], class: 'file-pdf' },
  { extensions: ['doc', 'docx'], class: 'file-word' },
  { extensions: ['xls', 'xlsx'], class: 'file-excel' },
  { extensions: ['ppt', 'pptx'], class: 'file-powerpoint' },
  { extensions: ['txt', 'md', 'rtf'], class: 'file-text' },
  { extensions: ['zip', 'rar', '7z', 'tar', 'gz'], class: 'file-archive' },
  { extensions: ['mp3', 'wav', 'flac', 'aac'], class: 'file-audio' },
  { extensions: ['mp4', 'webm', 'avi', 'mov', 'mkv'], class: 'file-video' },
]

// 1. 计算最终的 href (处理 URI 解码)
const finalHref = computed(() => {
  let url = props.node.href
  if (!url) return ''

  if (url.includes('%')) {
    try {
      const decoded = decodeURIComponent(url)
      // 假设 isLocalFilePath 已在外部定义
      if (isLocalFilePath(decoded)) {
        url = decoded
      }
    } catch (e) {
      console.warn('URL Decode failed', e)
    }
  }
  return url
})

// 2. 提取扩展名（忽略查询参数和哈希）
function extOf(url: string): string {
  if (!url) return ''
  const ext = url.split('?')[0].split('#')[0].split('.').pop()
  return (ext || '').toLowerCase()
}

// 3. 文件类型 Class
const fileTypeClass = computed(() => {
  const ext = extOf(finalHref.value)
  if (!ext) return ''
  const matched = typeMap.find(item => item.extensions.includes(ext))
  return matched ? matched.class : 'file-unknown'
})

// 4. 图标 src
const iconSrc = computed(() => {
  const ext = extOf(finalHref.value)
  const matched = iconMap.find(([exts]) => exts.includes(ext))
  return matched ? `/svg/${matched[1]}.svg` : ''
})

// 5. 是否为服务端文件 / 本地文件
const isServerFile = computed(() => isServerFileUrl(finalHref.value))
const isLocal = computed(() => !isServerFile.value && isLocalFilePath(finalHref.value))
const inPywebview = computed(() => isRunningInPyWebView())

// 6. 显示名：优先取 link 文本（若它确实像个文件名），否则从 URL 提取
const displayName = computed(() => {
  const text = (props.node.text || '').trim()
  if (text && !/^https?:\/\//i.test(text) && !text.includes('/') && !text.includes('\\')) {
    return text
  }
  return extractFileName(finalHref.value, '下载文件')
})

// 7. 下载文件名
const downloadName = computed(() => {
  const name = extractFileName(finalHref.value, '')
  return name || displayName.value
})

// 8. 本地文件路径（去掉 file:// 前缀，便于打开/复制）
const localPath = computed(() => {
  let url = finalHref.value
  try {
    url = decodeURIComponent(url)
  } catch {
    /* ignore */
  }
  return url.replace(/^file:\/{2,3}/, '')
})

async function download() {
  const res = await downloadFile(finalHref.value, downloadName.value)
  if (!res.success && res.error) {
    message.warning(res.error)
  }
}

async function openLocal() {
  if (!inPywebview.value) return
  const api = (window as any).pywebview?.api
  if (api?.open_with_default_app) {
    try {
      await api.open_with_default_app(localPath.value)
    } catch (e: any) {
      message.warning(String(e?.message || e))
    }
  }
}

function copyPath() {
  copyToClipboard(localPath.value)
  message.success('路径已复制')
}

// 9. 普通链接的智能点击（pywebview 下载 http 文件）
const handleSmartClick = async (e: MouseEvent) => {
  const url = finalHref.value
  if (!url || url.startsWith('javascript:') || url === '#') return

  if (isRunningInPyWebView()) {
    if (isLocalFilePath(url)) {
      e.preventDefault()
      const decodedPath = decodeURI(url.replace(/^file:\/{2,3}/, ''))
      if ((window as any).pywebview?.api?.open_with_default_app) {
        await (window as any).pywebview.api.open_with_default_app(decodedPath)
      }
    } else if (url.startsWith('http://') || url.startsWith('https://') || url.startsWith('/files/generate/')) {
      e.preventDefault()
      const fullUrl = url.startsWith('/') ? window.location.origin + url : url
      const fileName = downloadName.value || 'download'
      if ((window as any).pywebview?.api?.download_file) {
        await (window as any).pywebview.api.download_file(fullUrl, fileName)
      }
    }
  } else {
    if (isLocalFilePath(url)) {
      e.preventDefault()
      message.warning('暂不支持浏览器中打开，请使用桌面端打开')
    }
  }
}
</script>

<style scoped>
.file-card {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  max-width: 100%;
  padding: 8px 12px;
  margin: 4px 0;
  border-radius: 8px;
  background: var(--bg-secondary, rgba(255, 255, 255, 0.08));
  border: 1px solid var(--border-color, rgba(255, 255, 255, 0.12));
}

.file-card-icon {
  width: 20px;
  height: 20px;
  flex-shrink: 0;
  object-fit: contain;
}

.file-card-emoji {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
}

.file-card-name {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 14px;
  color: var(--text-primary, #fff);
}

.file-card-btn {
  flex-shrink: 0;
  border: none;
  border-radius: 6px;
  padding: 4px 12px;
  font-size: 13px;
  cursor: pointer;
  transition: background 0.2s;
  color: #fff;
}

.file-card-btn.download {
  background: var(--primary-color, #6366f1);
}
.file-card-btn.download:hover {
  background: #4f46e5;
}

.file-card-btn.open {
  background: rgba(99, 102, 241, 0.2);
  color: var(--primary-color, #6366f1);
}
.file-card-btn.open:hover {
  background: rgba(99, 102, 241, 0.35);
}

.file-card-btn.copy {
  background: var(--bg-tertiary, rgba(0, 0, 0, 0.2));
  color: var(--text-secondary, #ccc);
}
.file-card-btn.copy:hover {
  background: var(--bg-tertiary, rgba(0, 0, 0, 0.3));
}

.link-node {
  text-decoration: none;
}
.message-content .file-pdf::before {
  content: '';
  display: inline-block;
  width: 1.6em;
  height: 1.6em;
  background: url('/svg/pdf.svg') no-repeat center;
  background-size: contain;
  margin-right: 0.3em;
  vertical-align: middle;
}
.message-content .file-word::before {
  content: '';
  display: inline-block;
  width: 1.6em;
  height: 1.6em;
  background: url('/svg/word.svg') no-repeat center;
  background-size: contain;
  margin-right: 0.3em;
  vertical-align: middle;
}
.message-content .file-excel::before {
  content: '';
  display: inline-block;
  width: 1.6em;
  height: 1.6em;
  background: url('/svg/excel.svg') no-repeat center;
  background-size: contain;
  margin-right: 0.3em;
  vertical-align: middle;
}
.message-content .file-powerpoint::before {
  content: '';
  display: inline-block;
  width: 1.6em;
  height: 1.6em;
  background: url('/svg/ppt.svg') no-repeat center;
  background-size: contain;
  margin-right: 0.3em;
  vertical-align: middle;
}
.message-content .file-text::before {
  content: '';
  display: inline-block;
  width: 1.6em;
  height: 1.6em;
  background: url('/svg/text.svg') no-repeat center;
  background-size: contain;
  margin-right: 0.3em;
  vertical-align: middle;
}
.message-content .file-archive::before {
  content: '';
  display: inline-block;
  width: 1.6em;
  height: 1.6em;
  background: url('/svg/zip.svg') no-repeat center;
  background-size: contain;
  margin-right: 0.3em;
  vertical-align: middle;
}
.message-content .file-image::before {
  content: '';
  display: inline-block;
  width: 1.6em;
  height: 1.6em;
  background: url('/svg/image.svg') no-repeat center;
  background-size: contain;
  margin-right: 0.3em;
  vertical-align: middle;
}
.message-content a {
  display: inline-block;
  padding: 8px 12px;
  background: rgba(255, 255, 255, 0.18);
  color: #fff;
  border-radius: 4px;
  position: relative;
}
.message-content a:hover {
  background: rgba(100, 104, 230, 0.4);
}

[theme-mode='light'] .message-content a {
  background: rgba(0, 0, 0, 0.1);
  color: #000;
}

[theme-mode='light'] .message-content a:hover {
  background: rgba(0, 0, 0, 0.3);
}
</style>
