"""Pure trade-plan construction; alpha does not choose quantity."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from execution.models import TradePlan, build_trade_plan


class PortfolioConstructor:
    """Convert a risk-approved candidate into the existing typed TradePlan."""

    def construct(self, candidate: Mapping[str, Any], risk_result: Mapping[str, Any],
                  market_data: Mapping[str, Any], *, now: datetime) -> TradePlan:
        if risk_result.get("approved") is not True:
            raise ValueError("portfolio construction requires risk approval")
        return build_trade_plan(candidate, risk_result, market_data, now=now)
