import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface KbTreeNode {
  label: string
  key: string
  isDir: boolean
  editable?: boolean
  readonly?: boolean
  children?: KbTreeNode[]
}

export interface KbFile {
  path: string
  content: string
  format: string
  editable?: boolean
  readonly?: boolean
  raw_url?: string
  abs_path?: string
}

export const useKnowledgeStore = defineStore('knowledge', () => {
  const root = ref<string>('')
  const tree = ref<KbTreeNode[]>([])
  const loading = ref(false)

  // ---------- 共享选中态（目录树与内容窗口共用） ----------
  const currentPath = ref('')      // 当前打开的文件路径（空表示未选中）
  const fileContent = ref('')      // 当前文件内容
  const isEditable = ref(false)    // 当前文件是否可编辑
  const isReadonly = ref(false)    // 当前文件是否位于只读目录（如公共基础）
  const selectedKey = ref('')      // 目录树选中项（可能是文件或文件夹）
  const fileFormat = ref('')       // 当前文件格式：text/markdown/image/pdf/binary 等
  const rawUrl = ref('')           // image/pdf 的原始字节地址（内嵌查看用）
  const absPath = ref('')          // 当前文件的绝对路径（供"用系统程序打开"）

  // 打开并读取一个文件，更新共享选中态
  // 以后端返回的 editable 为准（若后端未返回则回退到传入的 hint）
  async function openFile(path: string, editable: boolean) {
    const data = await readFile(path)
    currentPath.value = data.path
    fileContent.value = data.content
    isEditable.value = data.editable ?? editable
    isReadonly.value = data.readonly ?? false
    fileFormat.value = data.format || ''
    rawUrl.value = data.raw_url || ''
    absPath.value = data.abs_path || ''
    selectedKey.value = data.path
  }

  // 清空选中态（切换目录、删除当前文件时调用）
  function resetSelection() {
    selectedKey.value = ''
    currentPath.value = ''
    fileContent.value = ''
    isEditable.value = false
    isReadonly.value = false
    fileFormat.value = ''
    rawUrl.value = ''
    absPath.value = ''
  }

  // 读取已保存的知识库根目录
  async function loadRoot() {
    try {
      const res = await fetch('/api/kb/root')
      const data = await res.json()
      root.value = data.path || ''
    } catch (e) {
      console.warn('获取知识库目录失败', e)
    }
  }

  // 设置知识库根目录
  async function setRoot(path: string): Promise<boolean> {
    const res = await fetch('/api/kb/root/set', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path })
    })
    if (!res.ok) return false
    const data = await res.json()
    root.value = data.path || ''
    localStorage.setItem('kbPath', root.value)
    return true
  }

  // 加载目录树
  async function loadTree() {
    if (!root.value) {
      tree.value = []
      return
    }
    loading.value = true
    try {
      const res = await fetch('/api/kb/tree')
      if (!res.ok) {
        tree.value = []
        return
      }
      const data = await res.json()
      tree.value = data.tree || []
    } catch (e) {
      console.warn('加载知识库目录树失败', e)
      tree.value = []
    } finally {
      loading.value = false
    }
  }

  // 读取文件
  async function readFile(path: string): Promise<KbFile> {
    const res = await fetch(`/api/kb/file?path=${encodeURIComponent(path)}`)
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || '读取文件失败')
    }
    return await res.json()
  }

  // 保存文件
  async function saveFile(path: string, content: string): Promise<void> {
    const res = await fetch('/api/kb/file/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path, content })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || '保存失败')
    }
  }

  // 新建笔记或文件夹
  async function createEntry(parent: string, name: string, type: 'file' | 'dir'): Promise<string> {
    const res = await fetch('/api/kb/create', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ parent, name, type })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || '创建失败')
    }
    const data = await res.json()
    return data.path as string
  }

  // 删除文件或文件夹
  async function deleteEntry(path: string): Promise<void> {
    const res = await fetch('/api/kb/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path })
    })
    if (!res.ok) {
      const err = await res.json().catch(() => ({}))
      throw new Error(err.detail || '删除失败')
    }
  }

  return {
    root,
    tree,
    loading,
    currentPath,
    fileContent,
    isEditable,
    isReadonly,
    selectedKey,
    fileFormat,
    rawUrl,
    absPath,
    openFile,
    resetSelection,
    loadRoot,
    setRoot,
    loadTree,
    readFile,
    saveFile,
    createEntry,
    deleteEntry,
  }
})
