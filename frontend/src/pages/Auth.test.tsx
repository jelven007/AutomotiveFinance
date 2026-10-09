// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { api, type AuthStatus } from '@/lib/api'
import { Auth } from './Auth'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

let root: Root | null = null

afterEach(async () => {
  vi.restoreAllMocks()
  if (root) {
    await act(async () => root?.unmount())
    root = null
  }
  document.body.innerHTML = ''
})

describe('desktop authentication bypass', () => {
  it('redirects the login route directly to the application', async () => {
    const status: AuthStatus = {
      auth_required: false,
      configured: false,
      has_users: false,
      legacy_migration_required: false,
      registration_enabled: false,
      email_verification_required: false,
      authenticated: true,
      user: null,
    }
    vi.spyOn(api, 'authStatus').mockResolvedValue(status)

    const container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    await act(async () => {
      root?.render(
        <MemoryRouter initialEntries={['/login']}>
          <QueryClientProvider client={queryClient}>
            <Routes>
              <Route path="/login" element={<Auth />} />
              <Route path="/" element={<div>业务页面</div>} />
            </Routes>
          </QueryClientProvider>
        </MemoryRouter>,
      )
    })
    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 0))
    })

    expect(document.body.textContent).toContain('业务页面')
    expect(document.body.textContent).not.toContain('创建账户')
    expect(document.body.textContent).not.toContain('登录后继续')
  })
})
