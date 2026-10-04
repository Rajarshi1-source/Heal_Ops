"""The runbook contract.

Five members, and the split between them is the whole safety design:

    plan()               describe intent; read state, mutate NOTHING
    execute()            apply the plan -- called ONLY after the gate and any approval
    compensate()         undo; must be idempotent
    extra_verification() optional runbook-specific probe on top of the generic signal check
    chaos_fixture        names the fault injector that proves this runbook; enforced in CI

**The framework decides whether to execute, never the runbook.** A runbook that could choose to act
would make the policy gate advisory, so `execute()` is only ever called by the orchestrator, and
only after `policy.evaluate()` returned something other than deny.

`compensate()` is abstract on purpose. A runbook that cannot describe its own undo is not
registered -- there is no default implementation to inherit, and no way to satisfy the interface
without writing one.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from bundle import IncidentBundle, Target
from plan import RemediationPlan


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """The outcome of `execute()` or `compensate()`.

    `ok` means the API calls applied, and nothing more than that. Whether the *incident* is over is
    the verifier's question -- conflating the two is defect D1.

    `state_before` and `state_after` are what the dashboard diffs and what the compensation chaos
    test compares byte-for-byte, so they must capture the fields a fix changes rather than a
    summary of them.
    """

    ok: bool
    detail: str
    state_before: dict[str, Any] = field(default_factory=dict)
    state_after: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "detail": self.detail,
            "state_before": self.state_before,
            "state_after": self.state_after,
        }


@dataclass(frozen=True, slots=True)
class SignalWindow:
    """Observed signals over a window, before or after an action.

    A value of `None` means **not observed**, which is different from zero and must stay
    distinguishable: a missing signal is never a healthy signal.
    """

    alert_firing: bool | None = None
    restarts: int | None = None
    oom_events: float | None = None
    throttle_ratio: float | None = None
    psi_full: float | None = None
    memory_usage_ratio: float | None = None
    new_alerts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "alert_firing": self.alert_firing,
            "restarts": self.restarts,
            "oom_events": self.oom_events,
            "throttle_ratio": self.throttle_ratio,
            "psi_full": self.psi_full,
            "memory_usage_ratio": self.memory_usage_ratio,
            "new_alerts": list(self.new_alerts),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> SignalWindow:
        """Rebuild a persisted window. Missing keys come back as `None` -- not observed."""
        raw = raw or {}

        def _float(key: str) -> float | None:
            value = raw.get(key)
            return float(value) if value is not None else None

        restarts = raw.get("restarts")
        firing = raw.get("alert_firing")
        return cls(
            alert_firing=bool(firing) if firing is not None else None,
            restarts=int(restarts) if restarts is not None else None,
            oom_events=_float("oom_events"),
            throttle_ratio=_float("throttle_ratio"),
            psi_full=_float("psi_full"),
            memory_usage_ratio=_float("memory_usage_ratio"),
            new_alerts=tuple(str(a) for a in raw.get("new_alerts") or ()),
        )


class Runbook(abc.ABC):
    """Base class for every remediation.

    Subclasses declare `name`, `triggers` and `chaos_fixture`, and implement the three verbs.
    """

    name: str
    """Registry key, and the value recorded on plans, executions and promotion rows."""

    triggers: tuple[str, ...]
    """Alertnames this runbook answers. Matched against the *cause* alert only."""

    @property
    @abc.abstractmethod
    def chaos_fixture(self) -> str:
        """The fault injector that proves this runbook works.

        Abstract because CI asserts that every registered runbook names a fixture the chaos app
        actually ships. A runbook with no way to prove itself does not merge.
        """

    @abc.abstractmethod
    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        """Describe what would be done, without doing any of it.

        Must be a **pure function of the bundle**. No live cluster reads: if planning reached out to
        a cluster, the decision eval could not be hermetic, the determinism digest could not be
        stable, and the CI gate would be flaky. Everything needed is already on `target`.

        Returns a refusal (via `plan.refuse`) rather than raising when the right answer is
        "not this one" -- a refusal is an outcome, not an error.
        """

    @abc.abstractmethod
    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        """Apply the plan's actions.

        Called ONLY by the orchestrator, ONLY after the policy gate allowed it and any required
        approval was granted. Never call this from `plan()`.
        """

    @abc.abstractmethod
    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        """Undo the plan's actions.

        **Must be idempotent.** Both the verifier and the watchdog may call it, and the watchdog may
        call it after `execute()` already rolled itself back -- a compensation owned by the process
        that can crash is not a compensation.

        Uses `plan.compensation`, which was decided and persisted *before* the action ran. Do not
        re-derive the undo from current cluster state: by the time compensation runs, that state is
        the thing being undone.
        """

    async def extra_verification(self, plan: RemediationPlan, after: SignalWindow) -> str | None:
        """Optional runbook-specific probe, on top of the generic signal check.

        Returns a failure reason, or None when satisfied. Defaults to None because most runbooks are
        fully covered by the golden signals; the OOM runbook overrides it to assert that
        `restartCount` really did not move.
        """
        return None

    # --- helpers shared by every runbook ---------------------------------------------------------

    def answers(self, alertname: str) -> bool:
        return alertname in self.triggers

    def __repr__(self) -> str:
        return f"<Runbook {self.name} triggers={self.triggers}>"


class RunbookRegistry:
    """Maps alertnames to runbooks.

    Registration validates the contract, so a runbook that cannot be verified or that collides with
    an existing trigger fails at import time rather than during an incident.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, Runbook] = {}
        self._by_trigger: dict[str, Runbook] = {}

    def register(self, runbook: Runbook) -> Runbook:
        if not runbook.name:
            raise ValueError(f"{type(runbook).__name__} has no name")
        if runbook.name in self._by_name:
            raise ValueError(f"duplicate runbook name: {runbook.name}")
        if not runbook.triggers:
            raise ValueError(f"runbook {runbook.name} declares no triggers")
        if not runbook.chaos_fixture:
            # Enforced here as well as in CI, so the failure is immediate and local.
            raise ValueError(
                f"runbook {runbook.name} names no chaos fixture; "
                "a runbook that cannot prove itself is not registered"
            )

        for trigger in runbook.triggers:
            if trigger in self._by_trigger:
                # Two runbooks answering one alert would make selection non-deterministic, and the
                # determinism digest would stop being reproducible.
                raise ValueError(
                    f"alert {trigger} is already answered by "
                    f"{self._by_trigger[trigger].name}; two runbooks for one alert makes "
                    "runbook selection ambiguous"
                )
            self._by_trigger[trigger] = runbook

        self._by_name[runbook.name] = runbook
        return runbook

    def for_alert(self, alertname: str) -> Runbook | None:
        return self._by_trigger.get(alertname)

    def get(self, name: str) -> Runbook | None:
        return self._by_name.get(name)

    def all(self) -> tuple[Runbook, ...]:
        return tuple(self._by_name.values())

    @property
    def triggers(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_trigger))

    def describe(self) -> list[dict[str, Any]]:
        """Feeds `GET /api/runbooks` and the dashboard's runbook catalog -- the model card as UI."""
        return [
            {
                "name": r.name,
                "triggers": list(r.triggers),
                "chaos_fixture": r.chaos_fixture,
            }
            for r in self._by_name.values()
        ]


registry = RunbookRegistry()
