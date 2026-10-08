# 部署指南

本项目的几种运行方式，按推荐程度排序。配置项详解见
[配置说明](./configuration.md)；生产巡检、备份与恢复见
[运维手册](./operations.md)；正式版本放行和回滚见
[发布与上线运营](./release-operations.md)。

> 📌 前置依赖(仅方式 D 需要):Python ≥ 3.11 · Node ≥ 20 · [`uv`](https://docs.astral.sh/uv/) · `pnpm`（`npm i -g pnpm`）

---

## 方式 A:GHCR 现成镜像(免本地构建,多数用户推荐)

GitHub Actions 在 `main` 的同一提交通过 CI 后自动构建多架构镜像
(linux/amd64 · arm64)并发布到 GHCR，直接拉取运行，本地无需 Python / Node，
也不用现场 build：

```bash
docker run -d --name tsp -p 3018:3018 -v ${PWD}/data:/app/data ghcr.io/shy3130/tick-stock-panel:latest
# 打开 http://localhost:3018
```

- 需要配置时:从 `.env.example` 复制出 `.env`,命令里加 `--env-file .env`。
- `latest` 会被 `main` 的每次成功 CI 刷新，只适合快速体验；生产环境应在验收后
  固定 `v*` 版本标签、提交 SHA 标签或镜像 digest。
- 镜像默认不含 `legacy-cpu` / `backtest` extras；老 CPU(无 AVX2)或需要
  vectorbt 回测时，请用方式 B 通过 `BACKEND_EXTRAS` 自构建。
- 跑自己改过的代码:fork 后到仓库 Actions 页启用 workflow(fork 默认禁用),构建出的 `ghcr.io/<你的用户名>/tick-stock-panel` 用法相同。
- 想要 compose 全套挂载(`.env` / `tiers.yaml` / 数据卷):参考根目录 `docker-compose.yml`,把 `build:` 段换成 `image: ghcr.io/shy3130/tick-stock-panel:latest`。

更新到新版本:

```bash
docker pull ghcr.io/shy3130/tick-stock-panel:latest
docker rm -f tsp
# 重新执行上面的 docker run
```

---


## 方式 B:Docker Compose(本地构建,全套挂载)

```bash
cp .env.example .env
docker compose up --build
# 打开 http://localhost:3018
```

Docker 采用两阶段构建,前端 dist 拷进后端镜像,**单容器**运行,数据完全在自己手里。

更新到新版本:

```bash
git pull
docker compose up --build -d
```

---

## 方式 C:本机 AI 代部署(小白推荐)

装一个本机 AI 编程助手(Trae / Codex / OpenCode / ZCode / WorkBuddy 等,任选其一),把 [README · 快速开始](../README.md#-快速开始) 里方式 C 的提示词原样发给它,AI 会自动完成克隆、装依赖、启动服务。适合完全不想碰命令行的用户;AI 最终执行的仍是方式 A / B / D 之一。

---


## 方式 D:Dev 模式(二次开发推荐)

由于刚开源近期更新频繁,推荐开发模式运行,可随时 `git pull` 同步最新代码。

```bash
git clone https://github.com/shy3130/tick-stock-panel.git
cd tick-stock-panel
cp .env.example .env       # 按需填 TICKFLOW_API_KEY(留空 = None 模式)
./dev.sh                   # Windows: .\dev.ps1
```

`dev.sh` 自动检查 / 下载依赖、释放端口、同时起前后端,Ctrl-C 一并关闭。默认:

- 后端 → <http://localhost:3018> · 前端 → <http://localhost:3011>
- 自定义端口:`BACKEND_PORT=8000 FRONTEND_PORT=5173 ./dev.sh`

### 手动分别启动(不想用 dev.sh)

```bash
# 后端
cd backend && uv sync --extra backtest   # 含回测依赖
# 老 CPU: uv sync --extra legacy-cpu
# 老 CPU + 回测: uv sync --extra legacy-cpu --extra backtest
uv run uvicorn app.main:app --reload --port 3018

# 前端
cd frontend && pnpm install && pnpm dev   # http://localhost:3011
```

---

## 老 CPU 兼容(avx2/fma 缺失)

如果运行时报 `avx2`/`fma` 缺失,或进程 `exit 132`,说明 CPU 不支持 AVX2 指令集(常见于老 VPS)。解决:

- **Dev 源码启动**:在根目录 `.env` 设置后运行 `./dev.sh` 或 Windows 的 `.\dev.ps1`;即使已有 `.venv`,启动器也会同步兼容内核
- **Docker**:在根目录 `.env` 设置后执行 `docker compose up --build`

```ini
BACKEND_EXTRAS=legacy-cpu          # 兼容老 CPU
BACKEND_EXTRAS=legacy-cpu backtest # 兼容老 CPU + 回测依赖
```

手动启动源码时，也可以在 `backend/` 目录直接执行 `uv sync --extra legacy-cpu`。不要设置 `POLARS_SKIP_CPU_CHECK`，它只会隐藏警告，实际执行不支持的指令时仍可能崩溃。

### 回测依赖说明

vectorbt → numba 体积较大,作为可选 extras(`uv sync --extra backtest`)。macOS / Intel 无预构建 wheel 时需 `brew install cmake` 现场编译。

---

## 更新代码(已部署用户必读)

拉取新版本只需一条命令(Dev / Compose 本地构建用户):

```bash
git pull
```

> 用方式 A 镜像直跑(无本地仓库)的用户:`docker pull ghcr.io/shy3130/tick-stock-panel:latest` 后删除旧容器重跑;compose 换 `image:` 的用户执行 `docker compose pull && docker compose up -d`。

**整个 `data/` 目录都不纳入 git** —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据,`git pull` 物理上无法影响它们。新用户每次启动时,概念/行业两份扩展数据会在后台立即拉取一次,此后仅在 A 股交易日北京时间 09:00-16:00 每 30 分钟刷新;已有用户保存的启停和调度设置不会被升级覆盖。

> ⚠️ **切勿使用以下命令"解决冲突"或"清理",它们会一次性删光 `data/` 下所有未被 git 跟踪的数据:**
> - `git clean -fdx`(最危险,会删掉所有 `.gitignore` 忽略的文件)
> - `git reset --hard`
> - 直接删除整个项目文件夹重新 `git clone`
>
> 若 `git pull` 报冲突,通常是本地误改了被跟踪的文件,请先 `git stash` 暂存再 pull,或单独联系作者,不要直接执行上面的命令。

---

## 邮箱账户设置(公网部署必读)

首次打开系统可使用邮箱注册。注册前必须发送并校验 6 位邮箱验证码, 验证码 10 分钟有效。多个账户共享同一套业务数据和系统配置。

自动化部署可在 `.env` 中预置首个账户:

```bash
AUTH_EMAIL='admin@example.com'
AUTH_PASSWORD='至少八位的密码'
```

两项同时配置时, 服务首次启动会创建邮箱账户。该部署初始化不要求验证码; 已有账户时会跳过, 不会覆盖页面中修改过的密码。

要开放浏览器注册, 全新部署还需配置发信 SMTP:

```bash
AUTH_SMTP_HOST='smtp.example.com'
AUTH_SMTP_PORT=465
AUTH_SMTP_SECURITY='ssl'
AUTH_SMTP_USERNAME='no-reply@example.com'
AUTH_SMTP_PASSWORD='SMTP 密码或授权码'
AUTH_SMTP_FROM_ADDRESS='no-reply@example.com'
```

`AUTH_SMTP_SECURITY` 可选 `ssl`、`starttls` 或 `none`。`AUTH_SMTP_HOST` 留空时会尝试复用设置页保存的邮件 SMTP, 但全新安装尚不能进入设置页, 因此应配置环境变量或先预置首个账户。

只配置 `AUTH_PASSWORD` 的旧部署会进入兼容模式。登录页要求使用原密码绑定一个邮箱, 绑定过程不会修改行情、策略、自选或系统设置。

**注意事项:**

- `.env` 文件权限保持 `600`, 不要提交到 Git; SMTP 优先使用独立授权码。
- 密码含 `$` 等字符时建议使用单引号, 避免 Docker Compose 插值。
- 公网部署必须通过 HTTPS 访问, 会话 cookie 在 HTTPS 反向代理下自动启用 `Secure`。
- 后续改密码使用 `设置 → 账户`, 修改后该账户的所有会话都会退出。

忘记密码时, 当前验证码不用于密码找回。停服并备份后可删除 `data/user_data/auth.json`, 重启后重新注册或通过环境变量预置:

```bash
cp data/user_data/auth.json data/user_data/auth.json.bak
rm data/user_data/auth.json
```

删除认证文件会移除全部登录账户, 但不会删除其他业务数据。完整说明见[邮箱账户初始化](./deploy-password.md)。
