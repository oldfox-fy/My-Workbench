<template>
  <n-drawer :show="show" :width="520" placement="right" @update:show="emit('update:show', $event)">
    <n-drawer-content title=" 记忆管理" closable>
      <n-tabs v-model:value="tab" type="segment" animated>
        <!-- ───────── 长期记忆（结构化） ───────── -->
        <n-tab-pane name="structured" tab="长期记忆">
          <div class="toolbar">
            <n-select
              v-model:value="categoryFilter"
              :options="categoryOptions"
              size="small"
              style="width: 160px"
              placeholder="全部分类"
              @update:value="loadStructured"
            />
            <n-button size="small" type="primary" @click="openAdd">
              <template #icon><n-icon><AddOutline /></n-icon></template>
              新增记忆
            </n-button>
          </div>

          <div v-if="loading" style="text-align:center;padding:40px"><n-spin size="large" /></div>
          <n-empty v-else-if="!memories.length" description="还没有长期记忆。开启工具模式多聊几轮，系统会自动抽取关于你的信息。" style="padding:40px 0" />

          <n-list v-else hoverable clickable :show-divider="true">
            <n-list-item v-for="mem in memories" :key="mem.id">
              <div class="mem-row">
                <div class="mem-main">
                  <div class="mem-content">{{ mem.content }}</div>
                  <div class="mem-meta">
                    <n-tag size="tiny" :bordered="false">{{ categoryLabel(mem.category) }}</n-tag>
                    <span v-if="mem.created_at" class="mem-time">{{ mem.created_at.slice(0, 10) }}</span>
                  </div>
                </div>
                <div class="mem-actions">
                  <n-button text size="tiny" @click="openEdit(mem)" title="编辑">
                    <template #icon><n-icon :size="16"><CreateOutline /></n-icon></template>
                  </n-button>
                  <n-popconfirm @positive-click="removeMem(mem)" negative-text="取消" positive-text="删除">
                    <template #trigger>
                      <n-button text size="tiny" title="删除">
                        <template #icon><n-icon :size="16"><CloseOutline /></n-icon></template>
                      </n-button>
                    </template>
                    确定删除这条记忆吗？
                  </n-popconfirm>
                </div>
              </div>
            </n-list-item>
          </n-list>
        </n-tab-pane>

        <!-- ───────── 原始会话记忆 ───────── -->
        <n-tab-pane name="raw" tab="会话记忆">
          <div class="toolbar">
            <n-select
              v-model:value="roleFilter"
              :options="roleOptions"
              size="small"
              style="width: 160px"
              placeholder="全部角色"
              @update:value="loadRaw"
            />
            <n-text depth="3" style="font-size:12px">双向索引：你的输入 + AI 回复</n-text>
          </div>

          <div v-if="loading" style="text-align:center;padding:40px"><n-spin size="large" /></div>
          <n-empty v-else-if="!rawMemories.length" description="还没有会话记忆。" style="padding:40px 0" />

          <n-list v-else hoverable clickable :show-divider="true">
            <n-list-item v-for="mem in rawMemories" :key="mem.id">
              <div class="mem-row">
                <div class="mem-main">
                  <div class="mem-content">{{ mem.content }}</div>
                  <div class="mem-meta">
                    <n-tag size="tiny" :bordered="false" :type="mem.role === 'user' ? 'info' : 'success'">
                      {{ mem.role === 'user' ? '我的输入' : 'AI 回复' }}
                    </n-tag>
                    <span v-if="mem.created_at" class="mem-time">{{ mem.created_at.slice(0, 10) }}</span>
                  </div>
                </div>
                <div class="mem-actions">
                  <n-popconfirm @positive-click="removeRaw(mem)" negative-text="取消" positive-text="删除">
                    <template #trigger>
                      <n-button text size="tiny" title="删除">
                        <template #icon><n-icon :size="16"><CloseOutline /></n-icon></template>
                      </n-button>
                    </template>
                    确定删除这条会话记忆吗？
                  </n-popconfirm>
                </div>
              </div>
            </n-list-item>
          </n-list>
        </n-tab-pane>
      </n-tabs>
    </n-drawer-content>

    <!-- 新增 / 编辑记忆弹窗 -->
    <n-modal v-model:show="editModal" preset="dialog" title="记忆" positive-text="保存" negative-text="取消" @positive-click="saveEdit">
      <div style="margin-bottom:12px">
        <n-select v-model:value="editCategory" :options="categoryOptions.filter(o => o.value !== '')" placeholder="选择分类" />
      </div>
      <n-input v-model:value="editContent" type="textarea" :autosize="{ minRows: 3, maxRows: 8 }" placeholder="例如：用户正在开发一个 RAG 知识库应用" />
    </n-modal>
  </n-drawer>
