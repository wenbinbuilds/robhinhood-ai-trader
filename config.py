"""Configuration for non-trading analysis and local shadow simulation."""

from __future__ import annotations

MODE = "SHADOW_TRADING"

# Factual Robinhood data is collected directly with the official MCP Python
# SDK. The legacy Codex transport remains opt-in for diagnostics only and is
# never an automatic fallback.
ROBINHOOD_DATA_PROVIDER = "DIRECT_MCP"
ROBINHOOD_MCP_SERVER_URL = "https://agent.robinhood.com/mcp/trading"
ROBINHOOD_MCP_OAUTH_CALLBACK_HOST = "127.0.0.1"
ROBINHOOD_MCP_OAUTH_CALLBACK_PORT = 8765
ROBINHOOD_MCP_CONNECT_TIMEOUT_SECONDS = 30
ROBINHOOD_MCP_REQUEST_TIMEOUT_SECONDS = 30
ROBINHOOD_MCP_MAX_CONCURRENCY = 4
ROBINHOOD_MCP_READ_RETRIES = 2
ROBINHOOD_MCP_RETRY_BASE_SECONDS = 0.25
ROBINHOOD_DIRECT_TIMINGS_PATH = "state/direct_mcp_timings.json"

SCAN_INTERVAL_SECONDS = 300
SLOW_CYCLE_TARGET_SECONDS = 300
SLOW_LOOP_START_TO_START_SCHEDULING = True
FAST_WATCHER_ENABLED = True
FAST_QUOTE_INTERVAL_SECONDS = 2
FAST_QUOTE_MAX_AGE_SECONDS = 5
PRE_EXECUTION_MAX_QUOTE_AGE_SECONDS = 15
FAST_QUOTE_PROVIDER = "DIRECT_MCP"
FAST_WATCH_ONLY_OPEN_POSITIONS = False
FAST_WATCH_MAX_SYMBOLS = 20
FAST_QUOTE_LOG_INTERVAL_SECONDS = 60
FAST_EVENT_LOG_MAX_BYTES = 1_000_000
QUOTE_PROVENANCE_LOG_MAX_BYTES = 50_000_000
QUOTE_PROVENANCE_LOG_PATH = "logs/quote_provenance.jsonl"
SHADOW_ENTRY_VIA_FAST_WATCHLIST = True
CANDIDATE_CONTEXT_TTL_SECONDS = SLOW_CYCLE_TARGET_SECONDS
# Kept equal to the existing coordinator NO_TRADE boundary below.  It is an
# admission threshold for monitoring, not the 0.72 entry threshold.
WATCHLIST_MIN_SLOW_CONTEXT_SCORE = 0.60
SLOW_WEIGHT_AT_RESEARCH = 0.70
SLOW_WEIGHT_AT_EXPIRATION = 0.50
FAST_ENTRY_CONFIRMATION_UPDATES = 2
SCORE_HISTORY_SAMPLE_SECONDS = 20
CANDIDATE_WATCHLIST_PATH = "state/candidate_watchlist.json"
CANDIDATE_SCORE_HISTORY_PATH = "logs/candidate_score_history.jsonl"
MAX_CANDIDATES_TO_ANALYZE = 10
SNAPSHOT_MAX_AGE_SECONDS = 180
CODEX_EXEC_TIMEOUT_SECONDS = 600
# Snapshot collection is split into bounded factual stages.  The measured
# monolithic run finished its MCP work in ~116s but then waited until the 600s
# process timeout, so no individual stage inherits that oversized deadline.
CORE_SNAPSHOT_TIMEOUT_SECONDS = 120
BENCHMARK_SNAPSHOT_TIMEOUT_SECONDS = 150
BENCHMARK_FALLBACK_TIMEOUT_SECONDS = 45
CANDIDATE_BATCH_TIMEOUT_SECONDS = 90
SNAPSHOT_OVERALL_TIMEOUT_SECONDS = 540
CANDIDATE_BATCH_SIZE = 3
SNAPSHOT_STAGE_TIMINGS_PATH = "state/snapshot_stage_timings.json"
INSTRUMENT_METADATA_CACHE_PATH = "state/instrument_metadata_cache.json"
INSTRUMENT_METADATA_CACHE_TTL_SECONDS = 86400
CODEX_REASONING_EFFORT = "low"
CODEX_REASONING_WEB_SEARCH = "disabled"
CODEX_LLM_REASONING_TIMEOUT_SECONDS = 300
CODEX_REASONING_MODEL = "gpt-5.6-sol"
LLM_QUALITATIVE_CONTEXT_TTL_SECONDS = 1800
LLM_QUALITATIVE_CACHE_ALLOW_FAILURE_FALLBACK = False
LLM_REASONING_PROMPT_VERSION = "v1"
LLM_REASONING_SCHEMA_VERSION = "1.0"
LLM_REASONING_MAX_PAYLOAD_BYTES = 250_000 
LLM_REASONING_MAX_NEWS_CLUSTERS = 5
LLM_REASONING_MAX_SOURCES_PER_CLUSTER = 3
LLM_REASONING_MAX_CANDLES = 12
CONNECTIVITY_CHECK_SYMBOL = "NVDA"
PROJECT_SCANNER_NAME = "AI_INTRADAY_MOMENTUM_V1"

