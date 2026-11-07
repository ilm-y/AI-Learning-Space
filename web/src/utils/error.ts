import { ElMessage } from 'element-plus'
import { ApiError } from '@/api/http'

/** 把 ApiError / Error / 未知值统一成可展示的文案。 */
export function errorText(error: unknown, fallback: string): string {
  if (error instanceof ApiError && error.message) {
    return error.message
  }
  if (error instanceof Error && error.message) {
    return error.message
  }
  return fallback
}

/** 统一的错误提示；后端业务错误优先展示 `message`。 */
export function notifyError(error: unknown, fallback: string): void {
  ElMessage.error(errorText(error, fallback))
}
