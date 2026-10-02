# Research Workbench v1 — Report (Nacht 01.→02.10.2026)

Status: **DRAFT während der Nacht** (wird beim Freeze finalisiert). Branch `research/workbench-v1`, Basis `release/2026-10-02-final` @ cdc8a56.
**Production = `sprint1/integration` @ 11c6aec, unverändert; nichts hieraus wird morgen in Production gemergt.**
Evidence-Tags: MEASURED / MODELED / NOT_MEASURED; VERIFIED = Code/Test/Codex gesehen.

## A. BASELINE
- Production: sprint1/integration 11c6aec (Tree clean, STOP gesetzt, kein Runner/Supervisor). Release-Candidate cdc8a56 (Observer + research_speed) als Entwicklungsbasis.
- Vorhanden und wiederverwendet (nicht neu gebaut): FeatureStore (`alpha/fast/store.py`), StrategySpec (`alpha/fast/spec.py`), `simulate_fast` (`alpha/fast/sim.py`), Fast-Screen (`alpha/fast/screen.py`), Nautilus-BacktestEngine (`nautilus_kernel/backtest.py`), `research_speed` (artifact, importgraph, parallel, scheduler, segments, progress, runlock), Entry/Exit-Analytics (`demo/entry_exit_quality.py`, `demo/shadow_exit_lab.py`, `demo/exit_policies.py`).
- Basis-Befund: `alpha/fast/__init__.py` importiert `store.py`; der Live-Trader lädt über `alpha.fast.sim` (CandidateArrays, EXIT_TRAIL) daher FeatureStore/TA-Lib beim Import (kein Aufruf von `simulate_fast`).

## B. BRANCH / COMMITS (Stand jetzt; wird beim Freeze aktualisiert)
2877cc9 FeatureStore Härtung · e01df5d FeatureStore Review-Fixes · f292475 Test-Tiers/Impact/Import-Manifest · 4581fa5 Review-Fixes dazu · 16f709f Fast↔Nautilus Differential + Golden · 9fa868d FeatureStore Runde 2 · 5380524 Workbench-Kern · 108370f Differential Review-Fixes · 666eaaa Boundary-Docs · bedb557 Workbench Review-Fixes + Compare · 5d94d44/25f6e1d Kausalitätstest. Offen (uncommitted, in Arbeit): DAG-Transaktion, Right-Tail-NaN, Netting-Diagnose.

## C. COMPONENTS REUSED
FeatureStore.build/load_or_build, StrategySpec/evaluate_spec, simulate_fast, light_screen, Nautilus BacktestEngine (neue Replay-Strategie in eigener Datei), research_speed.importgraph/artifact/segments/parallel/scheduler, demo.entry_exit_quality/exit_policies/shadow_exit_lab. Kein zweiter Scheduler, Feature Store, Strategy-Language, Simulator oder Importgraph.

## D. NEW COMPONENTS
`src/research_workbench/{status,experiment,dag,fastrun,stage_adapters,compare,report,entry_exit_adapter,differential,golden}.py`, `src/nautilus_kernel/replay_backtest.py` (nur neue Datei, `backtest.py`/`proof_strategy.py` unberührt), `scripts/research_strategy.py` (einziger Einstieg), `scripts/runtime_import_manifest.py`, Docs `research_workbench_interfaces.md`.

## E. FEATURESTORE HARDENING (FUNCTIONAL)
Atomares Publizieren (temp+fsync+os.replace, Manifest zuletzt als Commit-Marker); Manifest mit Artifact-SHA-256/Größe, Array-Namen/Dtypes/Shapes, `cache_format_version=2`; **Verified Load**: jeder Defekt = MISS (nie Absturz, nie stale Hit); Code-Fingerprint = statischer Import-Closure-Hash (`research_speed.importgraph`, lazy importiert) statt fester Fünf-Module-Liste, dynamische Imports ⇒ UNCACHEABLE; Zeitzonen-Identität (TZif-Regeldateien tzdata/zoneinfo/pytz + gesampelte Offsets 1970–2100; unlocatable ⇒ uncacheable); serialisiertes Publizieren (Lock-Datei, atomare Stale-Übernahme per Token), Publish-Fehler degradiert zu ungecachten Arrays. Importgraph: AST-Erkennung aliasierter/indirekter dynamischer Imports (28 Formen, 6 Negativfälle). Codex: 2 Review-Runden (2 High, 5 Medium, 3 Low), alle gefixt. Tests 137 passed/1 skipped. Importsicher für den Live-Trader (kein Modul-Level-Import von research_speed, `build()` unverändert).

