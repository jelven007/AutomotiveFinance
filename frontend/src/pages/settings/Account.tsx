import { useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { CheckCircle2, KeyRound, Loader2, LogOut, Mail, ShieldAlert, UserRound } from 'lucide-react'
import { PageHeader } from '@/components/PageHeader'
import { api } from '@/lib/api'
import { QK } from '@/lib/queryKeys'

export function SettingsAccountPanel() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [formError, setFormError] = useState('')

  const statusQuery = useQuery({
    queryKey: QK.authStatus,
    queryFn: api.authStatus,
    staleTime: 30_000,
  })

  const clearSessionAndRedirect = (path: string) => {
    window.localStorage.removeItem('tf-query-cache')
    queryClient.clear()
    navigate(path, { replace: true })
  }

  const logoutMutation = useMutation({
    mutationFn: api.authLogout,
    onSuccess: () => clearSessionAndRedirect('/login'),
  })

  const passwordMutation = useMutation({
    mutationFn: () => api.authChangePassword(oldPassword, newPassword),
    onSuccess: async () => {
      await api.authLogout()
      clearSessionAndRedirect('/login?passwordChanged=1')
    },
    onError: (error: Error) => setFormError(error.message || '密码修改失败'),
  })

  const handlePasswordSubmit = (event: FormEvent) => {
    event.preventDefault()
    setFormError('')
    if (newPassword.length < 8) {
      setFormError('新密码至少 8 位')
      return
    }
    if (newPassword !== confirmPassword) {
      setFormError('两次输入的新密码不一致')
      return
    }
    if (oldPassword === newPassword) {
      setFormError('新密码不能与原密码相同')
      return
    }
    passwordMutation.mutate()
  }

  const user = statusQuery.data?.user
  const createdAt = user?.created_at
    ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric' })
      .format(new Date(user.created_at * 1000))
    : '--'

  return (
    <>
      <PageHeader title="账户" subtitle="管理登录邮箱、密码和当前会话" />

      <section className="rounded-card border border-border bg-surface p-5">
        <div className="mb-4 flex items-center gap-2">
          <UserRound className="h-4 w-4 text-accent" />
          <h3 className="text-sm font-medium text-foreground">账户信息</h3>
        </div>
        <dl className="divide-y divide-border/60">
          <div className="flex items-center justify-between gap-4 py-3">
            <dt className="flex items-center gap-2 text-xs text-muted">
              <Mail className="h-3.5 w-3.5" />
              登录邮箱
            </dt>
            <dd className="truncate text-sm text-foreground">
              {statusQuery.isLoading ? '加载中…' : user?.email || '--'}
            </dd>
          </div>
          <div className="flex items-center justify-between gap-4 py-3">
            <dt className="text-xs text-muted">创建时间</dt>
            <dd className="text-xs text-secondary">{createdAt}</dd>
          </div>
        </dl>
      </section>

      <section className="mt-6 rounded-card border border-border bg-surface p-5">
        <div className="mb-4 flex items-center gap-2">
          <KeyRound className="h-4 w-4 text-accent" />
          <div>
            <h3 className="text-sm font-medium text-foreground">修改密码</h3>
            <p className="mt-0.5 text-[11px] text-muted">修改后当前账户的所有会话都会退出</p>
          </div>
        </div>

        <form onSubmit={handlePasswordSubmit} className="max-w-md space-y-3">
          <label className="block">
            <span className="mb-1.5 block text-xs text-secondary">原密码</span>
            <input
              type="password"
              value={oldPassword}
              onChange={(event) => setOldPassword(event.target.value)}
              autoComplete="current-password"
              className="h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent/60"
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-xs text-secondary">新密码</span>
            <input
              type="password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
              placeholder="至少 8 位"
              autoComplete="new-password"
              className="h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none placeholder:text-muted/60 focus:border-accent/60"
            />
          </label>
          <label className="block">
            <span className="mb-1.5 block text-xs text-secondary">确认新密码</span>
            <input
              type="password"
              value={confirmPassword}
              onChange={(event) => setConfirmPassword(event.target.value)}
              autoComplete="new-password"
              className="h-10 w-full rounded-btn border border-border bg-base px-3 text-sm text-foreground outline-none focus:border-accent/60"
            />
          </label>

          {formError && (
            <div className="flex items-start gap-2 rounded-btn bg-danger/10 px-3 py-2 text-xs text-danger">
              <ShieldAlert className="mt-px h-3.5 w-3.5 shrink-0" />
              <span>{formError}</span>
            </div>
          )}

          <button
            type="submit"
            disabled={passwordMutation.isPending || !oldPassword || !newPassword || !confirmPassword}
            className="inline-flex h-9 items-center justify-center gap-2 rounded-btn bg-accent px-4 text-xs font-semibold text-white hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {passwordMutation.isPending
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <CheckCircle2 className="h-3.5 w-3.5" />}
            保存新密码
          </button>
        </form>
      </section>

      <section className="mt-6 border-t border-border pt-5">
        <div className="flex items-center justify-between gap-4">
          <div>
            <h3 className="text-sm font-medium text-foreground">退出当前设备</h3>
            <p className="mt-1 text-[11px] text-muted">清除当前浏览器的登录会话</p>
          </div>
          <button
            type="button"
            onClick={() => logoutMutation.mutate()}
            disabled={logoutMutation.isPending}
            className="inline-flex h-9 items-center gap-2 rounded-btn border border-border px-3 text-xs text-secondary hover:bg-elevated hover:text-foreground disabled:opacity-50"
          >
            {logoutMutation.isPending
              ? <Loader2 className="h-3.5 w-3.5 animate-spin" />
              : <LogOut className="h-3.5 w-3.5" />}
            退出登录
          </button>
        </div>
      </section>
    </>
  )
}
