<template>
  <div class="kb-tree-panel" :style="{ width: (width ?? 240) + 'px' }">
    <!-- 顶部：标题 + 目录操作 -->
    <div class="kb-tree-head">
      <span class="kb-tree-title">
        <n-icon :size="16" style="vertical-align:-3px"><LibraryOutline /></n-icon>
        我的知识库
      </span>
      <n-button v-if="showClose" text size="tiny" @click="emit('close')" title="收起知识库">
        <template #icon><n-icon :size="18"><CloseOutline /></n-icon></template>
      </n-button>
    </div>

    <template v-if="kbStore.root">
      <div class="kb-sidebar-actions">
        <n-button size="tiny" secondary @click="openCreate(null, 'file')">
          <template #icon><n-icon><DocumentTextOutline /></n-icon></template>
          新建笔记
        </n-button>
        <n-button size="tiny" secondary @click="openCreate(null, 'dir')">
          <template #icon><n-icon><FolderOutline /></n-icon></template>
          新建文件夹
        </n-button>
        <n-button size="tiny" text @click="refreshTree" title="刷新">
          <template #icon><n-icon><RefreshOutline /></n-icon></template>
        </n-button>
        <n-button size="tiny" text @click="goGraph" title="知识图谱">
          <template #icon><n-icon><GitNetworkOutline /></n-icon></template>
        </n-button>
        <n-button size="tiny" text @click="selectFolder" :title="kbStore.root">
          <template #icon><n-icon><FolderOpenOutline /></n-icon></template>
        </n-button>
      </div>
      <n-scrollbar class="kb-tree-scroll">
        <n-spin :show="kbStore.loading">
          <n-tree
            v-if="kbStore.tree.length"
            block-line
            :data="kbStore.tree as any"
            :selected-keys="selectedKeys"
            key-field="key"
            label-field="label"
            children-field="children"
            :node-props="nodeProps"
            :render-label="renderLabel"
            @update:selected-keys="onSelect"
          />
          <n-empty v-else-if="!kbStore.loading" description="空空如也，先新建一个笔记或文件夹吧" style="margin-top:40px" />
        </n-spin>
      </n-scrollbar>
    </template>

    <!-- 未设置目录 -->
    <div v-else class="kb-empty-root">
      <n-empty description="尚未设置知识库目录">
        <template #extra>
          <n-button size="small" type="primary" @click="selectFolder">
            <template #icon><n-icon><FolderOpenOutline /></n-icon></template>
            选择目录
          </n-button>
        </template>
      </n-empty>
    </div>

    <!-- 新建对话框 -->
    <n-modal
      v-model:show="createModal.show"
      preset="dialog"
      :auto-focus="false"
      :title="createModal.type === 'dir' ? '新建文件夹' : '新建笔记'"
      positive-text="创建"
      negative-text="取消"
      @positive-click="doCreate"
    >
      <n-text depth="3" style="font-size:0.8rem" v-if="createModal.parent">
        位置：{{ createModal.parent }}/
      </n-text>
      <n-input
        v-model:value="createModal.name"
        style="margin-top:8px"
        :placeholder="createModal.type === 'dir' ? '文件夹名称' : '笔记名称（默认 .md）'"
        @keydown.enter="doCreate"
      />
    </n-modal>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  NButton, NIcon, NText, NTree, NScrollbar, NSpin, NEmpty,
  NInput, NModal, useMessage, useDialog
} from 'naive-ui'
import type { TreeOption } from 'naive-ui'
import { h } from 'vue'
import {
  LibraryOutline, CloseOutline, FolderOpenOutline, FolderOutline,
  DocumentTextOutline, RefreshOutline, LockClosedOutline, GitNetworkOutline
} from '@vicons/ionicons5'
import { useKnowledgeStore, type KbTreeNode } from '@/stores/knowledge'

defineProps<{ showClose?: boolean; width?: number }>()
const emit = defineEmits<{ (e: 'close'): void }>()

const router = useRouter()
const message = useMessage()
const dialog = useDialog()
const kbStore = useKnowledgeStore()

function goGraph() {
  router.push('/knowledge/graph')
}

const selectedKeys = computed(() => kbStore.selectedKey ? [kbStore.selectedKey] : [])

async function selectFolder() {
  try {
    const folder = await (window as any).pywebview.api.select_folder()
    if (folder) {
      const ok = await kbStore.setRoot(folder)
      if (ok) {
        kbStore.resetSelection()
        await kbStore.loadTree()
        message.success('知识库目录已设置')
      } else {
        message.error('目录设置失败')
      }
    }
  } catch {
    message.warning('文件夹选择仅支持桌面环境')
  }
}

async function refreshTree() {
  await kbStore.loadTree()
}

