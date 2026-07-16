<template>
  <n-space vertical :size="16">
    <n-text depth="3" style="font-size: 0.8rem">
      配置向量化（embedding）服务后，可为「我的知识库」建立语义索引，
      让 AI 通过语义检索精准命中相关笔记，而非整篇读取。
    </n-text>

    <!-- ========== embedding 配置 ========== -->
    <n-form label-placement="left" label-width="80" size="small">
      <n-form-item label="提供方">
        <n-radio-group v-model:value="form.provider">
          <n-radio value="ollama">本地 Ollama</n-radio>
          <n-radio value="openai">云端 OpenAI</n-radio>
        </n-radio-group>
      </n-form-item>
      <n-form-item label="服务地址">
        <n-input v-model:value="form.base_url" placeholder="http://127.0.0.1:11434/v1" />
      </n-form-item>
      <n-form-item label="模型">
        <n-input v-model:value="form.model" :placeholder="modelPlaceholder" />
      </n-form-item>
      <n-form-item v-if="form.provider === 'openai'" label="API Key">
        <n-input v-model:value="form.api_key" type="password" placeholder="sk-..." />
      </n-form-item>
    </n-form>

    <n-space>
      <n-button size="small" :loading="testing" @click="onTest">测试连接</n-button>
      <n-button size="small" type="primary" :loading="saving" @click="onSave">保存配置</n-button>
    </n-space>

    <n-alert v-if="dimInfo" :type="dimInfo.type" size="small" :title="dimInfo.title">
      {{ dimInfo.body }}
    </n-alert>

    <!-- 换模型 / 维度变化警告 -->
    <n-alert v-if="dimChanged" type="warning" size="small" title="向量维度已改变">
      检测到 embedding 模型维度与已建索引不同（{{ savedDim }} → {{ form.dim }}），
      请点击下方「重建索引」以保证检索正常。
    </n-alert>

    <n-divider style="margin: 4px 0" />

    <!-- ========== reranker 配置（可选） ========== -->
    <h4 style="margin: 0">Reranker 模型（可选）</h4>

    <n-text depth="3" style="font-size: 0.8rem">
      Reranker 对语义检索的候选结果做二次精排，提升检索精准度。
      需平台支持标准 /rerank 端点（如 SiliconFlow、Jina、Cohere）。
      未填写地址和 Key 时会自动复用上方 Embedding 配置。
    </n-text>

    <n-form label-placement="left" label-width="80" size="small">
      <n-form-item label="启用">
        <n-switch v-model:value="rerankerForm.enabled" />
      </n-form-item>
      <n-form-item v-if="rerankerForm.enabled" label="模型">
        <n-input v-model:value="rerankerForm.model" placeholder="BAAI/bge-reranker-v2-m3" />
      </n-form-item>
      <n-form-item v-if="rerankerForm.enabled" label="地址（可选）">
        <n-input v-model:value="rerankerForm.base_url" placeholder="留空则复用 embedding 地址" />
      </n-form-item>
      <n-form-item v-if="rerankerForm.enabled" label="Key（可选）">
        <n-input v-model:value="rerankerForm.api_key" type="password" placeholder="留空则复用 embedding Key" />
      </n-form-item>
    </n-form>

    <n-space v-if="rerankerForm.enabled">
      <n-button size="small" :loading="rerankerTesting" @click="onRerankerTest">测试连接</n-button>
      <n-button size="small" type="primary" :loading="rerankerSaving" @click="onRerankerSave">保存配置</n-button>
    </n-space>

    <n-alert v-if="rerankerInfo" :type="rerankerInfo.type" size="small" :title="rerankerInfo.title">
      {{ rerankerInfo.body }}
    </n-alert>

    <n-divider style="margin: 4px 0" />

    <!-- ========== 索引管理 ========== -->
    <h4 style="margin: 0">索引管理</h4>

    <n-alert v-if="status && !status.vec_available" type="error" size="small" title="向量扩展不可用">
      {{ status.vec_message || '当前 Python 的 sqlite3 不支持加载扩展，无法使用语义检索。' }}
    </n-alert>

    <n-descriptions v-if="status" :column="2" size="small" label-placement="top" bordered>
      <n-descriptions-item label="已索引文件">{{ status.indexed_files }}</n-descriptions-item>
      <n-descriptions-item label="向量片段">{{ status.chunk_count }}</n-descriptions-item>
      <n-descriptions-item label="模型">{{ status.model_name || '—' }}</n-descriptions-item>
      <n-descriptions-item label="维度">{{ status.dim || '—' }}</n-descriptions-item>
      <n-descriptions-item label="上次索引" :span="2">
        {{ status.last_indexed_at || '尚未索引' }}
      </n-descriptions-item>
    </n-descriptions>

    <n-space>
      <n-button size="small" :loading="indexing" :disabled="!canIndex" @click="onRebuild(false)">
        增量索引
      </n-button>
      <n-popconfirm @positive-click="onRebuild(true)" positive-text="重建" negative-text="取消">
        <template #trigger>
          <n-button size="small" type="warning" secondary :disabled="!canIndex">全量重建</n-button>
        </template>
        全量重建会清空现有向量并重新索引所有笔记，确定继续？
      </n-popconfirm>
      <n-button size="small" text @click="refreshStatus">刷新状态</n-button>
    </n-space>

    <n-text v-if="indexing" depth="3" style="font-size: 0.8rem">
      正在后台索引，可关闭此面板，稍后点「刷新状态」查看进度…
    </n-text>
  </n-space>
