<template>
  <div class="app-container" :class="[configStore.themeMode]">
    <!-- 移动端侧边栏遮罩 -->
    <div v-if="isMobile && sidebarOpen" class="sidebar-overlay" @click="sidebarCollapsed = true; sidebarOpen = false"></div>

    <!-- ========== 侧边栏（折叠式） ========== -->
     <Transition name="sidebar">
      <aside v-if="!sidebarCollapsed" class="sidebar-panel border-marquee-right" :class="{ collapsed: sidebarCollapsed, 'sidebar-open': sidebarOpen }" :style="{ width: sidebarResize.width.value + 'px' }">
        <div class="sidebar-header">
          <span class="logo-text"><m-svg name="star" style="position: absolute;left:120px;top:18px;"/>✨ My Workbench</span>
          <n-button v-if="!isMobile" text class="icon-btn" @click="sidebarCollapsed = true" title="收起侧栏">
            <template #icon>
              <n-icon :size="22">
                <m-svg name="expand"/>
              </n-icon>
            </template>
          </n-button>
        </div>
        <div class="btn-main">
          <n-button block strong secondary size="large" class="new-chat-btn" @click="createChat">
            <template #icon><m-svg name="add"/></template>
            创建新对话
          </n-button>
          <n-button block strong secondary size="large" class="new-chat-btn kb-btn" @click="openKnowledge">
            <template #icon><n-icon><LibraryOutline /></n-icon></template>
            我的知识库
          </n-button>
        </div>
        <n-scrollbar content-style="padding:0 16px" style="max-height: calc(100vh - 200px);">
          <n-list hoverable clickable :show-divider="false">
            <n-list-item v-for="chat in chatStore.chats" :key="chat.id"
              @click="openChat(chat.id)"
              :class="{ active: chat.id === chatStore.activeChatId }">
              <div class="chat-item-row">
                <div class="chat-title" v-if="renamingChatId !== chat.id">
                  {{ chat.title }}
                </div>
                <n-input v-else
                  v-model:value="renameText"
                  size="small"
                  autofocus
                  @blur="confirmRename(chat.id)"
                  @keydown.enter="confirmRename(chat.id)"
                  placeholder="请输入标题"
                />
                <div class="chat-actions" v-if="renamingChatId !== chat.id">
                  <n-button text size="tiny" @click.stop="exportChat(chat.id, chat.title)" title="导出">
                    <template #icon><n-icon :size="16"><DownloadOutline /></n-icon></template>
                  </n-button>
                  <n-button text size="tiny" @click.stop="startRename(chat)" title="重命名">
                    <template #icon><n-icon :size="16"><m-svg name="edit"/></n-icon></template>
                  </n-button>
                  <n-popconfirm
                    @positive-click="() => chatStore.deleteChat(chat.id)"
                    negative-text="取消"
                    positive-text="好的"
                    :negative-button-props="{size: 'tiny'}"
                    :positive-button-props="{size: 'tiny'}"
                  >
                    <template #trigger>
                      <n-button text size="tiny" @click.stop title="删除">
                        <template #icon><n-icon :size="16"><m-svg name="del"/></n-icon></template>
                      </n-button>
                    </template>
                    确定删除整个对话「{{ chat.title }}」吗？
                  </n-popconfirm>
                </div>
              </div>
            </n-list-item>
          </n-list>
        </n-scrollbar>
        <div class="sidebar-footer">
          <n-button text @click="showTools = true">
            <template #icon>
              <n-icon><ConstructOutline /></n-icon>
            </template>
            工具列表
          </n-button>
          <n-button text @click="importChat">
            <template #icon>
              <n-icon><CloudUploadOutline /></n-icon>
            </template>
            导入对话
          </n-button>
          <n-button text @click="showSettings = true">
            <template #icon>
              <n-icon><SettingsOutline /></n-icon>
            </template>
            系统设置
          </n-button>
        </div>
      </aside>
    </Transition>

    <!-- 侧边栏拖拽手柄 -->
    <div
      v-if="!isMobile && !sidebarCollapsed"
      class="resize-handle resize-handle-sidebar"
      :class="{ active: sidebarResize.isDragging.value }"
      :style="{ left: sidebarResize.width.value + 'px' }"
      v-bind="sidebarResize.handleProps"
    ></div>

    <!-- ========== 工作区（知识库面板 + 主区域） ========== -->
    <div
      class="workspace"
      :style="{
        paddingLeft: (!isMobile && !sidebarCollapsed) ? sidebarResize.width.value + 'px' : '0'
      }"
    >
      <!-- 知识库目录侧栏 -->
      <KbTreePanel v-if="kbOpen" show-close :width="treePanelResize.width.value" @close="closeKnowledge" />

      <!-- 知识库树 → 内容 拖拽手柄 -->
      <div
        v-if="kbOpen"
        class="resize-handle"
        :class="{ active: treePanelResize.isDragging.value }"
        v-bind="treePanelResize.handleProps"
      ></div>

      <!-- 知识库内容窗口（打开文件后才展开） -->
      <KbContentPanel v-if="kbOpen && kbStore.currentPath" :width="contentPanelResize.width.value" @close="kbStore.resetSelection()" />

      <!-- 知识库内容 → 主区域 拖拽手柄 -->
      <div
        v-if="kbOpen && kbStore.currentPath"
        class="resize-handle"
        :class="{ active: contentPanelResize.isDragging.value }"
        v-bind="contentPanelResize.handleProps"
      ></div>

      <!-- 主区域 -->
      <main
        class="main-stage"
        @dragenter="onDragEnter($event, isLoading)"
        @dragover="onDragOver"
        @dragleave="onDragLeave"
        @drop="onDrop($event, chatStore.activeChatId, isLoading)"
      >
      <div v-if="isDragging && chatStore.activeChatId" class="drag-overlay">
        <div class="drag-hint"><n-icon><DocumentOutline /></n-icon> 释放文件以上传</div>
      </div>

      <!-- 顶部工具栏 -->
      <header v-if="chatStore.activeChatId" class="top-bar" :class="[sidebarCollapsed || isMobile ? 'border-marquee-center' : '']">
        <n-flex style="width:100%" justify="space-between">
          <n-flex>
            <n-button v-if="isMobile" text @click="sidebarOpen = !sidebarOpen; sidebarCollapsed = !sidebarCollapsed">
              <template #icon><n-icon><MenuOutline /></n-icon></template>
            </n-button>
            <n-button v-if="!isMobile && sidebarCollapsed" text class="icon-btn" @click="sidebarCollapsed = !sidebarCollapsed" title="展开侧栏">
              <template #icon>
                <n-icon :size="22">
                  <m-svg name="expand"/>
                </n-icon>
              </template>
            </n-button>
            <div class="model-badge">
              <n-select
                v-model:value="activeModelId"
                :options="modelOptions"
                size="small"
                style="width: 120px"
                placeholder="选择模型"
                @update:value="switchActiveModel"
              />
            </div>
            <div class="toolbar-right">
              <n-select
                v-if="chatStore.enableProfile"
                v-model:value="profileStore.activeProfileId"
                :options="profileOptions"
                size="small"
                placeholder="选择角色"
                style="width: 150px; margin-right: 12px;"
                clearable
                @update:value="switchActiveProfile"
              />
            </div>
          </n-flex>
          <n-popover
            v-if="showQRCode"
            placement="bottom"
            trigger="hover"
          >
            <template #trigger>
              <n-button text>
                <template #icon>
                  <n-icon><QrCodeOutline /></n-icon>
                </template>
              </n-button>
            </template>
            <div style="text-align:center;font-size:14px;">
              <n-qr-code :value="qrCodeUrl" />
              <div style="margin-top:4px">移动设备扫码开启对话</div>
            </div>
          </n-popover>
        </n-flex>
      </header>

      <!-- 消息列表组件 -->
      <MessageList
        ref="messageListRef"
        v-if="chatStore.activeChatId"
        :chat-id="chatStore.activeChatId"
        :messages="currentMessages"
        :is-mobile="isMobile"
        :is-loading="isLoading"
        :streaming-content="streamingContent"
        :regenerating-msg="regeneratingMsg"
        :is-dark="isDark"
        :show-welcome="showWelcome"
        :copy-svg-name="copySvgName"
        @copy="copyContent"
        @regenerate="handleRegenerateResponse"
        @edit="startEditMessage"
        @delete="chatStore.deleteMessage"
        @branch="handleBranch"
      />

      <!-- 聊天输入框组件 -->
      <ChatInput
        v-if="chatStore.activeChatId"
        v-model="currentInput"
        v-model:selected="selected"
        v-model:file-list="uploadFileList"
        :is-loading="isLoading"
        :disabled="isLoading || !chatStore.activeChatId || !activeModelId"
        :uploaded-files="uploadedFiles"
        :show-scroll-btn="messageListRef?.showScrollBtn && currentMessages.length > 0"
        :show-regenerate-hint="!isLoading && currentMessages.length >= 1 && currentMessages[currentMessages.length - 1]?.role === 'user'"
        :show-deep-think="configStore.activeModel?.type === 'online'"
        :auto-read="autoRead"
        :max-files="fileConfig.max"
        :file-accept="fileConfig.accept"
        :before-upload="onBeforeUpload"
        @send="onSendMessage"
        @stop="stopGeneration"
        @scroll-bottom="messageListRef?.scrollToLatestSmooth()"
        @remove-file="removeFile"
        @regenerate-current="onRegenerateFromCurrentHistory"
        @files-paste="handlePasteFiles"
        @upload-change="handleFileUpload"
        @toggle-auto-read="toggleAutoRead"
        @voice-text="onVoiceText"
      />
    </main>
    </div>

    <!-- 设置抽屉 -->
    <SettingsDrawer v-model:show="showSettings" />

    <!-- 工具列表抽屉 -->
    <ToolsDrawer v-model:show="showTools" />

    <!-- 工具审批弹窗 -->
    <ToolApprovalDialog
      ref="approvalDialogRef"
      :call-id="approvalCallId"
      :tool-name="approvalToolName"
      :args-preview="approvalArgsPreview"
      @resolved="onApprovalResolved"
    />

    <!-- 编辑消息模态框 -->
    <n-modal
      v-model:show="showEditModal"
      preset="dialog"
      :draggable="!isFullscreen"
      :mask-closable="false"
      :close-on-esc="false"
      title="编辑消息"
      positive-text="保存"
      negative-text="取消"
      :class="{ 'edit-modal-fullscreen': isFullscreen }"
      :style="isFullscreen
        ? { width: '100vw', maxWidth: '100vw', height: '100vh', maxHeight: '100vh', margin: 0, borderRadius: 0 }
        : { width: '600px' }"
      @positive-click="onSaveEdit"
      @after-leave="onEditModalClose"
    >
      <template #header>
        <div class="edit-modal-title">
          <span>编辑消息</span>
          <n-popover trigger="hover" placement="bottom">
            <template #trigger>
              <n-button quaternary size="tiny" @click="toggleFullscreen" class="fullscreen-btn">
                <template #icon>
                  <n-icon :size="20">
                    <m-svg :name="isFullscreen ? 'fullscreen-exit' : 'fullscreen'" />
                  </n-icon>
                </template>
              </n-button>
            </template>
            {{ isFullscreen ? '退出全屏 (Esc)' : '全屏编辑' }}
          </n-popover>
        </div>
      </template>

      <n-input
        v-model:value="editContent"
        type="textarea"
        :autosize="isFullscreen ? false : { minRows: 12, maxRows: 12 }"
        :style="isFullscreen ? 'height: calc(100vh - 180px);' : ''"
        placeholder="请输入内容"
      />
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, nextTick } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { NButton, NInput, NList, NListItem, NIcon, NScrollbar, NFlex, NSelect, NModal, NPopconfirm, NPopover, NQrCode } from 'naive-ui'
import type { UploadFileInfo } from 'naive-ui'
import { SettingsOutline, DocumentOutline, MenuOutline, QrCodeOutline, ConstructOutline, LibraryOutline, DownloadOutline, CloudUploadOutline } from '@vicons/ionicons5'
import { useChatStore, type Message } from '@/stores/chat'
import { useConfigStore, fileConfig } from '@/stores/config'
import { useProfileStore } from '@/stores/profiles'
import { useToolStore } from '@/stores/tools'
import { useKnowledgeStore } from '@/stores/knowledge'
import SettingsDrawer from '@/components/SettingsDrawer.vue'
import ToolsDrawer from '@/components/ToolsDrawer.vue'
import KbTreePanel from '@/components/kb/KbTreePanel.vue'
import KbContentPanel from '@/components/kb/KbContentPanel.vue'
import mSvg from '@/components/mSvg.vue'
// import MessageList from '@/components/VirtualMessageList.vue'
import MessageList from '@/components/MessageList.vue'
import ChatInput from '@/components/ChatInput.vue'
import ToolApprovalDialog from '@/components/ToolApprovalDialog.vue'