## F. WORKBENCH CLI
`scripts/research_strategy.py plan | fast | compare | report` (+ `--demo-synthetic`, `--spec exp.json`). `plan`: LIGHT, read-only, keine Simulation, keine Writes. `fast`/`compare`: HEAVY, Exit 3 solange `demo_trader.py`/`supervisor.py` läuft (unlesbare Prozesstabelle zählt als „läuft“; Override `RESEARCH_SPEED_ALLOW_WITH_LIVE`); BLAS=1, harden_process. `fast` liefert ausschließlich `REJECT_FAST | PROMOTE_TO_FIDELITY`; `PromotionStatus` hat kein Live-Mitglied (Test). `compare` mappt PASS→READY_FOR_ROBUSTNESS, FAIL→FIDELITY_MISMATCH, BLOCKED/ERROR→nicht befördert.

## G. ARTIFACT DAG
Keys (sha256 kanonisches JSON, jeder enthält den vorherigen): FEATURES (dataset_hash, data_range, feature_config, feature_code_hash, library versions) → SIGNALS (+spec_hash, signal_code_hash, adapter_code_hash) → SIMULATION (+cost, sizing, rules, window, sim_code_hash, adapter_code_hash) → METRICS (+metric_version, split, partitions_read, gate, screen code hash) → FIDELITY (signals+simulation key, replay config, nautilus_trader-Version, differential/golden/replay Code-Hash) → REPORT (Key-Funktion). Verified Load (sha256+Größe), Unsicherheit ⇒ MISS, Stage-Status PENDING/RUNNING/COMPLETE/FAILED.

## H. CACHE INVALIDATION — Speed-Invarianten (VERIFIED durch Tests)
Identischer Lauf ⇒ alle HIT · nur Metric-Änderung ⇒ Features/Signals/Simulation HIT, Metrics MISS · nur Kosten ⇒ Features/Signals HIT, Simulation MISS · Entry-Regel ⇒ Features HIT, Signals/Simulation MISS · cold == warm · Wrapper == direkte API (bit-identisch) · veralteter Adapter-Code ⇒ MISS.

## I. CHECKPOINT / RESUME
Markt×Stage-Artefakte: Fehler in Markt B lässt Markt A COMPLETE; Re-Run ⇒ A HIT. Ctrl+C/Interrupt hinterlässt kein COMPLETE. DAG-Publish wird gerade transaktional gemacht (Content-Addressed: gültige Publikation = No-Op; sonst temp-Verzeichnis + atomares Umbenennen). jobs=1 == jobs=2 mit Thread-Stand-in getestet; **echter Process-Pool-Test: SKIPPED (RAM < 1,5 GB) = NOT_MEASURED**.

## J. FAST SIM STATUS (FUNCTIONAL)
`simulate_fast`: Numba, vorallokierte Arrays, kein SQLite/JSON/Logging pro Bar. Fill = nächste Bar-Open (+max(spread)·mult + slip), Stop vor Target in derselben Bar, Short auf Ask-Basis, Target ohne Slippage, Session-/Data-Gap-Exit zum Open, Day-End zum Close (sim.py:366-479). Bottleneck Candidate×Holding-Scan. Nicht vorhanden: Varianten-Fusion, Parallelismus in `simulate_many`.

