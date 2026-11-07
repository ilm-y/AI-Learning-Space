/**
 * 后端 API 类型定义。
 *
 * 严格对应 docs/design/FRONTEND_API_SPEC.md（源码审计结果）。
 * 枚举在 JSON 中是大写字符串；LocalDateTime 是无时区的 ISO-8601 本地时间。
 */

/* ============================================================
 * 统一响应
 * ============================================================ */

export interface ApiResponse<T> {
  /** 0 = 成功；非 0 时与 HTTP 状态码一致 */
  code: number
  message: string
  data: T | null
}

/* ============================================================
 * 枚举（后端序列化为大写常量名）
 * ============================================================ */

export type SpaceVisibility = 'PRIVATE' | 'SHARED'
export type ConversationStatus = 'ACTIVE' | 'ARCHIVED'
export type SessionStatus = 'ACTIVE' | 'PAUSED' | 'COMPLETED' | 'ABANDONED'
export type DocumentStatus =
  | 'UPLOADING'
  | 'PROCESSING'
  | 'READY'
  | 'FAILED'
  | 'DELETING'
  | 'DELETED'
export type MessageRole = 'USER' | 'ASSISTANT' | 'SYSTEM' | 'TOOL'
export type MessageType = 'TEXT' | 'TEST' | 'EVENT'
export type HighlightType =
  | 'IMPORTANT'
  | 'QUESTION'
  | 'MY_UNDERSTANDING'
  | 'MISCONCEPTION_CANDIDATE'
  | 'CONCEPT_LINK'
export type EvidenceType =
  | 'USER_EXPLANATION'
  | 'OBSERVATION'
  | 'TEST_RESULT'
  | 'USER_CORRECTION'
  | 'HIGHLIGHT_SIGNAL'
export type EvidenceCorrectness = 'CORRECT' | 'INCORRECT' | 'PARTIAL' | 'UNKNOWN'
export type EvidenceFollowUpStatus = 'NONE' | 'PENDING' | 'RESOLVED'
export type EvidenceSourceType = 'MESSAGE' | 'HIGHLIGHT' | 'TEST_ANSWER'
export type UnderstandingStatus =
  | 'UNKNOWN'
  | 'EXPOSED'
  | 'PARTIAL'
  | 'DEMONSTRATED'
  | 'MISCONCEPTION'
export type MisconceptionStatus = 'ACTIVE' | 'PENDING'
export type AttentionReason =
  | 'MISCONCEPTION'
  | 'MISCONCEPTION_RESOLVING'
  | 'LONG_UNVERIFIED'
  | 'NEEDS_REVIEW'
export type QuestionType = 'SINGLE_CHOICE' | 'TRUE_FALSE' | 'SHORT_ANSWER'
export type TestTaskStatus = 'PENDING' | 'ANSWERED' | 'EXPIRED'
export type MemoryScopeType = 'GLOBAL' | 'SPACE' | 'CONCEPT'
export type MemoryStatus = 'CANDIDATE' | 'ACTIVE' | 'ARCHIVED'
export type MemoryType = 'USER' | 'LEARNING' | 'PREFERENCE'
export type MemoryHybridStatus =
  | 'OK'
  | 'DEGRADED_VECTOR'
  | 'DEGRADED_KEYWORD'
  | 'UNAVAILABLE'
  | 'BLANK_QUERY'

/* ============================================================
 * 通用
 * ============================================================ */

/** 后端 LocalDateTime：ISO-8601 本地时间，无时区，如 "2026-11-07T22:36:00" */
export type LocalDateTime = string

/* ============================================================
 * Auth / User
 * ============================================================ */

export interface RegisterRequest {
  username: string
  password: string
  email?: string | null
}

export interface LoginRequest {
  username: string
  password: string
}

export interface UserDTO {
  id: number
  username: string
  email: string | null
  createdAt: LocalDateTime
}

export interface AuthResponse {
  token: string
  /** 固定 "Bearer" */
  tokenType: string
  user: UserDTO
}

export interface UpdateProfileRequest {
  email?: string | null
}

export interface ChangePasswordRequest {
  oldPassword: string
  newPassword: string
}

