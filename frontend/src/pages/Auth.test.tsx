// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'
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

describe('desktop registration email setup', () => {
  it('shows SMTP fields and blocks verification until delivery is tested', async () => {
    const status: AuthStatus = {
      configured: false,
      has_users: false,
      legacy_migration_required: false,
      registration_enabled: true,
      email_verification_required: true,
      registration_email_configurable: true,
      registration_email_configured: false,
      authenticated: false,
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
            <Auth />
          </QueryClientProvider>
        </MemoryRouter>,
      )
    })
    await act(async () => {
      await new Promise(resolve => window.setTimeout(resolve, 0))
    })

    expect(document.body.textContent).toContain('SMTP 服务器')
    expect(document.body.textContent).toContain('保存并发送测试邮件')
    const sendCode = Array.from(document.querySelectorAll('button'))
      .find(button => button.textContent?.includes('发送验证码'))
    expect(sendCode).toBeDefined()
    expect(sendCode?.disabled).toBe(true)
  })
})
