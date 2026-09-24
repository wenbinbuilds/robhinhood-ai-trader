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
    micro_bar_freshness: dict[str, Any] = field(default_factory=dict)
    feature_provenance: dict[str, Any] = field(default_factory=dict)
    signal_data_status: str = 'UNAVAILABLE'
    score_breakdown: dict[str, Any] = field(default_factory=dict)
    # Canonical name; relative_volume remains a read-compatible persisted alias.
    volume_expansion: float | None = None
    price_timing_source: str = 'PROVIDER_5_MINUTE_CONTEXT'
    quote_sample_count: int = 0
    quote_window_seconds: float | None = None
    quote_microstructure: dict[str, Any] = field(default_factory=dict)
    # Audit metadata only. Production entry_extension above remains the value
    # consumed by the score and hard gate.
    entry_extension_reference: dict[str, Any] = field(default_factory=dict)

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
    last_updated_at: str | None = None
    structural_fingerprint: str | None = None
    initial_structural_fingerprint: str | None = None
    structure_changed: bool = False
    new_episode_allowed: bool = True
    stale_after_seconds: float | None = None
    stale_reason: str | None = None
    closed_at: str | None = None
    close_reason: str | None = None
    fingerprint_fields: dict[str, Any] = field(default_factory=dict)
    original_fingerprint_fields: dict[str, Any] = field(default_factory=dict)
    current_quote_price: float | None = None
    original_anchor_price: float | None = None
    current_bar_timestamp: str | None = None
    original_bar_timestamp: str | None = None
    current_volume_expansion: float | None = None
    original_volume_expansion: float | None = None
    exact_block_reason: str | None = None
    transition_history: tuple[dict[str, Any], ...] = ()
    # First-observed wall-clock milestones survive restarts. Per-observation
    # monotonic phase durations live in the diagnostics journal instead.
    latency_milestones: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ScalpEntryDecision:
    strategy_id: str
    episode_id: str
    symbol: str
    setup_type: str
    setup_evidence: tuple[str, ...]
    signal_score: float | None
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
