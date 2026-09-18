"""`healopsctl` command surface.

    healopsctl plan <bundle|incident>   print the plan a runbook would produce, without executing
    healopsctl replay <sha>             offline reconstruction from an evidence bundle
    healopsctl audit --verify-chain     full chain walk; exit non-zero on a break
    healopsctl freeze --reason "..."    global kill switch, itself a ledger entry

`plan` and `replay` are deliberately **offline**: they read a committed JSON bundle and need no
cluster, no network and no database. That is the same property the decision eval depends on, so a
command that works on a laptop with the cluster disconnected is evidence the planner is pure rather
than a claim that it is.
"""

from __future__ import annotations

from cli.healopsctl.main import app

__all__ = ["app"]
