# Pending User Input

This list is updated as foundation work lands -- it reflects only what exists
today (cost-model safety + config/secrets scaffolding + script skeletons) and
will grow once the `src/instruments/` and MT5 adapter work land.

Tasks that need the user's own action before MT5/ActivTrades work can proceed past its current (credential-free) foundation stage. Nothing below requires typing secrets into chat or committing them anywhere — set them locally per `.env.example`.

- [ ] Open ActivTrades MT5 DEMO and log in via the MetaTrader5 terminal
- [ ] Set `MT5_LOGIN` locally (your DEMO account number)
- [ ] Set `MT5_PASSWORD` locally (your DEMO account password)
- [ ] Set `MT5_SERVER` locally (the ActivTrades demo server name shown in the terminal)
- [ ] Set `MT5_TERMINAL_PATH` locally if the terminal isn't in its default install location
- [ ] Run the prepared preflight script (see README) once it exists
- [ ] Confirm/correct the automatically-discovered DAX/NASDAQ100/WTI broker symbol mapping if flagged UNVERIFIED