import { useModel } from '@/composables/useModel'
import { useFileUpload } from '@/composables/useFileUpload'
import { useChat } from '@/composables/useChat'
import { useMessageActions } from '@/composables/useMessageActions'
import { useResizeHandle } from '@/composables/useResizeHandle'
import { useTTS } from '@/composables/useVoice'
import { localIP, uploadDir } from '@/utils/message'

const route = useRoute()
const router = useRouter()
const chatStore = useChatStore()
const configStore = useConfigStore()
const profileStore = useProfileStore()
const toolStore = useToolStore()
const kbStore = useKnowledgeStore()

const isMobile = ref(false)
const sidebarOpen = ref(false)
const qrCodeUrl = ref('')
const showQRCode = ref(true)

// 知识库面板（桌面端在对话页内嵌打开）
const kbOpen = ref(false)

// 面板拖拽调整宽度
const sidebarResize = useResizeHandle({
  minWidth: 180, maxWidth: 400, storageKey: 'panel-sidebar-width', initialWidth: 260, direction: 'right',
})
const treePanelResize = useResizeHandle({
  minWidth: 150, maxWidth: 500, storageKey: 'panel-tree-width', initialWidth: 240, direction: 'right',
})
const contentPanelResize = useResizeHandle({
  minWidth: 240, maxWidth: 700, storageKey: 'panel-content-width', initialWidth: 420, direction: 'right',
})

