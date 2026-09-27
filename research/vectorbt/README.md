# VectorBT research

High-volume exploratory backtests belong here. Production execution code must not import this tree.

Sprint 1 deliberately does not import or run VectorBT. Use
`research.preparation.prepare_vectorbt_inputs` to produce deterministic column-oriented inputs,
then adapt those columns to pandas only in a future experiment module after the dependency and
dataset version have been recorded.

