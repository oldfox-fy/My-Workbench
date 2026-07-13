// frontend/src/composables/useVoice.ts
// 语音输入（STT）+ 语音输出（TTS）—— 支持两种模式：
//
// 模式 1（优先）：LLM 语音模型 — 通过后端 OpenAI-compatible API 调用
//   条件：配置了 role=audio 的模型
//   后端路由：POST /api/stt（语音转文字）、POST /api/tts（文字转语音）
//
// 模式 2（fallback）：浏览器原生 API — SpeechRecognition / speechSynthesis
//   条件：无 audio 角色模型，或 LLM API 调用失败
//   零后端依赖，纯浏览器端完成
//
// 优先级：配置了 audio 模型 > 浏览器原生

import { ref, onUnmounted } from 'vue'
import { useConfigStore } from '@/stores/config'

// ──────────── 检测是否有 LLM 语音模型可用 ────────────

export function hasAudioModel(): boolean {
  const configStore = useConfigStore()
  return configStore.savedModels.some(m => m.role === 'audio' && m.modelName)
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

// ──────────── SpeechRecognition 类型声明 ────────────

declare class SpeechRecognition {
  lang: string
  interimResults: boolean
  continuous: boolean
  maxAlternatives: number
  onresult: ((event: SpeechRecognitionEvent) => void) | null
  onerror: ((event: SpeechRecognitionErrorEvent) => void) | null
  onend: (() => void) | null
  start(): void
  stop(): void
  abort(): void
}
declare interface SpeechRecognitionEvent {
  results: SpeechRecognitionResultList
  resultIndex: number
}
declare interface SpeechRecognitionResultList {
  length: number
  [index: number]: SpeechRecognitionResult
}
declare interface SpeechRecognitionResult {
  isFinal: boolean
  length: number
  [index: number]: SpeechRecognitionAlternative
}
declare interface SpeechRecognitionAlternative {
  transcript: string
  confidence: number
}
declare interface SpeechRecognitionErrorEvent {
  error: string
  message: string
}

// ──────────────────── STT 语音转文字 ────────────────────

export function useVoiceRecorder() {
  const isRecording = ref(false)
  const interimText = ref('')
  const errorMsg = ref('')
  let recognition: SpeechRecognition | null = null
  let mediaRecorder: MediaRecorder | null = null
  let audioChunks: Blob[] = []

  // ── 浏览器原生 STT ──
  function _getRecognition(): SpeechRecognition {
    const Ctor =
      (window as any).SpeechRecognition ||
      (window as any).webkitSpeechRecognition
    if (!Ctor) {
      throw new Error('当前浏览器不支持语音识别。请使用 Chrome 或 Edge。')
    }
    const rec = new Ctor() as SpeechRecognition
    rec.lang = 'zh-CN'
    rec.interimResults = true
    rec.continuous = true
    rec.maxAlternatives = 1
    return rec
  }

  // ── LLM API STT（通过后端） ──
  async function _llmStt(audioBlob: Blob): Promise<string> {
    const audioConfig = getAudioModelConfig()
    if (!audioConfig) {
      throw new Error('未配置语音模型')
    }

    const formData = new FormData()
    formData.append('file', audioBlob, 'recording.webm')
    formData.append('model', audioConfig.model_name)
    if (audioConfig.base_url) formData.append('base_url', audioConfig.base_url)
    if (audioConfig.api_key) formData.append('api_key', audioConfig.api_key)

    const res = await fetch('/api/stt', {
      method: 'POST',
      body: formData,
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || '语音识别失败')
    }
    const data = await res.json()
    return data.text || ''
  }

  const startRecording = async (): Promise<void> => {
    errorMsg.value = ''
    interimText.value = ''

    // ── 优先尝试 LLM 语音模型（MediaRecorder → 后端 STT） ──
    if (hasAudioModel()) {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
        audioChunks = []
        mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' })

        mediaRecorder.ondataavailable = (e) => {
          if (e.data.size > 0) audioChunks.push(e.data)
        }

        mediaRecorder.start()
        isRecording.value = true
        return
      } catch (e: any) {
        console.warn('[Voice] LLM STT 启动失败，fallback 浏览器原生:', e.message)
        // fallthrough to browser native
      }
    }

    // ── Fallback: 浏览器原生 SpeechRecognition ──
    try {
      recognition = _getRecognition()
    } catch (e: any) {
      errorMsg.value = e.message
      return
    }

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      let interim = ''
      let final = ''
      for (let i = event.resultIndex; i < event.results.length; i++) {
        const result = event.results[i]
        if (result.isFinal) {
          final += result[0]?.transcript || ''
        } else {
          interim += result[0]?.transcript || ''
        }
      }
      interimText.value = final + interim
    }

    recognition.onerror = (event: SpeechRecognitionErrorEvent) => {
      if (event.error === 'no-speech') return
      if (event.error === 'aborted') return
      errorMsg.value = `语音识别出错：${event.message || event.error}`
      isRecording.value = false
    }

    recognition.onend = () => {
      if (isRecording.value && recognition) {
        try { recognition.start() } catch { /* ignore */ }
      } else {
        isRecording.value = false
      }
    }

    try {
      recognition.start()
      isRecording.value = true
    } catch {
      errorMsg.value = '启动语音识别失败'
    }
  }

  const stopRecording = async (): Promise<string> => {
    // ── LLM 模式：停止录音 → 发送到后端 ──
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      return new Promise((resolve) => {
        mediaRecorder!.onstop = async () => {
          isRecording.value = false
          // 停止所有音轨
          mediaRecorder!.stream.getTracks().forEach(t => t.stop())
          mediaRecorder = null

          const audioBlob = new Blob(audioChunks, { type: 'audio/webm' })
          audioChunks = []

          if (audioBlob.size === 0) {
            resolve('')
            return
          }

          try {
            const text = await _llmStt(audioBlob)
            interimText.value = ''
            resolve(text)
          } catch (e: any) {
            errorMsg.value = `LLM 语音识别失败: ${e.message}`
            console.warn('[Voice] LLM STT 失败:', e.message)
            resolve('')
          }
        }
        mediaRecorder!.stop()
      })
    }

    // ── 浏览器原生模式 ──
    if (!recognition) {
      isRecording.value = false
      return interimText.value || ''
    }

    return new Promise((resolve) => {
      recognition!.onend = () => {
        isRecording.value = false
        const text = interimText.value.trim()
        interimText.value = ''
        resolve(text)
      }
      recognition!.stop()
    })
  }

  const cancelRecording = () => {
    // LLM 模式
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.onstop = null
      mediaRecorder.stop()
      mediaRecorder.stream.getTracks().forEach(t => t.stop())
      mediaRecorder = null
      audioChunks = []
    }
    // 浏览器原生模式
    if (recognition) {
      recognition.onend = null
      recognition.abort()
      recognition = null
    }
    isRecording.value = false
    interimText.value = ''
  }

  onUnmounted(() => {
    cancelRecording()
  })

  return {
    isRecording,
    interimText,
    errorMsg,
    startRecording,
    stopRecording,
    cancelRecording,
    hasAudioModel: () => hasAudioModel(),
  }
}