# This allowlist is passed to Codex CLI for the robinhood-trading MCP on every
# refresh. Order placement/review/cancellation, options, crypto, watchlist
# mutation, transfers, and account-setting tools are therefore not exposed to
# the nested collection agent. Scanner mutation is limited by prompt and
# validation to PROJECT_SCANNER_NAME.
ROBINHOOD_MCP_ENABLED_TOOLS = (
    "get_accounts",
    "get_portfolio",
    "get_realized_pnl",
    "get_pnl_trade_history",
    "search",
    "get_equity_historicals",
    "get_equity_fundamentals",
    "get_equity_price_book",
    "get_equity_technical_indicators",
    "get_earnings_results",
    "get_earnings_calendar",
    "get_indexes",
    "get_index_quotes",
    "get_equity_positions",
    "get_equity_quotes",
    "get_equity_orders",
    "get_equity_tradability",
    "get_scans",
    "get_scanner_filter_specs",
    "create_scan",
    "run_scan",
    "update_scan_filters",
    "update_scan_config",
)

MAX_SIMULTANEOUS_POSITIONS = 2
MAX_TRADES_PER_DAY = 5
MAX_POSITION_PERCENT = 0.05
MAX_RISK_PER_TRADE_PERCENT = 0.005
MAX_DAILY_LOSS_PERCENT = 0.02

# These risk percentages are development placeholders. They only constrain
# theoretical calculations and must never cause an order to be created.

REGULAR_MARKET_ONLY = True
ALLOW_OPTIONS = False
ALLOW_CRYPTO = False
ALLOW_SHORTING = False
ALLOW_MARGIN = False
ALLOW_OVERNIGHT = False

STRATEGY_NAME = "INTRADAY_MOMENTUM_V1"
MIN_CANDIDATE_CONFIDENCE = 0.65
MIN_RISK_REWARD_RATIO = 1.5
MAX_QUOTE_AGE_SECONDS = 120
# Measured 2026-09-10: 175–196s snapshots; 149–241s quote ages.
# One 300s research cadence bounds slow evidence; NOT an execution-price limit.
SLOW_ANALYSIS_QUOTE_MAX_AGE_SECONDS = 300
ANALYSIS_CANDLES_TO_RETAIN = 50
MIN_ANALYSIS_5_MINUTE_CANDLES = 6  # Existing strategy minimum, unchanged.
MAX_SPREAD_PERCENT = 0.005

MARKET_TIMEZONE = "America/New_York"
REGULAR_MARKET_OPEN = (9, 30)
REGULAR_MARKET_CLOSE = (16, 0)

