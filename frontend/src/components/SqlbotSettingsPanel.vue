<template>
  <n-space vertical :size="16">
    <n-text depth="3" style="font-size: 0.8rem">
      智能问数由本地 SQLBot（dataease/SQLBot）提供：连接你的业务数据库后，
      即可用自然语言查数（text-to-SQL）并返回数据与图表。模型复用工作台已配置的大模型。
    </n-text>

    <!-- ========== 连接与模型同步 ========== -->
    <n-card size="small" title="连接状态">
      <template #header-extra>
        <n-space :size="8">
          <n-button size="tiny" @click="loadStatus" :loading="statusLoading">刷新</n-button>
          <n-button size="tiny" type="primary" @click="onSync" :loading="syncing">同步模型</n-button>
        </n-space>
      </template>
      <n-space vertical :size="6">
        <n-space align="center" :size="8">
          <n-tag :type="status.connected ? 'success' : 'error'" size="small" round>
            {{ status.connected ? '已连接' : '未连接' }}
          </n-tag>
          <n-text depth="3" style="font-size: 0.78rem">{{ status.message || '—' }}</n-text>
        </n-space>
        <n-text v-if="status.rest_url" depth="3" style="font-size: 0.78rem">
          REST 地址：{{ status.rest_url }}
        </n-text>
        <n-text v-if="status.connected" depth="3" style="font-size: 0.78rem">
          工作台模型 {{ status.wb_models ?? 0 }} 个 · SQLBot 模型 {{ status.sqlbot_models ?? 0 }} 个 · 已同步映射 {{ status.synced ?? 0 }} 个
        </n-text>
      </n-space>
    </n-card>

    <n-alert v-if="syncResult" :type="syncResult.ok ? 'success' : 'error'" size="small" :title="syncResult.ok ? '同步完成' : '同步失败'">
      <template v-if="syncResult.ok">
        新建 {{ syncResult.created ?? 0 }} · 更新 {{ syncResult.updated ?? 0 }} · 删除 {{ syncResult.deleted ?? 0 }} · 绑定 {{ syncResult.bound ?? 0 }}
      </template>
      <template v-else>{{ syncResult.error }}</template>
    </n-alert>

    <!-- ========== 数据源列表 ========== -->
    <n-card size="small" title="数据源">
      <template #header-extra>
        <n-button size="tiny" type="primary" @click="showAddForm = !showAddForm">
          {{ showAddForm ? '收起' : '添加数据源' }}
        </n-button>
      </template>

      <!-- 添加数据源表单 -->
      <n-form v-if="showAddForm" label-placement="left" label-width="88" size="small" style="margin-bottom: 12px">
        <n-form-item label="类型" required>
          <n-select
            v-model:value="form.type"
            :options="typeOptions"
            placeholder="选择数据库类型"
            @update:value="onTypeChange"
          />
        </n-form-item>
        <n-form-item label="名称" required>
          <n-input v-model:value="form.name" placeholder="例如：销售库" />
        </n-form-item>
        <n-form-item label="主机">
          <n-input v-model:value="form.host" placeholder="例如：127.0.0.1" />
        </n-form-item>
        <n-form-item label="端口">
          <n-input-number v-model:value="form.port" :min="1" :max="65535" style="width: 100%" />
        </n-form-item>
        <n-form-item label="用户名">
          <n-input v-model:value="form.username" placeholder="数据库用户名" />
        </n-form-item>
        <n-form-item label="密码">
          <n-input v-model:value="form.password" type="password" show-password-on="click" placeholder="数据库密码" />
        </n-form-item>
        <n-form-item label="数据库">
          <n-input v-model:value="form.database" placeholder="数据库名 / 服务名" />
        </n-form-item>
        <n-form-item label="Schema">
          <n-input v-model:value="form.dbSchema" placeholder="可选（PostgreSQL/Oracle 等）" />
        </n-form-item>
        <n-form-item label="额外参数">
          <n-input v-model:value="form.extraJdbc" placeholder="可选，JDBC 连接参数" />
        </n-form-item>
        <n-form-item label="SSL">
          <n-switch v-model:value="form.ssl" />
        </n-form-item>
        <n-form-item label=" ">
          <n-space>
            <n-button size="small" :loading="checking" @click="onCheck">测试连接</n-button>
            <n-button size="small" type="primary" :loading="saving" @click="onAdd">保存</n-button>
            <n-button size="small" @click="resetForm">清空</n-button>
          </n-space>
        </n-form-item>
      </n-form>

      <!-- 数据源列表 -->
      <n-empty v-if="datasources.length === 0" description="暂无数据源，点击右上角「添加数据源」连接数据库" size="small" />
      <n-list v-else hoverable>
        <n-list-item v-for="ds in datasources" :key="ds.id">
          <template #suffix>
            <n-popconfirm
              @positive-click="() => onDelete(ds)"
              negative-text="取消" positive-text="删除"
              :negative-button-props="{ size: 'tiny' }"
              :positive-button-props="{ size: 'tiny', type: 'error' }"
            >
              <template #trigger>
                <n-button text size="tiny" type="error">删除</n-button>
              </template>
              确定删除数据源「{{ ds.name }}」吗？
            </n-popconfirm>
          </template>
          <div>
            <n-space align="center" :size="8">
              <n-text strong>{{ ds.name }}</n-text>
              <n-tag size="tiny" round>{{ ds.type_name || ds.type }}</n-tag>
              <n-tag :type="ds.status === 'Success' ? 'success' : 'warning'" size="tiny" round>
                {{ ds.status === 'Success' ? '正常' : (ds.status || '—') }}
              </n-tag>
            </n-space>
            <n-text depth="3" style="font-size: 0.78rem">ID：{{ ds.id }}</n-text>
            <n-text v-if="ds.description" depth="3" style="font-size: 0.78rem; display: block">
              {{ ds.description }}
            </n-text>
          </div>
        </n-list-item>
      </n-list>
    </n-card>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted, computed } from 'vue'