// ──────────────────── TTS 文字转语音 ────────────────────

export function useTTS() {
  const isSpeaking = ref(false)
  const autoRead = ref(false)
  let currentUtterance: SpeechSynthesisUtterance | null = null
  let currentAudio: HTMLAudioElement | null = null

  /** 获取最佳中文语音 */
  function _pickVoice(): SpeechSynthesisVoice | null {
    const voices = speechSynthesis.getVoices()
    const mandarin = voices.find(v => v.lang.startsWith('zh-CN'))
    if (mandarin) return mandarin
    const anyChinese = voices.find(v => v.lang.startsWith('zh'))
    if (anyChinese) return anyChinese
    return voices[0] || null
  }

  // ── LLM API TTS（通过后端） ──
  async function _llmTts(text: string): Promise<void> {
    const audioConfig = getAudioModelConfig()
    if (!audioConfig) {
      throw new Error('未配置语音模型')
    }

    const res = await fetch('/api/tts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        input: text,
        model: audioConfig.model_name,
        voice: 'nova',
        base_url: audioConfig.base_url || '',
        api_key: audioConfig.api_key || '',
      }),
    })

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }))
      throw new Error(err.detail || '语音合成失败')
    }

    // TTS 返回音频 blob
    const blob = await res.blob()
    const url = URL.createObjectURL(blob)

    return new Promise((resolve, reject) => {
      currentAudio = new Audio(url)
      currentAudio.onended = () => {
        isSpeaking.value = false
        currentAudio = null
        URL.revokeObjectURL(url)
        resolve()
      }
      currentAudio.onerror = () => {
        isSpeaking.value = false
        currentAudio = null
        URL.revokeObjectURL(url)
        reject(new Error('音频播放失败'))
      }
      isSpeaking.value = true
      currentAudio.play().catch(reject)
    })
  }

  const speak = async (text: string): Promise<void> => {
    stop()

    if (!text.trim()) return

    // 清除 HTML 标记只留纯文本
    const plain = text
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/<[^>]*>/g, '')
      .replace(/[*_~`#>#]/g, '')
      .replace(/\n+/g, '。')
      .trim()

    if (!plain) return

    // ── 优先尝试 LLM TTS ──
    if (hasAudioModel()) {
      try {
        await _llmTts(plain.slice(0, 2000))
        return
      } catch (e: any) {
        console.warn('[Voice] LLM TTS 失败，fallback 浏览器原生:', e.message)
        // fallthrough to browser native
      }
    }

    // ── Fallback: 浏览器原生 speechSynthesis ──
    if (!('speechSynthesis' in window)) {
      console.warn('[TTS] 浏览器不支持 speechSynthesis')
      return
    }

    currentUtterance = new SpeechSynthesisUtterance(plain.slice(0, 2000))
    currentUtterance.rate = 1.0
    currentUtterance.pitch = 1.0

    const voice = _pickVoice()
    if (voice) currentUtterance.voice = voice

    currentUtterance.onstart = () => { isSpeaking.value = true }
    currentUtterance.onend = () => {
      isSpeaking.value = false
      currentUtterance = null
    }
    currentUtterance.onerror = (e) => {
      if (e.error !== 'interrupted') {
        console.warn('[TTS] 朗读出错:', e.error)
      }
      isSpeaking.value = false
      currentUtterance = null
    }

    speechSynthesis.speak(currentUtterance)
  }

  const stop = () => {
    // LLM TTS
    if (currentAudio) {
      currentAudio.pause()
      currentAudio.onended = null
      currentAudio.onerror = null
      currentAudio = null
    }
    // 浏览器原生 TTS
    speechSynthesis.cancel()
    isSpeaking.value = false
    currentUtterance = null
  }

  onUnmounted(() => {
    stop()
  })

  return { isSpeaking, autoRead, speak, stop, hasAudioModel: () => hasAudioModel() }
}