const isDark = computed(() => configStore.themeMode === 'dark')

function checkMobile() {
  isMobile.value = window.innerWidth <= 768
  if (!isMobile.value) sidebarOpen.value = false
  showQRCode.value = !(/Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent) && 'ontouchstart' in window || navigator.maxTouchPoints > 0)
}

const selected = ref(localStorage.getItem('thinking') === 'true')

// 工具审批
const approvalCallId = ref('')
const approvalToolName = ref('')
const approvalArgsPreview = ref('')
const approvalResolve = ref<((approved: boolean) => void) | null>(null)
const sessionWhitelistTools = ref<Set<string>>(new Set())
const approvalDialogRef = ref<InstanceType<typeof ToolApprovalDialog> | null>(null)

function onApprovalResolved(callId: string, approved: boolean, sessionWhitelist: boolean) {
  if (approved && sessionWhitelist) {
    sessionWhitelistTools.value.add(approvalToolName.value)
  }
  approvalResolve.value?.(approved)
  approvalResolve.value = null
}

// 语音功能
const { autoRead, speak } = useTTS()
autoRead.value = localStorage.getItem('autoRead') === 'true'

function toggleAutoRead() {
  autoRead.value = !autoRead.value
  localStorage.setItem('autoRead', String(autoRead.value))
}