## K. NAUTILUS FIDELITY STATUS (FUNCTIONAL, jetzt mit Candidate-Replay)
Echter BacktestEngine (Nautilus 1.231). Replay: Entry als Market-Order in `on_bar` zum Decision-Close (BUY Ask / SELL Bid), Stop/Target als Brackets in `on_order_filled`, forced exit via `close_position`. Gemessene Semantik: Slippage nur 1 Tick (FillModel prob_slippage=1) auf Market-/Stop-/Limit-Fills; STOP_MARKET füllt zum Trigger, bei Gap zum Bar-Open; Targets reduce-only LIMIT zum Limit; Tick-Reihenfolge je Bar O,H,L,C (Long: Target vor Stop, Short: Stop vor Target); Matching-Engine ist Rust (kein Quelltext).

## L. FAST ↔ FIDELITY DIFFERENTIAL (FUNCTIONAL; 12 Golden-Szenarien PASS; Methodik 2× Codex-reviewed)
Normalisierte Trades beider Seiten; Feldklassen EXACT_MATCH / TOLERANCE_MATCH / EXPECTED_ABSTRACTION / BROKER_FIDELITY_DIFFERENCE / BUG_SUSPECTED; unerklärte Differenz ⇒ FAIL; Status PASS/FAIL/BLOCKED/ERROR; Legs ENTRY_/EXIT_MISMATCH/BOTH. **Offenlegung:** Richtung, Stop, Target, Menge, Signal-/Decision-Zeitstempel kommen aus FAST (BY_CONSTRUCTION, nicht unabhängig validiert, zählen nicht als „matched“); der Differential validiert Ausführung/Fills/Exits, **nicht** Candidate-Generierung oder Sizing (Sizing: unabhängiges Dezimal-Orakel in den Golden-Tests). Joint-Felder (R, PnL) werden nur aus Preis-Deltas und geteilten Inputs erklärt (nicht zirkulär) plus Konsistenzprüfung je Engine.
Gefundene Engine-Unterschiede: Entry-Timing (Next-Open vs Decision-Close, EXPECTED_ABSTRACTION), Slippage ≠ 0/1 Tick oder Limit-Slippage (BROKER_FIDELITY_DIFFERENCE), Intrabar-Reihenfolge Long (Nautilus optimistisch, EXPECTED_ABSTRACTION), STOP_GAP-Zeitstempel eine Bar später, DATA_GAP-Exit (EXPECTED_ABSTRACTION).
**ECHTER BEFUND (Diagnose läuft):** auf einem nicht-trivialen synthetischen Markt (14 Tage, RSI<30 Long) FAST 33 Trades vs Nautilus 23 (Replay: `position_or_order_active`) — Verdacht: FAST lässt überlappende/gleich-Bar-Re-Entries zu, das reale Konto ist Netting (eine Position je Instrument). **Ergebnis: siehe Abschnitt T (offene Risiken) / Nachtrag.**

## M. GOLDEN DATASET
12 deterministische synthetische Szenarien (long/short target, long/short stop, gap-through-stop, target-crossed-at-fill, session end, data gap, spread+1-Tick Stop/Target, Stop+Target in derselben Bar), je Version + gepinnte sha256 (kanonisch: Name/Dtype/Shape/Little-Endian) für Daten und Candidates; erwartete FAST-Zahlen handgerechnet unabhängig von `simulate_fast`. `research/reference/ar2_ref12k/golden.pkl` bleibt FAST-vs-Referenzimplementierung (kein Fidelity-Golden). Keine realen Daten erfunden.

## N. TEST SPEED
`tests/unit/demo/test_observer_parity.py` (12 Tests, ~130 s) FAST→SLOW; Tiers partitionieren weiterhin (fast 4500→4488, slow 759→771; neue Guard-Tests +55). `MOVED_OUT_OF_FAST` + Guard sorgt dafür, dass `-m fast`-basierte Pläne verschobene Tests weiter per Pfad wählen. Impact: Live-erreichbares Alpha → `tests/unit/demo` + `-m safety`; sicherheitskritische Pfade → Safety-Overlay; `git diff`-Fehler laut. Import-Manifest `scripts/runtime_import_manifest.py` (146/370 `src`-Dateien live-erreichbar, 33 Alpha-Dateien, 0 dynamische Imports, `--check`, LAUNCHERS-Tabelle). Keine Testabdeckung entfernt, keine skip/xfail-Tricks.

