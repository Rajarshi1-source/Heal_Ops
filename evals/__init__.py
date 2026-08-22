"""CI gates that grade decisions rather than code paths.

`policy_eval` requires every policy rule to carry a must-allow and a must-deny case.
`decision_eval` replays the labelled corpus and grades runbook choice, calibration and determinism.
`ledger_verify` walks the hash chain over the committed corpus.
"""
