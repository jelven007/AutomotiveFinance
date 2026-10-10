# 配置详解

所有配置从根目录 `.env` 读取(复制 `.env.example` 开始),也可在面板 **设置** 页面可视化修改。本文件解释每个配置项的作用。

部署相关配置(端口/密码/老 CPU 兼容)的实操见 [deployment.md](./deployment.md)。

---

## 数据源

```ini
TICKFLOW_API_KEY=              # 留空 = None 模式(历史日K免费);填 Key = 按订阅档位解锁
```

项目首次启动时七项数据能力默认路由到 `rustdx`;保留已有保存路由，同时支持切换到 mootdx、TickFlow、
fuyao 或自定义数据源(YAML 声明自有接口见
[custom-data-source.md](./custom-data-source.md),插件开发见
[plugin-development.md](./plugin-development.md))。

仓库内置的 `mootdx` 插件无需 API Key，可提供日K、除权因子、实时行情、
分钟K、五档盘口、财务和全量分钟七项能力。Docker 镜像已预装依赖；源码环境
缺少依赖时可在数据源卡片安装。公开通达信服务器可能限流或不可达，安装、
环境变量、单位校准、竞价采集及覆盖边界见
[mootdx 数据源](./mootdx-data-source.md)。
仓库默认的 `rustdx` 插件使用原生 Rust 协议引擎，七类能力共用最多
35 条持久连接，历史财务也通过 rustdx 下载并独立解析。安装、连接池和
能力限制见 [rustdx 数据源](./rustdx-data-source.md)。
选择任一非 TickFlow 源后，请求失败不会静默切回 TickFlow。

- **留空(None 模式)**:通过 free-api 使用历史日 K(当日数据盘后 1-2 小时可用),**无需付费**即可体验核心选股/回测功能
- **填入 API Key**:按你的订阅档位解锁更多能力

### 实时行情按档位

| 档位     | 实时能力                                 |
| :------- | :--------------------------------------- |
| Free     | 自选页前 5 个标的实时监控(最低 6 秒刷新) |
| Starter+ | 全市场实时行情                           |
| Pro      | 分钟 K + 盘口                            |
| Expert   | WebSocket + 财务数据 + 全量分钟          |

