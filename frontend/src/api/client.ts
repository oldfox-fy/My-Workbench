// frontend/src/api/client.ts
// 统一封装 fetch，自动注入认证 token

import { useAuthStore } from '@/stores/auth'

export async function apiFetch(
  url: string,
  options: RequestInit = {}
): Promise<Response> {
  const authStore = useAuthStore()
  const headers: Record<string, string> = {}

  // 复制已有 headers
  if (options.headers) {
    if (options.headers instanceof Headers) {
      options.headers.forEach((value, key) => {
        headers[key] = value
      })
    } else if (Array.isArray(options.headers)) {
      for (const [k, v] of options.headers) {
        headers[k] = v
      }
    } else {
      Object.assign(headers, options.headers)
    }
  }

  // 注入 token
  if (authStore.token) {
    headers['Authorization'] = `Bearer ${authStore.token}`
  }

  // 自动设置 Content-Type（如果 body 是 JSON 且未设置）
  if (!headers['Content-Type'] && options.body && typeof options.body === 'string') {
    try {
      JSON.parse(options.body)
      headers['Content-Type'] = 'application/json'
    } catch { /* not JSON */ }
  }

  return fetch(url, { ...options, headers })
}
