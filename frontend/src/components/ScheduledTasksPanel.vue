<template>
  <n-drawer :show="show" :auto-focus="false" @update:show="(v: boolean) => emit('update:show', v)" width="640">
    <n-drawer-content title="定时任务" closable>
      <n-space vertical size="large">
        <n-alert type="info" :bordered="false" size="small">
          用自然语言创建一次性或周期任务，到点自动调用 AI 执行并通知你。
          仅应用运行时触发，关机期间错过的任务自动顺延/跳过。
        </n-alert>

        <n-button type="primary" block @click="openCreate">+ 新建任务</n-button>

        <n-empty v-if="!store.loading && store.tasks.length === 0" description="暂无定时任务" size="small" />

        <n-card v-for="task in store.tasks" :key="task.id" size="small" hoverable>
          <template #header>
            <n-space align="center" :size="8" style="width: 100%; justify-content: space-between;">
              <n-space align="center" :size="8">
                <n-text strong>{{ task.title || '未命名任务' }}</n-text>
                <n-tag :type="statusTag(task.status)" size="small">{{ statusLabel(task.status) }}</n-tag>
                <n-tag v-if="task.auto_approve" type="warning" size="tiny" round>自动放行</n-tag>
              </n-space>
              <n-switch
                size="small"
                :value="task.status === 'active'"
                @update-value="() => onToggle(task)"
              />
            </n-space>
          </template>
          <n-text depth="3" style="font-size: 0.8rem">{{ task.human_readable }}</n-text>
          <n-text depth="3" tag="div" style="font-size: 0.78rem; margin-top: 6px; word-break: break-all">
            下次执行：{{ task.next_run_at || '—' }}
            <template v-if="task.last_run_status"> · 上次：{{ runLabel(task.last_run_status) }}</template>
          </n-text>
          <template #action>
            <n-space align="center" justify="end">
              <n-button text size="small" @click="onRunNow(task)">立即执行</n-button>
              <n-button text size="small" @click="openLogs(task)">日志</n-button>
              <n-popconfirm
                @positive-click="() => onDelete(task)"
                negative-text="取消" positive-text="好的"
                :negative-button-props="{ size: 'tiny' }"
                :positive-button-props="{ size: 'tiny' }"
              >
                <template #trigger>
                  <n-button text size="small" type="error">删除</n-button>
                </template>
                确定删除任务「{{ task.title || '未命名任务' }}」吗？
              </n-popconfirm>
            </n-space>
          </template>
        </n-card>
      </n-space>
    </n-drawer-content>
  </n-drawer>

  <!-- 新建任务对话框 -->
  <n-modal
    v-model:show="showCreate"
    :auto-focus="false"
    preset="dialog"
    draggable
    :mask-closable="false"
    style="max-width: 600px; width: 96%;"
    title="新建定时任务"
    positive-text="创建"
    negative-text="取消"
    :loading="creating"
    @positive-click="submitCreate"
  >
    <n-space vertical size="large">
      <n-form label-placement="left" label-width="86">
        <n-form-item label="自然语言">
          <n-space vertical style="width: 100%">
            <n-input
              v-model:value="nlText"
              type="textarea"
              placeholder="例如：每天早上 8 点帮我整理今天的重要新闻并总结"
              :autosize="{ minRows: 2, maxRows: 4 }"
            />
            <n-button size="small" secondary :loading="parsing" @click="onParse">解析</n-button>
          </n-space>
        </n-form-item>

        <n-form-item label="任务指令">
          <n-input v-model:value="form.prompt" type="textarea" placeholder="AI 执行的具体提示词"
            :autosize="{ minRows: 2, maxRows: 5 }" />
        </n-form-item>

        <n-form-item label="调度类型">
          <n-select v-model:value="form.schedule_type" :options="scheduleTypeOptions" />
        </n-form-item>

        <n-form-item v-if="form.schedule_type !== 'once'" label="执行时间">
          <n-input v-model:value="form.time_of_day" placeholder="HH:MM，如 08:30" />
        </n-form-item>

        <n-form-item v-if="form.schedule_type === 'weekly'" label="星期">
          <n-select v-model:value="form.weekday" :options="weekdayOptions" />
        </n-form-item>

        <n-form-item v-if="form.schedule_type === 'monthly'" label="每月几号">
          <n-input-number v-model:value="form.monthday" :min="1" :max="31" style="width: 120px" />
        </n-form-item>

        <n-form-item v-if="form.schedule_type === 'once'" label="执行时间">
          <n-input v-model:value="form.run_at" placeholder="YYYY-MM-DD HH:MM:SS" />
        </n-form-item>

        <n-form-item label="自动放行">
          <n-space align="center">
            <n-switch v-model:value="form.auto_approve" />
            <n-text depth="3" style="font-size: 0.75rem">执行时自动放行敏感工具（运行命令/写文件等）</n-text>
          </n-space>
        </n-form-item>
      </n-form>

      <n-alert v-if="parsePreview" type="success" size="small" :bordered="false">
        解析结果：{{ parsePreview }}
      </n-alert>
    </n-space>
  </n-modal>

  <!-- 执行日志对话框 -->
  <n-modal v-model:show="showLogs" :auto-focus="false" preset="dialog" title="执行日志"
    style="max-width: 720px; width: 96%;" :show-icon="false">
    <n-data-table
      :columns="runColumns"
      :data="runs"
      :max-height="420"
      size="small"
      :loading="loadingRuns"
    />
  </n-modal>
