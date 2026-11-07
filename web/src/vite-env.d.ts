/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Spring Boot 服务地址，缺省 http://localhost:8080 */
  readonly VITE_API_BASE_URL?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