## O. MEASURED TIMINGS (synthetisch, 5 880 M5 Bars; RAM-knappe, geteilte Maschine)
| Stufe | Zeit | Evidenz |
|---|---|---|
| FeatureStore build cold | 3,7 s (zweiter Lauf 1,6 s) | MEASURED |
| FeatureStore warm load (SHA-verifiziert) | 0,66 s | MEASURED |
| Signals (evaluate_spec + Market-Arrays) | ~0,7 s | MEASURED |
| Simulation | 11 s cold-numba / 1,6 s mit Numba-Cache / 0,09 s warm | MEASURED |
| Metrics | 0,01–0,02 s | MEASURED |
| Entry/Exit-Diagnostic (119 Entries, 100 Lab-Entries) | 2–2,8 s | MEASURED |
| Voller warmer Lauf | 5,6 s (überwiegend Python-Imports/Cache-Verifikation) | MEASURED |
| Golden-Szenario durch beide Engines | erstes ~4–7 s (Imports), danach 0,05–0,17 s; ~290 MB flach über 12 Szenarien | MEASURED |
| Kleine Tests heute: ruff 2,8 s, compileall 1,0 s, `tests/unit/exits` 3,0 s | | MEASURED |
| **7-Märkte Benchmark** (7 synthetische Märkte à 40 Tage ≈ 5 900 M5-Bars, `scripts/bench_workbench.py`; RAM-Reserve 100 MB, BLAS=1, Numba-Cache vorhanden; freier RAM 763→643 MB; **gleichzeitig lief der T3-Testlauf ⇒ verrauscht**) | siehe Tabelle unten | MEASURED |
| Worker-Speicher je Prozess | psutil nicht installiert | **NOT_MEASURED** |
| T3/FULL | Fast-Segment-Zeitlimit (600 s, unter Last) — Wiederholung auf ruhiger Maschine geplant; Integration 1122 passed/1 skipped in 394 s | **teilweise MEASURED** |

**Workbench-Benchmark (MEASURED, 7 Märkte, verrauschte Maschine):**
| Szenario | Zeit | Faktor | Cache-Verhalten |
|---|---|---|---|
| COLD jobs=1 | 17,5 s | 1,00× | alles MISS (features 10,2 s, signals 13,2 s inkl. Feature-Load, simulation 1,1 s, metrics 0,02 s als Summe) |
| COLD jobs=2/3/4 (effektiv 2, `clamp_jobs` bei 763 MB frei) | 14,8 / 12,5 / 13,5 s | 1,18× / 1,39× / 1,30× | Spawn-/Import-Overhead dominiert kleine Läufe |
| WARM jobs=1 | 0,53 s | ~33× vs cold | alles HIT |
| WARM jobs=4 | 4,0 s | langsamer als jobs=1 | alles HIT; reine Process-Pool-Startkosten ⇒ für kleine/warme Läufe jobs=1 |
| RESUME: SIMULATION-Artefakt von M3 gelöscht | 0,87 s | | nur M3 SIMULATION+METRICS neu; 6 Märkte HIT |
| RESUME: SIMULATION-Artefakt von M4 korrupt (1 Byte) | 1,0 s | | nur M4 neu |
| RESUME: M6 fehlgeschlagen → Re-Run | 13,0 s (Fehlerlauf) → 2,4 s | | 6 Märkte COMPLETE/HIT, nur M6 alle vier Stufen neu |
| Speed-Invariante: nur Metric geändert | 1,13 s | 15,5× vs cold | features/signals/simulation HIT, metrics MISS |
| Speed-Invariante: nur Kosten geändert | 1,30 s | 13,5× | features/signals HIT, simulation/metrics MISS |
| Speed-Invariante: nur Entry-Regel geändert | 3,43 s | 5,1× | features HIT, signals/simulation/metrics MISS |
| Determinismus (sha256 metrics.json je Markt) jobs=1/2/3/4 | identisch | PASS | |
Main-Prozess-Spitze 193 MB (jobs=1). Echte Aussage: Feature-Building dominiert die Kaltkosten, der DAG entfernt es aus jedem Re-Run; Parallelisierung lohnt erst bei größeren Märkten/mehr RAM.

