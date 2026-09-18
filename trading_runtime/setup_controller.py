"""Setup identity and confirmation; no execution or position ownership."""
from hashlib import sha256
from dataclasses import asdict
import config
from trading_runtime.contracts import AlphaSnapshot, ComponentProvenance, SetupEpisode


def episode_id(symbol, research_cycle_id):
    return symbol.upper() + '-' + sha256(str(research_cycle_id).encode()).hexdigest()[:24]


class SetupController:
    @staticmethod
    def episode(context):
        return SetupEpisode(context.episode_id, context.symbol, context.research_timestamp,
                            context.research_cycle_id, context.analysis_price, context.slow_context_score,
                            context.candidate_state, (('support', context.intraday_support_reference),
                                                      ('resistance', context.intraday_resistance_reference)))

    @staticmethod
    def confirm(context, dynamic):
        if dynamic >= config.COORDINATOR_TRADE_CANDIDATE_THRESHOLD:
            context.consecutive_qualifying_updates += 1
            context.status = 'PENDING_CONFIRMATION'
        else:
            context.consecutive_qualifying_updates = 0
            context.status = 'WATCH'
            context.candidate_state = 'SETUP_FORMING'
        return context.consecutive_qualifying_updates >= config.FAST_ENTRY_CONFIRMATION_UPDATES

    @staticmethod
    def alpha(context, now):
        provenance = []
        supplied = context.metadata.get('score_provenance', {})
        for name in ('technical', 'news', 'sector', 'market', 'qualitative'):
            fields = supplied.get(name, {})
            provenance.append(ComponentProvenance(
                component=name, status=fields.get('status', 'UNKNOWN'),
                source=fields.get('source'), source_timestamp=fields.get('source_timestamp'),
                fallback_used=fields.get('fallback_used'), cache_hit=fields.get('cache_hit'),
                context=fields.get('context'),
            ))
        return AlphaSnapshot(
            context.episode_id, context.symbol, now.isoformat(), context.technical_context_score,
            context.news_score, context.sector_score, context.market_score, context.qualitative_score,
            context.slow_context_score, context.live_market_score, context.dynamic_score,
            context.slow_weight, context.live_weight, tuple(provenance),
        )

    @staticmethod
    def infrastructure_reason(reason):
        return any(token in reason for token in (
            'QUOTE_', 'REFRESH_FAILED', 'REFRESH_UNAVAILABLE', 'STRUCTURE_STALE',
            'STRUCTURE_UNAVAILABLE', 'TIMESTAMP_', 'REQUIRED_FIELDS_PRESENT',
            'MINIMUM_CANDLES', 'REFRESH_MALFORMED', 'SYMBOL_MISMATCH',
        ))
