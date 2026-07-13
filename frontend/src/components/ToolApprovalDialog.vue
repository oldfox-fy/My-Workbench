<template>
  <!-- system_ask_user：文本输入对话框 -->
  <n-modal v-if="isAskUser" :show="visible" :mask-closable="false" :closable="false" preset="dialog"
    title="💬 AI 向你提问" positive-text="回复" negative-text="跳过"
    @positive-click="submitAnswer" @negative-click="skip"
  >
    <div class="approval-body">
      <p class="approval-question">{{ decodedQuestion }}</p>
      <n-input
        v-model:value="userAnswer"
        type="textarea"
        placeholder="在此输入你的回复..."
        :autosize="{ minRows: 2, maxRows: 5 }"
        @keydown.enter.ctrl="submitAnswer"
      />
    </div>
  </n-modal>

  <!-- 普通工具审批对话框 -->
  <n-modal v-else :show="visible" :mask-closable="false" :closable="false" preset="dialog"
    title=" 工具调用审批" positive-text="批准" negative-text="拒绝"
    @positive-click="approve" @negative-click="reject"
  >
    <div class="approval-body">
      <div class="approval-tool-name">
        <n-tag type="warning" size="large">{{ toolName }}</n-tag>
      </div>
      <p class="approval-hint">AI 请求调用此工具，请确认是否允许：</p>
      <n-scrollbar style="max-height: 120px">
        <pre class="approval-args">{{ argsPreview }}</pre>
      </n-scrollbar>
      <n-checkbox v-model:checked="sessionWhitelist" class="approval-check">
        此会话中后续同类工具自动批准
      </n-checkbox>
    </div>
  </n-modal>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { NModal, NTag, NScrollbar, NCheckbox, NInput } from 'naive-ui'

const props = defineProps<{
  callId: string
  toolName: string
  argsPreview: string
}>()

const emit = defineEmits<{
  resolved: [callId: string, approved: boolean, sessionWhitelist: boolean, answer?: string]
}>()

const visible = ref(false)
const sessionWhitelist = ref(false)
const userAnswer = ref('')

const isAskUser = computed(() => props.toolName === 'system_ask_user')

// ask_user 的 question 参数是 base64 编码的
const decodedQuestion = computed(() => {
  if (!isAskUser.value) return ''
  try {
    // argsPreview 已是 JSON，提取 question 字段
    const parsed = JSON.parse(props.argsPreview)
    return parsed.question || props.argsPreview
  } catch {
    return props.argsPreview
  }
})

watch(() => props.callId, (val) => {
  if (val) { visible.value = true; userAnswer.value = '' }
})

async function submitAnswer() {
  visible.value = false
  const answer = userAnswer.value.trim() || '(未输入)'
  await sendApproval(true, answer)
  emit('resolved', props.callId, true, false, answer)
}

async function skip() {
  visible.value = false
  await sendApproval(false)
  emit('resolved', props.callId, false, false)
}

async function approve() {
  visible.value = false
  await sendApproval(true)
  emit('resolved', props.callId, true, sessionWhitelist.value)
}

async function reject() {
  visible.value = false
  await sendApproval(false)
  emit('resolved', props.callId, false, false)
}

async function sendApproval(approved: boolean, answer?: string) {
  try {
    await fetch('/api/tool-calls/approval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ call_id: props.callId, approved, answer }),
    })
  } catch { /* 忽略网络错误 */ }
}

defineExpose({ show: () => { visible.value = true; userAnswer.value = '' } })
</script>

<style scoped>
.approval-body { display: flex; flex-direction: column; gap: 12px; }
.approval-tool-name { text-align: center; }
.approval-hint { font-size: 14px; color: var(--text-secondary); }
.approval-question { font-size: 15px; line-height: 1.5; padding: 8px; background: var(--bg-secondary); border-radius: 6px; }
.approval-args { font-size: 12px; padding: 8px; background: var(--bg-secondary); border-radius: 6px; white-space: pre-wrap; word-break: break-all; max-height: 120px; overflow: auto; }
.approval-check { margin-top: 4px; }
</style>