</template>

<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import {
  NDrawer, NDrawerContent, NTabs, NTabPane, NList, NListItem, NButton, NInput,
  NSelect, NTag, NIcon, NPopconfirm, NModal, NSpin, NEmpty, NText, useMessage,
} from 'naive-ui'
import { AddOutline, CreateOutline, CloseOutline } from '@vicons/ionicons5'
import {
  listMemories, createMemory, updateMemory, deleteMemory,
  listRawMemories, deleteRawMemory,
  type StructuredMemory, type RawMemory, type CategoryMap,
} from '@/api/memory'

const props = defineProps<{ show: boolean }>()
const emit = defineEmits<{ 'update:show': [v: boolean] }>()
const message = useMessage()

const FALLBACK_CATEGORIES: CategoryMap = {
  fact: '个人信息', preference: '偏好习惯', project: '项目工作',
  relationship: '人际关系', other: '其它',
}

const tab = ref<'structured' | 'raw'>('structured')
const loading = ref(false)
const memories = ref<StructuredMemory[]>([])
const rawMemories = ref<RawMemory[]>([])
const categories = ref<CategoryMap>(FALLBACK_CATEGORIES)

const categoryFilter = ref('')
const roleFilter = ref('')

const categoryOptions = computed(() => {
  const opts = Object.entries(categories.value).map(([value, label]) => ({ label, value }))
  return [{ label: '全部分类', value: '' }, ...opts]
})

const roleOptions = [
  { label: '全部角色', value: '' },
  { label: '我的输入', value: 'user' },
  { label: 'AI 回复', value: 'assistant' },
]

function categoryLabel(cat: string): string {
  return categories.value[cat] || FALLBACK_CATEGORIES[cat] || cat
}

async function loadStructured() {
  loading.value = true
  try {
    const data = await listMemories(categoryFilter.value)
    memories.value = data.memories || []
    if (data.categories) categories.value = data.categories
  } catch (e: any) {
    message.error(e.message || '获取记忆失败')
  } finally {
    loading.value = false
  }
}

async function loadRaw() {
  loading.value = true
  try {
    const data = await listRawMemories(roleFilter.value)
    rawMemories.value = data.memories || []
  } catch (e: any) {
    message.error(e.message || '获取会话记忆失败')
  } finally {
    loading.value = false
  }
}

// ── 新增 / 编辑 ──
const editModal = ref(false)
const editId = ref<number | null>(null)
const editContent = ref('')
const editCategory = ref('fact')

function openAdd() {
  editId.value = null
  editContent.value = ''
  editCategory.value = 'fact'
  editModal.value = true
}

function openEdit(mem: StructuredMemory) {
  editId.value = mem.id
  editContent.value = mem.content
  editCategory.value = mem.category
  editModal.value = true
}

async function saveEdit() {
  const content = editContent.value.trim()
  if (!content) { message.warning('内容不能为空'); return }
  try {
    if (editId.value == null) {
      await createMemory(content, editCategory.value)
      message.success('已新增记忆')
    } else {
      await updateMemory(editId.value, { content, category: editCategory.value })
      message.success('已保存')
    }
    editModal.value = false
    await loadStructured()
  } catch (e: any) {
    message.error(e.message || '保存失败')
  }
}

async function removeMem(mem: StructuredMemory) {
  try {
    await deleteMemory(mem.id)
    message.success('已删除')
    await loadStructured()
  } catch (e: any) {
    message.error(e.message || '删除失败')
  }
}

async function removeRaw(mem: RawMemory) {
  try {
    await deleteRawMemory(mem.id)
    message.success('已删除')
    await loadRaw()
  } catch (e: any) {
    message.error(e.message || '删除失败')
  }
}

watch(() => props.show, (v) => {
  if (v) {
    loadStructured()
    loadRaw()
  }
})
</script>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;
}
.mem-row {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  width: 100%;
  gap: 8px;
}
.mem-main { flex: 1; min-width: 0; }
.mem-content {
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
}
.mem-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 4px;
}
.mem-time { font-size: 12px; color: var(--text-secondary); }
.mem-actions {
  display: flex;
  gap: 2px;
  flex-shrink: 0;
  opacity: 0;
  transition: opacity 0.2s;
}
:deep(.n-list-item:hover) .mem-actions { opacity: 1; }
</style>