function onVoiceText(text: string) {
  // 将语音识别的文字追加到输入框
  currentInput.value = currentInput.value
    ? currentInput.value + ' ' + text
    : text
}

const { activeModelId, modelOptions, switchActiveModel } = useModel()
const { uploadFileList, uploadedFiles, isDragging, onDragEnter, onDragOver,
    onDragLeave, onDrop, onBeforeUpload, handleFileUpload, removeFile, clearFiles
} = useFileUpload()
const { currentInput, isLoading, streamingContent, regeneratingMsg,
    sendMessage, regenerateResponse, regenerateFromCurrentHistory, stopGeneration,
    setApprovalHandler,
} = useChat()

  // 工具审批处理器（必须在 useChat 之后设置）
  setApprovalHandler(async (callId, toolName, argsPreview) => {
    if (sessionWhitelistTools.value.has(toolName)) return true
    return new Promise((resolve) => {
      approvalCallId.value = callId
      approvalToolName.value = toolName
      approvalArgsPreview.value = argsPreview
      approvalResolve.value = resolve
      nextTick(() => approvalDialogRef.value?.show())
    })
  })

const { showEditModal, editContent, copySvgName, copyContent,
  startEditMessage, saveEdit, renamingChatId, renameText, startRename, confirmRename
} = useMessageActions()

