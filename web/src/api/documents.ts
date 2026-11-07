import { del, get, post, upload } from './http'
import type { DocumentResponse, DocumentUploadResponse } from '@/types/api'

/** 某个学习空间下的文档，后端按 createdAt、id 倒序返回。 */
export function listDocuments(spaceId: number): Promise<DocumentResponse[]> {
  return get<DocumentResponse[]>(`/api/spaces/${spaceId}/documents`)
}

/** multipart 上传；字段名固定为 `file`。 */
export function uploadDocument(
  spaceId: number,
  form: FormData,
): Promise<DocumentUploadResponse> {
  return upload<DocumentUploadResponse>(
    `/api/spaces/${spaceId}/documents`,
    form,
  )
}

export function getDocument(id: number): Promise<DocumentResponse> {
  return get<DocumentResponse>(`/api/documents/${id}`)
}

/** 重建索引：READY 文档重建期间保持 READY；DELETING 时后端返回 409。 */
export function reindexDocument(id: number): Promise<DocumentResponse> {
  return post<DocumentResponse>(`/api/documents/${id}/reindex`)
}

/** 语义是「提交删除」：文档先进入 DELETING，异步清理完成后从列表消失。 */
export function deleteDocument(id: number): Promise<null> {
  return del<null>(`/api/documents/${id}`)
}
