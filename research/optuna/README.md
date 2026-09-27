# Optuna optimization

Versioned search spaces, seeds, trials, and out-of-sample results belong here. Optimization output
is research evidence, never a direct live configuration update.

Sprint 1 provides `research.preparation.OptimizationPlan`. It requires an explicit seed and an
objective prefixed with `oos_`; it does not launch trials or select parameters. Optuna remains an
optional future dependency and large optimization jobs are intentionally out of scope.

