"""Stable response contracts for Tier A open API endpoints.

Only fields that callers can rely on are declared here. ``extra="allow"``
keeps additive provider, indicator, and strategy fields in responses while
still validating the stable envelope and documenting it in OpenAPI.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

JsonRecord = dict[str, Any]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="allow")

    @model_serializer(mode="wrap")
    def _serialize_without_injected_defaults(
        self,
        handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        """Keep the wire payload additive without inventing omitted fields."""
        data = handler(self)
        for field_name in type(self).model_fields:
            if field_name not in self.model_fields_set:
                data.pop(field_name, None)
        return data


# Market data


class InstrumentItem(ContractModel):
    symbol: str
    name: str | None = None
    code: str | None = None
    asset_type: str | None = None


class InstrumentSearchResponse(ContractModel):
    results: list[InstrumentItem]


class DailyKlineResponse(ContractModel):
    symbol: str
    name: str | None = None
    stock_info: JsonRecord | None = None
    rows: list[JsonRecord]
    source: str | None = None


class DailyKlineLatestResponse(ContractModel):
    symbol: str
    row: JsonRecord | None
    source: Literal["live", "none"]


class MinuteKlineResponse(ContractModel):
    symbol: str
    name: str | None = None
    stock_info: JsonRecord | None = None
    date: str | None
    rows: list[JsonRecord]
    source: str | None = None
    asset_type: str | None = None
    price_limit: JsonRecord | None = None
    prev_close: float | None = None


class MinuteSession(ContractModel):
    date: str
    prev_close: float | None = None
    rows: list[JsonRecord]


class MinuteRangeResponse(ContractModel):
    symbol: str
    name: str | None = None
    asset_type: str
    requested_days: int
    sessions: list[MinuteSession]
    source: str


class IndexDailyResponse(ContractModel):
    symbol: str
    name: str | None = None
    index_info: JsonRecord | None = None
    rows: list[JsonRecord]
    source: str


class IndexMinuteResponse(ContractModel):
    symbol: str
    name: str | None = None
    index_info: JsonRecord | None = None
    date: str
    rows: list[JsonRecord]
    source: str


class QuoteStatusResponse(ContractModel):
    enabled: bool
    running: bool
    symbol_count: int
    index_symbol_count: int = 0
    quote_age_ms: int | float | None
    is_trading_hours: bool
    last_fetch_ms: int | float | None


class IndexQuote(ContractModel):
    symbol: str
    name: str | None = None
    last_price: float | None = None
    close: float | None = None
    prev_close: float | None = None
    change_pct: float | None = None


class IndexQuotesResponse(ContractModel):
    rows: list[IndexQuote]
    count: int
    source: str


class MarketOverviewResponse(ContractModel):
    as_of: str | None
    quote_status: JsonRecord
    indices: list[JsonRecord]
    breadth: JsonRecord
    amount: JsonRecord
    boards: list[JsonRecord]
    limit: JsonRecord
    distribution: list[JsonRecord]
    trend: JsonRecord
    activity: JsonRecord
    radar: list[JsonRecord]
    emotion: JsonRecord
    top_gainers: list[JsonRecord]
    top_losers: list[JsonRecord]
    turnover_leaders: list[JsonRecord]
    active_leaders: list[JsonRecord]
    concept_rank: JsonRecord
    industry_rank: JsonRecord


# Extension data


class ExtField(ContractModel):
    name: str
    dtype: str
    label: str = ""


class ExtDataConfig(ContractModel):
    id: str
    label: str
    mode: Literal["snapshot", "timeseries"]
    fields: list[ExtField]
    description: str = ""
    latest_sync_date: str | None = None
    date_range: list[str] | None = None


class ExtDataListResponse(ContractModel):
    items: list[ExtDataConfig]


class ExtDataRowsResponse(ContractModel):
    id: str
    label: str
    mode: Literal["snapshot", "timeseries"]
    date: str | None
    total: int
    offset: int
    limit: int
    fields: list[ExtField]
    rows: list[JsonRecord]


class ExtValueCount(ContractModel):
    value: Any = None
    count: int


class ExtDataValuesResponse(ContractModel):
    id: str
    field: str
    date: str | None
    total: int
    distinct: int
    values: list[ExtValueCount]


class DimensionMembersResponse(ContractModel):
    id: str
    label: str
    date: str | None
    field: str
    value: str
    total: int
    limit: int
    rows: list[JsonRecord]


class DimensionPoint(ContractModel):
    time: str
    sector: float | None
    market: float | None


class DimensionIntradayResponse(ContractModel):
    status: Literal["ok", "no_data", "empty"]
    reason: str | None = None
    date: str | None = None
    basis: Literal["prev_close", "first_close", "mixed"] | None = None
    member_count: int | None = None
    members_with_minute: int | None = None
    points: list[DimensionPoint]


class ExtDataIngestResponse(ContractModel):
    status: str
    rows: int
    date: str


class SchemaColumn(ContractModel):
    name: str
    type: str
    label: str | None = None


class ExtDataSchemaResponse(ContractModel):
    columns: list[SchemaColumn]


class ExtDataSchemaItem(ContractModel):
    id: str
    label: str
    mode: str
    columns: list[SchemaColumn]


class ExtDataSchemasResponse(ContractModel):
    items: list[ExtDataSchemaItem]


# Alerts and events


class AlertRecord(ContractModel):
    ts: int
    source: str
    type: str
    message: str | None = None
    symbol: str | None = None
    name: str | None = None
    severity: str | None = None


class AlertsResponse(ContractModel):
    alerts: list[AlertRecord]
    total: int


class EventTicketResponse(ContractModel):
    ticket: str
    expires_in: int
    stream: str


# Screener and strategies


class StrategyLoadError(ContractModel):
    file: str
    error: str


class StrategyParam(ContractModel):
    id: str
    label: str
    type: str
    default: Any


class StrategyDetailResponse(ContractModel):
    id: str
    name: str
    description: str
    tags: list[str]
    source: str
    research_only: bool = False
    execution_backend: str
    asset_types: list[str]
    timeframes: list[str]
    version: str
    basic_filter: JsonRecord
    params: list[StrategyParam]
    params_defaults: JsonRecord
    scoring: dict[str, float]
    scoring_directions: dict[str, str]
    entry_signals: list[str]
    exit_signals: list[str]
    minute_exit_trigger_supported_signals: list[str]
    stop_loss: float | None = None
    take_profit: float | None = None
    trailing_stop: float | None = None
    trailing_take_profit_activate: float | None = None
    trailing_take_profit_drawdown: float | None = None
    max_hold_days: int | None = None
    order_by: str
    descending: bool
    limit: int


class StrategiesResponse(ContractModel):
    strategies: list[StrategyDetailResponse]
    load_errors: list[StrategyLoadError]


class ScreenerStrategiesResponse(ContractModel):
    presets: list[JsonRecord]
    load_errors: list[StrategyLoadError]


class ScreenerResultResponse(ContractModel):
    as_of: str | date
    strategy: str | None = None
    rows: list[JsonRecord]
    total: int
    elapsed_ms: float


class ScreenerCachedResponse(ContractModel):
    as_of: str | None
    results: dict[str, JsonRecord]
    updated_at: int | float | None


class ScreenerRunAllResponse(ContractModel):
    as_of: str | None
    results: dict[str, JsonRecord]
    pending: list[str] = Field(default_factory=list)
    errors: dict[str, str] = Field(default_factory=dict)
    complete: bool | None = None
    error: str | None = None
    started_at: int | None = None


class MarketSnapshotResponse(ContractModel):
    as_of: str | None
    rows: list[JsonRecord]


class LimitLadderResponse(ContractModel):
    as_of: str
    tiers: list[JsonRecord]
    counts: dict[str, int]


class AiStatusResponse(ContractModel):
    configured: bool
    has_key: bool
    has_model: bool
    provider: str


class StrategySourceResponse(ContractModel):
    code: str
    source: str


# Backtests


class BacktestStatusResponse(ContractModel):
    available: bool


class BacktestProvenance(ContractModel):
    schema_version: int
    captured_at: str
    dataset_release_id: str | None
    asset_type: str
    data_generation: str | None
    release_consistent: bool
    providers: dict[str, str]
    config_hash: str
    strategy_hash: str | None


class SignalBacktestResponse(ContractModel):
    run_id: str
    config: JsonRecord
    stats: JsonRecord
    equity_curve: list[JsonRecord]
    trades: list[JsonRecord]
    per_symbol_stats: list[JsonRecord]
    provenance: BacktestProvenance


class FactorColumn(ContractModel):
    id: str
    label: str
    group: str
    desc: str


class FactorColumnsResponse(ContractModel):
    columns: list[FactorColumn]


class FactorBacktestResponse(ContractModel):
    run_id: str
    config: JsonRecord
    ic_mean: float | None
    ic_std: float | None
    ir: float | None
    ic_win_rate: float | None
    ic_series: list[JsonRecord]
    group_stats: list[JsonRecord]
    group_nav: list[JsonRecord]
    long_short_stats: JsonRecord
    long_short_nav: list[JsonRecord]
    elapsed_ms: float
    n_symbols: int
    n_dates: int
    error: str | None
    provenance: BacktestProvenance


class FactorBatchResponse(ContractModel):
    run_id: str
    config: JsonRecord
    results: list[JsonRecord]
    elapsed_ms: float
    n_symbols: int
    n_dates: int
    error: str | None
    provenance: BacktestProvenance


class CandidateRecord(ContractModel):
    id: str
    kind: Literal["factor", "strategy"]
    name: str
    source_id: str
    metrics: JsonRecord
    data_as_of: str | None
    status: Literal["pending", "validated", "rejected"]
    created_at: str


class CandidatesResponse(ContractModel):
    items: list[CandidateRecord]


class StrategyBacktestResponse(ContractModel):
    run_id: str
    config: JsonRecord
    stats: JsonRecord
    equity_curve: list[JsonRecord]
    drawdown_curve: list[JsonRecord]
    benchmark_curve: list[JsonRecord] = Field(default_factory=list)
    trades: list[JsonRecord]
    per_symbol_stats: list[JsonRecord]
    strategy_info: JsonRecord
    factor_attribution: JsonRecord | None = None
    elapsed_ms: float
    error: str | None
    provenance: BacktestProvenance


# Regime


class RegimeHistoryResponse(ContractModel):
    rows: list[JsonRecord]
    total: int


class RegimeLatestResponse(ContractModel):
    row: JsonRecord | None


class RegimeStatesResponse(ContractModel):
    distribution: list[JsonRecord]
    days: int


class RegimeCoverageResponse(ContractModel):
    rows: int
    earliest_date: str | None
    latest_date: str | None


class RegimePhasesResponse(ContractModel):
    segments: list[JsonRecord]
    total: int


class RegimeMainlineResponse(ContractModel):
    rows: list[JsonRecord]
    leaders: list[JsonRecord]
    membership_note: str
    filter: JsonRecord


# Paper trading


class PaperAccount(ContractModel):
    id: str
    name: str | None = None
    initial_cash: float
    cash: float
    commission_pct: float
    stamp_tax_pct: float
    slippage_bps: float
    queue_limit_orders: bool = False
    status: Literal["active", "frozen"]
    created_at: str


class PaperAccountSummary(ContractModel):
    id: str
    name: str
    status: Literal["active", "frozen"]
    initial_cash: float | None = None
    cash: float | None = None
    latest_nav: float | None = None
    created_at: str | None = None


class PaperAccountsResponse(ContractModel):
    accounts: list[PaperAccountSummary]


class PaperAccountResponse(ContractModel):
    account: PaperAccount | None


class PaperHolding(ContractModel):
    symbol: str
    asset_type: str
    qty: int
    avg_cost: float
    last_price: float
    market_value: float
    pnl: float
    pnl_pct: float
    available_qty: int


class PaperOverviewResponse(ContractModel):
    initialized: bool
    account_id: str | None = None
    account_name: str | None = None
    status: str | None = None
    cash: float | None = None
    market_value: float | None = None
    total: float | None = None
    total_pnl: float | None = None
    initial_cash: float | None = None
    holdings: list[PaperHolding] = Field(default_factory=list)


class PaperOrder(ContractModel):
    id: str
    symbol: str
    asset_type: str
    side: Literal["buy", "sell"]
    qty: int
    order_type: str
    status: str
    postponed: int
    source: str
    created_at: str


class PaperOrderResponse(ContractModel):
    order: PaperOrder


class PaperOrdersResponse(ContractModel):
    orders: list[PaperOrder]


class PaperTradesResponse(ContractModel):
    fills: list[JsonRecord]


class PaperPositionsResponse(ContractModel):
    holdings: list[PaperHolding]
    initialized: bool


class PaperNavResponse(ContractModel):
    nav: list[JsonRecord]


class PaperStatsResponse(ContractModel):
    rounds: int
    win_rate: float
    profit_loss_ratio: float | None
    avg_holding_days: float
    realized_pnl: float
    max_drawdown: float | None


class PaperCompareResponse(ContractModel):
    accounts: list[JsonRecord]


class PaperRebuildResponse(ContractModel):
    symbols: int


class PaperAutoRule(ContractModel):
    id: str
    name: str
    match_kind: str
    match_id: str
    side: str
    size_mode: str
    size_value: float
    order_type: str
    cooldown_days: int
    enabled: bool
    created_at: str


class PaperAutoRulesResponse(ContractModel):
    rules: list[PaperAutoRule]


class PaperAutoRuleResponse(ContractModel):
    rule: PaperAutoRule


class OkResponse(ContractModel):
    ok: bool


class ArenaCreatedItem(ContractModel):
    account: str
    name: str
    rule_id: str


class PaperArenaResponse(ContractModel):
    created: list[ArenaCreatedItem]
