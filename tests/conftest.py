"""Path-based test tiering and marking (no per-file edits needed).

Every collected test receives exactly ONE tier marker:

* ``slow``        heavy research / real-data / replay / chaos / parity suites (files listed in
                  ``SLOW_FILES`` or living under ``SLOW_DIRS``; measured >= ~9 s per file);
* ``integration`` framework / persistence / subprocess / sqlite / runner / MT5-harness boundaries;
* ``fast``        everything else (pure unit, contracts, property).

The tiers partition the suite, so FULL == fast + integration + slow. On top of the tier, tests get
overlay markers:

* ``safety``  risk / execution / reconciliation / persistence / reduce-only / idempotency /
              stale-signal / exposure / leverage invariant suites (see ``SAFETY_PREFIXES``);
* ``chaos``, ``replay``, ``broker`` by path;
* ``serial``  files that share process-global or on-disk resources (sqlite files, subprocesses,
              MT5 lock/probe, wall-clock heartbeats). They must not be spread across xdist workers
              concurrently with each other; with xdist they share one worker via ``xdist_group``.

Nothing here deselects, skips or xfails a test. ``docs/TEST_TIMING.md`` documents the measurements
behind these lists; update both together.
"""

from __future__ import annotations

import pytest

# --- tier: slow -------------------------------------------------------------------------------
SLOW_DIRS = (
    "tests/chaos/",
    "tests/replay/",
    "tests/parity/",
    "tests/unit/demo/opportunity/",  # real-slice causality/parity/catch-up (2-25 s each)
    "tests/unit/demo/learning/",  # MLflow/river/lightgbm training
)
SLOW_FILES = frozenset(
    {
        "tests/temporal/test_real_events_causality.py",
        "tests/temporal/test_prefix_cache_equivalence.py",
        "tests/temporal/test_kernel_reference_parity.py",
        "tests/temporal/test_spec_validate.py",
        "tests/events/test_event_prefix_equality.py",
        "tests/test_ar2_fast_runner.py",
        "tests/test_v2_probe_runner.py",
        "tests/test_alpha_fast_kernels_a.py",
        "tests/test_alpha_fast_kernels_c.py",
        "tests/test_v2_families_planted.py",
        "tests/test_discovery_stages.py",
        "tests/test_ad1_benchmark.py",
        "tests/test_v2_rawscan_core.py",
        "tests/test_alpha_fast_sim_target_guard.py",
        "tests/test_alpha_fast_screen.py",
        "tests/test_v2_probe_null.py",
        "tests/test_temporal_discovery_search.py",
        "tests/test_v2_multimarket_loader.py",
        "tests/test_alpha_fast_price_action.py",
        "tests/test_formula_alpha_gp.py",
        "tests/test_v2_metalabel_eval.py",
        "tests/test_formula_alpha_real.py",
        "tests/test_v2_probe_clock.py",
        "tests/test_v2_directional_context.py",
        "tests/test_v2_multimarket_frame.py",
        "tests/test_discovery_grammar.py",
        "tests/test_temporal_discovery_genome.py",
        "tests/test_temporal_discovery_evaluate.py",
        "tests/test_v2_session_levels.py",
        # GATE A: real OpportunityEngine replayed over real DEV slices, observer on/off
        # (~130 s, 12 tests): real-data parity suite, kept out of the < 2 min FAST loop.
        "tests/unit/demo/test_observer_parity.py",
        # research workbench: real Nautilus BacktestEngine x ~12 scenarios (~250 MB each, ~40 s)
        "tests/unit/research_workbench/test_differential_e2e.py",
        "tests/unit/research_workbench/test_differential_e2e_nonvacuity.py",  # starts Nautilus engines
        "tests/unit/research_workbench/test_netting_e2e.py",  # same-bar re-entry / trending scenarios through Nautilus
        "tests/unit/research_workbench/test_netting_e2e_regression.py",  # original 10-ignored-candidates regression (Nautilus)
        "tests/unit/research_workbench/test_workbench_compare_integration.py",  # compare through the real Nautilus engine (~17 s)
        "tests/unit/research_workbench/test_workbench_fastrun.py",  # real FeatureStore/numba/shadow-lab (~28 s)
        "tests/unit/research_workbench/test_workbench_report_cli.py",  # CLI smoke incl. fast run (~15 s)
    }
)

