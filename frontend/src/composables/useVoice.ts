// frontend/src/composables/useVoice.ts
// 语音输入（STT）+ 语音输出（TTS）—— 全部使用浏览器原生 API，零后端依赖
//
// STT:  SpeechRecognition / webkitSpeechRecognition（Chrome/Edge/PyWebView 内置）
// TTS:  speechSynthesis（所有浏览器内置）
//
// 不需要任何 API Key 或后端语音服务，纯浏览器端完成。

import { ref, onUnmounted } from 'vue'

// SpeechRecognition 类型声明（TS 标准库可能缺失）
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
  const interimText = ref('')   // 实时识别中间结果
  const errorMsg = ref('')
  let recognition: SpeechRecognition | null = null

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

  const startRecording = async (): Promise<void> => {
    errorMsg.value = ''
    interimText.value = ''

    try {
      recognition = _getRecognition()
    } catch (e: any) {
      errorMsg.value = e.message
      return
    }

    recognition.onresult = (event: SpeechRecognitionEvent) => {
      // 拼接所有 interim 片段用于实时显示
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
      if (event.error === 'no-speech') {
        // 静默不算错误，继续监听
        return
      }
      if (event.error === 'aborted') {
        return
      }
      errorMsg.value = `语音识别出错：${event.message || event.error}`
      isRecording.value = false
    }

    recognition.onend = () => {
      // 如果还在录音状态（非手动停止），自动重启以支持连续识别
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
  }
}

// ──────────────────── TTS 文字转语音 ────────────────────

export function useTTS() {
  const isSpeaking = ref(false)
  const autoRead = ref(false)
  let currentUtterance: SpeechSynthesisUtterance | null = null

  /** 获取最佳中文语音 */
  function _pickVoice(): SpeechSynthesisVoice | null {
    const voices = speechSynthesis.getVoices()
    // 优先级：中文普通话 > 任何中文 > 默认
    const mandarin = voices.find(v => v.lang.startsWith('zh-CN'))
    if (mandarin) return mandarin
    const anyChinese = voices.find(v => v.lang.startsWith('zh'))
    if (anyChinese) return anyChinese
    return voices[0] || null
  }

  const speak = (text: string): void => {
    stop()

    if (!text.trim()) return
    if (!('speechSynthesis' in window)) {
      console.warn('[TTS] 浏览器不支持 speechSynthesis')
      return
    }

    // 清除 HTML 标记只留纯文本
    const plain = text
      .replace(/<!--[\s\S]*?-->/g, '')
      .replace(/<[^>]*>/g, '')
      .replace(/[*_~`#>#]/g, '')
      .replace(/\n+/g, '。')
      .trim()

    if (!plain) return

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
      // "interrupted" 是 stop() 触发的，不算错误
      if (e.error !== 'interrupted') {
        console.warn('[TTS] 朗读出错:', e.error)
      }
      isSpeaking.value = false
      currentUtterance = null
    }

    speechSynthesis.speak(currentUtterance)
  }

  const stop = () => {
    speechSynthesis.cancel()
    isSpeaking.value = false
    currentUtterance = null
  }

  onUnmounted(() => {
    stop()
  })

  return { isSpeaking, autoRead, speak, stop }
}
