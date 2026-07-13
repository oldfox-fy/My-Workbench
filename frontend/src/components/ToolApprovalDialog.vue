<template>
  <n-modal :show="visible" :mask-closable="false" :closable="false" preset="dialog"
    title="🔐 工具调用审批" positive-text="批准" negative-text="拒绝"
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
import { ref, watch } from 'vue'
import { NModal, NTag, NScrollbar, NCheckbox } from 'naive-ui'

const props = defineProps<{
  callId: string
  toolName: string
  argsPreview: string
}>()

const emit = defineEmits<{
  resolved: [callId: string, approved: boolean, sessionWhitelist: boolean]
}>()

const visible = ref(false)
const sessionWhitelist = ref(false)

watch(() => props.callId, (val) => {
  if (val) visible.value = true
})

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

async function sendApproval(approved: boolean) {
  try {
    await fetch('/api/tool-calls/approval', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ call_id: props.callId, approved }),
    })
  } catch { /* 忽略网络错误 */ }
}

// 暴露给父组件调用
defineExpose({ show: () => { visible.value = true } })
</script>

<style scoped>
.approval-body { display: flex; flex-direction: column; gap: 12px; }
.approval-tool-name { text-align: center; }
.approval-hint { font-size: 14px; color: var(--text-secondary); }
.approval-args { font-size: 12px; padding: 8px; background: var(--bg-secondary); border-radius: 6px; white-space: pre-wrap; word-break: break-all; }
.approval-check { margin-top: 4px; }
</style>
