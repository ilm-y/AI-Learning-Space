import { get } from './http'
import type {
  AttentionItem,
  LearningStateResponse,
  SessionResponse,
  UnderstandingResponse,
} from '@/types/api'

/** 当前学习活动；没有进行中的 Session 时后端返回 `data: null`，不是 404。 */
export function getCurrentSession(
  spaceId?: number,
): Promise<SessionResponse | null> {
  return get<SessionResponse | null>('/api/sessions/current', {
    params: { spaceId },
  })
}

/** 学习状态投影，按 reviewPriority、updatedAt 倒序。 */
export function getLearningStateOverview(): Promise<LearningStateResponse[]> {
  return get<LearningStateResponse[]>('/api/learning-state/overview')
}

/** 推荐关注：误解 > 久未验证 > 到期待复习。 */
export function getAttentionItems(limit = 10): Promise<AttentionItem[]> {
  return get<AttentionItem[]>('/api/learning-state/attention', {
    params: { limit },
  })
}

/** 知识点存在但尚未建立理解时返回 `null`（HTTP 200），不是 404。 */
export function getUnderstanding(
  conceptId: number,
): Promise<UnderstandingResponse | null> {
  return get<UnderstandingResponse | null>(`/api/understandings/${conceptId}`)
}
