// frontend/src/composables/useVoice.ts
// 语音输入（STT）+ 语音输出（TTS）组合式函数

import { ref, onUnmounted } from 'vue'

/** 录音状态 */
export function useVoiceRecorder() {
  const isRecording = ref(false)
  const isProcessing = ref(false)
  const errorMsg = ref('')
  let mediaRecorder: MediaRecorder | null = null
  let audioChunks: Blob[] = []

  const startRecording = async (): Promise<void> => {
    errorMsg.value = ''
    audioChunks = []
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true })
      // 优先 webm（Chrome/Edge），其次 mp4
      const mimeType = MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus'
        : MediaRecorder.isTypeSupported('audio/webm')
          ? 'audio/webm'
          : 'audio/mp4'

      mediaRecorder = new MediaRecorder(stream, { mimeType })
      mediaRecorder.ondataavailable = (e) => {
        if (e.data.size > 0) audioChunks.push(e.data)
      }
      mediaRecorder.onerror = () => {
        errorMsg.value = '录音设备出错'
        stopRecording()
      }
      mediaRecorder.start()
      isRecording.value = true
    } catch (e: any) {
      if (e.name === 'NotAllowedError') {
        errorMsg.value = '麦克风权限被拒绝，请在浏览器设置中允许访问麦克风'
      } else {
        errorMsg.value = `无法启动录音：${e.message}`
      }
    }
  }

  const stopRecording = (): Promise<string> => {
    return new Promise((resolve, reject) => {
      if (!mediaRecorder || mediaRecorder.state === 'inactive') {
        isRecording.value = false
        reject(new Error('录音未启动'))
        return
      }
      mediaRecorder.onstop = async () => {
        // 释放麦克风
        mediaRecorder!.stream.getTracks().forEach(t => t.stop())
        isRecording.value = false
        isProcessing.value = true

        try {
          const blob = new Blob(audioChunks, { type: mediaRecorder!.mimeType })
          const text = await sendToSTT(blob)
          isProcessing.value = false
          resolve(text)
        } catch (e: any) {
          isProcessing.value = false
          errorMsg.value = e.message || '语音识别失败'
          reject(e)
        }
      }
      mediaRecorder.stop()
    })
  }

  const cancelRecording = () => {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
      mediaRecorder.onstop = null // 阻止 stopRecording 的 Promise
      mediaRecorder.stream.getTracks().forEach(t => t.stop())
      mediaRecorder.stop()
    }
    isRecording.value = false
    audioChunks = []
  }

  onUnmounted(() => {
    cancelRecording()
  })

  return { isRecording, isProcessing, errorMsg, startRecording, stopRecording, cancelRecording }
}

/** 发送音频到后端 STT */
async function sendToSTT(audioBlob: Blob): Promise<string> {
  const formData = new FormData()
  formData.append('file', audioBlob, 'recording.webm')
  formData.append('language', 'zh')

  const resp = await fetch('/api/stt', {
    method: 'POST',
    body: formData,
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }))
    throw new Error(err.detail || `语音识别失败 (${resp.status})`)
  }
  const data = await resp.json()
  return data.text || ''
}

/** TTS 播放 */
export function useTTS() {
  const isSpeaking = ref(false)
  const autoRead = ref(false)
  let currentAudio: HTMLAudioElement | null = null

  const speak = async (text: string, voice?: string): Promise<void> => {
    // 先清理旧音频
    stop()

    if (!text.trim()) return
    isSpeaking.value = true

    try {
      const resp = await fetch('/api/tts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: text.slice(0, 2000), voice }),
      })
      if (!resp.ok) {
        const err = await resp.json().catch(() => ({ detail: `HTTP ${resp.status}` }))
        throw new Error(err.detail || `语音合成失败 (${resp.status})`)
      }
      const blob = await resp.blob()
      const url = URL.createObjectURL(blob)
      currentAudio = new Audio(url)
      currentAudio.onended = () => {
        isSpeaking.value = false
        URL.revokeObjectURL(url)
        currentAudio = null
      }
      currentAudio.onerror = () => {
        isSpeaking.value = false
        currentAudio = null
      }
      await currentAudio.play()
    } catch (e: any) {
      isSpeaking.value = false
      console.warn('[TTS] 播放失败:', e.message)
    }
  }

  const stop = () => {
    if (currentAudio) {
      currentAudio.pause()
      currentAudio.currentTime = 0
      currentAudio = null
    }
    isSpeaking.value = false
  }

  const speakLastAssistantMsg = async (messages: any[]) => {
    // 找最后一条 assistant 消息
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i]?.role === 'assistant' && messages[i]?.content) {
        await speak(messages[i].content)
        return
      }
    }
  }

  onUnmounted(() => {
    stop()
  })

  return { isSpeaking, autoRead, speak, stop, speakLastAssistantMsg }
}