## P. PRODUCTION / RESEARCH BOUNDARY
`src/alpha/__init__.py` behauptete fälschlich „research-only“ → korrigiert: 33 live-erreichbare Alpha-Dateien (aus dem Manifest generiert) vs. Research-only; Drift-Guard-Test; `docs/ARCHITECTURE.md` Boundary-Map (Production Runtime / Shared Alpha / Offline Research / Observer-Shadow / Workbench / Fidelity). Workbench-Paket und Replay-Strategie werden von nichts Production-Erreichbarem importiert (Codex-grep + Manifest).

## Q. ALPHA SHARED CONTRACTS
Extraktion von `CandidateArrays`/`EXIT_TRAIL` aus `alpha.fast.sim` **bewusst NICHT durchgeführt**: 12 Live-Familienmodule (`alpha/families/{common,data,eod,gap,leadlag,orb,overnight,registry,roundnum,spec,structbrk,volrev}.py`) importieren ebenfalls aus `alpha.fast.sim` (CandidateArrays, EXIT_FIXED_R, MarketArrays, SimWindow), und `alpha/fast/__init__.py` zieht `store.py` mit. Nötig wäre: neutrales Modul (CandidateArrays, MarketArrays, SimWindow, EXIT_*), Importänderungen in 12 Live-Dateien + engine/policy, lazy FeatureStore-Export, Re-Export-Identitätstests, Demo-Parity — größer als der ~150-Zeilen-Rahmen; als „Open boundary debt“ dokumentiert (Post-Forward, braucht Opus/Codex-Review, da Shared-Production-Importcode).

## R. RESEARCH RUNNER INVENTORY (nur Inventar, nichts gelöscht)
23 Runner in `research/runners/` (ad1_discovery 304 L, ar2_fast 1209 L, v2_probe 796 L, v2_survival 611 L, …). Klassifikation CANONICAL / SPECIALIZED_KEEP / LEGACY_KEEP_FOR_REPRO / DEPRECATE_CANDIDATE: **NICHT erstellt (P2, Zeitbudget)** → Backlog. Normalweg für neue Strategien: Workbench.

## S. CODEX REVIEWS (Codex read-only; Builder≠Reviewer)
| Scope | Ergebnis | Disposition |
|---|---|---|
| FeatureStore (2 Runden) | 2 High, 5 Medium, 3 Low | alle gefixt (TZ-Regeldateien, Dynamic-Import-AST, Publish-Lock/Retry/Degrade, Tests geschärft) |
| Test-Tiers/Impact/Manifest | 1 High, 2 Medium | gefixt (MOVED_OUT_OF_FAST, Reviewed-Sites mit Argument, LAUNCHERS-Tabelle) |
| Differential-Methodik | 1 „Critical“ (Replay kopiert FAST-Felder), 2 High, 3 Medium, 1 Low | Critical = Offenlegung + unabhängiges Sizing-Orakel; High (zirkuläre Joint-Felder, ERROR statt BLOCKED) und Medium gefixt |
| Workbench-Kern (2 Runden) | 3 High, 3 Medium, 1 Low; Runde 2: 1 High (DAG-Atomarität), 1 Medium (Right-Tail NaN), 2 Low | Runde 1 gefixt; Runde 2 in Arbeit |
Eigene unabhängige Zusatzprüfung: Kausalitäts-Test über alle 147 Features, 7 Schnitte × 2 Seeds (Frühjahrs-/Herbst-DST) — kein Feature nutzt Zukunftsdaten (als Test committed).

