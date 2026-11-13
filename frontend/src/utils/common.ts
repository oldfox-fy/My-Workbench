/**
 * 判断一个路径是否代表本地文件
 * 支持：
 * - file:///C:/path/file.txt
 * - C:\path\file.txt 或 D:/path/file.txt
 * - /home/user/file.pdf (Linux/macOS 绝对路径)
 */
export function isLocalFilePath(path: string): boolean {
  if (path.startsWith('file://')) return true
  if (/^[a-zA-Z]:[\\/]/.test(path)) return true // Windows 盘符
  return false
}


export function isRunningInPyWebView(): boolean {
  // 优先检查 pywebview 对象（最准确）
  if (typeof window !== 'undefined' && (window as any).pywebview) {
    return true
  }
  // 后备检查 UA（某些旧版本可能没有 pywebview 全局对象）
  return /pywebview/i.test(navigator.userAgent)
}

/** 判断是否为服务端生成/上传的文件链接（可统一走下载流程） */
export function isServerFileUrl(url: string): boolean {
  if (!url) return false
  return url.startsWith('/files/generate/') || url.startsWith('/files/uploads/')
}

/** 把相对路径补全为完整 URL（/xxx → http://host/xxx） */
export function toFullUrl(url: string): string {
  if (!url) return ''
  if (url.startsWith('/')) {
    return window.location.origin + url
  }
  return url
}

/** 从 URL 中提取文件名（忽略查询参数与哈希，尽量解码） */
export function extractFileName(url: string, fallback = 'download'): string {
  if (!url) return fallback
  const clean = url.split('?')[0].split('#')[0]
  const name = clean.split(/[\\/]/).filter(Boolean).pop()
  if (!name) return fallback
  try {
    return decodeURIComponent(name)
  } catch {
    return name
  }
}

/**
 * 统一文件下载入口：
 * - 桌面端（pywebview）→ 调用原生 download_file 弹保存对话框
 * - 浏览器 → 触发 <a download> 下载（/files/* 服务端会以附件返回）
 */
export async function downloadFile(
  url: string,
  filename?: string,
): Promise<{ success: boolean; error?: string }> {
  if (!url) return { success: false, error: '下载地址为空' }
  const fullUrl = toFullUrl(url)
  const name = filename || extractFileName(url)

  if (isRunningInPyWebView()) {
    const api = (window as any).pywebview?.api
    if (api?.download_file) {
      try {
        const res = await api.download_file(fullUrl, name)
        return {
          success: !res || res.success !== false,
          error: res && typeof res === 'object' ? res.error : undefined,
        }
      } catch (e: any) {
        return { success: false, error: String(e?.message || e) }
      }
    }
  }

  // 浏览器兜底：同源文件用 a[download] 触发下载
  const a = document.createElement('a')
  a.href = fullUrl
  a.download = name
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  return { success: true }
}

// 复制文本到粘贴板
export const copyToClipboard = (text: string) => {

    if (!navigator.clipboard) {
        // 如果浏览器不支持 Clipboard API，则退回到旧的 execCommand 方法
        var tempInput = document.createElement('input')
        document.body.appendChild(tempInput)
        tempInput.value = text
        tempInput.select()
        document.execCommand('copy')
        document.body.removeChild(tempInput)
        return
    }

    // 使用现代的 Clipboard API
    navigator.clipboard.writeText(text).catch(err => {
        console.error('Could not copy text: ', err)
    })
}