</template>

<script setup lang="ts">
import { ref, reactive, computed, watch, h } from 'vue'
import {
  NDrawer, NDrawerContent, NSpace, NAlert, NButton, NCard, NText, NTag, NSwitch,
  NEmpty, NModal, NForm, NFormItem, NInput, NSelect, NInputNumber, NDataTable,
  NPopconfirm, useMessage,
} from 'naive-ui'
import { useScheduledTasksStore } from '@/stores/scheduledTasks'
import { useConfigStore } from '@/stores/config'
import { parseTask, listRuns, type ScheduledTask, type TaskRun, type ScheduleType } from '@/api/scheduledTasks'

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [value: boolean] }>()

const message = useMessage()
const store = useScheduledTasksStore()
const configStore = useConfigStore()

const scheduleTypeOptions = [
  { label: '一次性', value: 'once' },
  { label: '每天', value: 'daily' },
  { label: '工作日', value: 'workdays' },
  { label: '每周', value: 'weekly' },
  { label: '每月', value: 'monthly' },
]

const weekdayOptions = [
  { label: '周一', value: 0 },
  { label: '周二', value: 1 },
  { label: '周三', value: 2 },
  { label: '周四', value: 3 },
  { label: '周五', value: 4 },
  { label: '周六', value: 5 },
  { label: '周日', value: 6 },
]

const showCreate = ref(false)
const showLogs = ref(false)
const creating = ref(false)
const parsing = ref(false)
const loadingRuns = ref(false)
const nlText = ref('')
const parsePreview = ref('')
const runs = ref<TaskRun[]>([])
const currentTask = ref<ScheduledTask | null>(null)

const form = reactive<{
  prompt: string
  schedule_type: ScheduleType
  time_of_day: string
  weekday: number | null
  monthday: number | null
  run_at: string | null
  auto_approve: boolean
}>({
  prompt: '',
  schedule_type: 'once',
  time_of_day: '',
  weekday: null,
  monthday: null,
  run_at: null,
  auto_approve: true,
})

function buildModelSnapshot(): Record<string, any> | undefined {
  let m = configStore.activeModel
  // 定时任务是 chat 型任务，跳过生图/生视频等非 chat 模型，回落默认 chat 模型
  if (!m || m.role === 'image_gen' || m.role === 'video') {
    m = configStore.getModelByRole('default') || m
  }
  if (!m) return undefined
  return {
    type: m.type,
    model_name: m.modelName,
    base_url: m.baseUrl,
    api_key: m.apiKey,
    thinking: localStorage.getItem('thinking') === 'true' ? 'enabled' : 'disabled',
    role: m.role || 'default',
  }
}

