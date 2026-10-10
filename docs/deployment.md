# 部署指南

本文覆盖源码部署、Docker Compose、开发模式和桌面安装包。配置项见
[配置说明](./configuration.md)，备份与恢复见[运维手册](./operations.md)，
正式放行见[发布流程](./release-operations.md)。

## 部署方式

| 方式 | 适用场景 | 认证模式 |
| --- | --- | --- |
| Docker Compose | 服务器、NAS、长期运行 | 保留邮箱账户认证 |
| Dev 模式 | 二次开发和本地调试 | 保留邮箱账户认证 |
| 桌面安装包 | 单机直接使用 | 本地免登录 |

当前仓库文档默认从源码构建容器，不依赖未经当前项目验证的公共镜像标签。

## Docker Compose

### 前置要求

- Git
- Docker Engine
- Docker Compose v2
- 建议至少 4 GB 可用内存和足够的数据盘空间

### 首次部署

```bash
git clone https://github.com/jelven007/TSP.git
cd TSP
cp .env.example .env
docker compose up --build -d
```

访问 <http://localhost:3018>。

查看状态和日志：

```bash
docker compose ps
docker compose logs -f app
curl -fsS http://localhost:3018/health/live
curl -fsS http://localhost:3018/health/ready
```

Compose 会把以下内容挂载到容器：

- `./data` → `/app/data`
- `./tiers.yaml` → `/app/tiers.yaml`
- `./.env` → `/app/.env`
- 主机 Codex 配置目录 → `/root/.codex`，只读

生产环境应限制 `.env` 和 `data/` 的文件权限，并把数据目录放在有备份和容量监控
的磁盘上。

### 更新

先备份 `data/`、`.env` 和自定义插件，再执行：

```bash
git pull --ff-only
docker compose up --build -d
docker compose logs -f app
```

更新后检查健康端点、版本、数据画像、Provider 能力、策略、回测和关键调度任务。

### 停止

```bash
docker compose down
```

不要使用 `docker compose down -v`，除非已经确认卷中没有需要保留的数据。

## Dev 模式

### 前置要求

- Python 3.11+
- Node.js 20+
- [`uv`](https://docs.astral.sh/uv/)
- pnpm 9

macOS / Linux：

```bash
cp .env.example .env
./dev.sh
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
.\dev.ps1
```

默认地址：

- 前端：<http://localhost:3011>
- 后端：<http://localhost:3018>

自定义端口：

```bash
BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh
```

手动分别启动：

```bash
cd backend
uv sync --extra backtest
uv run uvicorn app.main:app --reload --port 3018
```

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

## 桌面安装包

桌面安装包从当前仓库的
[GitHub Releases](https://github.com/jelven007/TSP/releases) 获取。
仅使用实际存在且校验通过的平台资产。

桌面版与服务端版的明确差异：

- Windows、macOS、Linux 桌面版安装后直接进入系统。
- 桌面版不注册账户、不登录、不发送注册验证码。
- 桌面版隐藏账户管理和退出登录入口。
- 服务端和 Dev 模式继续保留认证。

覆盖安装前仍应备份安装目录下的 `data/`。安装包不会主动删除运行时数据，但备份是
升级和回滚的前置条件。

## 账户初始化

服务端部署可在 `.env` 中预置首个账户：

```ini
AUTH_EMAIL='admin@example.com'
AUTH_PASSWORD='至少八位的密码'
```

两项同时配置时，首次启动会创建邮箱账户；已有账户后不会覆盖页面中维护的密码。

要开放浏览器注册，还需配置注册口令和可用 SMTP：

```ini
AUTH_REGISTRATION_SECRET='请设置注册口令'
AUTH_SMTP_HOST='smtp.example.com'
AUTH_SMTP_PORT=465
AUTH_SMTP_SECURITY='ssl'
AUTH_SMTP_USERNAME='no-reply@example.com'
AUTH_SMTP_PASSWORD='SMTP 密码或授权码'
AUTH_SMTP_FROM_ADDRESS='no-reply@example.com'
```

注册口令首次启动后以带随机盐的 PBKDF2 哈希写入 `auth.json`。确认初始化成功后，
可从 `.env` 删除明文口令并重启。SMTP 配置必须通过真实发信测试后再用于注册。

公网部署要求：

- 使用 HTTPS。
- `.env` 权限设为 `600`。
- SMTP 使用独立授权码。
- 限制反向代理请求体、超时和访问来源。
- 定期备份 `data/user_data/auth.json` 和完整 `data/`。

完整账户流程见[邮箱账户初始化](./deploy-password.md)。

## 扩展概念与行业上游

新安装不再隐式连接任何个人服务。需要内置扩展概念/行业自动拉取时，在 `.env`
中配置自有或已授权的 JSON 接口：

```ini
EXT_CONCEPT_DATA_URL='https://data.example.com/concepts'
EXT_INDUSTRY_DATA_URL='https://data.example.com/industries'
```

配置后，新建预设会在交易日 `09:15-11:30`、`13:00-15:15` 每分钟拉取，
并在调度器启动时执行首轮。未配置时预设保留但默认关闭。已有用户配置不被覆盖。

## 老 CPU 兼容

CPU 不支持 AVX2/FMA、进程出现 `exit 132` 时，在 `.env` 设置：

```ini
BACKEND_EXTRAS=legacy-cpu
```

同时需要回测依赖：

```ini
BACKEND_EXTRAS=legacy-cpu backtest
```

然后重新执行 `docker compose up --build -d` 或启动脚本。不要用
`POLARS_SKIP_CPU_CHECK` 隐藏硬件不兼容。

## Codex CLI

Compose 默认只读挂载 `${HOME}/.codex`。Windows 的 `HOME` 可能未设置，可在
`.env` 指定：

```ini
CODEX_HOME_HOST=C:\Users\your-name\.codex
```

覆盖容器内 Codex CLI 版本：

```bash
CODEX_CLI_VERSION=0.144.3 docker compose up --build -d
```

只在可信主机启用 Codex 登录态挂载。

## 数据安全

整个 `data/` 都是运行时数据，不纳入 Git。禁止用以下命令处理升级冲突：

```text
git clean -fdx
git reset --hard
```

前者会删除被 `.gitignore` 排除的运行时数据，后者会丢弃未提交代码。遇到拉取
冲突时先停止服务、备份，再使用 `git status`、`git diff` 和非破坏性方式处理。

## 反向代理

公网部署至少需要：

- HTTPS 终止。
- 正确转发 `X-Forwarded-Proto`。
- 支持 SSE 长连接并关闭代理缓冲。
- 为回测和同步接口设置合理超时。
- 不直接暴露数据目录和配置文件。

反向代理上线后重新验证登录、SSE、回测进度、文件下载和更新检查。