/* ============================================================
 * Learning Space / Concept
 * ============================================================ */

export interface LearningSpaceRequest {
  name: string
  description?: string | null
  visibility?: SpaceVisibility | null
}

export interface LearningSpaceResponse {
  id: number
  userId: number
  name: string
  description: string | null
  visibility: SpaceVisibility
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

export interface ConceptRequest {
  canonicalName: string
  displayName?: string | null
  description?: string | null
  /** 1~5 */
  difficultyLevel?: number | null
}

export interface ConceptResponse {
  id: number
  learningSpaceId: number
  canonicalName: string
  displayName: string | null
  description: string | null
  difficultyLevel: number | null
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

/* ============================================================
 * Conversation / Message
 * ============================================================ */

export interface ConversationRequest {
  title?: string | null
}

export interface ConversationResponse {
  id: number
  learningSpaceId: number
  /** 为 null 是正常状态：尚未归属任何学习活动 */
  sessionId: number | null
  title: string | null
  status: ConversationStatus
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

export interface CitationResponse {
  documentId: number
  chunkId: number
  chunkIndex: number | null
  sourceTitle: string
  /** 当前是否仍可打开（文档删除 / 重建后会变成 false） */
  available: boolean
}

export interface MessageRequest {
  content: string
}

export interface MessageResponse {
  id: number
  conversationId: number
  role: MessageRole
  messageType: MessageType
  content: string
  tokens: number | null
  sequenceNo: number | null
  createdAt: LocalDateTime
  truncated: boolean
  citations: CitationResponse[]
}

/* ============================================================
 * SSE（POST /api/conversations/{id}/messages）
 * ============================================================ */

export interface SseMessageStart {
  runId: number
  conversationId: number
}

export interface SseMessageDelta {
  delta: string
}

export interface SseMessageEnd {
  messageId: number
  runId: number
  citations: CitationResponse[]
  /** 仅在回答因 Token 上限截断时出现 */
  truncated?: true
  finishReason?: string | null
}

export interface SseMessageError {
  runId: number
  code: number
  message: string
}

/* ============================================================
 * Document
 * ============================================================ */

export interface DocumentResponse {
  id: number
  learningSpaceId: number
  userId: number
  name: string
  fileName: string
  /** "md" | "txt" */
  fileType: string
  size: number
  contentHash: string
  status: DocumentStatus
  chunkCount: number | null
  indexAttempts: number | null
  indexError: string | null
  embeddingModel: string | null
  indexVersion: number | null
  indexedAt: LocalDateTime | null
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
  embeddingProvider: string | null
  embeddingDimension: number | null
  embeddingFingerprint: string | null
  indexStartedAt: LocalDateTime | null
  deleteError: string | null
}

export interface DocumentUploadResponse {
  document: DocumentResponse
  /** true 表示命中同空间内内容相同的已有文档，未新建 */
  duplicate: boolean
}

export interface ReadyDocument {
  id: number
  indexVersion: number
}

/* ============================================================
 * Highlight
 * ============================================================ */

export interface HighlightRequest {
  markType: HighlightType
  selectedText: string
  startOffset?: number | null
  endOffset?: number | null
  note?: string | null
}

export interface HighlightResponse {
  id: number
  messageId: number
  markType: HighlightType
  selectedText: string
  startOffset: number | null
  endOffset: number | null
  note: string | null
  createdAt: LocalDateTime
}

/* ============================================================
 * Understanding / Evidence
 * ============================================================ */

export interface Misconception {
  text: string
  status: MisconceptionStatus
  firstDetectedAt: LocalDateTime
  lastEvidenceAt: LocalDateTime | null
  reversalEvidenceIds: number[]
}

export interface UnderstandingResponse {
  id: number
  conceptId: number
  conceptName: string | null
  summary: string | null
  knownParts: string[]
  /** 仅当前仍成立的误区 */
  misconceptions: Misconception[]
  uncertainParts: string[]
  latestUserExplanation: string | null
  status: UnderstandingStatus
  currentRevisionId: number | null
  updatedAt: LocalDateTime
}

export interface UnderstandingRevisionResponse {
  id: number
  revisionNo: number
  summary: string | null
  knownParts: string[]
  misconceptions: Misconception[]
  uncertainParts: string[]
  userExplanation: string | null
  status: UnderstandingStatus
  triggerEvidenceId: number | null
  createdAt: LocalDateTime
}

export interface EvidenceSourceResponse {
  sourceType: EvidenceSourceType
  sourceId: number
}

export interface EvidenceResponse {
  id: number
  conceptId: number
  understandingId: number
  evidenceType: EvidenceType
  observation: string
  correctness: EvidenceCorrectness
  reasoningQuality: number | null
  specificity: number | null
  aiConfidence: number | null
  needFollowUp: boolean | null
  followUpStatus: EvidenceFollowUpStatus
  createdAt: LocalDateTime
  sources: EvidenceSourceResponse[]
}

/* ============================================================
 * Learning State
 * ============================================================ */

export interface LearningStateResponse {
  userId: number
  conceptId: number
  conceptName: string | null
  status: UnderstandingStatus
  lastEvidenceAt: LocalDateTime | null
  nextReviewAt: LocalDateTime | null
  reviewPriority: number | null
  updatedAt: LocalDateTime
}

export interface AttentionItem {
  conceptId: number
  conceptName: string | null
  status: UnderstandingStatus
  reason: AttentionReason
  lastEvidenceAt: LocalDateTime | null
  nextReviewAt: LocalDateTime | null
  reviewPriority: number | null
}

export interface TopicCoverageItem {
  conceptId: number
  conceptName: string | null
  status: UnderstandingStatus
  evidenceCount: number
  revisionCount: number
  lastEvidenceAt: LocalDateTime | null
}

/* ============================================================
 * Test
 * ============================================================ */

export interface TestTaskRequest {
  conceptId: number
  conversationId?: number | null
  questionType?: QuestionType | null
  questionText?: string | null
  correctAnswer?: string | null
  /** JSON 字符串，不是对象 */
  evaluationRubric?: string | null
}

export interface TestTaskResponse {
  id: number
  conceptId: number
  conceptName: string | null
  conversationId: number | null
  questionType: QuestionType
  questionText: string | null
  options: string[]
  hint: string | null
  status: TestTaskStatus
  expiredAt: LocalDateTime | null
  createdAt: LocalDateTime
}

export interface AnswerRequest {
  userAnswer: string
}

export interface AnswerResult {
  isCorrect: boolean | null
  feedback: string | null
  evidenceId: number | null
  revisionId: number | null
  status: UnderstandingStatus | null
}

/* ============================================================
 * Memory
 * ============================================================ */

export interface MemoryRequest {
  learningSpaceId?: number | null
  conceptId?: number | null
  memoryType: MemoryType
  memoryKey?: string | null
  content: string
  importance?: number | null
  /** 仅 PATCH 生效；创建时忽略 */
  status?: MemoryStatus | null
}

export interface MemoryResponse {
  id: number
  scopeType: MemoryScopeType
  learningSpaceId: number | null
  conceptId: number | null
  memoryType: MemoryType
  memoryKey: string | null
  content: string
  status: MemoryStatus
  importance: number | null
  createdAt: LocalDateTime
  updatedAt: LocalDateTime
}

export interface HybridMemoryHit {
  memory: MemoryResponse
  rrfScore: number
  lexicalRank: number | null
  vectorRank: number | null
  vectorScore: number | null
}

export interface HybridMemorySearchResponse {
  status: MemoryHybridStatus
  items: HybridMemoryHit[]
  warnings: string[]
  elapsedMs: number
}

/* ============================================================
 * Learning Session
 * ============================================================ */

export interface SessionRequest {
  learningSpaceId: number
  goal: string
  title?: string | null
}

export interface SessionUpdateRequest {
  status?: SessionStatus | null
  title?: string | null
  goal?: string | null
  /** 传 "" 清空焦点；传 null / 省略表示不改 */
  currentFocus?: string | null
}

export interface SessionResponse {
  id: number
  learningSpaceId: number
  title: string | null
  goal: string | null
  currentFocus: string | null
  status: SessionStatus
  startedAt: LocalDateTime | null
  lastActiveAt: LocalDateTime | null
  completedAt: LocalDateTime | null
}