## T. OPEN RISKS
- Netting-/Overlap-Unterschied FAST vs Nautilus (siehe L) — Ergebnis der Diagnose entscheidet, ob FAST-Trade-Counts ohne Netting-Anpassung vertrauenswürdig sind.
- Kausalität der Features beruht auf Truncation-Invarianz-Tests (kein formaler Beweis); Features sind bar-open-gestempelt, nutzen die ganze Bar (Entscheidung zum Close, Fill zum nächsten Open).
- Echter Multi-Prozess-Determinismus nicht ausgeführt (RAM). Process-Pool-Test existiert und überspringt mit Begründung.
- Replay nimmt Qty/Target/Stop aus FAST (by construction); Kosten-Dekomposition schwächer als Net-PnL-Vergleich; Kandidat auf demselben Bar wie der vorherige Exit wird vom Replay ignoriert.
- Markt-/Kalender-Basis des Workbench ist GER40/Berlin (Standard-SimWindow); Nicht-Berlin-Märkte brauchen lokale Minuten-Basis.
- ENTRY_EXIT bleibt Diagnostic (spread-adjusted gross; **nicht** commission/slippage/swap-vollständig; nie alleiniges Promotion-Gate). Random-Entry-Control: NOT_RUN (benötigt FamilyData). Strukturelle TP1/TP2 NOT_AVAILABLE.
- OOS-Evaluation nur über `partitions_read`; OosGate nicht ausgelöst.

## U. WHAT WAS NOT BUILT
Kein neues FeatureStore-/Strategy-/Simulator-/Fidelity-/Scheduler-System, keine neue Strategie/Observer-Feature/Controls/Gate C, kein Rust/Ray/Dask/neue DB; keine Contract-Extraktion aus `alpha.fast.sim` (siehe Q); kein Runner-Registry/Archivierungsplan (R); kein NPZ-vs-NPY-Benchmark (nicht nötig, FeatureStore warm 0,66 s); kein Robustness-Modul (`ROBUSTNESS STATUS = NOT_RUN`); keine Exit-Policy geändert (Right-Tail nur Messung).

## V. TOMORROW USAGE
Production-Checkout bleibt auf sprint1/integration @ 11c6aec (kein Branch-Wechsel!). Entwicklung im Worktree `sprint1-worktrees/workbench` (Branch `research/workbench-v1`). Normalablauf: `plan` → `fast` → bei `PROMOTE_TO_FIDELITY` `compare` → `report`. Heavy-Befehle nicht neben dem laufenden Trader (Exit 3; RAM-Policy: Safety vor Convenience — HEAVY blockiert, nur `plan`/`report` leicht).

## W. EXACT COMMANDS
(Interpreter: `trader\.venv\Scripts\python.exe`; Env `OMP_NUM_THREADS=MKL_NUM_THREADS=OPENBLAS_NUM_THREADS=1`; `PYTHONPATH=<worktree>\src`)
- `python scripts/research_strategy.py plan --demo-synthetic --artifact-root <dir> [--json]`
- `python scripts/research_strategy.py fast --demo-synthetic --artifact-root <dir> [--entry-exit] [--jobs 2] [--allow-light-only]`
- `python scripts/research_strategy.py compare --spec exp.json`
- `python scripts/research_strategy.py report --demo-synthetic --artifact-root <dir> [--format md|json] [--out FILE]`
- Import-Manifest: `python scripts/runtime_import_manifest.py --check`
- Tests (RAM-sicher, seriell): `python scripts/run_tests.py t0|t1|changed` ; Cache-/Workbench-Tests: `pytest tests/unit/research_workbench tests/unit/alpha/test_feature_store_cache_safety.py -p no:cacheprovider`

## X. EXACT COMMITS
siehe Abschnitt B (wird beim Freeze mit finaler SHA ergänzt).