# Verified on 2026-09-09 against Robinhood's get_scanner_filter_specs response.
# Percentages are decimal ratios (0.01 = 1%); interval-bearing filters use an
# explicitly supported interval and length. This is intentionally broad enough
# to produce a useful long-only momentum universe rather than a trade signal.
SCANNER_CRITERIA = (
    {
        "filter_type": "FILTER_TYPE_INSTRUMENT_TYPE",
        "predicate": "=",
        "values": ["STOCK"],
    },
    {
        "filter_type": "FILTER_TYPE_LAST",
        "predicate": "BETWEEN",
        "values": ["10", "500"],
    },
    {
        "filter_type": "FILTER_TYPE_VOLUME",
        "predicate": ">=",
        "values": ["500000"],
        "interval": "1d",
        "length": 1,
    },
    {
        "filter_type": "FILTER_TYPE_AVERAGE_VOLUME",
        "predicate": ">=",
        "values": ["500000"],
        "interval": "1d",
        "length": 30,
    },
    {
        "filter_type": "FILTER_TYPE_RELATIVE_VOLUME",
        "predicate": ">=",
        "values": ["1.1"],
        "interval": "1d",
        "length": 30,
    },
    {
        "filter_type": "FILTER_TYPE_PERCENT_CHANGE_FROM_CLOSE",
        "predicate": ">=",
        "values": ["0.01"],
        "interval": "1d",
        "plot": "Close",
    },
)

SCANNER_SORT = {"column": "% Change", "direction": "desc"}

# Experimental research-coordinator weights and thresholds. These are analysis
# parameters, not trading authority, and every cycle logs the values used.
# POSITION is a deterministic technical strategy with bounded contextual
# confirmation.  All five inputs share the semantic [0, 1] scale where 0.5 is
# neutral, but the previous 35/65 technical/context split made neutral context
# more important than the actual setup.  Keep context meaningful without
# allowing an ordinary absence of catalyst to become an accidental veto.
COORDINATOR_WEIGHTS = {
    "technical": 0.70,
    "news": 0.10,
    "sector": 0.05,
    "market": 0.05,
    "qualitative": 0.10,
}
COORDINATOR_NO_TRADE_THRESHOLD = 0.60
COORDINATOR_TRADE_CANDIDATE_THRESHOLD = 0.72
NEWS_DEFAULT_HALF_LIFE_MINUTES = 180
NEWS_MAJOR_EVENT_HALF_LIFE_MINUTES = 720
NEWS_MAX_AGE_MINUTES = 1440
NEWS_NEGATIVE_VETO_IMPORTANCE = 0.70

SHADOW_STARTING_CAPITAL = 10_000.00
SHADOW_ENTRY_SLIPPAGE_BPS = 5.0
SHADOW_EXIT_SLIPPAGE_BPS = 5.0
FORCE_EXIT_MINUTES_BEFORE_CLOSE = 5
SHADOW_INTRABAR_BOTH_HIT_POLICY = "STOP_FIRST"

# Future live-execution gates. Repository defaults are intentionally incapable
# of reaching an order review or submission. Only the human operator may alter
# these values, and changing MODE alone is never sufficient.
LIVE_TRADING_ENABLED = False
ROBINHOOD_EXECUTION_ENABLED = False
LIVE_CONFIRMATION_TOKEN = None
LIVE_CONFIRMATION_EXPECTED_VALUE = None
LIVE_ALLOWED_ASSET_TYPES = ("EQUITY",)
LIVE_ALLOW_SHORTING = False
LIVE_ALLOW_OPTIONS = False
LIVE_ALLOW_CRYPTO = False
LIVE_ALLOW_MARGIN_BORROWING = False
LIVE_ALLOW_OVERNIGHT = False
MAX_LIVE_POSITION_PERCENT = MAX_POSITION_PERCENT
MAX_LIVE_RISK_PER_TRADE_PERCENT = MAX_RISK_PER_TRADE_PERCENT
MAX_LIVE_DAILY_LOSS_PERCENT = MAX_DAILY_LOSS_PERCENT
MAX_LIVE_OPEN_POSITIONS = MAX_SIMULTANEOUS_POSITIONS
MAX_LIVE_TRADES_PER_DAY = MAX_TRADES_PER_DAY
MAX_TRADE_PLAN_AGE_SECONDS = 120
MAX_ALLOWED_SPREAD_PERCENT = MAX_SPREAD_PERCENT
LIVE_KILL_SWITCH_PATH = "state/live_kill_switch.json"
LIVE_EXECUTION_STATE_PATH = "state/live_execution_state.json"
EXECUTION_AUDIT_LOG_PATH = "logs/execution_audit.jsonl"