> 完整能力矩阵见 [tickflow.org/pricing](https://tickflow.org/pricing/),高等档位含较低档全部权益。
> 在面板 **设置 → 凭据与能力** 点「重新检测」可查看当前档位标签。
>
> **档位仅适用于 TickFlow 数据源**。功能门槛的统一标准是"能力"(`kline.minute.batch`、`depth5.batch`、`financial` 等能力键):其他第三方/自定义数据源以声明的数据集能力为准,系统会按当前数据源配置自动合并判定,UI 提示一律以能力名表达,不再依赖 TickFlow 档位名。

### 全量分钟 (full_minute)

「全量分钟」是一项**独立能力**(能力键 `full_minute`,探测名 `intraday.universe`),与其他能力同样**可路由**:盘中把全市场当日 1 分钟 K 持续增量落盘到本地 `data/kline_minute/` 当日分区,分钟策略(`minute_filter`)与分时视图即可读到新鲜数据。接入方式二选一:

- **TickFlow Expert**:配置 Expert 档 Key,零配置即用(修复轮 `intraday.batch` + 稳态 `intraday.universe` 单请求增量)
- **插件/自定义源**:声明 `full_minute` 数据集并在 **设置 → 数据源 → 全量分钟** 路由到该源 — Python 插件实现 `get_intraday_batch`(必需)/`get_intraday_latest`(可选,未实现自动降级仅修复轮、节奏下限 60s);YAML 声明式源数据集配置与 `minute` 同形(仅修复轮语义)。契约细节见 [plugin-development.md](./plugin-development.md) 与 [custom-data-source.md](./custom-data-source.md)

接入步骤:

1. **设置 → 凭据与能力**(TickFlow 路径)配置 API Key(Expert 档),或在 **设置 → 数据源** 声明/安装提供 `full_minute` 的源并路由;点「重新检测」后能力列表出现「全量分钟」
2. 落盘服务默认开启；仅在交易日 09:15–11:30 / 13:00–15:15 运行，午休和 15:15 后自动暂停
3. 冷启动(如 10 点才开服务)自动触发**全天修复轮**,一次批量补齐当日缺口;稳态走**增量轮**(默认 1 秒一轮,可配 1–120 秒),幂等合并滚出全天
4. 与盘后分钟同步写同一分区(`unique(symbol, datetime)` 幂等合并),互不冲突

说明:标的池为 A 股股票(CN_Equity_A),ETF 不在内(分时走批量补拉路径);覆盖滞后超阈值或连续空轮会自动再跑修复轮自愈。

---

## AI(可选)

用于自然语言生成策略。**所有配置留空即跳过**,不影响核心功能。支持任意 OpenAI 兼容接口。

```ini
AI_PROVIDER=openai_compat              # openai_compat | ollama
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=                            # 留空 = 关闭 AI
AI_MODEL=deepseek-chat
AI_DAILY_TOKEN_BUDGET=500000           # 每日 token 预算上限
```

| 配置项 | 说明 |
| :--- | :--- |
| `AI_PROVIDER` | `openai_compat`(OpenAI 兼容,支持 DeepSeek / GLM / OpenAI 等)或 `ollama`(本地模型) |
| `AI_BASE_URL` | 接口地址,如 DeepSeek `https://api.deepseek.com/v1` |
| `AI_API_KEY` | 留空则关闭 AI 功能 |
| `AI_MODEL` | 模型名,如 `deepseek-chat` |
| `AI_DAILY_TOKEN_BUDGET` | 每日 token 预算,超限后当日不再调用 |

页面预设包括自定义、OpenAI、DeepSeek、GLM 和 Codex CLI。
通义千问与 Kimi 不再单列预设；已有其他 OpenAI 兼容配置会显示为“自定义”，
其地址、模型和密钥仍按原配置使用。

接入示例见 [strategy.md](./strategy.md) 的「AI 生成策略」章节。

---

## 内置扩展概念与行业(可选)

```ini
EXT_CONCEPT_DATA_URL=
EXT_INDUSTRY_DATA_URL=
```

两项分别配置扩展概念和扩展行业的 JSON 上游。项目不再内置个人服务地址：

- URL 为空时仍创建预设表结构，但自动拉取默认关闭。
- 配置 URL 后，新安装默认在交易日 `09:15-11:30`、`13:00-15:15`
  每分钟拉取一次。
- 调度器启动后会立即执行首轮，之后受交易日和双时段门控。
- 已存在的用户配置不会因环境变量或版本升级被覆盖。
- 上游必须由部署者自行确认授权、稳定性、字段契约和安全性。

预期概念响应行为 `{symbol, name, concepts: [...]}`，行业响应行为
`{symbol, name, industries: [...]}`；接口可直接返回数组，也可使用常见
`data`、`list`、`rows` 等信封字段。

---

## 服务

```ini
HOST=0.0.0.0          # 开发服务监听地址 / Docker 主机绑定地址
PORT=3018             # 开发后端端口 / Docker 主机映射端口
LOG_LEVEL=INFO        # DEBUG | INFO | WARNING | ERROR
```

- `HOST`:`0.0.0.0` 监听所有网卡(容器/公网部署需要);仅本机用可设 `127.0.0.1`
- `PORT`:默认 `3018`;开发模式兼容显式的 `BACKEND_PORT` 覆盖,改端口后 SSH 转发命令也要同步改
- `LOG_LEVEL`:排查问题时改 `DEBUG`

---

## 数据

```ini
DATA_DIR=./data       # Parquet / DuckDB 数据存储目录
```

整个 `data/` 目录都不纳入 git —— 行情 K线、财务、自选、回测、监控记录,乃至概念/行业扩展数据,全部是程序运行时生成/拉取的用户数据。

如需迁移数据,直接拷贝整个 `data/` 目录即可。详见 [deployment.md → 更新代码](./deployment.md#更新代码已部署用户必读)。

---

## 邮箱账户(公网部署)

```ini
AUTH_EMAIL='admin@example.com'
AUTH_PASSWORD='你的密码'  # 至少 8 位; 仅首次生效

# 浏览器注册口令与验证码 SMTP
AUTH_REGISTRATION_SECRET='请设置注册口令'  # 仅用于首次生成哈希
AUTH_SMTP_HOST='smtp.example.com'
AUTH_SMTP_PORT=465
AUTH_SMTP_SECURITY='ssl'  # ssl | starttls | none
AUTH_SMTP_USERNAME='no-reply@example.com'
AUTH_SMTP_PASSWORD='SMTP 密码或授权码'
AUTH_SMTP_FROM_ADDRESS='no-reply@example.com'
```

`AUTH_EMAIL` 与 `AUTH_PASSWORD` 同时设置时, 服务首次启动会预置邮箱账户。预置账户是部署初始化, 不要求验证码。已有账户后环境变量不再覆盖页面中维护的密码。

浏览器注册会先校验注册口令, 正确后才发送邮件验证码。`AUTH_REGISTRATION_SECRET` 首次启动后会以带随机盐的 PBKDF2 哈希保存到 `auth.json`, 确认成功后可从 `.env` 删除明文并重启。全新部署还需配置 `AUTH_SMTP_*`; `AUTH_SMTP_HOST` 留空时会尝试复用设置页已经保存的邮件 SMTP。验证码 10 分钟有效、60 秒后可重发, 连续输错 5 次后失效。

只配置 `AUTH_PASSWORD` 时保留旧版兼容模式, 首次访问需使用原密码绑定邮箱。密码建议使用单引号包裹; `.env` 文件权限应保持 `600`。

详细步骤和旧版迁移方法见[邮箱账户初始化](./deploy-password.md)。

---

## 后端依赖 Extras(可选)

```ini
BACKEND_EXTRAS=             # 留空默认;legacy-cpu 兼容老 CPU
```

老 CPU 无 AVX2/FMA 支持时设为 `legacy-cpu`,会给 Polars 切到 `rtcompat` 运行时;需回测则 `legacy-cpu backtest`。Docker 构建和 `./dev.sh` / `.\dev.ps1` 都会读取此值并同步依赖。详见 [deployment.md → 老 CPU 兼容](./deployment.md#老-cpu-兼容avx2fma-缺失)。

---

## 配置优先级

1. **面板设置页**(`设置 → ...`):UI 修改后立即生效,持久化到 `data/`
2. **`.env` 文件**:启动时读取
3. **环境变量**:Docker / 系统环境变量,优先级最高

> 多数配置可在面板设置页修改,无需手动编辑 `.env`。仅 AI Key、API Key 等敏感项建议放 `.env`(不提交到 git)。
