"""Deterministic Sprint 1 paper-trading glue: MarketSnapshot -> Signal -> Risk -> Execution."""

from pipeline.paper import PaperTradingPipeline, PipelineOutcome, PipelineStage

__all__ = ["PaperTradingPipeline", "PipelineOutcome", "PipelineStage"]
