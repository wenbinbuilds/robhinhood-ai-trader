"""Small typed boundary models independent of MCP SDK response classes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: Mapping[str, Any]
    value: Any
    duration_seconds: float


@dataclass
class DirectTiming:
    connection_latency_seconds: float | None = None
    scanner_latency_seconds: float | None = None
    quote_latency_seconds: float | None = None
    historical_latency_seconds: float | None = None
    candidate_collection_latency_seconds: float | None = None
    indicator_calculation_latency_seconds: float | None = None
    snapshot_normalization_latency_seconds: float | None = None
    snapshot_total_seconds: float | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "connection_latency_seconds": self.connection_latency_seconds,
            "scanner_latency_seconds": self.scanner_latency_seconds,
            "quote_latency_seconds": self.quote_latency_seconds,
            "historical_latency_seconds": self.historical_latency_seconds,
            "candidate_collection_latency_seconds": self.candidate_collection_latency_seconds,
            "indicator_calculation_latency_seconds": self.indicator_calculation_latency_seconds,
            "snapshot_normalization_latency_seconds": self.snapshot_normalization_latency_seconds,
            "snapshot_total_seconds": self.snapshot_total_seconds,
            "calls": list(self.calls),
        }
