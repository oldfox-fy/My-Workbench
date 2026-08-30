// frontend/src/composables/useAssistant.ts
// 「小爱同学」式语音助手前端组合式函数（P0：按键触发）。
//
// 职责：录音（MediaRecorder）→ 上传 /api/assistant/ask（后端 ASR + 意图路由 + Agent）
//      → 返回 { transcript, answer }。播放与消息展示由调用方（ChatWindow）处理。
//
// 说明：P0 复用浏览器 MediaRecorder 采集 + 后端语音链路，避免后端直接开麦克风；
//      P1 起后端接入 openWakeWord，改为免按键唤醒，此组合式函数仅保留手动触发入口。
import { ref, onUnmounted } from 'vue'
import { apiFetch } from '@/api/client'
import { useConfigStore } from '@/stores/config'
import { useAuthStore } from '@/stores/auth'

export interface AssistantResult {
  transcript: string
  answer: string
  state: string
  spoken_locally?: boolean
}

export type AssistantStateName = 'idle' | 'listening' | 'thinking' | 'acting' | 'speaking' | 'error' | 'paused'

// ── 免按键唤醒：模块级单例状态（ChatInput 徽标 + ChatWindow 回答处理共享同一份）──
// 说明：/ws/assistant 是服务端广播通道，主窗口只需订阅一次；用模块级单例避免
//      ChatInput / ChatWindow 各自建连导致重复播报。
const assistantState = ref<AssistantStateName>('idle')
const wakeAvailable = ref(false)
const lastHandsfree = ref<{ transcript: string; answer: string; spoken_locally?: boolean } | null>(null)
const lastReminder = ref<{ id: number; content: string } | null>(null)
const lastScheduledTask = ref<{ task_id: number; title: string; status: string; output_preview: string; chat_id?: string } | null>(null)

let ws: WebSocket | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let answerHandler: ((r: { transcript: string; answer: string; spoken_locally?: boolean }) => void) | null = null
let reminderHandler: ((r: { id: number; content: string }) => void) | null = null
let scheduledTaskHandler: ((r: { task_id: number; title: string; status: string; output_preview: string; chat_id?: string }) => void) | null = null
let started = false

function wsUrl(): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const token = useAuthStore().token || ''
  return `${protocol}//${location.host}/ws/assistant?token=${encodeURIComponent(token)}`
}

function onWsMessage(ev: MessageEvent) {
  let msg: any
  try { msg = JSON.parse(ev.data) } catch { return }
  if (!msg || typeof msg !== 'object') return
  if (msg.type === 'state' && msg.state) {
    assistantState.value = msg.state as AssistantStateName
    if (typeof msg.wake_available === 'boolean') {
      wakeAvailable.value = msg.wake_available
    }
  } else if (msg.type === 'assistant_answer') {
    const r = { transcript: String(msg.transcript || ''), answer: String(msg.answer || ''), spoken_locally: !!msg.spoken_locally }
    lastHandsfree.value = r
    answerHandler?.(r)
  } else if (msg.type === 'reminder') {
    const r = { id: Number(msg.id || 0), content: String(msg.content || '') }
    lastReminder.value = r
    reminderHandler?.(r)
  } else if (msg.type === 'scheduled_task') {
    const r = {
      task_id: Number(msg.task_id || 0),
      title: String(msg.title || '定时任务'),
      status: String(msg.status || 'success'),
      output_preview: String(msg.output_preview || ''),
      chat_id: msg.chat_id ? String(msg.chat_id) : undefined,
    }
    lastScheduledTask.value = r
    scheduledTaskHandler?.(r)
  }
}

function onWsClose() {
  assistantState.value = 'idle'
  ws = null
  if (started && !reconnectTimer) {
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null
      if (started) connect()
    }, 3000)
  }
}

function connect() {
  if (ws && ws.readyState < WebSocket.CLOSING) return
  try {
    ws = new WebSocket(wsUrl())
    ws.onmessage = onWsMessage
    ws.onclose = onWsClose
    ws.onerror = () => { try { ws?.close() } catch { /* ignore */ } }
  } catch {
    ws = null
  }
}

/** 开始免按键唤醒订阅（主窗口挂载时调用）。onAnswer 处理唤醒后的识别+回答；onReminder 处理定时提醒。 */
async function startHandsfree(
  onAnswer?: (r: { transcript: string; answer: string; spoken_locally?: boolean }) => void,
  onReminder?: (r: { id: number; content: string }) => void,
  onScheduledTask?: (r: { task_id: number; title: string; status: string; output_preview: string; chat_id?: string }) => void,
) {
  if (onAnswer) answerHandler = onAnswer
  if (onReminder) reminderHandler = onReminder
  if (onScheduledTask) scheduledTaskHandler = onScheduledTask
  started = true
  connect()
  try {
    const res = await apiFetch('/api/assistant/state')
    if (res.ok) {
      const data = await res.json()
      wakeAvailable.value = !!data.wake_available
      if (data.state) assistantState.value = data.state as AssistantStateName
    }
  } catch { /* ignore */ }
}

/** 停止订阅（主窗口卸载时调用）。 */
function stopHandsfree() {
  started = false
  answerHandler = null
  reminderHandler = null
  scheduledTaskHandler = null
  if (reconnectTimer) { clearTimeout(reconnectTimer); reconnectTimer = null }
  if (ws) {
    try { ws.onclose = null; ws.close() } catch { /* ignore */ }
    ws = null
  }
  assistantState.value = 'idle'
}

