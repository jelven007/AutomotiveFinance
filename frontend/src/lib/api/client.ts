import { toast } from '@/components/Toast'

const API_BASE = ''

export type RequestOptions = RequestInit & {
  /** Suppress the shared error toast when the caller presents an aggregate error. */
  quiet?: boolean
  /** Request timeout in milliseconds. Set to null to disable it. */
  timeoutMs?: number | null
}

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000
export const COMPUTE_REQUEST_TIMEOUT_MS = 300_000

export function extPullTimeoutMs(timeoutSeconds?: number): number {
  return (timeoutSeconds ?? 30) * 1000 + 10_000
}

export async function request<T>(path: string, init?: RequestOptions): Promise<T> {
  const { quiet, timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, ...fetchInit } = init ?? {}
  const isFormData = fetchInit.body instanceof FormData
  const headers: Record<string, string> = {}
  if (!isFormData) headers['Content-Type'] = 'application/json'
  Object.assign(headers, fetchInit.headers as Record<string, string> | undefined)

  const controller = timeoutMs == null || fetchInit.signal ? undefined : new AbortController()
  const timeoutSeconds = Math.round((timeoutMs ?? DEFAULT_REQUEST_TIMEOUT_MS) / 1000)
  let timer: number | undefined
  if (controller && timeoutMs != null) {
    timer = window.setTimeout(() => controller.abort(), timeoutMs)
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      ...fetchInit,
      headers,
      ...(controller ? { signal: controller.signal } : {}),
    })
  } catch (error) {
    if (controller && error instanceof DOMException && error.name === 'AbortError') {
      const message = `请求超时（${timeoutSeconds}s）· ${path.split('?')[0]}`
      if (!quiet) toast(message, 'error')
      throw new ApiError(message, 0)
    }
    throw error
  } finally {
    if (timer !== undefined) window.clearTimeout(timer)
  }

  if (!response.ok) {
    let detail = ''
    try {
      const payload = JSON.parse(await response.text())
      const raw = payload.detail ?? payload.message ?? ''
      if (Array.isArray(raw)) {
        detail = raw.map((item: any) => item?.msg || String(item)).join('; ')
      } else if (typeof raw === 'string') {
        detail = raw
      } else if (raw && typeof raw === 'object') {
        detail = JSON.stringify(raw)
      }
    } catch {
      // Fall back to the HTTP status when the response is not JSON.
    }
    const message = detail || `${response.status} ${response.statusText}`
    if (response.status !== 401 && !quiet) toast(message, 'error')
    throw new ApiError(message, response.status)
  }

  return response.json() as Promise<T>
}
