# TSP 文档中心

本文档是项目文档的统一入口。当前基线版本为 `0.3.3`，代码、测试和运行时
行为仍是最终事实来源；文档与代码冲突时，应先核对实现和测试，再同步修正文档。

## 生命周期覆盖

| 阶段 | 主文档 | 配套文档 | 覆盖状态 |
| --- | --- | --- | --- |
| 需求 | [需求基线](./requirements.md) | [功能手册](./features.md)、[操作说明书](../操作说明书.md)、各专项方案 | 完整 |
| 设计 | [当前架构](./architecture.md) | [开放平台](./open-platform-plan.md)、[因子系统](./factor-system-design.md)、[模拟盘](./paper-trading-plan.md)、[市场阶段](./market-phase.md) | 完整 |
| 开发 | [贡献与复审指南](../CONTRIBUTING.md) | [二次开发](./secondary-development.md)、[插件开发](./plugin-development.md)、[策略开发](./strategy.md) | 完整 |
| 测试 | [测试与验收](./testing.md) | [技术验证记录](./technical-validation.md)、专项文档内测试矩阵、CI workflow | 完整 |
| 运维 | [运维手册](./operations.md) | [部署](./deployment.md)、[配置](./configuration.md)、[公网密码](./deploy-password.md) | 完整 |
| 上线运营 | [发布与上线运营](./release-operations.md) | GitHub Actions、Release、GHCR、反馈与复盘流程 | 完整 |

这里的“完整”表示每个阶段已有明确责任文档、输入、输出和检查项，不表示所有流程均已
自动化。当前人工门禁和自动化缺口见[发布与上线运营](./release-operations.md)。

## 专项文档索引

| 领域 | 文档 |
| --- | --- |
| 产品与使用 | [功能手册](./features.md)、[操作说明书](../操作说明书.md) |
| 部署与配置 | [部署指南](./deployment.md)、[配置说明](./configuration.md)、[公网访问密码](./deploy-password.md) |
| 数据源 | [自定义数据源](./custom-data-source.md)、[mootdx 数据源](./mootdx-data-source.md)、[插件开发](./plugin-development.md) |
| TickFlow 专项 | [Pro 一阶段探测](./tickflow-pro-phase1-probe.md)、[共享限流](./tickflow-pro-shared-rate-limit.md) |
| 策略与研究 | [策略开发](./strategy.md)、[策略迭代](./strategy-iteration.md)、[因子平台方案](./factor-platform-plan.md)、[因子系统设计](./factor-system-design.md)、[因子挖掘](./mining.md) |
| 市场与交易 | [市场阶段](./market-phase.md)、[模拟盘](./paper-trading-plan.md) |
| 开放能力 | [开放平台](./open-platform-plan.md)、[MCP 服务器](../mcp-server/README.md)、[Open API 示例](../examples/open-api/README.md) |
| 验证记录 | [技术验证记录](./technical-validation.md) |
| 二次开发 | [二次开发指南](./secondary-development.md)、[自定义数据源示例](./examples/custom-data-source/README.md) |

## 文档类型与状态

不同文档承担不同职责，不应把设计目标、历史验证结果和当前可用功能混为一谈。

| 类型 | 含义 | 更新要求 |
| --- | --- | --- |
| 基线 | 当前产品目标、范围、验收标准或架构事实 | 行为变化必须同步 |
| 使用说明 | 面向部署者或用户的实际操作 | UI、配置、路径或限制变化时同步 |
| 开发规范 | 代码边界、数据契约和提交标准 | 工程规则变化时同步 |
| 设计方案 | 同时包含现状与目标设计 | 每项必须明确标注“已实现”或“设计” |
| 验证记录 | 某次版本、数据窗口和环境下的证据 | 保留日期和版本，不覆盖成永久结论 |
| Runbook | 可直接执行的部署、恢复和故障处理步骤 | 命令、端口、目录或流程变化时同步 |

## 单一事实源

发生冲突时按以下顺序判断：

1. 可执行代码、数据 schema 和公开 API 契约。
2. 自动化测试及固定契约快照。
3. [CONTRIBUTING.md](../CONTRIBUTING.md) 中的数据、并发和兼容规则。
4. 当前基线文档。
5. 专项设计方案和历史验证记录。

版本以 `frontend/package.json` 为发布流水线权威输入，并与
`backend/pyproject.toml`、`backend/app/__init__.py` 的 fallback 和根目录
`VERSION` 保持一致。MCP 服务器是独立包，可单独维护版本号。

## 按角色阅读

| 角色 | 建议顺序 |
| --- | --- |
| 产品/需求负责人 | 需求基线 → 功能手册 → 专项方案 |
| 架构/技术负责人 | 当前架构 → CONTRIBUTING → 专项设计 |
| 开发者 | CONTRIBUTING → 二次开发 → 目标模块专项文档 → 测试与验收 |
| 测试人员 | 需求基线验收项 → 测试与验收 → 技术验证记录 |
| 部署/运维人员 | 部署 → 配置 → 运维手册 → 发布与上线运营 |
| 普通使用者 | README → 操作说明书 → 功能手册 |

## 变更同步规则

以下变化不能只改代码：

- 用户流程、页面入口或功能边界变化：更新 `README.md`、`features.md` 和
  `操作说明书.md` 中受影响部分。
- 数据字段、单位、时点或 Provider 能力变化：更新 `CONTRIBUTING.md` 和对应
  数据源/插件文档。
- 架构边界、部署拓扑或缓存一致性变化：更新 `architecture.md`。
- 测试命令、CI 门禁或验收标准变化：更新 `testing.md`。
- 端口、环境变量、目录、调度任务、备份恢复变化：更新 `operations.md`、
  `configuration.md` 或 `deployment.md`。
- 发版产物、版本规则、灰度或回滚流程变化：更新 `release-operations.md`。

PR 描述应列出“文档影响”；确认无影响时也应明确写“无文档变更”。