</template>

<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import {
  NSpace, NText, NForm, NFormItem, NRadioGroup, NRadio, NInput,
  NButton, NAlert, NDivider, NDescriptions, NDescriptionsItem,
  NPopconfirm, NSwitch, useMessage,
} from 'naive-ui'
import {
  getEmbeddingConfig, saveEmbeddingConfig, testEmbeddingConfig,
  getRerankerConfig, saveRerankerConfig, testRerankerConfig,
  getIndexStatus, rebuildIndex, type EmbeddingConfig, type IndexStatus,
  type RerankerConfig,
} from '@/api/knowledge'

const message = useMessage()

const form = reactive<EmbeddingConfig>({
  provider: 'ollama',
  base_url: 'http://127.0.0.1:11434/v1',
  api_key: '',
  model: 'bge-m3',
  dim: 0,
})

const savedDim = ref(0)
const testing = ref(false)
const saving = ref(false)
const indexing = ref(false)
const status = ref<IndexStatus | null>(null)

const modelPlaceholder = computed(() =>
  form.provider === 'ollama' ? 'bge-m3 / nomic-embed-text' : 'text-embedding-3-small'
)

const dimInfo = computed(() => {
  if (!form.dim) return null
  return {
    type: 'success' as const,
    title: `连接正常，向量维度 ${form.dim}`,
    body: '可以进行索引了。',
  }
})

// 已索引维度与当前配置维度不一致 → 需重建
const dimChanged = computed(
  () => !!savedDim.value && !!form.dim && savedDim.value !== form.dim
)

const canIndex = computed(() => !!form.dim && (!status.value || status.value.vec_available))

// ── reranker 配置 ──
const rerankerForm = reactive<RerankerConfig>({
  enabled: false,
  provider: 'openai',
  base_url: '',
  api_key: '',
  model: 'BAAI/bge-reranker-v2-m3',
})

const rerankerTesting = ref(false)
const rerankerSaving = ref(false)

const rerankerInfo = ref<{ type: 'success' | 'error'; title: string; body: string } | null>(null)

async function loadRerankerConfig() {
  try {
    const cfg = await getRerankerConfig()
    Object.assign(rerankerForm, cfg)
  } catch (e: any) {
    console.warn('加载 reranker 配置失败', e)
  }
}

async function onRerankerTest() {
  rerankerTesting.value = true
  rerankerInfo.value = null
  try {
    const r = await testRerankerConfig({ ...rerankerForm })
    if (r.success) {
      rerankerInfo.value = {
        type: 'success',
        title: `连接正常，最高相关性分数 ${r.top_score}`,
        body: '可以启用 reranker 了。',
      }
    } else {
      rerankerInfo.value = { type: 'error', title: '连接失败', body: r.error || '未知错误' }
    }
  } catch (e: any) {
    rerankerInfo.value = { type: 'error', title: '连接失败', body: e.message || '测试失败' }
  } finally {
    rerankerTesting.value = false
  }
}

async function onRerankerSave() {
  rerankerSaving.value = true
  rerankerInfo.value = null
  try {
    const saved = await saveRerankerConfig({ ...rerankerForm })
    Object.assign(rerankerForm, saved)
    message.success('Reranker 配置已保存')
  } catch (e: any) {
    message.error(e.message || '保存失败')
  } finally {
    rerankerSaving.value = false
  }
}

async function loadConfig() {
  try {
    const cfg = await getEmbeddingConfig()
    Object.assign(form, cfg)
    savedDim.value = cfg.dim || 0
  } catch (e: any) {
    console.warn('加载 embedding 配置失败', e)
  }
}

async function refreshStatus() {
  try {
    status.value = await getIndexStatus()
    if (status.value) savedDim.value = status.value.dim || savedDim.value
  } catch (e: any) {
    console.warn('获取索引状态失败', e)
  }
}

async function onTest() {
  testing.value = true
  try {
    const r = await testEmbeddingConfig({ ...form })
    if (r.success) {
      form.dim = r.dim
      message.success(`连接成功，向量维度 ${r.dim}`)
    } else {
      message.error(r.error || '连接失败')
    }
  } catch (e: any) {
    message.error(e.message || '测试失败')
  } finally {
    testing.value = false
  }
}

async function onSave() {
  saving.value = true
  try {
    const saved = await saveEmbeddingConfig({ ...form })
    Object.assign(form, saved)
    message.success('配置已保存')
  } catch (e: any) {
    message.error(e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function onRebuild(full: boolean) {
  indexing.value = true
  try {
    await rebuildIndex(full)
    message.success(full ? '已开始全量重建' : '已开始增量索引')
    // 轮询状态直到完成（简单起见，延迟后刷新）
    setTimeout(refreshStatus, 1500)
  } catch (e: any) {
    message.error(e.message || '索引失败')
  } finally {
    indexing.value = false
  }
}

onMounted(async () => {
  await loadConfig()
  await loadRerankerConfig()
  await refreshStatus()
})
</script>
