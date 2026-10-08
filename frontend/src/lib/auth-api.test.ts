// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/Toast', () => ({
  toast: vi.fn(),
}))

import { api } from './api'

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: 'OK',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response
}

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('auth API', () => {
  it('sends verification, registration, and login payloads', async () => {
    const fetchMock = vi.fn(async () => ok({
      ok: true,
      authenticated: true,
      user: { id: 'user-1', email: 'user@example.com', created_at: 1 },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await api.authSendRegistrationCode('user@example.com')
    await api.authRegister('user@example.com', 'password-123', '123456')
    await api.authLogin('user@example.com', 'password-123')

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/auth/register/code',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'user@example.com' }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/auth/register',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          email: 'user@example.com',
          password: 'password-123',
          code: '123456',
        }),
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/auth/login',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'user@example.com', password: 'password-123' }),
      }),
    )
  })

  it('uses the dedicated legacy migration endpoint', async () => {
    const fetchMock = vi.fn(async () => ok({
      ok: true,
      authenticated: true,
      user: { id: 'owner-1', email: 'owner@example.com', created_at: 1 },
    }))
    vi.stubGlobal('fetch', fetchMock)

    await api.authMigrate('owner@example.com', 'legacy-secret')

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/auth/migrate',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ email: 'owner@example.com', password: 'legacy-secret' }),
      }),
    )
  })
})
