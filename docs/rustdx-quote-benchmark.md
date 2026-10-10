# rustdx 行情连接数压测

脚本 [`backend/scripts/benchmark_rustdx_quotes.py`](../backend/scripts/benchmark_rustdx_quotes.py)
对同一份本地股票池逐档测量 8–30 条持久连接的全量行情耗时。默认每个整数档位均测试，
每档单独创建进程与连接池，关闭后再运行下一档。

## 运行

在已安装 rustdx 原生扩展的源码环境中，从项目根目录执行：

```bash
cd backend

# 参数和股票池预检，不连接服务器、不写结果
uv run --no-sync python scripts/benchmark_rustdx_quotes.py --dry-run

# 23 个档位；每档冷启动 1 轮、预热 5 轮、正式采样 100 轮
uv run --no-sync python scripts/benchmark_rustdx_quotes.py --connections 8-30

# 以 1000ms 为 P95 验收上限
uv run --no-sync python scripts/benchmark_rustdx_quotes.py --connections 8-30 --max-p95-ms 1000

# 先做少量真实网络冒烟验证，不作为 P95 验收
uv run --no-sync python scripts/benchmark_rustdx_quotes.py --connections 8,30 --rounds 3 --warmup 1
```

默认轮次开始间隔为 1 秒，完整运行约 41 分钟起；慢轮次会增加总时间。上一轮完成后
才开始下一轮，绝不重叠。`--interval 0` 用于连续压测；其他档位可写为
`--connections 8,12,16,20,24,30`。需要更多尾部样本时设置 `--rounds 300`。

输入固定为 `data/instruments/instruments.parquet`，使用现有的股票身份与退市过滤规则，
保留有效沪深主板、科创板、创业板股票，包含 ST 和停牌股票；排除指数、ETF、B 股和
不支持的市场。`--data-dir` 指定另一个数据目录。股票池在父进程一次性读取并去重，
所有档位传入相同列表；不会在压测中重新请求证券目录。

`--server IP:PORT` 固定全部档位的行情节点，默认读取当前 `RUSTDX_SERVER`，未配置时
使用内置节点。`--socket-timeout` 默认为 8 秒，`--round-timeout` 默认为整轮 60 秒；
整轮超时后终止该档进程、保留已完成样本，再继续下一档。Ctrl+C 会回收当前进程并保存结果。
建议在相同机器、节点、股票池和时段下比较；同时运行的其他采集进程会影响节点负载。

脚本仅发起独立行情请求并写压测报告，不启动后端服务、调度器或修改用户采集配置，
也不落盘行情、股票池和历史数据。源码 CLI 可在 macOS、Linux、Windows 上运行，
各平台需安装对应的 rustdx 原生扩展。

## 测量口径

测量 `RustdxProvider.get_auction_snapshot(固定股票列表)` 调用。这与实时行情使用相同的
原生分批采集与字段标准化路径，额外保留盘口和单票事件时间。耗时包含网络、冷启动时
的建连、原生返回、JSON 解码和 Provider 标准化；不包含目录同步、股票名称查询、
覆盖率检查、磁盘写入、SSE、前端及策略计算。冷启动和预热不计入正式分位数。

正式轮次要求全部股票唯一返回、价格非空且有限，且无多余记录。停牌或尚未成交股票
允许零价。缺股票、重复股票、无效价格、调用异常都不能算成功：

- `p50_ms`、`p95_ms`、`p99_ms`：仅对完整成功的正式轮次计算，使用 nearest-rank，
  即排序后取第 `ceil(样本数 × 分位比例)` 个值，不做插值。
- `all_attempts_p95_ms`：包含失败正式轮次的耗时。超时样本只有耗时下界，
  因而 `all_attempts_censored=true` 时不能将这个数当作完整尾部耗时。
- `success_rate`：成功正式轮次 / 请求的正式轮数；进程中断而未尝试的轮数单独记录。
- `min_coverage` 与 `min_valid_coverage`：分别为返回的唯一股票覆盖率、有效价格覆盖率。
- `observed_connections_min/max`：实际存在的持久连接数量；同时保存每轮原生池统计和
  `connection_config_matches`，区分配置上限与实际建连数。
- `snapshot_capacity_per_second`：`1000 / mean_ms`，仅是调用速度换算值，
  不是指定采样间隔下的实际发布频率。

少于 100 个成功正式样本时 `sample_sufficient=false`。100 个样本只是本脚本的最低
验收样本数，不能代替交易高峰和多次重复测试。指定 `--max-p95-ms` 时，只有每档
成功完成全部正式轮次、连接配置一致、至少 100 个成功样本且 P95 不超过上限才通过。

`source_time_values` 保留源端单票事件时间。该字段不代表批次时间，也不用于新鲜度
判定。盘后测得的耗时可用于检查连接复用效率；交易时段 P95 需要在对应时段重新采样。

## 结果

默认输出到新的 `data/research/rustdx-quotes-<北京时间>/` 目录：

| 文件 | 内容 |
| --- | --- |
| `summary.csv` | 每档冷启动、P50/P95/P99、覆盖率、失败率和连接统计 |
| `results.json` | 环境、固定股票池哈希和日期、参数、逐档结果、逐轮原始数据 |
| `samples.jsonl` | 含冷启动、预热、正式轮次及失败信息的逐轮记录 |

使用 `--output <新目录>` 可更改路径，已有目录会被拒绝。每完成一档即保存一次；
Ctrl+C 保留已经接收的当前档样本。数据报告应保留在忽略目录中，不提交到 Git。

退出码：`0` 表示全部档位完成且无失败；指定上限时还须通过验收。`1` 表示失败、
整轮超时、连接配置不符或验收未通过；`2` 表示参数/输入错误；`130` 表示用户中断。

## 离线验证

```bash
cd backend
uv run --no-sync pytest tests/test_rustdx_quote_benchmark.py tests/test_rustdx_provider.py -q
uv run --no-sync ruff check scripts/benchmark_rustdx_quotes.py tests/test_rustdx_quote_benchmark.py
```

离线测试覆盖股票池过滤、分位数算法、失败与超时样本、连接数进程隔离、资源释放、
报告内容和预检不访问网络。

2026-10-10 盘后在 macOS arm64、固定节点 `117.34.114.13:7709` 完成真实冒烟，
股票池 5,226 只，8 和 30 连接各 3 个正式样本均完整成功，实际连接数与配置一致。
8 连接三轮耗时为 925.8 / 1126.0 / 935.3ms，30 连接为 411.9 / 599.9 / 445.7ms。
这是脚本连通性及报告输出验证，样本量不足以得出正式 P95 结论；尚未执行全部档位
各 100 轮的正式压测。
