/** 通用展示格式化。 */

export function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '—'
  }
  if (bytes < 1024) {
    return `${bytes} B`
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`
  }
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`
}

/** 后端 LocalDateTime 形如 `2026-11-07T22:36:00`，这里只展示到分钟。 */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) {
    return '—'
  }
  const normalized = value.replace('T', ' ')
  return normalized.length > 16 ? normalized.slice(0, 16) : normalized
}