// 目录树节点：自定义标签（只读节点加锁图标）
function renderLabel({ option }: { option: TreeOption }) {
  const node = option as unknown as KbTreeNode
  if (node.readonly) {
    return h('span', { style: { display: 'inline-flex', alignItems: 'center', gap: '4px' } }, [
      h('span', {}, node.label),
      h(NIcon, { size: 13, style: { color: 'var(--text-secondary)', opacity: 0.7 } }, () => h(LockClosedOutline))
    ])
  }
  return node.label
}

// 目录树节点：右键删除（只读节点阻止删除）
function nodeProps({ option }: { option: TreeOption }) {
  const node = option as unknown as KbTreeNode
  if (node.readonly) {
    return {
      oncontextmenu(e: MouseEvent) {
        e.preventDefault()
        message.warning('「' + node.label + '」位于只读目录（公共基础），不可删除')
      },
      title: '只读目录，不可删除'
    }
  }
  return {
    oncontextmenu(e: MouseEvent) {
      e.preventDefault()
      confirmDelete(node)
    },
    title: node.isDir ? '右键删除文件夹' : '右键删除，单击查看'
  }
}

async function onSelect(_keys: Array<string | number>, _opt: unknown, meta: { node: TreeOption | null; action: 'select' | 'unselect' }) {
  const node = meta.node as unknown as KbTreeNode | null
  if (!node || meta.action === 'unselect') return
  const key = String(node.key)
  if (node.isDir) {
    // 选中文件夹：仅记录，供新建时作为父目录
    kbStore.selectedKey = key
    return
  }
  try {
    await kbStore.openFile(key, !!node.editable)
  } catch (e: any) {
    message.error(e.message || '读取失败')
  }
}

// ---------- 新建 ----------
const createModal = ref<{ show: boolean; parent: string | null; name: string; type: 'file' | 'dir' }>({
  show: false, parent: null, name: '', type: 'file'
})

function currentSelectedDir(): string | null {
  const key = kbStore.selectedKey
  if (!key) return null
  const node = findNode(kbStore.tree, key)
  if (node?.isDir) return key
  if (key.includes('/')) return key.slice(0, key.lastIndexOf('/'))
  return null
}

function findNode(nodes: KbTreeNode[], key: string): KbTreeNode | null {
  for (const n of nodes) {
    if (n.key === key) return n
    if (n.children) {
      const f = findNode(n.children, key)
      if (f) return f
    }
  }
  return null
}

function openCreate(parent: string | null, type: 'file' | 'dir') {
  createModal.value = {
    show: true,
    parent: parent ?? currentSelectedDir(),
    name: '',
    type
  }
}

async function doCreate() {
  const { parent, name, type } = createModal.value
  let finalName = name.trim()
  if (!finalName) {
    message.warning('请输入名称')
    return false
  }
  if (type === 'file' && !/\.[^.]+$/.test(finalName)) {
    finalName += '.md'
  }
  try {
    const path = await kbStore.createEntry(parent || '', finalName, type)
    createModal.value.show = false
    await kbStore.loadTree()
    if (type === 'file') {
      await kbStore.openFile(path, true)
    }
    message.success('已创建')
  } catch (e: any) {
    message.error(e.message || '创建失败')
    return false
  }
}

// ---------- 删除 ----------
function confirmDelete(node: KbTreeNode) {
  dialog.warning({
    title: '确认删除',
    content: node.isDir
      ? `删除文件夹「${node.label}」及其全部内容，无法恢复，确定吗？`
      : `删除「${node.label}」，无法恢复，确定吗？`,
    positiveText: '删除',
    negativeText: '取消',
    onPositiveClick: async () => {
      try {
        await kbStore.deleteEntry(node.key)
        if (kbStore.currentPath === node.key ||
            (node.isDir && kbStore.currentPath.startsWith(node.key + '/'))) {
          kbStore.resetSelection()
        }
        await kbStore.loadTree()
        message.success('已删除')
      } catch (e: any) {
        message.error(e.message || '删除失败')
      }
    }
  })
}
</script>

<style scoped>
.kb-tree-panel {
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  background: var(--glass-bg);
  backdrop-filter: blur(12px);
  border-right: var(--glass-border);
  overflow: hidden;
}

.kb-tree-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 12px 8px;
  flex-shrink: 0;
}
.kb-tree-title {
  font-size: 0.95rem;
  font-weight: 700;
  background: var(--accent-gradient);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}

.kb-sidebar-actions {
  display: flex;
  gap: 6px;
  padding: 0 12px 10px;
  flex-wrap: wrap;
  align-items: center;
}
.kb-tree-scroll { flex: 1; padding: 0 8px; }

.kb-empty-root {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}
</style>