const messageListRef = ref<InstanceType<typeof MessageList> | null>(null)

const currentMessages = computed(() => chatStore.currentChatMessages)

const isFullscreen = ref(false)

const toggleFullscreen = () => {
  isFullscreen.value = !isFullscreen.value
}

// 关闭弹窗时重置全屏状态
const onEditModalClose = () => {
  isFullscreen.value = false
}

// ESC 退出全屏（不关闭弹窗）
const onKeydown = (e: KeyboardEvent) => {
  if (e.key === 'Escape' && isFullscreen.value && showEditModal.value) {
    // 输入框聚焦时不触发，避免和取消操作冲突
    const active = document.activeElement
    if (active && active.tagName === 'TEXTAREA') {
      (active as HTMLTextAreaElement).blur()
      return
    }
    isFullscreen.value = false
  }
}

// 粘贴文件处理：由 ChatInput 发出 paste 事件，此处处理
function handlePasteFiles(files: File[]) {
  if (!chatStore.activeChatId || isLoading.value || !activeModelId.value) return

  // 过滤文件类型
  const acceptedFiles = files.filter(f => {
    const suffix = '.' + f.name.split('.').pop()?.toLowerCase()
    const acceptList = fileConfig.accept.split(',').map(s => s.trim())
    return acceptList.includes(suffix)
  })
  if (acceptedFiles.length === 0) return

  const remaining = fileConfig.max - uploadedFiles.value.length
  if (remaining <= 0) return

  const filesToAdd = acceptedFiles.slice(0, remaining)
  for (const file of filesToAdd) {
    let filename = file.name
    if (!filename || filename === 'image.png' || filename === 'blob' || filename === 'clipboard') {
      const ext = file.name.split('.').pop() || 'bin'
      const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19)
      filename = `paste-${timestamp}-${Math.random().toString(36).slice(2, 6)}.${ext}`
    }

    const uploadFile: UploadFileInfo = {
      id: `${Date.now()}-${Math.random().toString(36).slice(2)}`,
      name: filename,
      status: 'pending',
      file: new File([file], filename, { type: file.type }),
    }
    uploadFileList.value.push(uploadFile)
  }

  handleFileUpload({ fileList: uploadFileList.value })
}

// 发送消息
const onSendMessage = () => {
  sendMessage(uploadedFiles.value, () => {
    messageListRef.value?.scrollToLatest()
  })
  clearFiles()
}

// 重新生成当前响应
const onRegenerateFromCurrentHistory = async () => {
  await regenerateFromCurrentHistory()
}

// 重新生成特定消息
const handleRegenerateResponse = async (msg: Message) => {
  await regenerateResponse(msg)
}

// 编辑后保存并重新生成
const onSaveEdit = async () => {
  await saveEdit(() => onRegenerateFromCurrentHistory())
}

// ========== 动效与初始化 ==========
const showWelcome = ref(false)

