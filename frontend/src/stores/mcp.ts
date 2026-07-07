import { defineStore } from 'pinia'
import { ref } from 'vue'

export interface MCPServer {
  name: string
  transport: 'http' | 'stdio'
  url?: string | null
  command?: string | null
  args: string[]
  connected: boolean
  tools: string[]
}

export interface SaveServerPayload {
  name: string
  transport: 'http' | 'stdio'
  url?: string
  command?: string
  args?: string[]
}

export interface SaveServerResult {
  status: string
  connected: boolean
  tools: string[]
  error?: string | null
}

export const useMcpStore = defineStore('mcp', () => {
  const servers = ref<MCPServer[]>([])
  const loading = ref(false)

  async function loadServers() {
    loading.value = true
    try {
      const res = await fetch('/api/mcp/servers')
      const data = await res.json()
      servers.value = data.servers || []
    } catch (e) {
      console.warn('获取 MCP 服务列表失败', e)
    } finally {
      loading.value = false
    }
  }

  async function saveServer(payload: SaveServerPayload): Promise<SaveServerResult> {
    const res = await fetch('/api/mcp/servers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
    const result = await res.json()
    if (!res.ok) {
      throw new Error(result.detail || '保存失败')
    }
    await loadServers()
    return result
  }

  async function deleteServer(name: string) {
    await fetch(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' })
    await loadServers()
  }

  return { servers, loading, loadServers, saveServer, deleteServer }
})
