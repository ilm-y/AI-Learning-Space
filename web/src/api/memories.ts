import { del, get, patch } from './http'
import type {
  HybridMemorySearchResponse,
  MemoryRequest,
  MemoryResponse,
  MemoryStatus,
} from '@/types/api'

export interface MemoryListQuery {
  spaceId?: number
  conceptId?: number
  status?: MemoryStatus
}

export interface HybridMemorySearchQuery {
  spaceId?: number
  conceptId?: number
  keyword?: string
  /** 后端缺省 10，上限 200。 */
  limit?: number
}

/** 精确过滤列表：`GET /api/memories`。 */
export function listMemories(
  query: MemoryListQuery = {},
): Promise<MemoryResponse[]> {
  return get<MemoryResponse[]>('/api/memories', { params: query })
}

/**
 * 混合检索：`GET /api/memories/search/hybrid`。
 * 返回体带 status，调用方必须区分「检索不可用」与「没有匹配」。
 */
export function hybridSearchMemories(
  query: HybridMemorySearchQuery = {},
): Promise<HybridMemorySearchResponse> {
  return get<HybridMemorySearchResponse>('/api/memories/search/hybrid', {
    params: query,
  })
}

/** PATCH 是全量校验：memoryType / content 必填，importance 不传会被清空。 */
export function updateMemory(
  id: number,
  payload: MemoryRequest,
): Promise<MemoryResponse> {
  return patch<MemoryResponse>(`/api/memories/${id}`, payload)
}

/** DELETE 的语义是归档（ARCHIVED），不是物理删除。 */
export function archiveMemory(id: number): Promise<null> {
  return del<null>(`/api/memories/${id}`)
}
