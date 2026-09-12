"""Pure helpers for ranking and merging bounded factual snapshot stages."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

import config
from agent.technical_indicators import calculate_indicators
from watcher.storage import atomic_json


def number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in (float("inf"), float("-inf")) else None


def normalize_symbol(value: Any) -> str:
    symbol = str(value or "").upper().strip()
    return symbol if symbol and len(symbol) <= 10 and all(c.isalnum() or c in ".-" for c in symbol) else ""


def rank_scanner_candidates(values: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate and rank scanner rows before any expensive collection."""

    unique: dict[str, dict[str, Any]] = {}
    for value in values:
        symbol = normalize_symbol(value.get("symbol"))
        if not symbol or symbol in unique:
            continue
        unique[symbol] = {
            "symbol": symbol,
            "instrument_type": str(value.get("instrument_type") or "EQUITY"),
            "last": number(value.get("last")),
            "volume": number(value.get("volume")),
            "average_volume": number(value.get("average_volume")),
            "relative_volume": number(value.get("relative_volume")),
            "percent_change": number(value.get("percent_change")),
        }
    def key(row: Mapping[str, Any]) -> tuple[float, float, float, str]:
        return (
            -(number(row.get("percent_change")) or -1e20),
            -(number(row.get("relative_volume")) or -1e20),
            -(number(row.get("volume")) or -1e20),
            str(row["symbol"]),
        )
    return sorted(unique.values(), key=key)[: config.MAX_CANDIDATES_TO_ANALYZE]


