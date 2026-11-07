import { post } from './http'
import type { AuthResponse, LoginRequest } from '@/types/api'

/** 登录；返回的 token 由调用方写入 localStorage。 */
export function login(payload: LoginRequest): Promise<AuthResponse> {
  return post<AuthResponse>('/api/auth/login', payload)
}
