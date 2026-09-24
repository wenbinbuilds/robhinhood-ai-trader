"""Pure normalization of factual Robinhood MCP payloads.

Only explicitly selected market/account fields leave this module. Raw response
envelopes and identifiers are never written to disk or reasoning prompts.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Sequence

import config
from agent.staged_snapshot import number, normalize_symbol
from agent.technical_indicators import normalize_candles
from robinhood_mcp.errors import RobinhoodResponseError


def mappings(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from mappings(child)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for child in value:
            yield from mappings(child)


def first(row: Mapping[str, Any], *names: str) -> Any:
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def text(value: Any) -> str | None:
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return None
    result = str(value).strip()
    return result or None


def normalized_account(value: Any) -> tuple[dict[str, Any], Mapping[str, Any]]:
    candidates = list(mappings(value))
    agentic = next(
        (
            row for row in candidates
            if first(row, "agentic_allowed", "is_agentic_account") is True
            or "agentic" in " ".join(
                filter(None, (text(first(row, "nickname", "account_name", "name")),
                              text(first(row, "brokerage_trading_type", "account_type", "type"))))
            ).lower()
        ),
        None,
    )
    if agentic is None:
        raise RobinhoodResponseError("Robinhood Agentic account was not positively identified")
    return (
        {
            "is_agentic_account": True,
            "nickname": text(first(agentic, "nickname", "account_name", "name")),
            "account_type": text(first(agentic, "account_type", "type")),
            "brokerage_trading_type": text(first(agentic, "brokerage_trading_type", "trading_type")),
            "state": text(first(agentic, "state", "status")),
        },
        agentic,
    )


def normalized_portfolio(value: Any) -> dict[str, Any]:
    rows = list(mappings(value))
    row = next(
        (item for item in rows if first(item, "portfolio_value", "total_value", "equity") is not None),
        value if isinstance(value, Mapping) else {},
    )
    portfolio_value = first(row, "portfolio_value", "total_value", "equity", "market_value")
    buying_power_raw = first(row, "buying_power", "withdrawable_amount")
    buying_power_row = buying_power_raw if isinstance(buying_power_raw, Mapping) else {}
    buying_power = first(buying_power_row, "buying_power") if buying_power_row else buying_power_raw
    unleveraged = (
        first(buying_power_row, "unleveraged_buying_power", "cash_available")
        if buying_power_row else first(row, "unleveraged_buying_power", "cash_available", "cash")
    )
    if portfolio_value is None or buying_power is None or unleveraged is None:
        raise RobinhoodResponseError("required portfolio values are unavailable")
    return {
        "portfolio_value": text(portfolio_value),
        "buying_power": text(buying_power),
        "unleveraged_buying_power": text(unleveraged),
        "cash": text(first(row, "cash", "cash_held_for_orders", "withdrawable_amount")),
        "equity_value": text(first(row, "equity_value", "equity", "market_value")),
        "currency": text(first(row, "currency", "currency_code")) or "USD",
    }


def normalized_positions(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in mappings(value):
        symbol = normalize_symbol(first(row, "symbol", "ticker"))
        quantity = first(row, "quantity", "shares", "amount")
        if not symbol or quantity is None or symbol in seen:
            continue
        seen.add(symbol)
        result.append({
            "symbol": symbol,
            "quantity": text(quantity) or "0",
            "average_buy_price": text(first(row, "average_buy_price", "average_price", "cost_basis")),
            "current_price": text(first(row, "current_price", "price", "last_trade_price")),
            "intraday_quantity": text(first(row, "intraday_quantity", "day_trade_quantity")),
            "position_type": text(first(row, "position_type", "type")) or "EQUITY",
        })
    return result


def normalized_orders(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in mappings(value):
        symbol = normalize_symbol(first(row, "symbol", "ticker"))
        side = text(first(row, "side"))
        state = text(first(row, "state", "status"))
        if not symbol or not side or not state:
            continue
        if state.lower() not in {"open", "queued", "confirmed", "pending", "partially_filled", "unconfirmed"}:
            continue
        result.append({
            "symbol": symbol,
            "side": side,
            "state": state,
            "quantity": text(first(row, "quantity", "cumulative_quantity")),
            "dollar_amount": text(first(row, "dollar_amount", "dollar_based_amount", "amount")),
            "price": text(first(row, "price", "limit_price", "average_price")),
            "stop_price": text(first(row, "stop_price")),
            "created_at": text(first(row, "created_at", "submitted_at", "updated_at")),
        })
    return result


def normalized_realized_pnl(value: Any) -> str | float | None:
    for row in mappings(value):
        found = first(row, "realized_pnl", "realized_profit_loss", "pnl", "amount")
        if found is not None:
            return number(found)
    return number(value)


def project_scans(value: Any) -> list[Mapping[str, Any]]:
    return [
        row for row in mappings(value)
        if text(first(row, "name", "scan_name", "title"))
        == config.PROJECT_SCANNER_NAME
    ]


def find_project_scan(value: Any) -> Mapping[str, Any] | None:
    rows = project_scans(value)
    return rows[0] if rows else None


def safe_scan_id(row: Mapping[str, Any]) -> str | None:
    return text(first(row, "scan_id", "id", "uuid"))


def _scanner_columns(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    raw = first(row, "columns", "values")
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            name = text(first(item, "name", "column", "label"))
            if name:
                result[name.lower()] = first(item, "value", "raw_value")
    return result


def scanner_candidates(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in mappings(value):
        symbol = normalize_symbol(first(row, "symbol", "ticker"))
        if not symbol or symbol in seen:
            continue
        columns = _scanner_columns(row)
        def pick(*names: str) -> Any:
            direct = first(row, *names)
            if direct is not None:
                return direct
            for name in names:
                if name.lower() in columns:
                    return columns[name.lower()]
            return None
        if not columns and not any(pick(name) is not None for name in ("last", "volume", "relative_volume", "percent_change")):
            continue
        seen.add(symbol)
        result.append({
            "symbol": symbol,
            "instrument_type": text(first(row, "instrument_type", "asset_type", "type")) or "EQUITY",
            "last": number(pick("last", "last_price", "price")),
            "volume": number(pick("volume", "day_volume")),
            "average_volume": number(pick("average_volume", "average volume")),
            "relative_volume": number(pick("relative_volume", "relative volume")),
            "percent_change": number(pick("percent_change", "% change", "change_percent")),
        })
    return result


def normalized_criteria(scan: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = first(scan, "criteria", "filters", "filter_summary")
    source = raw if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)) else config.SCANNER_CRITERIA
    result = []
    for item in source:
        if not isinstance(item, Mapping):
            continue
        # Robinhood returns both a human-facing label (``filter_type``) and the
        # stable enum used to configure the scan (``filter_type_enum``).  The
        # downstream validator deliberately relies on the enum so that an
        # unrelated saved scan cannot be mistaken for this project's scan.
        # Preserve that enum when it is present; use the label only for display.
        filter_type = text(first(item, "filter_type_enum", "filter_type", "type"))
        values = first(item, "values", "value")
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            values = [] if values is None else [values]
        result.append({
            "filter_type": text(first(item, "display_name", "filter_type")),
            "filter_type_enum": filter_type,
            "predicate": text(first(item, "predicate", "operator")) or "=",
            "values": [str(value) for value in values],
            "interval": text(first(item, "interval")),
            "length": int(first(item, "length")) if number(first(item, "length")) is not None else None,
            "plot": text(first(item, "plot")),
            "expression": text(first(item, "expression")),
        })
    return result


def normalized_scanner(
    scan: Mapping[str, Any], run_result: Any,
    ranked: Sequence[Mapping[str, Any]], *, query_executed_at: datetime | None = None,
    saved_definition_count: int = 1,
) -> dict[str, Any]:
    raw_count = len(scanner_candidates(run_result))
    return {
        "status": "OK",
        "scan_name": config.PROJECT_SCANNER_NAME,
        "scan_id": safe_scan_id(scan),
        "lifecycle_action": "REUSED",
        "criteria": normalized_criteria(scan),
        "sort_configuration": dict(config.SCANNER_SORT),
        "result_count": raw_count,
        # REUSED describes the saved definition.  Results are produced by the
        # run_scan call made during this collection and are never a local cache.
        "results_source": "FRESH_PROVIDER_RUN",
        "query_executed_at": (
            query_executed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if query_executed_at is not None else None
        ),
        "provider_raw_count": raw_count,
        "saved_definition_count": saved_definition_count,
        "after_provider_filters_count": raw_count,
        "after_local_filters_count": raw_count,
        "top_n_count": len(ranked),
        "candidates": [
            {
                "symbol": row["symbol"],
                "instrument_type": row["instrument_type"],
                "columns": [
                    {"name": name, "value": "" if row.get(key) is None else str(row[key])}
                    for name, key in (("Last", "last"), ("Volume", "volume"),
                                      ("Average Volume", "average_volume"),
                                      ("Relative Volume", "relative_volume"),
                                      ("% Change", "percent_change"))
                ],
            }
            for row in ranked
        ],
    }


def normalized_quotes(
    value: Any,
    expected_symbols: Sequence[str],
    *,
    retrieved_at: datetime,
    request_started_at: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    expected = set(expected_symbols)
    result: dict[str, dict[str, Any]] = {}
    quality_by_symbol: dict[str, int] = {}
    for row in mappings(value):
        symbol = normalize_symbol(first(row, "symbol", "ticker"))
        if symbol not in expected:
            continue
        # ``get_equity_quotes`` also nests a previous-close record shaped like
        # {symbol, date, price, source}.  It is not a current quote and must not
        # overwrite the richer venue quote row encountered earlier.
        quote_keys = {
            "last_trade_price", "current_price", "last_price", "mark_price",
            "bid_price", "bid", "ask_price", "ask",
            "venue_last_trade_time", "venue_bid_time", "venue_ask_time",
        }
        quality = sum(key in row and row.get(key) is not None for key in quote_keys)
        if quality == 0 or quality < quality_by_symbol.get(symbol, -1):
            continue
        current = number(first(row, "last_trade_price", "current_price", "last_price", "mark_price"))
        bid = number(first(row, "bid_price", "bid"))
        ask = number(first(row, "ask_price", "ask"))
        # The direct Robinhood quote response names the exchange-side last-trade
        # timestamp ``venue_last_trade_time``.  Keep it separate from our own
        # request lifecycle timestamps: they have different freshness semantics.
        timestamp_fields = (
            "venue_last_trade_time",
            "updated_at",
            "quote_as_of",
            "last_trade_time",
            "timestamp",
        )
        quote_timestamp_field = next(
            (name for name in timestamp_fields if row.get(name) is not None), None
        )
        quote_at = text(row.get(quote_timestamp_field)) if quote_timestamp_field else None
        received_at = retrieved_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        started_at = (
            request_started_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if request_started_at is not None else None
        )
        if current is None and bid is None and ask is None:
            continue
        result[symbol] = {
            "symbol": symbol,
            "current_price": current,
            "bid": bid,
            "ask": ask,
            "quote_as_of": quote_at,
            "quote_timestamp_field": quote_timestamp_field,
            "quote_request_started_at": started_at,
            "quote_retrieved_at": received_at,
            "quote_freshness_source": (
                "exchange_timestamp" if quote_timestamp_field else "retrieval_timestamp"
            ),
            "previous_close": number(first(row, "previous_close", "previous_close_price", "adjusted_previous_close")),
        }
        quality_by_symbol[symbol] = quality
    return result


def normalized_historicals(value: Any, symbol: str) -> dict[str, Any]:
    candles = []
    previous_close = None
    relative_volume = None
    for row in mappings(value):
        if previous_close is None:
            previous_close = number(first(row, "previous_close", "previous_close_price"))
        if relative_volume is None:
            relative_volume = number(first(row, "relative_volume"))
        begins_at = text(first(row, "begins_at", "start_time", "timestamp", "time"))
        opened = number(first(row, "open_price", "open"))
        high = number(first(row, "high_price", "high"))
        low = number(first(row, "low_price", "low"))
        close = number(first(row, "close_price", "close"))
        volume = number(first(row, "volume"))
        if begins_at and None not in (opened, high, low, close, volume):
            candles.append({
                "begins_at": begins_at,
                "open": opened, "high": high, "low": low, "close": close,
                "volume": volume,
                "interpolated": bool(first(row, "interpolated", "is_interpolated") or False),
                # DirectSnapshotCollector requests Robinhood's five-minute
                # interval.  Keep that contract attached to every bar so
                # downstream freshness never has to guess its timeframe.
                "interval_seconds": 300.0,
                "bar_source": "ROBINHOOD_MCP_HISTORICALS_5MINUTE",
            })
    return {
        "symbol": symbol,
        "candles": normalize_candles(candles, limit=config.ANALYSIS_CANDLES_TO_RETAIN),
        "previous_close": previous_close,
        "relative_volume": relative_volume,
    }
