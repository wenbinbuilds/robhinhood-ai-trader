"""Persistent models for local simulated positions and trades."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


@dataclass
class ShadowPosition:
    trade_id: str
    symbol: str
    direction: str
    strategy: str
    entry_price: float
    requested_entry_price: float
    entry_timestamp: str
    quantity: int
    notional_value: float
    stop: float
    target: float
    risk_per_share: float
    maximum_theoretical_loss: float
    risk_reward_ratio: float
    coordinator_confidence: float
    coordinator_score: float
    technical_score: float
    news_score: float
    sector_score: float
    market_score: float
    catalyst_type: str
    sector: str
    market_regime: str
    news_event_ids_at_entry: list[str] = field(default_factory=list)
    last_price: float | None = None
    current_bid: float | None = None
    current_ask: float | None = None
    last_price_timestamp: str | None = None
    monitoring_status: str = "PRICE_MONITORING_DEGRADED"
    quote_mode: str = "DEGRADED_SNAPSHOT"
    quote_source: str | None = None
    mark_price_method: str | None = None
    unrealized_pnl: float = 0.0
    estimated_entry_slippage_cost: float = 0.0
    warnings: list[str] = field(default_factory=list)
    thesis: str | None = None
    invalidation_condition: str | None = None
    technical_context: dict[str, Any] = field(default_factory=dict)
    news_context: dict[str, Any] = field(default_factory=dict)
    sector_context: dict[str, Any] = field(default_factory=dict)
    market_context: dict[str, Any] = field(default_factory=dict)
    slow_context_score: float | None = None
    live_market_score: float | None = None
    dynamic_score: float | None = None
    slow_weight: float | None = None
    live_weight: float | None = None
    context_age_seconds: float | None = None
    qualitative_score: float | None = None
    llm_score: float | None = None
    discovery_timestamp: str | None = None
    watchlist_timestamp: str | None = None
    trade_ready_timestamp: str | None = None
    state_transition_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ShadowPosition:
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShadowTrade:
    trade_id: str
    symbol: str
    strategy: str
    sector: str
    entry_timestamp: str
    entry_price: float
    quantity: int
    stop: float
    target: float
    risk_reward_ratio: float
    exit_timestamp: str
    exit_price: float
    exit_reason: str
    gross_pnl: float
    estimated_slippage_cost: float
    net_pnl: float
    return_percent: float
    holding_time_minutes: float
    coordinator_confidence: float
    coordinator_score: float
    technical_score: float
    news_score: float
    sector_score: float
    market_score: float
    catalyst_type: str
    market_regime: str
    warnings: list[str] = field(default_factory=list)
    exit_method: str = "SLOW_SNAPSHOT_EXIT"
    slow_context_score: float | None = None
    live_market_score: float | None = None
    dynamic_score: float | None = None
    slow_weight: float | None = None
    live_weight: float | None = None
    context_age_seconds: float | None = None
    qualitative_score: float | None = None
    llm_score: float | None = None
    discovery_timestamp: str | None = None
    watchlist_timestamp: str | None = None
    trade_ready_timestamp: str | None = None
    state_transition_history: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ShadowTrade:
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ShadowState:
    schema_version: int
    starting_capital: float
    cash: float
    equity: float
    realized_pnl: float
    unrealized_pnl: float
    daily_pnl: float
    trading_date: str | None
    trades_today: int
    peak_equity: float
    maximum_drawdown: float
    maximum_drawdown_percent: float
    open_positions: list[ShadowPosition] = field(default_factory=list)
    closed_positions: list[ShadowTrade] = field(default_factory=list)
    benchmark_session: dict[str, Any] = field(default_factory=dict)
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        return value
