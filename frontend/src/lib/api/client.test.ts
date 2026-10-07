// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/Toast', () => ({
  toast: vi.fn(),
}))

import { toast } from '@/components/Toast'
import { ApiError, extPullTimeoutMs, request } from './client'

const toastMock = vi.mocked(toast)

function response(
  options: {
    ok?: boolean
    status?: number
    statusText?: string
    body?: unknown
  } = {},
): Response {
  const {
    ok = true,
    status = 200,
    statusText = 'OK',
    body = {},
  } = options
  return {
    ok,
    status,
    statusText,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response
}

beforeEach(() => {
  toastMock.mockReset()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('request', () => {
  it('adds JSON headers, preserves caller headers, and returns parsed data', async () => {
    const fetchMock = vi.fn(async () => response({ body: { ok: true } }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(request<{ ok: boolean }>('/api/example', {
      method: 'POST',
      headers: { 'X-Trace-Id': 'trace-1' },
      body: JSON.stringify({ value: 1 }),
    })).resolves.toEqual({ ok: true })

    expect(fetchMock).toHaveBeenCalledWith('/api/example', expect.objectContaining({
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Trace-Id': 'trace-1',
      },
    }))
  })

  it('turns FastAPI validation details into one ApiError and toast', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => response({
      ok: false,
      status: 422,
      statusText: 'Unprocessable Entity',
      body: { detail: [{ msg: '字段缺失' }, { msg: '格式错误' }] },
    })))

    const error = await request('/api/example').catch((caught) => caught)

    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({
      message: '字段缺失; 格式错误',
      status: 422,
    })
    expect(toastMock).toHaveBeenCalledWith('字段缺失; 格式错误', 'error')
  })

  it('suppresses quiet errors and leaves 401 handling to the auth interceptor', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({
        ok: false,
        status: 401,
        statusText: 'Unauthorized',
        body: { detail: '登录已过期' },
      }))
      .mockResolvedValueOnce(response({
        ok: false,
        status: 500,
        statusText: 'Internal Server Error',
        body: { detail: '批量请求失败' },
      }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(request('/api/authenticated')).rejects.toMatchObject({
      message: '登录已过期',
      status: 401,
    })
    await expect(request('/api/batch', { quiet: true })).rejects.toMatchObject({
      message: '批量请求失败',
      status: 500,
    })
    expect(toastMock).not.toHaveBeenCalled()
  })

  it('aborts timed-out requests and reports the path without its query', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => (
      new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('Aborted', 'AbortError'))
        })
      })
    )))

    const assertion = expect(request('/api/slow?day=1', { timeoutMs: 1_000 }))
      .rejects.toMatchObject({
        message: '请求超时（1s）· /api/slow',
        status: 0,
      })
    await vi.advanceTimersByTimeAsync(1_000)
    await assertion

    expect(toastMock).toHaveBeenCalledWith('请求超时（1s）· /api/slow', 'error')
  })
})

describe('extPullTimeoutMs', () => {
  it('adds parsing and persistence headroom to the backend timeout', () => {
    expect(extPullTimeoutMs()).toBe(40_000)
    expect(extPullTimeoutMs(120)).toBe(130_000)
  })
})
