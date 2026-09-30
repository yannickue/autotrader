# AutoTrader DEMO autostart (Windows Task Scheduler + bounded supervisor)

Scope: DEMO trader only (`scripts/demo_trader.py --demo-auto`). Native Windows solution: one scheduled
task, one PowerShell launcher, one small Python supervisor. No Docker, no daemon framework.
The scripts NEVER start, stop, restart or otherwise touch MetaTrader 5 (MT5 stays open) and NEVER
kill the runner.

## Operating model

```
Task Scheduler  AutoTrader-DemoDaily  (Mon-Fri 08:30 local = Europe/Berlin, only while user logged on)
  -> scripts/autostart/run_trader_day.ps1      cwd = this checkout, named mutex, uv run --frozen
       -> scripts/autostart/supervisor.py      operating-day guard, STOP guard, supervisor.lock,
                                               healthy-runner check, bounded restart loop, alerts
            -> scripts/demo_trader.py --demo-auto --confirm-demo-auto=... --artifacts artifacts/demo_100k
               --account-phase ALPHA_EXECUTION_DISCOVERY [--daily]
```

The runner itself does the start-up safety work (DEMO-account verification, broker connectivity,
persisted-state recovery, broker/local reconciliation, unknown order/position checks, market-data
health, READY only if safe). The supervisor only watches the heartbeat and logs
`READY: ... reconciliation=RECONCILED ...` once the heartbeat of the new pid is fresh and reconciled
(`WARN ... not READY/RECONCILED after 300s` otherwise; the runner's own transient grace then decides).

`--daily` (Lane P: after the 22:00 Berlin deadline, once the broker is flat and reconciled, exit 0 with
stop_reason `eod_flat_shutdown`) is passed when `demo_trader.py --help` lists it (`--daily auto`, the
default; `on`/`off` force). Until it exists the supervisor treats runner exit 0 as "day finished, do not
restart". Flattening at ~21:55 is the runner's job, not the supervisor's.

## Registering (lead only)

Run from the approved production checkout (the task points at the checkout the script lives in):

```powershell
cd C:\Users\yanni\.codex\.chatgpt-projects\g-p-6ab858dc4d7c8191818034e19b772625\trader
# 1. validate, nothing registered
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autostart\register_task.ps1 -DryRun
# 2. register (current user, logged-on only)
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\autostart\register_task.ps1
#    optional: -WakeToRun   (wake the PC for 08:30)   -Force (replace)   -OutXml review.xml
```

`-DryRun` (or `-WhatIf`) renders `AutoTrader-DemoDaily.task.xml`, validates it with the Task Scheduler
COM parser (in-memory, nothing registered) and prints the registration command.
Remove: `unregister_task.ps1` (does not stop a running trader). Status: `status_trader.ps1` (task,
locks, STOP, alert, `--status` verdict, newest log). Operator stop: `stop_trader.ps1` (creates
`<artifacts>\STOP`; `-Clear` removes it). The STOP file is never auto-removed: clear it before the next
day or tomorrow's start is intentionally blocked.

Task settings: trigger Mon-Fri 08:30 local; `StartWhenAvailable=true`; `MultipleInstancesPolicy=IgnoreNew`;
`RunOnlyIfNetworkAvailable=false`; runs as the current user with an interactive token, only while that
user is logged on (an interactive MT5 session is required; no stored password); ExecutionTimeLimit
`PT15H` (safety net, the supervisor ends itself after the runner's clean exit); battery does not stop
it; RestartOnFailure conservative (1 retry after 15 min; the supervisor's own day budget persists in
`watchdog_state.json`, so this cannot loop).

## DST / time zone

The OS time zone is `W. Europe Standard Time` (verified with `tzutil /g`). The trigger's `StartBoundary`
carries no zone suffix (`...T08:30:00`), i.e. local wall-clock time, which Task Scheduler keeps at 08:30
across the CET/CEST changes (no fixed UTC). The supervisor's weekday / 22:15 guard uses
`zoneinfo("Europe/Berlin")` (fallback: OS zone). If the OS zone were ever changed, the trigger would
follow the OS zone, not Berlin.

## Missed start, sleep and wake

* PC off / rebooting at 08:30: `StartWhenAvailable` runs the task as soon as the machine is up and the
  user is logged on. The supervisor launches immediately if it is still an operating day (Mon-Fri, before
  22:15). The runner reconciles before READY and never chases missed signals (existing no-late-chase).
* Started after 22:15 or on a weekend: the supervisor logs why and exits 0; nothing is started.
* Sleep (measured read-only with `powercfg` on this PC, desktop): standby after idle is `0` (never) on
  AC power and `600 s` on battery; hybrid sleep, hibernate and Fast Startup are available; S3 standby is
  supported. So on AC the PC does not sleep on its own; a manual/forced sleep would stop the runner and
  the next start is the `StartWhenAvailable` catch-up after wake. `-WakeToRun` only matters if the PC
  sleeps before 08:30; wake timers could not be listed (`powercfg /waketimers` needs admin) and waking
  also needs "Allow wake timers" enabled in the power plan. Default: off.

## Single-instance invariants

* Runner: `<artifacts>/runner.lock` (JSON: pid, process creation time, role). A second `--shadow` /
  `--demo-auto` on the same artifacts dir exits with code **9** and `REFUSED:` before any MT5 attach.
  Stale locks (dead pid, or pid reused by another process = different creation time) are taken over.
  Released on clean exit and on exceptions; a hard kill leaves a stale lock that is recovered next start.
  `--status` / `--analyze` / `--preflight` are unaffected.
* Supervisor: `<artifacts>/supervisor.lock` plus the launcher's named mutex
  `Global\AutoTrader-DemoDaily-<hash(artifacts)>` (second launcher exits 0).
* A healthy runner (fresh heartbeat <= 90 s, `process_alive`, live pid; this also covers a runner started
  before `runner.lock` existed) makes the supervisor log and exit 0 without launching. A runner whose lock
  holder is alive but whose heartbeat is stale is NOT duplicated and NOT killed: exit 10 +
  `watchdog_alert.json` (CRITICAL).

## Watchdog policy (runner exit codes)

| exit | meaning | supervisor |
|---|---|---|
| 0 | done / `eod_flat_shutdown` | done, no restart |
| 2 | bad args / DEMO auth refused | no restart, alert, supervisor exit 2 |
| 7 | fail-closed orderly stop | restart with backoff, max 3/day, alert each time |
| 8 | MT5 stack unavailable | bounded restart, alert |
| 9 | second runner refused | done (a runner already exists) |
| other / signal | crash | bounded restart with backoff |

Always: STOP file present -> never (re)start; after 22:15 (`--end-of-day`) or on a weekend -> never
(re)start; total restarts per day <= 8 (`--max-restarts-per-day`, persisted in
`<artifacts>/watchdog_state.json`, resets the next day) then exit 20 + CRITICAL alert. Backoff
30, 60, 120, 300, 600 s (saturating); a run longer than 30 min resets the consecutive streak. Alerts:
loud `!!! WATCHDOG ALERT !!!` log line and `<artifacts>/watchdog_alert.json` (latest + last 20 events).
Ctrl+C / SIGTERM / SIGBREAK on the supervisor forwards an orderly stop to the runner (CTRL_BREAK), waits
120 s, never kills it, and never restarts.

## Logs

* `artifacts\demo_100k\logs\trader_YYYYMMDD_HHMMSS.log`: one per supervisor start; `[SUP]` supervisor
  lines, `[RUN]` runner stdout/stderr, all restarts of that supervisor appended.
* `artifacts\demo_100k\logs\autostart_launcher.log`: launcher start / exit code.
* `artifacts\demo_100k\watchdog_alert.json`, `watchdog_state.json`, `runner.lock`, `supervisor.lock`.

## What the supervisor never does

Never starts/stops/closes/kills MetaTrader 5; never kills the runner; never places or modifies orders;
never logs in (runner refuses `MT5_ALLOW_ACCOUNT_LOGIN=1`); never restarts past the budget or after the
operating day; never deletes the STOP file.