watch(() => route.params.id, (newId) => {
  if (isLoading.value) stopGeneration()
  chatStore.activeChatId = newId as string
}, { immediate: true })

// 切换对话或首次加载时，滚动到底部
watch(() => chatStore.activeChatId, async (newId) => {
  if (newId) {
    await chatStore.loadMessages(newId)
    showWelcome.value = currentMessages.value.length === 0
    await nextTick()
    await new Promise(resolve => requestAnimationFrame(resolve))
    if (!showWelcome.value) {
      messageListRef.value?.scrollToLatest()
    }
  } else {
    // 没有激活对话时，自动进入对话页：优先打开最近一个，否则新建
    await chatStore.loadChats()
    if (chatStore.chats.length > 0) {
      openChat(chatStore.chats[0].id)
    } else {
      const newChatId = await chatStore.addChat()
      openChat(newChatId)
    }
  }
}, { immediate: true })

const sidebarCollapsed = ref(true)
const showSettings = ref(false)
const showTools = ref(false)

const profileOptions = computed(() =>
  profileStore.profiles.map((p) => ({ label: p.name, value: p.id }))
)

const switchActiveProfile = (profileId: string) => {
  localStorage.setItem('activeProfileId', profileId)
}

function setQRCodeUrl() {
  qrCodeUrl.value = window.location.href.replace(/\b(?:localhost|\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b/g, localIP.value)
}

const createChat = async () => {
  const newChatId = await chatStore.addChat()
  openChat(newChatId)
}

// 导出对话
async function exportChat(chatId: string, title: string) {
  const url = `/api/chats/${chatId}/export?format=md`
  const a = document.createElement('a')
  a.href = url
  a.download = `${title || '对话'}.md`
  a.click()
}

// 对话分叉
async function handleBranch(msg: any) {
  try {
    const resp = await fetch(`/api/chats/${chatStore.activeChatId}/branch?message_id=${msg.id}`, { method: 'POST' })
    if (!resp.ok) throw new Error('分叉失败')
    const data = await resp.json()
    await chatStore.loadChats()
    openChat(data.chat_id)
  } catch (e: any) {
    console.error('分叉失败:', e.message)
  }
}

// 导入对话
async function importChat() {
  const input = document.createElement('input')
  input.type = 'file'
  input.accept = '.json'
  input.onchange = async () => {
    const file = input.files?.[0]
    if (!file) return
    try {
      const text = await file.text()
      const data = JSON.parse(text)
      const resp = await fetch('/api/chats/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ data }),
      })
      if (!resp.ok) throw new Error('导入失败')
      await chatStore.loadChats()
    } catch (e: any) {
      console.error('导入失败:', e.message)
    }
  }
  input.click()
}

const openKnowledge = async () => {
  // 窄屏/移动端：跳转独立整页；桌面端：在当前页内嵌切换
  if (isMobile.value) {
    router.push('/knowledge')
    return
  }
  kbOpen.value = !kbOpen.value
  if (kbOpen.value) {
    // 首次打开时加载知识库根目录与目录树
    if (!kbStore.root) {
      await kbStore.loadRoot()
      if (!kbStore.root) {
        const saved = localStorage.getItem('kbPath')
        if (saved) await kbStore.setRoot(saved).catch(() => {})
      }
    }
    if (kbStore.root && !kbStore.tree.length) await kbStore.loadTree()
  }
}

const closeKnowledge = () => {
  kbOpen.value = false
  kbStore.resetSelection()
}

const openChat = (chatId: string) => {
  if (isLoading.value) stopGeneration()
  chatStore.activeChatId = chatId
  router.push({ name: 'chat', params: { id: chatId } })
  setTimeout(() => {
    setQRCodeUrl()
  }, 300)
}

watch(() => selected.value, (newVal) => {
  localStorage.setItem('thinking', newVal ? 'true' : 'false')
})

