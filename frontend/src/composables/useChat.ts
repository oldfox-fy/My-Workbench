import { ref } from 'vue'
import { useChatStore, type Message } from '@/stores/chat'
import { useConfigStore } from '@/stores/config'
import { useProfileStore, VIRTUAL_PROFILE_ID } from '@/stores/profiles'
import { useSkillStore } from '@/stores/skills'
import { useMessage } from 'naive-ui'
import { cleanMessages } from '@/utils/message'
import type { UploadedFile } from '@/composables/useFileUpload'


/** 检测消息中是否包含图片（前端侧判断，用于后端的 auto_switch 占位） */
export function hasImageInMessages(msgs: Message[]): boolean {
  return msgs.some(m => {
    const refs = m.file_ref
    if (!refs) return false
    const arr = Array.isArray(refs) ? refs : [refs]
    return arr.some((f: any) => f.type?.startsWith('image/'))
  })
}


export function useChat() {
  const chatStore = useChatStore()
  const configStore = useConfigStore()
  const profileStore = useProfileStore()
  const skillStore = useSkillStore()
  const message = useMessage()

  /** 计算实际发送的 enable_tools 和 profile_id，考虑用户身份 */
  function getProfileSendParams() {
    const isAdmin = skillStore.isAdmin()
    let enableTools = chatStore.enableProfile
    let profileId: number | null = chatStore.enableProfile ? profileStore.activeProfileId : null

    // 普通用户必须开启角色模式，且不能使用全能助手
    if (!isAdmin) {
      enableTools = true
      if (profileId === VIRTUAL_PROFILE_ID || profileId == null) {
        // 回退到第一个非虚拟角色
        const firstReal = profileStore.profiles.find(p => p.id !== VIRTUAL_PROFILE_ID)
        profileId = firstReal?.id ?? null
      }
    }

    return { enable_tools: enableTools, profile_id: profileId }
  }

  const currentInput = ref('')
  const isLoading = ref(false)
  const streamingContent = ref('')
  const abortController = ref<AbortController | null>(null)
  const regeneratingMsg = ref<Message | null>(null)

  function stopGeneration() {
    if (abortController.value) {
      abortController.value.abort()
      // ✅ 不要在这里重置 isLoading，交给 fetch 的 finally 去处理
      // 否则用户在旧请求未完全清理完时，可能会再次发起新请求导致竞态
    } else {
      isLoading.value = false
      regeneratingMsg.value = null
    }
  }

  const onStreamEnd = ref<((fullText: string) => void) | null>(null)
  // 工具审批回调：检测到审批标记时调用
  let onApprovalNeeded: ((callId: string, toolName: string, argsPreview: string) => Promise<boolean>) | null = null
  function setApprovalHandler(handler: (callId: string, toolName: string, argsPreview: string) => Promise<boolean>) {
    onApprovalNeeded = handler
  }

  async function readStream(response: Response): Promise<string> {
    if (!response.ok || !response.body) throw new Error('网络响应失败')

    const reader = response.body.getReader()
    const decoder = new TextDecoder()
    let fullText = ''
    const approvalRegex = /<!--tool_approval:([^:]+):([^:]+):([\s\S]*?)-->/g

    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      fullText += decoder.decode(value, { stream: true })

      // 检测工具审批标记，弹出审批对话框
      if (onApprovalNeeded) {
        let m
        while ((m = approvalRegex.exec(fullText)) !== null) {
          const callId = m[1]; const toolName = m[2]; const argsPreview = m[3]
          try {
            const approved = await onApprovalNeeded(callId, toolName, argsPreview)
            if (!approved) {
              fullText = fullText.replace(m[0], `\n⛔ 工具 \`${toolName}\` 已被用户拒绝\n`)
            } else {
              fullText = fullText.replace(m[0], '')
            }
          } catch {
            fullText = fullText.replace(m[0], '')
          }
        }
      }

      streamingContent.value = fullText
    }

    return fullText
  }

  /** 构建 llm_config，支持智能切换 */
  function buildLlmConfig() {
    const currentModel = configStore.activeModel
    if (!currentModel) return null

    return {
      type: currentModel.type,
      model_name: currentModel.modelName,
      base_url: currentModel.baseUrl,
      api_key: currentModel.apiKey,
      thinking: localStorage.getItem('thinking') === 'true' ? 'enabled' : 'disabled',
      role: currentModel.role || 'default',  // 传递模型角色，确保后端正确路由 API（如 image_gen → images.generate）
    }
  }

  /**
   * 发送新消息
   */
  async function sendMessage(
    uploadedFiles: UploadedFile[],
    scrollToBottom: () => void
  ) {
    if (!currentInput.value.trim() || isLoading.value || !chatStore.activeChatId) return

    const currentModel = configStore.activeModel
    if (!currentModel) {
      message.error('请先选择一个模型')
      return
    }

    const chatId = chatStore.activeChatId

    const displayContent = currentInput.value.trim()
    const userMsg: Message = {
      id: Date.now(),
      role: 'user',
      content: displayContent,
      file_ref:
        uploadedFiles.length > 0
          ? uploadedFiles.map((f) => ({
              filename: f.filename,
              type: f.type,
              url: f.url,
            }))
          : null,
    }

    chatStore.addMessageToLocal(userMsg)
    await chatStore.saveMessageToBackend(userMsg)

    const assistantMsg: Message = {
      id: Date.now() + 1,
      role: 'assistant',
      content: '',
    }
    chatStore.addMessageToLocal(assistantMsg)
    await chatStore.saveMessageToBackend(assistantMsg)
    const assistantMessageId = assistantMsg.id

    isLoading.value = true
    streamingContent.value = ''
    currentInput.value = ''

    if (abortController.value) {
      abortController.value.abort()
    }
    const controller = new AbortController()
    abortController.value = controller
    let fullText = ''

    try {
      const allMessages = chatStore.getActiveMessages()
      const apiMessages = await cleanMessages(allMessages)

      const sendParams = getProfileSendParams()
      const requestBody: any = {
        messages: apiMessages,
        enable_tools: sendParams.enable_tools,
        llm_config: buildLlmConfig(),
        profile_id: sendParams.profile_id,
        message_id: assistantMessageId,
        auto_switch: configStore.autoSwitch,  // 智能切换开关
      }

      const body = JSON.stringify(requestBody)

      setTimeout(() => scrollToBottom(), 160)

      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: controller.signal,
      })

      fullText = await readStream(response)

      if (chatStore.activeChatId === chatId) {
        const localMsg = chatStore.currentChatMessages.find((m: any) => m.id === assistantMsg.id)
        if (localMsg) {
          localMsg.content = fullText
        }
        chatStore.editMessage(<number>assistantMsg.id, fullText).catch((e: any) =>
          console.warn('更新助手消息失败', e)
        )
      }

    } catch (error: any) {
      if (error.name === 'AbortError') {
        // ✅ 不管有没有内容，都更新原消息，不新建
        const partialContent = (streamingContent.value.trim() ? streamingContent.value.trim() + '\n\n' : '') + '[已停止]'
        const localMsg = chatStore.currentChatMessages.find((m: any) => m.id === assistantMsg.id)
        if (localMsg) {
          localMsg.content = partialContent
        }
        chatStore.editMessage(<number>assistantMsg.id, partialContent).catch((e) =>
          console.warn('保存截断消息失败', e)
        )
        streamingContent.value = ''
        return
      }
      if (chatStore.activeChatId === chatId) {
        console.error('发送失败:', error)
        const errorContent = `**错误：** ${error.message}`
        const localMsg = chatStore.currentChatMessages.find((m: any) => m.id === assistantMsg.id)
        if (localMsg) {
          localMsg.content = errorContent
        }
        // ✅ 报错也是更新原消息
        chatStore.editMessage(<number>assistantMsg.id, errorContent).catch((e: any) =>
          console.warn('保存错误消息失败', e)
        )
      }
    } finally {
      abortController.value = null
      isLoading.value = false
      streamingContent.value = ''
      if (onStreamEnd.value && fullText && chatStore.activeChatId === chatId) {
        onStreamEnd.value(fullText)
      }
    }
  }

  /**
   * 重新生成当前对话（通常用于编辑用户消息后）
   */
  async function regenerateFromCurrentHistory() {
    if (!chatStore.activeChatId || isLoading.value) return
    const currentModel = configStore.activeModel
    if (!currentModel) {
      message.error('请先选择一个模型')
      return
    }

    isLoading.value = true

    if (abortController.value) {
      abortController.value.abort()
    }
    const controller = new AbortController()
    abortController.value = controller

    const assistantMsg: Message = {
      id: Date.now() + 1,
      role: 'assistant',
      content: '',
    }
    chatStore.addMessageToLocal(assistantMsg)
    await chatStore.saveMessageToBackend(assistantMsg)
    const messageId = assistantMsg.id

    try {
      const sendParams = getProfileSendParams()
      const requestBody: any = {
        messages: await cleanMessages(chatStore.getActiveMessages()),
        enable_tools: sendParams.enable_tools,
        llm_config: buildLlmConfig(),
        profile_id: sendParams.profile_id,
        message_id: messageId,
        auto_switch: configStore.autoSwitch,
      }

      const body = JSON.stringify(requestBody)

      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: controller.signal,
      })

      const fullText = await readStream(response)

      const localMsg = chatStore.currentChatMessages.find(m => m.id === assistantMsg.id)
      if (localMsg) localMsg.content = fullText
      chatStore.editMessage(<number>assistantMsg.id, fullText).catch(e => console.warn(e))

    } catch (error: any) {
      if (error.name === 'AbortError') {
        // ✅ 中止时：必须更新原消息，不管有没有内容
        const partialContent = (streamingContent.value.trim() ? streamingContent.value.trim() + '\n\n' : '') + '[已停止]'
        const localMsg = chatStore.currentChatMessages.find(m => m.id === assistantMsg.id)
        if (localMsg) localMsg.content = partialContent
        chatStore.editMessage(<number>assistantMsg.id, partialContent).catch((e) =>
          console.warn('保存截断消息失败', e)
        )
        streamingContent.value = ''
        return
      }
      // ✅ 报错时：也必须更新原消息
      const errContent = `**错误：** ${error.message}`
      const localMsg = chatStore.currentChatMessages.find(m => m.id === assistantMsg.id)
      if (localMsg) localMsg.content = errContent
      chatStore.editMessage(<number>assistantMsg.id, errContent).catch((e) =>
        console.warn('保存错误消息失败', e)
      )
    } finally {
      abortController.value = null
      isLoading.value = false
      streamingContent.value = ''
    }
  }

  /**
   * 针对某条助手消息重新生成（使用该消息前的历史）
   */
  async function regenerateResponse(assistantMsg: Message) {
    if (!chatStore.activeChatId || isLoading.value) return
    const currentModel = configStore.activeModel
    if (!currentModel) {
      message.error('请先选择一个模型')
      return
    }

    const allMessages = chatStore.getActiveMessages()
    const idx = allMessages.indexOf(assistantMsg)
    if (idx === -1) return

    const history = allMessages.slice(0, idx)
    regeneratingMsg.value = assistantMsg
    const messageId = assistantMsg.id

    isLoading.value = true

    if (abortController.value) {
      abortController.value.abort()
    }
    const controller = new AbortController()
    abortController.value = controller

    // 清空原消息显示，配合 streamingContent 显示流式
    assistantMsg.content = ''
    streamingContent.value = ''

    try {
      const sendParams3 = getProfileSendParams()
      const requestBody: any = {
        messages: await cleanMessages(history),
        enable_tools: sendParams3.enable_tools,
        llm_config: buildLlmConfig(),
        profile_id: sendParams3.profile_id,
        message_id: messageId,
        auto_switch: configStore.autoSwitch,
      }

      const body = JSON.stringify(requestBody)

      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body,
        signal: controller.signal,
      })

      const fullText = await readStream(response)

      assistantMsg.content = fullText
      if (assistantMsg.id) {
        fetch(`/api/chats/${chatStore.activeChatId}/messages/${assistantMsg.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: 'assistant', content: fullText }),
        }).catch((e) => console.warn('更新消息失败', e))
      }

      if (onStreamEnd.value) {
        onStreamEnd.value(fullText)
      }
    } catch (error: any) {
      if (error.name === 'AbortError') {
        // ✅ 中止时：直接更新原消息，不新建
        const partialContent = (streamingContent.value.trim() ? streamingContent.value.trim() + '\n\n' : '') + '[已停止]'
        assistantMsg.content = partialContent
        if (assistantMsg.id) {
          fetch(`/api/chats/${chatStore.activeChatId}/messages/${assistantMsg.id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role: 'assistant', content: partialContent }),
          }).catch((e) => console.warn('更新截断消息失败', e))
        }
        streamingContent.value = ''
        return
      }
      // ✅ 报错时：直接更新原消息，不新建
      const errContent = `**错误：** ${error.message}`
      assistantMsg.content = errContent
      if (assistantMsg.id) {
        fetch(`/api/chats/${chatStore.activeChatId}/messages/${assistantMsg.id}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ role: 'assistant', content: errContent }),
        }).catch((e) => console.warn('更新错误消息失败', e))
      }
    } finally {
      abortController.value = null
      regeneratingMsg.value = null
      isLoading.value = false
      streamingContent.value = ''
    }
  }

  // ── WebSocket 聊天（双向通信，即时取消） ──
  let ws: WebSocket | null = null
  let wsCancelFlag = false

  function ensureWs(): WebSocket {
    if (!ws || ws.readyState >= WebSocket.CLOSING) {
      const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
      ws = new WebSocket(`${protocol}//${location.host}/ws/chat`)
    }
    return ws
  }

  async function sendMessageWs(
    uploadedFiles: UploadedFile[],
    scrollToBottom: () => void
  ) {
    if (!currentInput.value.trim() || isLoading.value || !chatStore.activeChatId) return

    const currentModel = configStore.activeModel
    if (!currentModel) return

    isLoading.value = true
    wsCancelFlag = false
    ;(window as any).__stopGeneration = () => { wsCancelFlag = true; ensureWs().send(JSON.stringify({ type: 'cancel' })) }

    // 添加用户消息
    chatStore.addMessageToActive({
      role: 'user',
      content: currentInput.value,
      file_ref: uploadedFiles.length ? uploadedFiles.map(f => ({ filename: f.filename, type: f.type })) : undefined,
    })
    currentInput.value = ''
    await chatStore.loadMessages(chatStore.activeChatId)
    scrollToBottom()

    // 准备消息列表
    const messages = chatStore.currentChatMessages.map(m => ({ role: m.role, content: m.content }))
    streamingContent.value = ''

    const socket = ensureWs()
    const wsParams = getProfileSendParams()
    const sendData = {
      type: 'chat',
      messages,
      enable_tools: wsParams.enable_tools,
      profile_id: wsParams.profile_id || undefined,
      llm_config: currentModel ? {
        type: currentModel.type,
        model_name: currentModel.modelName,
        base_url: currentModel.baseUrl,
        api_key: currentModel.apiKey,
      } : undefined,
      auto_switch: configStore.autoSwitch,  // 智能切换开关
    }

    // 设置消息处理
    socket.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data)
        switch (data.type) {
          case 'chunk':
            streamingContent.value += data.content
            break
          case 'tool_preview':
          case 'tool_status':
          case 'tool_block':
            // 工具调用在流式内容中显示
            break
          case 'tool_approval':
            // 工具审批由 ChatWindow 的 approvalDialog 处理
            if (onApprovalNeeded) {
              // 发送到 WS 审批通道
              onApprovalNeeded(data.call_id, data.tool_name, data.args_preview).then(approved => {
                socket.send(JSON.stringify({ type: 'approval_reply', call_id: data.call_id, approved }))
              })
            }
            break
          case 'done':
            isLoading.value = false
            if (data.usage) {
              streamingContent.value += `\n<!--token_usage:${JSON.stringify(data.usage)}-->`
            }
            // 保存 AI 回复
            const finalText = streamingContent.value.replace(/<!--[\s\S]*?-->/g, '').trim()
            if (finalText) {
              chatStore.addMessageToActive({ role: 'assistant', content: finalText })
            }
            chatStore.loadMessages(chatStore.activeChatId)
            break
          case 'error':
            streamingContent.value += `\n❌ ${data.message}\n`
            isLoading.value = false
            break
        }
      } catch { /* ignore parse errors */ }
    }

    socket.onerror = () => {
      if (!wsCancelFlag) {
        streamingContent.value += '\n❌ WebSocket 连接错误\n'
      }
      isLoading.value = false
    }

    socket.send(JSON.stringify(sendData))
  }

  function stopGenerationWs() {
    wsCancelFlag = true
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: 'cancel' }))
    }
    isLoading.value = false
  }

  return {
    currentInput,
    isLoading,
    streamingContent,
    regeneratingMsg,
    onStreamEnd,
    setApprovalHandler,
    sendMessage,
    sendMessageWs,
    regenerateResponse,
    regenerateFromCurrentHistory,
    stopGeneration,
    stopGenerationWs,
    ensureWs,
    buildLlmConfig,
  }
}