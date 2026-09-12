"""Typed execution boundary shared by shadow and future live adapters."""

from execution.models import ExecutionResult, TradePlan, TradePlanError

__all__ = ["ExecutionResult", "TradePlan", "TradePlanError"]