def final_scanner(core: Mapping[str, Any], ranked: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scanner = core.get("scanner", {})
    configured_criteria = [
        {
            "filter_type": item["filter_type"],
            "filter_type_enum": item["filter_type"],
            "predicate": item["predicate"],
            "values": list(item["values"]),
            "interval": item.get("interval"),
            "length": item.get("length"),
            "plot": item.get("plot"),
            "expression": None,
        }
        for item in config.SCANNER_CRITERIA
    ] if scanner.get("status") == "OK" else []
    actual_criteria = scanner.get("criteria")
    criteria = (
        [dict(item) for item in actual_criteria if isinstance(item, Mapping)]
        if isinstance(actual_criteria, Sequence) and not isinstance(actual_criteria, (str, bytes)) and actual_criteria
        else configured_criteria
    )
    candidates = []
    for row in ranked:
        columns = [
            {"name": name, "value": "" if row.get(key) is None else str(row[key])}
            for name, key in (
                ("Last", "last"), ("Volume", "volume"),
                ("Average Volume", "average_volume"),
                ("Relative Volume", "relative_volume"), ("% Change", "percent_change"),
            )
        ]
        candidates.append({"symbol": row["symbol"], "instrument_type": row["instrument_type"], "columns": columns})
    return {
        "status": scanner.get("status", "ERROR"),
        "scan_name": scanner.get("scan_name"),
        "scan_id": scanner.get("scan_id"),
        "lifecycle_action": scanner.get("lifecycle_action", "FAILED"),
        "criteria": criteria,
        "sort_configuration": scanner.get("sort_configuration", dict(config.SCANNER_SORT)),
        "result_count": int(scanner.get("result_count", 0) or 0),
        "candidates": candidates,
    }


class InstrumentMetadataCache:
    """24-hour sector/industry cache; never stores prices or account data."""

    def __init__(self, path: Path, *, now: datetime) -> None:
        self.path = path
        self.now = now.astimezone(timezone.utc)
        self.rows: dict[str, dict[str, Any]] = {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, Mapping):
                for symbol, row in value.get("instruments", {}).items():
                    expires = datetime.fromisoformat(str(row.get("expires_at", "")).replace("Z", "+00:00"))
                    if expires > self.now and normalize_symbol(symbol):
                        self.rows[normalize_symbol(symbol)] = dict(row)
        except (OSError, ValueError, TypeError, AttributeError):
            pass

    def get(self, symbol: str) -> tuple[str | None, str | None]:
        row = self.rows.get(symbol, {})
        return row.get("sector"), row.get("industry")

    def update(self, symbol: str, sector: Any, industry: Any) -> None:
        if not sector and not industry:
            return
        expires = self.now + timedelta(seconds=config.INSTRUMENT_METADATA_CACHE_TTL_SECONDS)
        self.rows[symbol] = {
            "sector": str(sector) if sector else None,
            "industry": str(industry) if industry else None,
            "expires_at": expires.isoformat().replace("+00:00", "Z"),
        }

    def save(self) -> None:
        atomic_json(self.path, {"schema_version": 1, "instruments": self.rows})


def candidate_bundle(
    symbol: str,
    raw: Mapping[str, Any] | None,
    *,
    scanner_row: Mapping[str, Any] | None,
    market_direction: str,
    cache: InstrumentMetadataCache,
    failure: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    raw = raw or {}
    indicators = calculate_indicators(
        raw.get("candles", []) if isinstance(raw.get("candles"), list) else [],
        now=now,
        completed_only=now is not None,
    )
    sector = raw.get("sector")
    industry = raw.get("industry")
    if sector is None and industry is None:
        sector, industry = cache.get(symbol)
    else:
        cache.update(symbol, sector, industry)
    relative_volume = number(raw.get("relative_volume"))
    if relative_volume is None and scanner_row is not None:
        relative_volume = number(scanner_row.get("relative_volume"))
    unavailable = set(str(item) for item in raw.get("unavailable_values", []) if isinstance(item, str))
    values = {
        "symbol": symbol,
        "current_price": number(raw.get("current_price")), "bid": number(raw.get("bid")), "ask": number(raw.get("ask")),
        "quote_as_of": raw.get("quote_as_of") if isinstance(raw.get("quote_as_of"), str) else None,
        "quote_timestamp_field": raw.get("quote_timestamp_field") if isinstance(raw.get("quote_timestamp_field"), str) else None,
        "quote_request_started_at": raw.get("quote_request_started_at") if isinstance(raw.get("quote_request_started_at"), str) else None,
        "candidate_quote_retrieved_at": raw.get("quote_retrieved_at") if isinstance(raw.get("quote_retrieved_at"), str) else None,
        "quote_freshness_source": raw.get("quote_freshness_source") if raw.get("quote_freshness_source") in {"exchange_timestamp", "retrieval_timestamp", "unavailable"} else "unavailable",
        "volume": indicators["volume"], "relative_volume": relative_volume,
        "vwap": indicators["vwap"], "ema9": indicators["ema9"], "ema20": indicators["ema20"],
        "rsi14": indicators["rsi14"], "macd": indicators["macd"], "macd_signal": indicators["macd_signal"],
        "macd_histogram": indicators["macd_histogram"],
        "intraday_support_reference": indicators["intraday_low"],
        "intraday_resistance_reference": indicators["intraday_high"],
        "intraday_high": indicators["intraday_high"], "intraday_low": indicators["intraday_low"],
        "previous_close": number(raw.get("previous_close")), "market_direction": market_direction,
        "sector": sector, "industry": industry, "sector_benchmark": None,
        "news_items": [], "candles": indicators["candles"], "level2": None,
        "collection_status": "DATA_UNAVAILABLE" if failure else "COMPLETE",
        "collection_error": failure,
    }
    for name, value in values.items():
        if name not in {"symbol", "market_direction", "sector", "industry", "sector_benchmark", "news_items", "candles", "level2", "collection_status", "collection_error"} and value is None:
            unavailable.add(name)
    unavailable.update(("level2", "sector_benchmark", "snapshot_news_deferred_to_reasoning"))
    values["unavailable_values"] = sorted(unavailable)
    return values


def benchmark_bundle(symbol: str, raw: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any]:
    raw = raw or {}
    indicators = calculate_indicators(
        raw.get("candles", []) if isinstance(raw.get("candles"), list) else [],
        now=now,
        completed_only=now is not None,
    )
    current = number(raw.get("current_price"))
    previous = number(raw.get("previous_close"))
    unavailable = set(str(item) for item in raw.get("unavailable_values", []) if isinstance(item, str))
    result = {
        "symbol": symbol, "current_price": current, "vwap": indicators["vwap"],
        "ema9": indicators["ema9"], "ema20": indicators["ema20"], "previous_close": previous,
        "intraday_change_percent": (current - previous) / previous if current is not None and previous not in (None, 0) else None,
        "candles": indicators["candles"],
    }
    unavailable.update(name for name, value in result.items() if name != "symbol" and value is None)
    result["unavailable_values"] = sorted(unavailable)
    return result
