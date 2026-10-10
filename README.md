# TSP

面向沪深市场研究、数据治理、策略筛选和回测验证的一体化量化工作台。项目运行时
沿用 `TSP` 作为应用标识和 API Token 前缀。

[![CI](https://github.com/jelven007/TSP/actions/workflows/ci.yml/badge.svg)](https://github.com/jelven007/TSP/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

> 本系统用于数据分析和量化研究，不提供投资建议。回测结果不代表未来收益，
> 使用者需要自行核验数据授权、数据质量和交易风险。

## 项目定位

TSP 是持续维护的专用版本，当前重点是：

- 默认使用 `rustdx` 建立沪深行情数据层，保留 mootdx 等可选 Provider。
- 将选股、信号、因子、回测、监控和复盘统一到同一份标准化数据口径。
- 对策略结果提供可追溯的配置、数据版本、费用和风险指标。
- 同时支持本地桌面运行和服务端部署，并明确区分两种环境的认证边界。
- 保持生产级测试、发布、备份、升级和回滚文档。

本仓库仅保留产品、工程、部署和合规所需内容，不包含与运行无关的推广或个人入口。

## 主要能力

| 领域 | 能力 |
| --- | --- |
| 数据 | 日 K、分钟 K、实时行情、除权因子、财务、指数、ETF、扩展概念与行业 |
| 数据治理 | 标的 SCD2、历史 ST/风险警示治理、数据完整性检查、增量与盘后同步 |
| 研究 | 内置与自定义策略、信号库、因子检验、因子组合、样本外挖掘 |
| 回测 | T+1、手续费、卖出印花税、滑点、止损止盈、分钟回放、归因与导出 |
| 盘中 | 自选、实时行情、集合竞价、异动、连板梯队、监控与通知 |
| 交易辅助 | 持仓提醒、模拟盘、多账户对比、自动跟单规则 |
| AI | OpenAI、DeepSeek、GLM、Codex CLI 和自定义 OpenAI 兼容接口 |
| 开放能力 | API Token、Open API、SSE 事件流、MCP 服务器 |

## 运行模式

### 本地桌面版

Windows、macOS 和 Linux 桌面安装包采用本地免登录模式：

- 安装后直接进入主功能页面。
- 不显示注册、登录、账户管理和退出入口。
- 数据和敏感配置保存在本机。

桌面产物以当前仓库的
[GitHub Releases](https://github.com/jelven007/TSP/releases) 为准。

### 服务端部署

源码、Docker Compose 和 Web 部署保留邮箱账户认证：

- 首次部署可通过环境变量预置账户。
- 可通过注册口令和 SMTP 开放邮箱验证码注册。
- 公网部署必须使用 HTTPS，并限制 `.env`、数据目录和备份权限。

## 快速开始

### Docker Compose

前置要求：Docker Engine 和 Docker Compose。

```bash
git clone https://github.com/jelven007/TSP.git
cd TSP
cp .env.example .env
docker compose up --build -d
```

打开 <http://localhost:3018>。

当前文档默认从源码构建镜像，不依赖未验证的公共镜像标签。生产环境应从经过验收的
提交或版本标签构建，并记录最终镜像 digest。

### 开发模式

前置要求：

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

- 前端开发服务：<http://localhost:3011>
- 后端服务：<http://localhost:3018>

完整部署、反向代理、老 CPU 兼容和账户初始化说明见
[部署指南](./docs/deployment.md)。

## 首次配置

1. 打开“设置 → 数据源”，检测并选择日 K、实时行情、分钟 K 等能力的 Provider。
2. 打开“数据”页面，同步标的目录和历史行情，并检查数据画像。
3. 运行盘后管道，生成 enriched 指标数据。
4. 在“策略”和“回测”页面验证策略结果及费用口径。
5. 按需配置 AI、监控通知、Open API Token 和 MCP。

全新安装的七项基础数据能力默认路由到 `rustdx`，全市场请求最多共用 35 个连接，
已保存的路由保持原值。数据源实际可用性受公开服务器、
网络和协议覆盖影响，系统不会在失败时静默切换到其他数据源。

## 关键配置

从 `.env.example` 创建 `.env`。不要提交真实密钥。

```ini
# 服务
HOST=0.0.0.0
PORT=3018
DATA_DIR=./data

# 可选数据源
TICKFLOW_API_KEY=

# 可选 AI
AI_PROVIDER=openai_compat
AI_BASE_URL=https://api.deepseek.com/v1
AI_API_KEY=
AI_MODEL=deepseek-chat

# 可选内置扩展概念/行业上游
EXT_CONCEPT_DATA_URL=
EXT_INDUSTRY_DATA_URL=
```

扩展概念和扩展行业不再绑定任何个人服务。只有显式配置可信 JSON 上游后，新安装
才会启用自动拉取；调度保持交易日 `09:15-11:30`、`13:00-15:15` 每分钟一次。
已有用户保存的 URL、启停状态和调度设置不会被升级覆盖。

配置全集见[配置说明](./docs/configuration.md)。

## 数据与调度原则

- 实时行情：交易日 `09:15-11:30`、`13:00-15:15`，默认每秒一次。
- 盘中分钟增量：相同交易时段，默认每秒一轮；不支持增量能力的 Provider 会降级。
- 扩展概念/行业：配置上游后，交易日双时段每分钟一次。
- 盘后主管道：同步日线、除权因子、分钟数据并重算 enriched 指标。
- 所有边界采用半开区间，午休期间不拉取。

`data/` 保存运行时行情、策略、回测、账户和系统配置，不纳入 Git。升级、迁移和
回滚前必须完整备份该目录。

## 回测正确性

生产研究应至少满足：

- 使用历史时点可见的数据，禁止未来函数。
- 纳入历史 ST/风险警示治理数据。
- 纳入佣金、滑点和卖出印花税。
- 遵守 A 股 T+1、停牌和涨跌停约束。
- 跨越完整市场周期评估，不以单一年度结果代替稳定性结论。
- 对比结果时确认策略源码、参数和数据 release/config hash 一致。

详细规则见[贡献与复审指南](./CONTRIBUTING.md)和
[测试与验收](./docs/testing.md)。

## 架构摘要

```text
数据源 / 插件
  -> Provider 能力路由与标准化
  -> Parquet / DuckDB 数据层
  -> enriched 指标流水线
  -> 策略 / 因子 / 回测 / 监控 / 分析服务
  -> FastAPI / SSE / Open API
  -> React 前端 / MCP 客户端
```

后端采用模块化单体。主服务负责 API、调度和数据管理，重计算任务可使用隔离子进程。
前端使用 React、TypeScript、Vite、TanStack Query 和 Tailwind CSS。

完整边界见[架构文档](./docs/architecture.md)。

## 验证

后端：

```bash
cd backend
uv sync --extra backtest --extra rustdx
uv run --no-sync pytest
```

前端：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm test -- --run
pnpm lint
pnpm build
```

提交前还应执行：

```bash
git diff --check
```

## 文档

| 文档 | 用途 |
| --- | --- |
| [文档中心](./docs/README.md) | 文档地图、事实源和更新规则 |
| [操作说明书](./操作说明书.md) | 页面操作、维护和排错 |
| [部署指南](./docs/deployment.md) | Compose、开发和服务端部署 |
| [配置说明](./docs/configuration.md) | 环境变量和页面配置 |
| [功能说明](./docs/features.md) | 当前功能边界 |
| [架构文档](./docs/architecture.md) | 模块、数据流和故障边界 |
| [运维手册](./docs/operations.md) | 巡检、备份、恢复和故障处理 |
| [发布流程](./docs/release-operations.md) | 版本、产物、上线和回滚 |
| [二次开发](./docs/secondary-development.md) | 扩展点和核心修改规则 |
| [mootdx 数据源](./docs/mootdx-data-source.md) | 数据能力、限制和验证 |
| [rustdx 数据源](./docs/rustdx-data-source.md) | 默认路由、35 连接池和历史财务 |
| [策略开发](./docs/strategy.md) | 内置、自定义和 AI 策略 |
| [MCP 服务器](./mcp-server/README.md) | AI 客户端接入 |

## 第三方组件与数据源

项目会按配置使用第三方依赖和数据服务，包括 `mootdx`、TickFlow、fuyao、OpenAI、
DeepSeek、GLM 等。仓库中的链接用于说明功能或许可证，不代表合作、背书或可用性
承诺。部署者必须自行确认服务条款、数据授权、隐私政策和网络可达性。

第三方许可证和翻译归属属于法定或上游许可信息，保留在相应文件中。

## License

本项目代码按 [MIT License](./LICENSE) 发布。第三方依赖及资源适用各自许可证。