## Y. MERGE PLAN
Nicht vor dem Forward-Tag. Reihenfolge nach dem Forward-Tag: (1) Test-Infra (f292475…, impact/manifest, Guard) zuerst — braucht neues `approve_deploy`, da `src`-Dateien (store.py) nur im Workbench-Branch liegen und Production-Importe berühren: **store.py/importgraph.py-Änderungen sind Production-Import-Code (alpha.fast.__init__ → store)** ⇒ Opus-/Codex-Review + Demo-Parity vor jedem Production-Merge; (2) Workbench-Paket (rein offline) separat; (3) Boundary-Contract-Extraktion als eigener Schritt.

## Z. RESEARCH_WORKBENCH_READY
Vorläufig (wird beim Freeze final gesetzt): WORKBENCH_CORE = FUNCTIONAL · FEATURESTORE_CACHE = FUNCTIONAL · FAST_ENGINE = FUNCTIONAL · FIDELITY_ENGINE = FUNCTIONAL · FAST_FIDELITY_DIFF = FUNCTIONAL (mit offengem Netting-Befund) · SIGNAL_CACHE = FUNCTIONAL (DAG-Stage) · SIM_CACHE = FUNCTIONAL (DAG-Stage) · RESUME = PARTIAL (Markt×Stage ja; echter Multi-Prozess NOT_MEASURED) · TEST_LOOP = PARTIAL · RESEARCH_BOUNDARY = PARTIAL (Docs/Guard fertig, Contract-Extraktion offen) · PRODUCTION = UNCHANGED.

---

## AA. OVERNIGHT ADDENDUM — Coverage Auditor, Market-Thesis Stack, Position-Thesis Monitor (alles OFFLINE/RESEARCH, keine Production-Änderung)

Stand: 02.10.2026 ~05:00, Branch `research/workbench-v1`, HEAD `7eb4153` (+ dieser Report-Commit). Production unverändert `sprint1/integration @ 11c6aec`.

**Commits (neu):** dcf26cf Coverage Auditor + Thesis-Contracts · 5350ac9 Coverage-Fixes nach CODEX-1 · fb50467 MarketMap/Main-Thesis/Setup-Engine/CONTINUATION_RETEST/Position-Thesis · 50765bf+db61f9a Position-Thesis-Fixes · 6b7b547 Specs/Engine-Fixes (RETEST_HELD, leere Kante fail-closed) · 47fbf54 Position-Thesis-Coverage + Thesis-Study-Adapter · 10e3902 SLOW-Registrierung · 14320c8 CODEX-4/5-Fixes · f15bbe2 Isolations-Guards erlauben research_workbench.

**Coverage Auditor (`src/research_workbench/coverage.py`, CLI `research_strategy.py coverage`):** read-only auf kopierter DB (keine -wal/-shm neben der Quelle, per Test), Klassen TRADED/HORIZON_OPEN/COUNTERFACTUAL_COMPLETE/EXPECTED_PENDING/UNEXPECTED_MISSING, eligible-Nenner ohne offene Horizonte, Phasen nie gepoolt, Epochen per git_commit, TRADED hat Vorrang vor Counterfactual (Kontaminationszähler), CLOSED-ohne-Outcome sichtbar, fehlendes Shadow-Lab = RED + no_promotion, NOT_AVAILABLE nie als 0. Top-Level `no_promotion_claim` ist wahr, sobald irgendeine Sektion (Position-Thesis NOT_AVAILABLE/pending/late events) einen Claim trägt; `promotion_claim_allowed` nur bei vollständigen Sektionen. CODEX-1: 3 High/1 Medium/1 Low → alle behoben.