import {
  NText, NSpace, NCard, NButton, NTag, NAlert, NForm, NFormItem, NInput,
  NInputNumber, NSelect, NSwitch, NList, NListItem, NPopconfirm, NEmpty, useMessage,
} from 'naive-ui'
import { apiFetch } from '@/api/client'

interface DsType { type: string; name: string; default_port: number }
interface Datasource {
  id: number; name: string; description?: string; type: string; type_name?: string;
  status?: string; num?: string
}

const message = useMessage()

// ── 连接状态 ──
const status = reactive<Record<string, any>>({ connected: false, message: '未检测' })
const statusLoading = ref(false)
const syncing = ref(false)
const syncResult = ref<any>(null)

// ── 数据源 ──
const typeOptions = ref<{ label: string; value: string; default_port: number }[]>([])
const datasources = ref<Datasource[]>([])
const showAddForm = ref(false)

const form = reactive({
  type: 'mysql',
  name: '',
  host: '127.0.0.1',
  port: 3306,
  username: '',
  password: '',
  database: '',
  dbSchema: '',
  extraJdbc: '',
  ssl: false,
})
const checking = ref(false)
const saving = ref(false)

const defaultPort = computed(() => {
  const t = typeOptions.value.find(x => x.value === form.type)
  return t?.default_port ?? 0
})

async function loadStatus() {
  statusLoading.value = true
  try {
    const res = await apiFetch('/api/sqlbot/status')
    const data = await res.json()
    Object.assign(status, data)
  } catch (e: any) {
    status.connected = false
    status.message = '状态获取失败'
  } finally {
    statusLoading.value = false
  }
}

async function loadTypes() {
  try {
    const res = await apiFetch('/api/sqlbot/datasource-types')
    const data = await res.json()
    typeOptions.value = (data.types || []).map((t: DsType) => ({
      label: `${t.name} (${t.type})`,
      value: t.type,
      default_port: t.default_port,
    }))
  } catch { /* ignore */ }
}

async function loadDatasources() {
  try {
    const res = await apiFetch('/api/sqlbot/datasources')
    const data = await res.json()
    if (data.ok) {
      datasources.value = data.datasources || []
    } else if (data.error) {
      message.error(data.error)
    }
  } catch (e: any) {
    message.error('获取数据源列表失败')
  }
}

async function onSync() {
  syncing.value = true
  syncResult.value = null
  try {
    const res = await apiFetch('/api/sqlbot/sync', { method: 'POST' })
    const data = await res.json()
    syncResult.value = data
    if (data.ok) {
      message.success('模型同步完成')
      loadStatus()
    } else {
      message.error(data.error || '同步失败')
    }
  } catch (e: any) {
    syncResult.value = { ok: false, error: '同步请求失败' }
  } finally {
    syncing.value = false
  }
}

function onTypeChange(type: string) {
  const t = typeOptions.value.find(x => x.value === type)
  if (t) form.port = t.default_port
}

function resetForm() {
  form.name = ''
  form.host = '127.0.0.1'
  form.port = defaultPort.value
  form.username = ''
  form.password = ''
  form.database = ''
  form.dbSchema = ''
  form.extraJdbc = ''
  form.ssl = false
}

function validate(): boolean {
  if (!form.name.trim()) { message.warning('请填写数据源名称'); return false }
  if (!form.type) { message.warning('请选择数据库类型'); return false }
  if (!form.host.trim()) { message.warning('请填写主机'); return false }
  return true
}

async function onCheck() {
  if (!validate()) return
  checking.value = true
  try {
    const res = await apiFetch('/api/sqlbot/datasources/check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form }),
    })
    const data = await res.json()
    if (data.ok) message.success('连接成功')
    else message.error(data.error || '连接失败')
  } catch (e: any) {
    message.error('连接测试失败')
  } finally {
    checking.value = false
  }
}

async function onAdd() {
  if (!validate()) return
  saving.value = true
  try {
    const res = await apiFetch('/api/sqlbot/datasources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...form }),
    })
    const data = await res.json()
    if (data.ok) {
      message.success('数据源已保存')
      showAddForm.value = false
      resetForm()
      loadDatasources()
    } else {
      message.error(data.error || '保存失败')
    }
  } catch (e: any) {
    message.error('保存失败')
  } finally {
    saving.value = false
  }
}

async function onDelete(ds: Datasource) {
  try {
    const res = await apiFetch(`/api/sqlbot/datasources/${ds.id}`, { method: 'DELETE' })
    const data = await res.json()
    if (data.ok) {
      message.success('已删除')
      loadDatasources()
    } else {
      message.error(data.error || '删除失败')
    }
  } catch (e: any) {
    message.error('删除失败')
  }
}

onMounted(async () => {
  await Promise.all([loadStatus(), loadTypes(), loadDatasources()])
  if (!form.port) form.port = defaultPort.value
})
</script>