# --- tier: integration ------------------------------------------------------------------------
INTEGRATION_PREFIXES = (
    "tests/integration/",
    "tests/unit/demo/runner/",
    "tests/unit/demo/store/",
    "tests/unit/demo/execution/test_live_stack.py",
    "tests/unit/nautilus_mt5/",
    "tests/unit/nautilus_kernel/",
    "tests/unit/adapters/",
    "tests/unit/persistence/",
    "tests/unit/scripts/",
    "tests/unit/research/test_backtest_cli.py",
    "tests/events/",
    "tests/temporal/test_real_events_frame.py",
)

# --- overlays ---------------------------------------------------------------------------------
SAFETY_PREFIXES = (
    "tests/contracts/",
    "tests/property/",
    "tests/chaos/",
    "tests/parity/",
    "tests/integration/test_e2e_paper_path.py",
    "tests/unit/test_contract_types.py",
    "tests/unit/risk/",
    "tests/unit/execution/",
    "tests/unit/exits/",
    "tests/unit/margin/",
    "tests/unit/portfolio/",
    "tests/unit/persistence/",
    "tests/unit/pipeline/",
    "tests/unit/nautilus_mt5/",
    "tests/unit/adapters/",
    "tests/unit/data/",
    "tests/unit/health/",
    "tests/unit/markets/",
    "tests/unit/instruments/",
    "tests/unit/demo/execution/",  # live risk policy, sizing, tranches, live stack, parity
    "tests/unit/demo/store/",
    "tests/unit/demo/runner/",  # reconciliation, heartbeat, closed-market/stale handling
)
CHAOS_PREFIXES = ("tests/chaos/",)
REPLAY_PREFIXES = ("tests/replay/", "tests/parity/")
BROKER_PREFIXES = (
    "tests/unit/nautilus_mt5/",
    "tests/unit/adapters/",
    "tests/contracts/test_mt5_contracts.py",
    "tests/unit/demo/execution/test_live_stack.py",
    "tests/unit/scripts/test_mt5_scripts.py",
)
SERIAL_FILES = frozenset(
    {
        "tests/unit/adapters/activtrades_mt5/test_bounded.py",
        "tests/unit/demo/execution/test_live_stack.py",
        "tests/unit/demo/learning/test_learning_shadow.py",
        "tests/unit/demo/opportunity/test_opp_spec.py",
        "tests/unit/demo/runner/test_runner_accounting.py",
        "tests/unit/demo/runner/test_runner_lane_i.py",
        "tests/unit/demo/runner/test_runner_resilience.py",
        "tests/unit/demo/runner/test_heartbeat_resilience.py",
        "tests/unit/demo/store/test_demo_store_crash.py",
        "tests/unit/nautilus_mt5/test_c65_executor.py",
        "tests/unit/persistence/test_reconciliation_source.py",
        "tests/unit/persistence/test_store.py",
        "tests/unit/research/test_backtest_cli.py",
        "tests/unit/risk/test_policy_purity_parity.py",
        "tests/unit/scripts/test_mt5_scripts.py",
        "tests/test_alpha_fast_sim_target_guard.py",
    }
)


def _rel(item: pytest.Item) -> str:
    return item.path.relative_to(item.config.rootpath).as_posix()


def _starts(rel: str, prefixes: tuple[str, ...]) -> bool:
    return rel.startswith(prefixes)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        rel = _rel(item)
        if rel in SLOW_FILES or _starts(rel, SLOW_DIRS):
            tier = "slow"
        elif _starts(rel, INTEGRATION_PREFIXES):
            tier = "integration"
        else:
            tier = "fast"
        item.add_marker(getattr(pytest.mark, tier))
        if _starts(rel, SAFETY_PREFIXES):
            item.add_marker(pytest.mark.safety)
        if _starts(rel, CHAOS_PREFIXES):
            item.add_marker(pytest.mark.chaos)
        if _starts(rel, REPLAY_PREFIXES):
            item.add_marker(pytest.mark.replay)
        if _starts(rel, BROKER_PREFIXES):
            item.add_marker(pytest.mark.broker)
        if rel in SERIAL_FILES:
            item.add_marker(pytest.mark.serial)
            # Only effective under `pytest -n N --dist loadgroup`; inert otherwise.
            item.add_marker(pytest.mark.xdist_group("serial"))
