import { get, post } from './http'
import type {
  ConversationResponse,
  LearningSpaceResponse,
  MessageResponse,
} from '@/types/api'

/** 当前用户的学习空间列表（对话列表按空间隔离，必须先选空间）。 */
export function listSpaces(): Promise<LearningSpaceResponse[]> {
  return get<LearningSpaceResponse[]>('/api/spaces')
}

export function listConversations(
  spaceId: number,
): Promise<ConversationResponse[]> {
  return get<ConversationResponse[]>(`/api/spaces/${spaceId}/conversations`)
}

export function createConversation(
  spaceId: number,
  title?: string,
): Promise<ConversationResponse> {
  return post<ConversationResponse>(`/api/spaces/${spaceId}/conversations`, {
    title: title ?? null,
  })
}

export function getConversation(
  conversationId: number,
): Promise<ConversationResponse> {
  return get<ConversationResponse>(`/api/conversations/${conversationId}`)
}

/** 历史消息，按 sequenceNo 升序返回。 */
export function listMessages(
  conversationId: number,
): Promise<MessageResponse[]> {
  return get<MessageResponse[]>(
    `/api/conversations/${conversationId}/messages`,
  )
}