function getAudioModelConfig() {
  const configStore = useConfigStore()
  const model = configStore.savedModels.find(m => m.role === 'audio' && m.modelName)
  if (!model) return null
  return {
    base_url: model.baseUrl,
    api_key: model.apiKey,
    model_name: model.modelName,
  }
}

// ── 语音助手设置（P4：语音设置面板）──

export interface VoiceConfig {
  enabled: boolean
  wake_mode: string
  wake_word: string
  whisper_model: string
  whisper_mirror: string
  silence_timeout: number
  max_listen_seconds: number
  stt_engine: string
  tts_engine: string
  edge_voice: string
  edge_rate: string
}

export interface VoiceConfigResponse {
  config: VoiceConfig
  enabled: boolean
  state: string
  wake_available: boolean
}

/** 读取当前语音助手配置（GET /api/assistant/config）。 */
async function fetchVoiceConfig(): Promise<VoiceConfigResponse | null> {
  try {
    const res = await apiFetch('/api/assistant/config')
    if (!res.ok) return null
    return (await res.json()) as VoiceConfigResponse
  } catch { return null }
}

/** 写入语音助手配置（PUT /api/assistant/config），后端实时重启助手生效。 */
async function saveVoiceConfig(updates: Partial<VoiceConfig>): Promise<VoiceConfigResponse | null> {
  const res = await apiFetch('/api/assistant/config', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(updates),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new Error(err.detail || '保存语音设置失败')
  }
  const data = (await res.json()) as VoiceConfigResponse
  // 同步模块级单例状态，让主窗口徽标 / 悬浮球状态即时刷新（如停用后显示「已暂停」）
  wakeAvailable.value = !!data.wake_available
  assistantState.value = (data.state as AssistantStateName) || 'idle'
  return data
}

export function useAssistant() {
  const isListening = ref(false)
  const errorMsg = ref('')

  let mediaRecorder: MediaRecorder | null = null
  let audioChunks: Blob[] = []
  let stream: MediaStream | null = null

  async function startListening(): Promise<void> {
    errorMsg.value = ''
    audioChunks = []

    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (e: any) {
      errorMsg.value = '无法访问麦克风，请检查浏览器权限。'
      throw e
    }

    // 优先 webm；部分环境不支持则退回默认格式
    let mimeType = ''
    if (MediaRecorder.isTypeSupported('audio/webm')) mimeType = 'audio/webm'
    mediaRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)

    mediaRecorder.ondataavailable = (e) => {
      if (e.data.size > 0) audioChunks.push(e.data)
    }

    mediaRecorder.start()
    isListening.value = true
  }

  async function stopListening(): Promise<AssistantResult | null> {
    if (!mediaRecorder || mediaRecorder.state === 'inactive') {
      isListening.value = false
      return null
    }

    return new Promise((resolve) => {
      mediaRecorder!.onstop = async () => {
        isListening.value = false
        stream?.getTracks().forEach(t => t.stop())
        stream = null
        mediaRecorder = null

        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' })
        audioChunks = []

        if (audioBlob.size === 0) {
          resolve(null)
          return
        }

        try {
          const result = await ask(audioBlob)
          resolve(result)
        } catch (e: any) {
          errorMsg.value = e.message || '语音助手调用失败'
          resolve(null)
        }
      }
      mediaRecorder!.stop()
    })
  }

  async function cancelListening() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.onstop = null
      mediaRecorder.stop()
    }
    stream?.getTracks().forEach(t => t.stop())
    stream = null
    mediaRecorder = null
    audioChunks = []
    isListening.value = false
  }

  /** 上传音频到 /api/assistant/ask，返回识别文本 + 回答 */
  async function ask(audioBlob: Blob): Promise<AssistantResult> {
    const audioConfig = getAudioModelConfig()
    const formData = new FormData()
    formData.append('file', audioBlob, 'recording.webm')
    if (audioConfig?.model_name) formData.append('model', audioConfig.model_name)
    if (audioConfig?.base_url) formData.append('base_url', audioConfig.base_url)
    if (audioConfig?.api_key) formData.append('api_key', audioConfig.api_key)

    const res = await apiFetch('/api/assistant/ask', {
      method: 'POST',
      body: formData,
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || '语音助手请求失败')
    }
    return (await res.json()) as AssistantResult
  }

  /** 直接以文本提问（浏览器本地识别后调用），返回识别文本 + 回答 */
  async function askText(text: string): Promise<AssistantResult> {
    const formData = new FormData()
    formData.append('text', text)
    const res = await apiFetch('/api/assistant/ask', {
      method: 'POST',
      body: formData,
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || '语音助手请求失败')
    }
    return (await res.json()) as AssistantResult
  }

  onUnmounted(() => {
    cancelListening()
  })

  return {
    isListening,
    errorMsg,
    startListening,
    stopListening,
    cancelListening,
    ask,
    askText,
    // 免按键唤醒（模块级单例，多组件共享）
    assistantState,
    wakeAvailable,
    lastHandsfree,
    lastReminder,
    lastScheduledTask,
    startHandsfree,
    stopHandsfree,
    // 语音设置面板（P4）
    fetchVoiceConfig,
    saveVoiceConfig,
  }
}