// 自动朗读：AI 回复完成后，若开启自动朗读则播放
watch(() => isLoading.value, (newVal, oldVal) => {
  if (oldVal && !newVal && autoRead.value && currentMessages.value.length > 0) {
    const last = currentMessages.value[currentMessages.value.length - 1]
    if (last?.role === 'assistant' && last?.content) {
      speak(last.content)
    }
  }
})

async function waitForBackend() {
  try {
    const res = await fetch('/api/wait-ready')
    const data = await res.json()
    if (data.ready) {
      await toolStore.loadToolsInfo()
    } else {
      console.error('❌ 后台初始化失败:', data.error)
      // 显示错误提示
    }
  } catch (err) {
    console.error('等待后台就绪时网络异常:', err)
  }
}

onMounted(async () => {
  checkMobile()
  let resizeTimer: ReturnType<typeof setTimeout>
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer)
    resizeTimer = setTimeout(() => {
      checkMobile()
    }, 150)
  })
  window.addEventListener('keydown', onKeydown)
  await profileStore.loadProfiles()
  fetch('/api/system-info').then(async (res) => {
    const data = await res.json()
    localIP.value = data.local_ip
    uploadDir.value = data.upload_dir
    setQRCodeUrl()
  })
  waitForBackend()
})

onUnmounted(() => {
  window.removeEventListener('resize', checkMobile)
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
/* ========== 全局布局 ========== */
.app-container {
  display: flex;
  height: 100vh;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-family: 'Inter', 'Segoe UI', sans-serif;
  overflow: hidden;
  position: relative;
}

/* ========== 拖拽手柄 ========== */
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

/* 侧边栏拖拽手柄（绝对定位，跟随侧边栏右边缘） */
.resize-handle-sidebar {
  position: absolute;
  top: 0;
  bottom: 0;
  height: 100%;
}

.sidebar-enter-active,
.sidebar-leave-active {
  transition: transform 0.3s ease;
}

.sidebar-enter-from,
.sidebar-leave-to {
  transform: translateX(-100%);
}

.sidebar-enter-to,
.sidebar-leave-from {
  transform: translateX(0);
}

/* ========== 侧边栏 ========== */
.sidebar-panel {
  position: absolute;
  top: 0;
  left: 0;
  height: 100%;
  width: 260px;
  background: var(--glass-bg);
  backdrop-filter: blur(12px);
  display: flex;
  flex-direction: column;
  padding: 16px 0;
  gap: 12px;
  overflow: hidden;
  z-index: 40;
  box-shadow: 2px 0 8px rgba(0,0,0,0.1);
}

.icon-btn {
  width: 30px;
  height: 30px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  transition: background 0.2s;
  color: var(--text-primary);
}
.icon-btn:hover, .icon-btn.active {
  background: rgba(74, 124, 247, 0.2);
  box-shadow: var(--shadow-glow);
}

.sidebar-header {
  display: flex;
  padding:0 16px;
  justify-content: space-between;
  align-items: center;
}
.logo-text {
  font-size: 1.2rem;
  font-weight: 700;
  background: var(--accent-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.collapse-btn {
  color: var(--text-secondary);
}
.btn-main {
  padding:0 16px;
}
.new-chat-btn {
  background: var(--accent-gradient) !important;
  border: none !important;
  color: white !important;
  font-weight: 600;
  text-align: center;
  border-radius: 20px;
  box-shadow: var(--shadow-glow);
  transition: transform 0.3s cubic-bezier(0.68, -0.55, 0.265, 1.55),
              box-shadow 0.3s ease,
              background 0.3s ease;
}
.new-chat-btn:hover {
  transform: scale(1.06);
  box-shadow: var(--shadow-glow);
  background: var(--accent-gradient) !important;
}
.new-chat-btn:active {
  transform: scale(1.02);
}
.kb-btn {
  margin-top: 10px;
}
.sidebar-footer {
  position: fixed;
  bottom:40px; left:20px; right:0;
  z-index:100;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
}

/* ========== 工作区 ========== */
.workspace {
  flex: 1;
  display: flex;
  min-width: 0;
  overflow: hidden;
  transition: padding-left 0.3s ease;
}

/* ========== 主区域 ========== */
.main-stage {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  background: var(--bg-secondary);
  box-sizing: border-box;
}

.drag-overlay {
  position: absolute;
  inset: 0;
  background: rgba(99, 102, 241, 0.1);
  backdrop-filter: blur(4px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  border: 2px dashed var(--accent);
  border-radius: 12px;
  margin: 8px;
}
.drag-hint {
  font-size: 1.5rem;
  color: var(--accent);
  background: var(--bg-secondary);
  padding: 1rem 2rem;
  border-radius: 12px;
}

.top-bar {
  display: flex;
  height: 30px;
  justify-content: space-between;
  align-items: center;
  padding: 8px 20px;
  background: var(--glass-bg);
  backdrop-filter: blur(8px);
  border-bottom: var(--glass-border);
}
.model-badge {
  display: flex;
  gap: 8px;
  font-size: 0.85rem;
  color: var(--text-secondary);
}
.toolbar-right {
  display: flex;
  align-items: center;
}
.theme-toggle-btn {
  font-size: 1.2rem;
  color: var(--text-secondary);
}
.theme-toggle-btn:hover {
  color: var(--accent);
}

.chat-item-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
}
:deep(.n-list-item__main) {
    width: 100%;
}
.chat-title {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.chat-actions {
  position: absolute;
  right: 0;
  height: 100%;
  padding: 0 8px;
  border-radius: 0 12px 12px 0;
  vertical-align: middle;
  display: flex;
  gap: 6px;
  opacity: 0;
  transition: opacity 0.2s;
}
.n-list-item:hover .chat-actions {
  opacity: 1;
}
.dark .chat-actions {
  background: linear-gradient(90deg,rgba(44,44,46,0)0%,rgba(44,44,46,.5)20.23%,rgb(44,44,46)40.62%);
}
.light .chat-actions {
  background: linear-gradient(90deg,rgba(226, 226, 226, 0)0%,rgba(226, 226, 226,.5)20.23%,rgb(226, 226, 226)40.62%);
}
.dark .active .chat-actions {
  background: linear-gradient(90deg,rgba(22, 31, 49, 0)0%,rgba(22, 31, 49,.5)20.23%,rgb(22, 31, 49)40.62%);
}
.light .active .chat-actions {
  background: linear-gradient(90deg,rgba(219, 228, 245, 0)0%,rgba(219, 228, 245,.5)20.23%,rgb(219, 228, 245)40.62%);
}

.n-list {
  background-color: unset;
}
.n-list-item {
  margin-bottom:2px;
}

/* 列表激活样式 */
.n-list-item.active {
  background: rgba(74, 124, 247, 0.1) !important;
}

.edit-modal-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding-right: 8px;
}
.edit-modal-title .fullscreen-btn {
  margin-left: 12px;
  color: var(--text-secondary);
  transition: color 0.2s, background 0.2s;
}
.edit-modal-title .fullscreen-btn:hover {
  color: var(--accent);
  background: rgba(74, 124, 247, 0.12);
}

/* 全屏状态下，针对 naive-ui 内部结构强制覆盖 */
:deep(.edit-modal-fullscreen) {
  width: 100vw !important;
  max-width: 100vw !important;
  height: 100vh !important;
  max-height: 100vh !important;
  margin: 0 !important;
  top: 0 !important;
  border-radius: 0 !important;
}
:deep(.edit-modal-fullscreen .n-modal-dialog) {
  width: 100vw !important;
  max-width: 100vw !important;
  height: 100vh !important;
  max-height: 100vh !important;
  margin: 0 !important;
  border-radius: 0 !important;
}
:deep(.edit-modal-fullscreen .n-modal__content) {
  height: calc(100vh - 130px);
}
:deep(.edit-modal-fullscreen .n-input) {
  height: 100%;
}
:deep(.edit-modal-fullscreen .n-input .n-input__textarea-el) {
  min-height: 100% !important;
  height: 100% !important;
}
</style>