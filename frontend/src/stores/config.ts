import { defineStore } from 'pinia'
import { ref, computed, watch } from 'vue'
import { getModels, createModel, updateModel, deleteModel } from '@/api/models'
import { saveUserPrefs } from '@/api/userPrefs'


export interface ModelConfig {
  id: string          // 唯一标识
  name: string        // 显示名称
  type: 'local' | 'online'
  modelName?: string   // 模型 ID
  baseUrl: string     // 本地模型需要，线上可为空
  apiKey: string      // 线上模型需要，本地可为空
  role: string        // 模型角色：default/vision/reasoning/audio/fast
}

// 模型角色定义
export const MODEL_ROLES = [
  { value: 'default', label: '默认', desc: '通用对话' },
  { value: 'vision', label: '视觉', desc: '图片/多模态理解' },
  { value: 'reasoning', label: '推理', desc: '深度推理分析' },
  { value: 'audio', label: '语音', desc: '语音输入/输出' },
  { value: 'fast', label: '快速', desc: '轻量快速对话' },
  { value: 'image_gen', label: '生图', desc: '图像生成' },
] as const

const fileAcceptedSuffixes = [
  '.txt', '.md', '.markdown', '.rst', '.py', '.js', '.ts', '.jsx', '.vue',
  '.pdf', '.doc', '.docx', '.xlsx', '.tsx', '.csv', '.tsv',
  '.json', '.yaml', '.yml', '.xml', '.html', '.htm', '.css', '.scss', '.less',
  '.sh', '.bash', '.zsh', '.fish', '.ps1', '.bat', '.cmd',
  '.sql', '.c', '.cpp', '.h', '.hpp', '.java', '.go', '.rs', '.rb', '.php',
  '.swift', '.kt', '.scala', '.r', '.m', '.mm', '.pl', '.lua', '.vim',
  '.dockerfile', '.gitignore', '.env', '.ini', '.cfg', '.conf', '.properties',
  '.log', '.svg', '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.ico', '.webp', '.tiff'
]

export const fileConfig = {
  max: 3,
  size: 10, // 10MB
  accept: fileAcceptedSuffixes.join(',')
}

// ── localStorage fallback keys (only used before API prefs load) ──
const LEGACY_ACTIVE_KEY = 'llm_active_model_id'

export const useConfigStore = defineStore('config', () => {
  const savedModels = ref<ModelConfig[]>([])
  const activeModelId = ref<string|null>(localStorage.getItem(LEGACY_ACTIVE_KEY) || null)
  const themeMode = ref<'light' | 'dark'>(localStorage.getItem('themeMode') as 'light' | 'dark' || 'dark')
  const autoSwitch = ref(false)

  // 模型列表加载后再校验 activeModelId
  function _validateActiveModel() {
    if (activeModelId.value && !savedModels.value.some(m => m.id === activeModelId.value)) {
      activeModelId.value = savedModels.value[0]?.id || ''
    } else if (!activeModelId.value && savedModels.value.length > 0) {
      activeModelId.value = savedModels.value[0].id
    }
  }

  // watch 持久化到后端（替代 localStorage）
  watch(activeModelId, (val) => {
    if (val) {
      localStorage.setItem(LEGACY_ACTIVE_KEY, val) // 保留本地缓存以加速下次
      saveUserPrefs({ activeModelId: val } as any)
    }
  })

  watch(autoSwitch, (val) => {
    saveUserPrefs({ autoSwitch: val } as any)
  })

  watch(themeMode, (val) => {
    localStorage.setItem('themeMode', val) // 保留本地缓存以加速渲染
    saveUserPrefs({ themeMode: val } as any)
  })

  const activeModel = computed(() => savedModels.value.find(m => m.id === activeModelId.value))
  const loading = ref(false)

  /** 从 API 加载用户偏好并应用 */
  function applyUserPrefs(prefs: Record<string, any>) {
    if (prefs.themeMode) themeMode.value = prefs.themeMode
    if (prefs.autoSwitch !== undefined) autoSwitch.value = prefs.autoSwitch
    if (prefs.activeModelId !== undefined && prefs.activeModelId) {
      activeModelId.value = prefs.activeModelId
    }
    if (prefs.themeAccent !== undefined && prefs.themeAccent) {
      localStorage.setItem('themeAccent', prefs.themeAccent)
    }
    if (prefs.themeRadius !== undefined) {
      localStorage.setItem('themeRadius', String(prefs.themeRadius))
    }
    if (prefs.themeFontSize !== undefined && prefs.themeFontSize) {
      localStorage.setItem('themeFontSize', prefs.themeFontSize)
    }
    if (prefs.thinking !== undefined) {
      localStorage.setItem('thinking', prefs.thinking ? 'true' : 'false')
    }
    if (prefs.autoRead !== undefined) {
      localStorage.setItem('autoRead', prefs.autoRead ? 'true' : 'false')
    }
    if (prefs.enableProfile !== undefined) {
      localStorage.setItem('enableProfile', prefs.enableProfile ? 'true' : 'false')
    }
    if (prefs.activeProfileId !== undefined) {
      if (prefs.activeProfileId != null) {
        localStorage.setItem('activeProfileId', String(prefs.activeProfileId))
      } else {
        localStorage.removeItem('activeProfileId')
      }
    }
  }

  /** 获取指定角色的模型（用于自动切换） */
  function getModelByRole(role: string): ModelConfig | undefined {
    const model = savedModels.value.find(m => m.role === role)
    if (model) return model
    if (role !== 'default') {
      return savedModels.value.find(m => m.role === 'default')
    }
    return savedModels.value[0]
  }

  function toggleAutoSwitch() {
    autoSwitch.value = !autoSwitch.value
  }

  // 从后端加载模型列表
  async function loadModels() {
    loading.value = true
    try {
      const models = await getModels()
      savedModels.value = models
      _validateActiveModel()
    } catch (err) {
      console.error('Failed to load models:', err)
    } finally {
      loading.value = false
    }
  }

  // 添加模型
  async function addModel(model: Omit<ModelConfig, 'id'>) {
    const newModel = await createModel(model)
    savedModels.value.push(newModel)
    if (!activeModelId.value) {
      activeModelId.value = newModel.id
    }
  }

  // 更新模型
  async function updateModelById(id: string, updates: Partial<Omit<ModelConfig, 'id'>>) {
    await updateModel(id, updates)
    const idx = savedModels.value.findIndex(m => m.id === id)
    if (idx !== -1) Object.assign(savedModels.value[idx], updates)
  }

  // 删除模型
  async function deleteModelById(id: string) {
    await deleteModel(id)
    savedModels.value = savedModels.value.filter(m => m.id !== id)
    if (activeModelId.value === id && savedModels.value.length > 0) {
      activeModelId.value = savedModels.value[0].id
    } else if (savedModels.value.length === 0) {
      activeModelId.value = ''
    }
  }

  function setActiveModel(id: string) {
    activeModelId.value = id
    localStorage.setItem(LEGACY_ACTIVE_KEY, id)
  }
  function getActiveModelId() {
    return activeModelId.value || localStorage.getItem(LEGACY_ACTIVE_KEY) || null
  }

  function toggleTheme() {
    themeMode.value = themeMode.value === 'light' ? 'dark' : 'light'
  }

  return {
    savedModels,
    activeModel,
    themeMode,
    loading,
    autoSwitch,
    loadModels,
    addModel,
    updateModel: updateModelById,
    deleteModel: deleteModelById,
    getActiveModelId,
    setActiveModel,
    toggleTheme,
    getModelByRole,
    toggleAutoSwitch,
    applyUserPrefs,
  }
})