from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class ScalpFeatures:
    symbol: str
    timestamp: str
    quote_age: float | None
    spread_pct: float | None
    price: float | None
    price_vs_vwap: float | None
    distance_to_ema9: float | None
    distance_to_ema20: float | None
    ema9_slope: float | None
    ema9_vs_ema20: float | None
    return_1: float | None
    return_3: float | None
    momentum: float | None
    rsi14: float | None
    macd_histogram: float | None
    relative_volume: float | None
    volume_slope: float | None
    volume_acceleration: float | None
    breakout_volume_expansion: float | None
    pullback_volume_contraction: float | None
    relative_strength_spy: float | None
    relative_strength_qqq: float | None
    relative_strength_sector: float | None
    entry_extension: float | None
    recent_high_distance: float | None
    recent_low_distance: float | None
    realized_volatility: float | None
    recent_high: float | None
    recent_low: float | None
    vwap: float | None
    ema9: float | None
    ema20: float | None
    latest_bar_timestamp: str | None
    bar_count: int

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class ScalpEpisode:
    strategy_id: str
    episode_id: str
    symbol: str
    setup_type: str
    created_at: str
    evidence_timestamp: str
    evidence: tuple[str, ...]
    state: str = 'FORMING'


@dataclass(frozen=True)
class ScalpEntryDecision:
    strategy_id: str
    episode_id: str
    symbol: str
    setup_type: str
    setup_evidence: tuple[str, ...]
    signal_score: float
    expected_move_pct: float | None
    estimated_cost_pct: float | None
    expected_net_edge_pct: float | None
    entry_price: float | None
    stop: float | None
    target: float | None
    risk_pct: float | None
    reward_pct: float | None
    risk_reward_ratio: float | None
    net_reward_pct: float | None
    net_risk_reward_ratio: float | None
    expected_hold_seconds: int
    approved: bool
    rejection_reasons: tuple[str, ...]
    timestamp: str
    features: ScalpFeatures

    def to_dict(self): return asdict(self)


@dataclass(frozen=True)
class ScalpEvent:
    event_id: str
    event: str
    strategy_id: str
    episode_id: str
    symbol: str
    timestamp: str
    payload: dict[str, Any] = field(default_factory=dict)
