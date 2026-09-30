"""Lane B: deterministic, causal opportunity generation for the ActivTrades DEMO trader.

Closed M5 bars (+ latest quote) -> frozen V2 family specs -> ``OpportunitySnapshot`` (pre-decision)
-> static demo policy -> ``Decision`` (+ ``TradeIntent`` when accepted). No MT5, no search, no LLM.
"""

from demo.opportunity.bar_source import BarSource, Quote, ReplayBarSource
from demo.opportunity.engine import InMemorySeenStore, OpportunityEngine, SeenStore
from demo.opportunity.policy import POLICY_ID, PolicyConfig, StaticDemoPolicy
from demo.opportunity.production_spec import ProductionSpecSet, load_production_spec
from demo.opportunity.replay import replay_opportunities

__all__ = (
    "POLICY_ID", "BarSource", "InMemorySeenStore", "OpportunityEngine", "PolicyConfig",
    "ProductionSpecSet", "Quote", "ReplayBarSource", "SeenStore", "StaticDemoPolicy",
    "load_production_spec", "replay_opportunities",
)
