// frontend/src/api/knowledge.ts
// 知识库 RAG 相关接口封装（embedding/reranker 配置、索引管理、语义搜索）

export interface EmbeddingConfig {
  provider: 'ollama' | 'openai'
  base_url: string
  api_key: string
  model: string
  dim: number
}

export interface EmbeddingTestResult {
  success: boolean
  dim: number
  error: string
}

// ---------- reranker 配置 ----------

export interface RerankerConfig {
  enabled: boolean
  provider: 'ollama' | 'openai'
  base_url: string
  api_key: string
  model: string
}

export interface RerankerTestResult {
  success: boolean
  top_score: number
  error: string
}

export async function getRerankerConfig(): Promise<RerankerConfig> {
  const res = await fetch('/api/kb/reranker/config')
  if (!res.ok) throw new Error('获取 reranker 配置失败')
  return res.json()
}

export async function saveRerankerConfig(
  cfg: Partial<RerankerConfig>
): Promise<RerankerConfig> {
  const res = await fetch('/api/kb/reranker/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '保存 reranker 配置失败')
  }
  const data = await res.json()
  return data.config as RerankerConfig
}

export async function testRerankerConfig(
  cfg: Partial<RerankerConfig>
): Promise<RerankerTestResult> {
  const res = await fetch('/api/kb/reranker/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '测试 reranker 连接失败')
  }
  return res.json()
}

// ---------- embedding 配置（M1） ----------

export async function getEmbeddingConfig(): Promise<EmbeddingConfig> {
  const res = await fetch('/api/kb/embedding/config')
  if (!res.ok) throw new Error('获取 embedding 配置失败')
  return res.json()
}

export async function saveEmbeddingConfig(
  cfg: Partial<EmbeddingConfig>
): Promise<EmbeddingConfig> {
  const res = await fetch('/api/kb/embedding/config', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '保存 embedding 配置失败')
  }
  const data = await res.json()
  return data.config as EmbeddingConfig
}

export async function testEmbeddingConfig(
  cfg: Partial<EmbeddingConfig>
): Promise<EmbeddingTestResult> {
  const res = await fetch('/api/kb/embedding/test', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(cfg),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '测试连接失败')
  }
  return res.json()
}

// ---------- 索引管理与语义搜索（M2） ----------

export interface IndexStatus {
  configured: boolean
  indexed_files: number
  chunk_count: number
  model_name: string
  dim: number
  last_indexed_at: string | null
  vec_available: boolean
  vec_message: string
}

export interface SearchHit {
  file_path: string
  heading_path: string
  content: string
  distance: number
  rerank_score?: number
  citation_id?: string
  citation_text?: string
  chunk_type?: string
  page_number?: number
}

export async function getIndexStatus(): Promise<IndexStatus> {
  const res = await fetch('/api/kb/index/status')
  if (!res.ok) throw new Error('获取索引状态失败')
  return res.json()
}

export async function rebuildIndex(full: boolean): Promise<{ status: string }> {
  const res = await fetch('/api/kb/index/rebuild', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ full }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '重建索引失败')
  }
  return res.json()
}

export async function kbSearch(query: string, topK = 8, useRerank = false): Promise<SearchHit[]> {
  const res = await fetch('/api/kb/search', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ query, top_k: topK, use_rerank: useRerank }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '搜索失败')
  }
  const data = await res.json()
  return data.hits || []
}

// ---------- 双链与图谱（M3 / M4） ----------

export interface GraphNode {
  id: string
  label: string
  type: 'note' | 'missing' | 'tag' | 'attachment'
  degree: number
  tags: string[]
}

export interface GraphEdge {
  source: string
  target: string
  type: 'wiki' | 'md' | 'tag' | 'missing' | 'semantic'
}

export interface GraphData {
  nodes: GraphNode[]
  edges: GraphEdge[]
  stats: {
    note_count: number
    edge_count: number
    missing_count: number
    attachment_count?: number
    embedding_available?: boolean
  }
}

export interface Backlink {
  file_path: string
  type: 'wiki' | 'md' | 'semantic'
}

export async function getGraph(
  includeTags = false,
  includeSemantic = false,
  semanticThreshold = 0.72,
  files: string[] = [],
  keyword = "",
): Promise<GraphData> {
  const params = new URLSearchParams({ include_tags: String(includeTags) })
  if (includeSemantic) {
    params.set('include_semantic', 'true')
    params.set('semantic_threshold', String(semanticThreshold))
  }
  if (files.length) params.set('files', files.join(','))
  if (keyword) params.set('keyword', keyword)
  const res = await fetch(`/api/kb/graph?${params}`)
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '构建图谱失败')
  }
  return res.json()
}

export async function getBacklinks(path: string): Promise<Backlink[]> {
  const res = await fetch(`/api/kb/backlinks?path=${encodeURIComponent(path)}`)
  if (!res.ok) return []
  const data = await res.json()
  return data.backlinks || []
}

export async function getNoteNames(): Promise<string[]> {
  const res = await fetch('/api/kb/notes')
  if (!res.ok) return []
  const data = await res.json()
  return data.notes || []
}

// ---------- 附注 sidecar（为不可编辑资源提供双链附注） ----------

export interface Sidecar {
  path: string        // 附注笔记的相对路径（<原文件>.md）
  content: string
  exists: boolean
  editable: boolean
}

export async function getSidecar(path: string): Promise<Sidecar> {
  const res = await fetch(`/api/kb/sidecar?path=${encodeURIComponent(path)}`)
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '读取附注失败')
  }
  return res.json()
}

export async function saveSidecar(path: string, content: string): Promise<Sidecar> {
  const res = await fetch('/api/kb/sidecar/save', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path, content }),
  })
  if (!res.ok) {
    const err = await res.json().catch(() => ({}))
    throw new Error(err.detail || '保存附注失败')
  }
  return res.json()
}
