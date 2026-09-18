"""Analysis components, loaded lazily to keep strategy imports independent."""

from importlib import import_module

_EXPORTS = {
    'CandidateAnalyzer': ('agent.candidate_analyzer', 'CandidateAnalyzer'),
    'CandidateData': ('agent.candidate_analyzer', 'CandidateData'),
    'CandidateDecision': ('agent.candidate_analyzer', 'CandidateDecision'),
    'CoordinatorAgent': ('agent.coordinator', 'CoordinatorAgent'),
    'reasoning_dashboard_projection': ('agent.dashboard_projection', 'reasoning_dashboard_projection'),
    'MacroAgent': ('agent.macro_agent', 'MacroAgent'),
    'LlmReasoningBridge': ('agent.llm_reasoning_bridge', 'LlmReasoningBridge'),
    'JsonSnapshotProvider': ('agent.market_cycle', 'JsonSnapshotProvider'),
    'MarketCycle': ('agent.market_cycle', 'MarketCycle'),
    'NewsAgent': ('agent.news_agent', 'NewsAgent'),
    'SectorAgent': ('agent.sector_agent', 'SectorAgent'),
    'TechnicalAgent': ('agent.technical_agent', 'TechnicalAgent'),
}


def __getattr__(name):
    if name not in _EXPORTS: raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value

__all__ = [
    "CandidateAnalyzer",
    "CandidateData",
    "CandidateDecision",
    "CoordinatorAgent",
    "JsonSnapshotProvider",
    "MacroAgent",
    "LlmReasoningBridge",
    "MarketCycle",
    "NewsAgent",
    "SectorAgent",
    "TechnicalAgent",
    "reasoning_dashboard_projection",
]
