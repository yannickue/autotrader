# Engine-agnostic safety contracts

These tests pin proven safety behavior through thin adapters (`conftest.py`), so a
future Nautilus-based implementation can be dropped in by adding one adapter.

* `EntryAdmission` (`admit`, `admit_reduce_only`): registered implementations are
  `pure_policy` (`risk.policy.RiskPolicyEvaluator`) and `legacy_engine`
  (`risk.engine.RiskEngine`, shadow oracle). Every risk contract runs against BOTH.
* `RuntimeUnderTest` (`open_long`, `submit_signal`, `exit_tick`, `halt_execution`,
  `replay_last_exit_fill`): registered implementation is `legacy_paper_pipeline`.
* MT5 contracts use `FakeMT5Client` and per-test lock files only.
* Adding an implementation: append to `ADMISSION_IMPLEMENTATIONS` /
  `RUNTIME_IMPLEMENTATIONS` in `conftest.py` (TODO(nautilus) markers). No other
  file may import Nautilus.

Test ids below are function-name prefixes; the risk ones are parametrized
`[pure_policy]` / `[legacy_engine]`, lifecycle ones `[legacy_paper_pipeline]`.

| # | Contract | Contract tests | Pre-existing tests that also pin it | Gaps / xfails |
|---|----------|----------------|-------------------------------------|---------------|
| 1 | Hard 30x leverage ceiling (ceiling, not a target) | `test_risk_entry_contracts.py::test_c01_*` | `tests/unit/risk/test_engine.py` (`test_risk_policy_rejects_leverage_above_system_ceiling`, `test_instrument_limits_reject_...`, `test_decision_post_init_rejects_leverage_above_max`); `tests/integration/test_e2e_paper_path.py::test_leverage_cap_binds_at_20x_and_policy_over_cap_raises (explicit 20x configured cap)` | The ceiling in `PositionSizer.max_leverage` is redundant with the construction checks for policy and instrument; only the account cap (unchecked at construction) can exceed 30, and the test covers that (50x account cap, parametrized at 20x and 30x). No case can isolate the `MAX_SYSTEM_LEVERAGE` term itself. |
| 2 | Position sizing (risk budget, cost allowance, round down to step) | `test_c02_*` | `test_engine.py::test_sizes_linear_usdm_by_risk_and_rounds_down_to_step`, `..._confidence_never_influences_sizing`, `test_size_below_minimum_rejects`; `tests/property/test_risk_invariants.py` | none |
| 3 | Stop-distance sizing, invalid stop side (`INVALID_STOP`) | `test_c03_*` | `test_engine.py::test_invalid_stop_*`; `test_e2e_paper_path.py::test_invalid_stop_distance_is_rejected` | none |
| 4 | Executable reference price (ask+slip BUY / bid-slip SELL, never `entry_price`, `ENTRY_PRICE_DEVIATION`) | `test_c04_*` | `test_engine.py::test_reference_price_uses_*`, `test_entry_price_far_from_market_*`, `..._tolerance_widens_...` | none |
| 5 | Spread guard | `test_c05_*` | `test_engine.py::test_spread_too_wide_rejects`; `test_e2e_paper_path.py::test_risk_reject_wide_spread_creates_zero_exposure` | none |
| 6 | Stale / non-live data | `test_c06_*` | `test_engine.py::test_data_stale_rejects`, `test_data_future_timestamp_rejects_stale`, `test_data_not_live_rejects` | none |
| 7 | Stale signal | `test_c07_*` | `test_engine.py::test_signal_stale_rejects`; `test_e2e_paper_path.py::test_stale_signal_is_rejected_with_zero_exposure` | none |
| 8 | Account known required | `test_c08_*` | `test_engine.py::test_account_unknown_rejects`; `test_e2e_paper_path.py::test_unknown_account_state_blocks_exposure` | none |
| 9 | Account reconciled required (NOT_RECONCILED, RECONCILING, MISMATCH) | `test_c09_*` (entry), `test_risk_reduce_only_and_halt_contracts.py::test_c15_reduce_only_requires_a_reconciled_account` | `test_engine.py::test_account_unreconciled_rejects` (NOT_RECONCILED only); `tests/unit/risk/test_policy_purity_parity.py`; `tests/unit/pipeline/test_reconciliation_and_daily_pnl.py`; `tests/unit/pipeline/test_paper.py::test_reduce_only_exit_blocked_during_reconciliation_mismatch_halt` | none |
| 10 | Daily-loss gate (+ single-trading-day PnL window) | `test_c10_*` | `test_engine.py::test_daily_loss_limit_rejects`; `test_paper.py::test_daily_loss_limit_trips_from_fees_alone_with_zero_price_pnl`; `tests/unit/risk/test_trading_day.py`; `tests/unit/portfolio/test_daily_pnl.py` | none |
| 11 | Drawdown gate | `test_c11_*` | `test_engine.py::test_drawdown_limit_rejects` | none |
| 12 | Consecutive-loss limit and risk reduction | `test_c12_*` | `test_engine.py::test_consecutive_loss_limit_rejects`, `test_reduce_risk_mode_applies_multiplier_on_drawdown` | none |
| 13 | Liquidity constraint | `test_c13_*` | `test_engine.py::test_liquidity_insufficient_rejects` | none |
| 14 | Gross/net exposure limits incl. pending exposure | `test_c14_*` (pending injected via `import_state` on the legacy adapter, `PendingExposure` on the pure one) | `test_engine.py::test_exposure_limit_*`, `test_reservation_counts_toward_gross_capacity`, `test_imported_reservation_feeds_real_capacity_math_...`; `test_e2e_paper_path.py::test_second_long_cannot_exceed_max_gross_notional` | none |
| 15 | Reduce-only never adds exposure / not above position / wrong side / no position | `test_risk_reduce_only_and_halt_contracts.py::test_c15_*` | `test_engine.py::test_reduce_only_*`, `test_reduce_only_reports_zero_exposure_increasing_notional`; `tests/unit/execution/test_reduce_only_fill_clamp.py` | none |
| 16 | Kill switch / latched halt block NEW exposure; reduce-only still allowed (account known + RECONCILED) | `test_c16_*` | `test_engine.py::test_kill_switch_rejects_halted`, `test_runtime_mode_halted_rejects_halted`, `test_halt_helper_latches_engine`, `test_reduce_only_allowed_while_halted`; `test_paper.py::test_reduce_only_exit_allowed_during_overfill_halt` | none |
| 17 | No same-tick flip through zero; concurrent reduce-only reservations cannot jointly exceed the position | `test_c17_*` (both adapters, reservation supplied as input); `test_c17_legacy_only_concurrent_reduce_only_reservations_cannot_jointly_flip` (LEGACY-ONLY: reservation ledger is legacy state) | `test_engine.py::test_reduce_only_rejects_quantity_exceeding_position`, `test_reduce_only_concurrent_requests_cannot_jointly_flip_position`; `test_paper.py::test_signal_reversal_closes_position_and_next_tick_can_open_opposite` (also exercised as contract 18) | The stateful reservation sequence (approve, reserve, reject, release) has no pure-policy equivalent by design; the pure evaluator only takes the reserved quantity as an input. |
| 18 | No implicit pyramiding (and no same-tick reversal flip) | `test_lifecycle_contracts.py::test_c18_*` | `test_paper.py::test_position_already_open_blocks_pyramiding_same_direction_signal`, `test_signal_reversal_closes_position_and_next_tick_can_open_opposite` | Only the legacy pipeline enforces this (`PipelineStage.POSITION_ALREADY_OPEN`); the pure risk policy does not know about open positions on the entry path, so this is a runtime contract, not a risk-policy one. |
| 19 | Emergency exit stays separate from normal exits | `test_c19_*` | `tests/unit/exits/test_engine.py::test_emergency_exit_overrides_every_other_rule`, `test_stale_market_data_forces_emergency_close_fail_closed`; `test_paper.py::test_emergency_exit_closes_position_on_stale_market_data`, `test_halt_and_kill_switch_do_not_by_themselves_trigger_any_exit_decision` | The runtime adapter cannot inject `risk_halt=True` into the exit engine (the pipeline hard-wires it to False by design, Q-X3), so the emergency path is reached only through stale data. |
| 20 | Partial take-profit idempotency (replayed tick, duplicate fill delivery, ladder) | `test_c20_*` | `tests/unit/exits/test_engine.py::test_partial_close_does_not_retrigger_after_target_already_taken`, `test_tp*_stage_*`, `test_no_third_stage_*`; `tests/unit/pipeline/test_persistence_recovery.py::test_recovery_after_partial_take_profit_restores_stage_and_remaining_size`, `..._before_partial_take_profit_persist_shows_pre_stage_state_and_refires_once`; `test_e2e_paper_path.py::test_duplicate_fill_is_counted_once` | Crash/restart replay (persisted store) is NOT in the adapter: it needs a persistence store fixture. It stays pinned only by `test_persistence_recovery.py`. Duplicate delivery is modelled as the same fill id re-reported (`report_fill`); a duplicate `TradeEvent` of a market exit is not applicable (market exits do not go through `on_trade`). |
| 21 | Broker account mismatch fails closed | `test_mt5_contracts.py::test_c21_*` | `tests/unit/adapters/activtrades_mt5/test_account_protection.py` | none |
| 22 | Attach-only default (no login/credentials unless `MT5_ALLOW_ACCOUNT_LOGIN=1` from process env) + single-owner lock (`CONNECTION_BUSY`) | `test_mt5_contracts.py::test_c22_*` | `test_account_protection.py`, `tests/unit/adapters/activtrades_mt5/test_lock.py`, `tests/unit/adapters/test_config.py` (`test_allow_account_login_*`) | `bounded.py` forwarding of the env flag to the worker process is not covered here (see `tests/unit/adapters/activtrades_mt5/test_bounded.py`). |

## xfail list

None. Every contract was expressible against current behavior without editing `src/`.
