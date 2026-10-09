import { useEffect, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Eye, EyeOff, KeyRound, Loader2, LockKeyhole, Mail, ShieldAlert, ShieldCheck, UserPlus } from 'lucide-react'
import { Logo } from '@/components/Logo'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'
import { QK } from '@/lib/queryKeys'

type AuthMode = 'login' | 'register' | 'migrate'

export function Auth() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [searchParams] = useSearchParams()
  const [mode, setMode] = useState<AuthMode>('login')
  const [email, setEmail] = useState('')
  const [registrationSecret, setRegistrationSecret] = useState('')
  const [verificationCode, setVerificationCode] = useState('')
  const [resendSeconds, setResendSeconds] = useState(0)
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [localError, setLocalError] = useState('')

  const statusQuery = useQuery({
    queryKey: QK.authStatus,
    queryFn: api.authStatus,
    staleTime: 0,
    retry: false,
  })
  const status = statusQuery.data

  useEffect(() => {
    if (!status) return
    if (status.legacy_migration_required) {
      setMode('migrate')
      return
    }
    if (status.authenticated && status.user) {
      navigate(searchParams.get('redirect') || '/', { replace: true })
      return
    }
    if (!status.has_users) setMode('register')
  }, [navigate, searchParams, status])

  useEffect(() => {
    if (resendSeconds <= 0) return
    const timer = window.setTimeout(() => {
      setResendSeconds((seconds) => Math.max(0, seconds - 1))
    }, 1000)
    return () => window.clearTimeout(timer)
  }, [resendSeconds])

  const sendCodeMutation = useMutation({
    mutationFn: () => api.authSendRegistrationCode(email.trim(), registrationSecret),
    onSuccess: (result) => {
      setResendSeconds(result.cooldown_seconds)
      setRegistrationSecret('')
      setLocalError('')
    },
    onError: (error: Error) => setLocalError(error.message || '验证码发送失败'),
  })

  const submitMutation = useMutation({
    mutationFn: async () => {
      if (mode === 'register') return api.authRegister(email, password, verificationCode)
      if (mode === 'migrate') return api.authMigrate(email, password)
      return api.authLogin(email, password)
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: QK.authStatus })
      navigate(searchParams.get('redirect') || '/', { replace: true })
    },
    onError: (error: Error) => setLocalError(error.message || '认证失败'),
  })

  const switchMode = (nextMode: AuthMode) => {
    setMode(nextMode)
    setPassword('')
    setConfirmPassword('')
    setRegistrationSecret('')
    setVerificationCode('')
    setLocalError('')
    sendCodeMutation.reset()
    submitMutation.reset()
  }

  const handleEmailChange = (value: string) => {
    setEmail(value)
    setVerificationCode('')
    setResendSeconds(0)
    setLocalError('')
    sendCodeMutation.reset()
  }

  const handleSendCode = () => {
    setLocalError('')
    const normalizedEmail = email.trim()
    if (!normalizedEmail || !normalizedEmail.includes('@')) {
      setLocalError('请输入有效的邮箱地址')
      return
    }
    if (!registrationSecret) {
      setLocalError('请输入注册口令')
      return
    }
    sendCodeMutation.mutate()
  }

  const handleSubmit = (event: FormEvent) => {
    event.preventDefault()
    setLocalError('')
    const normalizedEmail = email.trim()
    if (!normalizedEmail || !normalizedEmail.includes('@')) {
      setLocalError('请输入有效的邮箱地址')
      return
    }
    const minimum = mode === 'register' ? 8 : mode === 'migrate' ? 6 : 1
    if (password.length < minimum) {
      setLocalError(`密码至少 ${minimum} 位`)
      return
    }
    if (mode === 'register' && password !== confirmPassword) {
      setLocalError('两次输入的密码不一致')
      return
    }
    if (mode === 'register' && !/^\d{6}$/.test(verificationCode)) {
      setLocalError('请输入 6 位邮箱验证码')
      return
    }
    submitMutation.mutate()
  }

  if (statusQuery.isLoading) {
    return (
      <div className="grid min-h-screen place-items-center bg-base">
        <Loader2 className="h-5 w-5 animate-spin text-muted" />
      </div>
    )
  }

  const isMigrate = mode === 'migrate'
  const isRegister = mode === 'register'
  const title = isMigrate ? '升级现有账户' : isRegister ? '创建账户' : '登录'
  const subtitle = isMigrate
    ? '使用原访问密码绑定邮箱，现有数据和设置不会变化'
    : isRegister
      ? '输入注册口令并验证邮箱后创建账户'
      : '登录后继续使用量化工作台'

  return (
    <main className="min-h-screen bg-base px-4 py-10 text-foreground">
      <div className="mx-auto flex min-h-[calc(100vh-5rem)] w-full max-w-md flex-col justify-center">
        <header className="mb-8">
          <div className="mb-5 flex items-center gap-3">
            <Logo size={36} className="text-accent" />
            <div>
              <div className="text-sm font-semibold text-foreground">Tick Stock Panel</div>
              <div className="text-[11px] text-muted">自托管量化工作台</div>
            </div>
          </div>
          <h1 className="text-2xl font-semibold text-foreground">{title}</h1>
          <p className="mt-2 text-sm leading-6 text-secondary">{subtitle}</p>
        </header>

        {!isMigrate && status?.has_users && status.registration_enabled && (
          <div className="mb-5 grid h-9 grid-cols-2 rounded-btn bg-elevated p-1" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'login'}
              onClick={() => switchMode('login')}
              className={cn(
                'rounded text-xs font-medium transition-colors',
                mode === 'login' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary',
              )}
            >
              登录
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === 'register'}
              onClick={() => switchMode('register')}
              className={cn(
                'rounded text-xs font-medium transition-colors',
                mode === 'register' ? 'bg-surface text-foreground shadow-sm' : 'text-muted hover:text-secondary',
              )}
            >
              注册
            </button>
          </div>
        )}

        {searchParams.get('passwordChanged') === '1' && (
          <div className="mb-4 rounded-btn border border-accent/30 bg-accent/10 px-3 py-2 text-xs text-accent">
            密码已更新，请重新登录
          </div>
        )}

        <form onSubmit={handleSubmit} className="space-y-4">
          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-secondary">邮箱</span>
            <span className="relative block">
              <Mail className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                type="email"
                value={email}
                onChange={(event) => handleEmailChange(event.target.value)}
                placeholder="name@example.com"
                autoComplete="email"
                autoFocus
                className="h-11 w-full rounded-btn border border-border bg-surface pl-10 pr-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/60"
              />
            </span>
          </label>

          {isRegister && (
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-secondary">注册口令</span>
              <span className="relative block">
                <ShieldCheck className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                <input
                  type="password"
                  value={registrationSecret}
                  onChange={(event) => {
                    setRegistrationSecret(event.target.value)
                    setLocalError('')
                    sendCodeMutation.reset()
                  }}
                  placeholder="输入注册口令"
                  autoComplete="off"
                  maxLength={128}
                  className="h-11 w-full rounded-btn border border-border bg-surface pl-10 pr-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/60"
                />
              </span>
            </label>
          )}

          {isRegister && (
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-secondary">邮箱验证码</span>
              <span className="flex gap-2">
                <span className="relative min-w-0 flex-1">
                  <KeyRound className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
                  <input
                    type="text"
                    value={verificationCode}
                    onChange={(event) => {
                      setVerificationCode(event.target.value.replace(/\D/g, '').slice(0, 6))
                      setLocalError('')
                    }}
                    placeholder="6 位验证码"
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    maxLength={6}
                    className="h-11 w-full rounded-btn border border-border bg-surface pl-10 pr-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/60"
                  />
                </span>
                <button
                  type="button"
                  onClick={handleSendCode}
                  disabled={
                    sendCodeMutation.isPending
                    || resendSeconds > 0
                    || !email.trim()
                    || !registrationSecret
                  }
                  className="inline-flex h-11 w-28 shrink-0 items-center justify-center rounded-btn border border-border bg-surface px-2 text-xs font-medium text-secondary transition-colors hover:border-accent/40 hover:text-foreground disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {sendCodeMutation.isPending
                    ? <Loader2 className="h-4 w-4 animate-spin" />
                    : resendSeconds > 0
                      ? `${resendSeconds} 秒后重发`
                      : '发送验证码'}
                </button>
              </span>
            </label>
          )}

          <label className="block">
            <span className="mb-1.5 block text-xs font-medium text-secondary">
              {isMigrate ? '原访问密码' : '密码'}
            </span>
            <span className="relative block">
              <LockKeyhole className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <input
                type={showPassword ? 'text' : 'password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder={isMigrate ? '输入当前访问密码' : isRegister ? '至少 8 位' : '输入密码'}
                autoComplete={isRegister ? 'new-password' : 'current-password'}
                className="h-11 w-full rounded-btn border border-border bg-surface pl-10 pr-10 text-sm text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/60"
              />
              <button
                type="button"
                onClick={() => setShowPassword((visible) => !visible)}
                className="absolute right-2 top-1/2 grid h-7 w-7 -translate-y-1/2 place-items-center rounded text-muted hover:bg-elevated hover:text-foreground"
                aria-label={showPassword ? '隐藏密码' : '显示密码'}
              >
                {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
              </button>
            </span>
          </label>

          {isRegister && (
            <label className="block">
              <span className="mb-1.5 block text-xs font-medium text-secondary">确认密码</span>
              <input
                type={showPassword ? 'text' : 'password'}
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                placeholder="再次输入密码"
                autoComplete="new-password"
                className="h-11 w-full rounded-btn border border-border bg-surface px-3 text-sm text-foreground outline-none transition-colors placeholder:text-muted/60 focus:border-accent/60"
              />
            </label>
          )}

          {localError && (
            <div className="flex items-start gap-2 rounded-btn border border-danger/25 bg-danger/10 px-3 py-2.5 text-xs text-danger">
              <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{localError}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={
              submitMutation.isPending
              || !email
              || !password
              || (isRegister && verificationCode.length !== 6)
            }
            className="inline-flex h-11 w-full items-center justify-center gap-2 rounded-btn bg-accent text-sm font-semibold text-white transition-opacity hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {submitMutation.isPending
              ? <Loader2 className="h-4 w-4 animate-spin" />
              : isRegister
                ? <UserPlus className="h-4 w-4" />
                : <LockKeyhole className="h-4 w-4" />}
            {submitMutation.isPending ? '处理中…' : isMigrate ? '绑定邮箱并进入' : isRegister ? '注册并进入' : '登录'}
          </button>
        </form>

        {isMigrate && (
          <p className="mt-5 border-t border-border pt-4 text-xs leading-5 text-muted">
            这是旧版访问密码的一次性升级。绑定完成后，后续登录需要同时输入邮箱和密码。
          </p>
        )}
      </div>
    </main>
  )
}