**Thesis-Stack (`src/research_workbench/thesis/`):** `marketmap.py` (dünner kausaler Adapter über market_observer-Fakten, KEIN zweiter Swing-/Level-/Geometrie-Engine; Warm-up None statt Raten; Prefix-Invarianz + Spiegelsymmetrie getestet), `main_thesis.py` (NO_CLEAR_THESIS gültig, kein Score), `setup_engine.py` + `specs.py` (eine generische State-Machine, deklarative SetupSpec, legale Transitionstabelle, INVALIDATED>EXPIRED>Progress, terminal absorbierend; 1 implementierter Spec CONTINUATION_RETEST, 9 Stubs = NOT_EVALUATED), `position_thesis.py` (HEALTHY/OPPOSING_EVENT/OPPOSING_SETUP/THESIS_AT_RISK/THESIS_INVALIDATED/CLOSED; exit ≠ reverse per Test; hypothetische Exit-Varianten CONTROL/A/B/C/D auf identischer Entry-Kohorte via demo.labeling.simulate_hypothetical; Events nur mit ts==T, genau eine Beobachtung je Entscheidungsbar), `study.py` (DAG-Cache-Key mit allen Semantik-Versionen und Inputs, gematchte Kontrollen, 2×2-Ablation, Multiplizität über HypothesisRegistry inkl. Exit-Varianten, Ergebnis nie über "Research-Kandidat", `limitations`-Feld).

**Codex-Reviews:** CODEX-0 (Workbench, 12 Runden; alle Critical/High geschlossen) · CODEX-1 (Coverage) · CODEX-2/3 (Design) · Thesis-Implementierungs-Review (3 High, 1 Medium, 1 Low → behoben) · CODEX-4/5 (3 High → behoben; Medium: retrospektives Default-Matching, kein OOS/Embargo → als Limitation dokumentiert, `causal_controls`-Option). Finaler Closure-Re-Review der Fix-Commits (14320c8 etc.): 1 High + 2 Medium + 1 Low (Promotion-Claim bei NOT_AVAILABLE/pending, Registry-Cache-Identität, Late-Event-Zählung, Cleanup) → behoben in 7eb4153 (548 Tests grün). Ein erneuter Review dieses allerletzten Fix-Commits wurde nicht mehr durchgeführt (durch eigene Tests abgedeckt) — ehrliche Restlücke; kein offenes Critical/High bekannt.

**Tests (Stand):** Safety-Segment 3246 passed · Integration 1122 passed/1 skipped (Wiederholung nach den Thesis-Commits: identisch, 389 s) · Slow 830 passed/2 skipped · research_workbench-Unit-Tests 530 passed/1 skipped · Thesis-Tests 249 passed. **FAST:** `run_tests.py fast` bricht auf der geteilten 8-GB-Maschine auch ruhig nach 600 s ab (TIMEOUT_INCOMPLETE, kein Fehler; FAST ist ~5000 Tests und über Budget — bekanntes Performance-Thema, nicht durch diese Arbeit verursacht, Thesis-Anteil ~50 s). Direktlauf `pytest -m fast -n 2` ohne Segment-Limit: 5011 passed, 2 failed in 694 s; die 2 Failures waren echte Isolations-Guards (tests/unit/alpha/test_protocol_metrics.py: research_workbench importiert alpha/coverage_analysis) → Guards um das Offline-Paket erweitert, gezielter Re-Run 13 passed; Production-Reachability bleibt durch `test_workbench_production_closure` (0 erreichbare research_workbench-Dateien) gepinnt.

**Bewusst NICHT gebaut:** Runner-Inventory-Tool, generisches Robustness-Framework, Alpha-Contract-Extraktion (blockiert durch Live-Consumer), Production-Thesis-Gate, Production-Opposing-Signal-Exit, Auto-Reverse, ML/LLM-Entscheider.

**Offene Risiken (ehrlich):** MarketMap-/Setup-Schwellen sind eingefrorene Erstwerte, nur synthetisch validiert; keine echte Daten-Entdeckung/Edge-Aussage getroffen; Trigger-Events (STRUCT_RETEST_*) sind Platzhalter-Namen und müssen aus persistierten Families gespeist werden; Warm-up ≥603 Bars; kein OOS/Embargo im Study-Adapter (exploratory only); späte Events werden nur gezählt, nicht nachgeholt.