# Offline reinforcement-learning research. These defaults cannot affect entries.
RL_ENABLED = False
RL_MODE = "OFFLINE"  # OFFLINE | SHADOW_COMPARE | SHADOW_CONTROL; never LIVE.
RL_MODEL_ID = None
RL_DATASET_PATH = "state/rl/dataset.jsonl"
RL_MODEL_REGISTRY_PATH = "state/rl/models"
RL_SHADOW_COMPARE_PATH = "logs/rl_shadow_compare.jsonl"
RL_FEATURE_SCHEMA_VERSION = "1.0"
RL_REWARD_VERSION = "1.0"
RL_NORMALIZATION_VERSION = "1.0"
RL_RANDOM_SEED = 17
RL_COMMISSION_PER_SHARE = 0.0
RL_MINIMUM_LATENCY_SECONDS = 0.0

# Independent deterministic scalp strategy. Disabled until a human enables it.
SCALP_ENABLED = False
SCALP_MODE = "SHADOW"
SCALP_STRATEGY_ID = "SCALP_V1"
# A bounded, explicit liquid-equity seed universe lets scalp discovery continue
# when the momentum scanner has no results.  The current momentum scanner rows,
# recent local candidates, and open positions are unioned with these symbols at
# runtime; the shared market-data collector deduplicates the resulting request.
SCALP_DISCOVERY_SOURCE = "LIQUID_EQUITY_SEED_PLUS_SHARED_ACTIVE_UNIVERSE"
SCALP_DISCOVERY_SYMBOLS = (
    "AAPL", "AMD", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA",
)
SCALP_DISCOVERY_MAX_SYMBOLS = 12
SCALP_MAX_QUOTE_AGE_SECONDS = 2.0
SCALP_MAX_MICRO_BAR_AGE_SECONDS = 120
# Operational history-refresh cadence. These do not alter entry thresholds:
# the existing 120 seconds above is provider lag allowed only after the next
# five-minute bar should have completed.
SCALP_MICRO_BAR_REFRESH_GRACE_SECONDS = 2.0
SCALP_MICRO_BAR_REFRESH_RETRY_SECONDS = 30.0
SCALP_MAX_SPREAD_PCT = 0.001
SCALP_MIN_VOLUME_EXPANSION = 1.20
# Backward-compatible configuration alias.  Production diagnostics and gate
# reasons use VOLUME_EXPANSION because this value is a completed-short-bar
# ratio, not a measure of executable depth or order-book liquidity.
SCALP_MIN_RELATIVE_VOLUME = SCALP_MIN_VOLUME_EXPANSION
SCALP_MIN_SIGNAL_SCORE = 0.70
SCALP_MIN_EXPECTED_MOVE_PCT = 0.0015
SCALP_MAX_EXPECTED_MOVE_PCT = 0.006
SCALP_MIN_EXPECTED_NET_EDGE = 0.0005
SCALP_MIN_RISK_REWARD = 1.10
SCALP_MIN_STOP_DISTANCE_PCT = 0.0005
SCALP_MAX_EXTENSION_PCT = 0.003
SCALP_MAX_HOLD_SECONDS = 180
SCALP_ENTRY_SLIPPAGE_BPS = 2.5
SCALP_EXIT_SLIPPAGE_BPS = 2.5
SCALP_COMMISSION_PER_SHARE = 0.0
SCALP_MIN_ENTRY_LATENCY_SECONDS = 0.0
SCALP_MAX_TRADES_PER_SYMBOL = 3
SCALP_MAX_TRADES_PER_SESSION = 8
SCALP_MAX_CONSECUTIVE_LOSSES = 2
SCALP_MAX_DAILY_LOSS_PERCENT = 0.005
SCALP_MAX_TRANSACTION_COST_PERCENT = 0.002
SCALP_CAPITAL_ALLOCATION_PERCENT = 0.20
SCALP_MAX_POSITION_PERCENT = 0.02
SCALP_MAX_RISK_PER_TRADE_PERCENT = 0.001
SCALP_PROFIT_PROTECTION_ENABLED = False
SCALP_BREAKEVEN_ARM_R = 0.75
SCALP_STATE_PATH = "state/scalp_setups.json"
SCALP_EVENT_LOG_PATH = "logs/scalp_events.jsonl"
SCALP_DIAGNOSTICS_PATH = "logs/scalp_diagnostics.jsonl"