function statusLabel(s: string): string {
  return { active: '启用中', paused: '已暂停', completed: '已完成' }[s] || s
}
function statusTag(s: string): 'success' | 'warning' | 'default' {
  return { active: 'success', paused: 'warning', completed: 'default' }[s] as any || 'default'
}
function runLabel(s: string): string {
  return { success: '成功', failed: '失败', running: '执行中' }[s] || s
}

const runColumns = [
  { title: '时间', key: 'started_at', width: 150, render: (row: any) => row.started_at?.replace('T', ' ').substring(0, 19) || '' },
  {
    title: '状态', key: 'status', width: 80,
    render: (row: any) => h(NTag, { type: (row.status === 'success' ? 'success' : row.status === 'failed' ? 'error' : 'warning') as any, size: 'small' }, () => runLabel(row.status)),
  },
  {
    title: '输出 / 错误', key: 'output',
    render: (row: any) => h('div', { style: { whiteSpace: 'pre-wrap', wordBreak: 'break-all', maxHeight: '120px', overflow: 'auto' } }, row.status === 'failed' ? (row.error || '') : (row.output || '')),
  },
]

function openCreate() {
  nlText.value = ''
  parsePreview.value = ''
  form.prompt = ''
  form.schedule_type = 'once'
  form.time_of_day = ''
  form.weekday = null
  form.monthday = null
  form.run_at = null
  form.auto_approve = true
  showCreate.value = true
}

async function onParse() {
  if (!nlText.value.trim()) { message.warning('请输入任务描述'); return }
  parsing.value = true
  parsePreview.value = ''
  try {
    const r = await parseTask(nlText.value.trim(), buildModelSnapshot())
    form.prompt = r.prompt
    form.schedule_type = r.schedule_type
    form.time_of_day = r.time_of_day || ''
    form.weekday = r.weekday
    form.monthday = r.monthday
    form.run_at = r.run_at
    parsePreview.value = r.human_readable
  } catch (e: any) {
    message.error(e.message || '解析失败，请补充时间信息')
  } finally {
    parsing.value = false
  }
}

async function submitCreate() {
  if (!form.prompt.trim()) { message.warning('请填写任务指令'); return false }
  creating.value = true
  try {
    await store.create({
      prompt: form.prompt.trim(),
      schedule_type: form.schedule_type,
      time_of_day: form.time_of_day || '',
      weekday: form.weekday,
      monthday: form.monthday,
      run_at: form.run_at || null,
      title: form.prompt.trim().slice(0, 30),
      model_config: buildModelSnapshot(),
      enable_tools: true,
      auto_approve: form.auto_approve,
    })
    message.success('任务已创建')
    showCreate.value = false
  } catch (e: any) {
    message.error(e.message || '创建失败')
    return false
  } finally {
    creating.value = false
  }
}

async function onToggle(task: ScheduledTask) {
  try {
    await store.toggle(task.id)
  } catch (e: any) {
    message.error(e.message || '操作失败')
  }
}

async function onDelete(task: ScheduledTask) {
  try {
    await store.remove(task.id)
    message.success('已删除')
  } catch (e: any) {
    message.error(e.message || '删除失败')
  }
}

async function onRunNow(task: ScheduledTask) {
  try {
    await store.run(task.id)
    message.success('已开始执行，稍后可在日志中查看结果')
  } catch (e: any) {
    message.error(e.message || '执行失败')
  }
}

async function openLogs(task: ScheduledTask) {
  currentTask.value = task
  runs.value = []
  showLogs.value = true
  loadingRuns.value = true
  try {
    runs.value = await listRuns(task.id)
  } catch (e: any) {
    message.error(e.message || '加载日志失败')
  } finally {
    loadingRuns.value = false
  }
}

watch(() => props.show, (val) => {
  if (val) store.load()
})
</script>
