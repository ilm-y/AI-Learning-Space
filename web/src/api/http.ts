import axios, {
  type AxiosError,
  type AxiosInstance,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'
import type { ApiResponse } from '@/types/api'

/** 与后端约定的 token 存储键；退出登录时清除即可。 */
export const TOKEN_STORAGE_KEY = 'ails_token'

/** 认证失效时广播，路由 / 视图层可监听后跳转登录，避免请求层直接依赖 router。 */
export const UNAUTHORIZED_EVENT = 'ails:unauthorized'

/**
 * 业务错误。当 HTTP 200 但 `code !== 0`，或请求本身失败时抛出。
 */
export class ApiError extends Error {
  /** 后端业务码；网络层失败时为 HTTP 状态码或 0 */
  readonly code: number
  /** HTTP 状态码；无响应时为 undefined */
  readonly status?: number

  constructor(code: number, message: string, status?: number) {
    super(message)
    this.name = 'ApiError'
    this.code = code
    this.status = status
  }
}

/**
 * axios 实例。
 *
 * - BaseURL 指向 Spring Boot 默认地址，可用 `.env.development` 的
 *   `VITE_API_BASE_URL` 覆盖。
 * - 不设置全局 Content-Type：axios 会根据请求体自动选择 JSON，
 *   FormData 上传时也会正确生成 multipart boundary。
 */
export const http: AxiosInstance = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? 'http://localhost:8080',
  timeout: 30_000,
})

http.interceptors.request.use((config: InternalAxiosRequestConfig) => {
  const token = localStorage.getItem(TOKEN_STORAGE_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

http.interceptors.response.use(
  (response: AxiosResponse<ApiResponse<unknown>>) => {
    const body = response.data

    // 非统一响应体（如文件下载）原样放行。
    if (!body || typeof body !== 'object' || !('code' in body)) {
      return response
    }

    if (body.code !== 0) {
      return Promise.reject(new ApiError(body.code, body.message, response.status))
    }
    return response
  },
  (error: AxiosError<ApiResponse<unknown>>) => {
    const status = error.response?.status
    const body = error.response?.data
    const code = body?.code ?? status ?? 0
    const message = body?.message ?? error.message ?? '网络请求失败'

    if (status === 401) {
      localStorage.removeItem(TOKEN_STORAGE_KEY)
      window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT))
    }

    return Promise.reject(new ApiError(code, message, status))
  },
)

/** 发起请求并直接返回 `ApiResponse.data`。 */
export async function request<T>(config: AxiosRequestConfig): Promise<T> {
  const response = await http.request<ApiResponse<T>>(config)
  return response.data.data as T
}

export function get<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return request<T>({ ...config, method: 'GET', url })
}

export function post<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  return request<T>({ ...config, method: 'POST', url, data })
}

export function put<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  return request<T>({ ...config, method: 'PUT', url, data })
}

export function patch<T>(
  url: string,
  data?: unknown,
  config?: AxiosRequestConfig,
): Promise<T> {
  return request<T>({ ...config, method: 'PATCH', url, data })
}

export function del<T>(url: string, config?: AxiosRequestConfig): Promise<T> {
  return request<T>({ ...config, method: 'DELETE', url })
}

/** multipart 上传：字段名由调用方在 FormData 中决定。 */
export function upload<T>(
  url: string,
  form: FormData,
  config?: AxiosRequestConfig,
): Promise<T> {
  return request<T>({ ...config, method: 'POST', url, data: form })
}
