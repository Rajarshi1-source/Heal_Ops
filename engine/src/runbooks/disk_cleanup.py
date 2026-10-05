"""Runbook 2 -- disk cleanup.

The only runbook here that touches data, so it is the most conservative:

* **Archive-then-delete, never delete-only.** Nothing is removed until the upload has been verified
  by reading the object's size back.
* **A 7-day floor.** Recent logs are the ones an engineer needs while an incident is live, so they
  are never candidates however full the disk is.
* **The plan carries a dry-run listing.** A human approving this sees the exact files, so "what
  would this delete?" is answered before it runs, not after.
* **If S3 is unreachable, it refuses.** Deleting logs because the archive destination is down would
  turn disk pressure into data loss.

Classified `partially_reversible`: the bytes come back from S3, but the deletion happened. That
honesty is what routes it through the policy gate's approval rule instead of the auto path.
"""

from __future__ import annotations

from typing import TypedDict

import structlog

from bundle import IncidentBundle, Target
from config import get_settings
from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
    refuse,
)
from runbooks.base import ExecutionResult, Runbook, registry

log = structlog.get_logger(__name__)

#: Never archive more than this in one action. A single runbook invocation that moves tens of
#: gigabytes is a migration, not a remediation.
MAX_ARCHIVE_BYTES = 5 * 1024**3

#: Below this there is nothing worth doing, and churning the disk for a few megabytes is noise.
MIN_ARCHIVE_BYTES = 32 * 1024**2

DISK_TTL_SECONDS = 7200


class Candidate(TypedDict):
    """One archive candidate, captured into the bundle before planning.

    The shape is a contract between the chaos harness, the live capture path and this runbook, which
    is why it is declared rather than passed around as a loose dict.
    """

    path: str
    size_bytes: int
    age_days: float


class DiskCleanup(Runbook):
    name = "disk_cleanup"
    triggers = ("NodeDiskPressure",)

    @property
    def chaos_fixture(self) -> str:
        return "log-spammer"

    # --- plan ------------------------------------------------------------------------------------

    def plan(self, bundle: IncidentBundle, target: Target) -> RemediationPlan:
        settings = get_settings()

        # Candidates are captured into the bundle before planning, so `plan()` stays a pure function
        # and the dry-run listing is exactly what execution will act on.
        candidates = _candidates_from_bundle(bundle)

        if not candidates:
            return refuse(
                self.name,
                bundle.incident_id,
                f"no log files older than {settings.disk_archive_floor_days} days were found, so "
                "there is nothing safe to archive -- the disk is full of data this runbook must "
                "not touch",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        total = sum(c["size_bytes"] for c in candidates)
        if total < MIN_ARCHIVE_BYTES:
            return refuse(
                self.name,
                bundle.incident_id,
                f"only {_human(total)} of archivable logs exist, which will not relieve disk "
                "pressure -- the space is being used by something this runbook does not manage",
                target_kind=target.kind,
                target_name=target.name,
                target_namespace=target.namespace,
            )

        # Oldest first, so the cap keeps the least useful data rather than an arbitrary slice.
        ordered = sorted(candidates, key=lambda c: c["age_days"], reverse=True)
        selected: list[Candidate] = []
        running = 0
        for candidate in ordered:
            size = candidate["size_bytes"]
            if running + size > MAX_ARCHIVE_BYTES:
                continue
            selected.append(candidate)
            running += size

        action = ApiCall(
            verb="s3_archive_then_delete",
            resource="v1/Node",
            name=target.name,
            namespace=None,
            body={
                # The dry-run listing. This IS the plan, not a description of it.
                "files": [
                    {
                        "path": c["path"],
                        "size_bytes": c["size_bytes"],
                        "age_days": round(float(c["age_days"]), 1),
                    }
                    for c in selected
                ],
                "total_bytes": running,
                "floor_days": settings.disk_archive_floor_days,
                "bucket": settings.s3_bucket,
            },
        )
        compensation = ApiCall(
            verb="s3_restore",
            resource="v1/Node",
            name=target.name,
            namespace=None,
            body={
                "files": [
                    {"path": c["path"], "key": _key_for(target.name, str(c["path"]))}
                    for c in selected
                ],
                "bucket": settings.s3_bucket,
            },
        )

        return RemediationPlan(
            runbook=self.name,
            incident_id=bundle.incident_id,
            target_kind="Node",
            target_name=target.name,
            target_namespace=None,
            actions=(action,),
            compensation=(compensation,),
            # The bytes come back from S3, but the deletion itself happened. Claiming full
            # reversibility here would be the kind of overstatement that makes every other
            # classification untrustworthy.
            reversibility=Reversibility.PARTIAL,
            durability=Durability.EPHEMERAL,
            ttl_seconds=DISK_TTL_SECONDS,
            blast_radius=BlastRadius(pods=0, nodes=1, namespaces=(), user_facing=False),
            expected_signal=ExpectedSignal(
                alert_should_clear="NodeDiskPressure",
                # Scoped to this node: the verifier enforces committed thresholds, and an unscoped
                # max would let a different full node fail this node's cleanup.
                queries={
                    "disk_used_ratio": disk_used_query(
                        target.name, [str(c["path"]) for c in selected]
                    )
                },
                thresholds={"disk_used_ratio": 0.80},
                window_seconds=300,
            ),
            confidence=_confidence(running, len(selected)),
            summary=(
                f"archive {len(selected)} log files ({_human(running)}) older than "
                f"{settings.disk_archive_floor_days}d from {target.name} to "
                f"s3://{settings.s3_bucket}, then delete the originals"
            ),
            cost_delta_inr_month=0.0,
        )

    # --- execute ---------------------------------------------------------------------------------

    async def execute(self, plan: RemediationPlan) -> ExecutionResult:
        """Archive every file and verify it, then delete. Never the other way round.

        The work happens on the node under pressure, in a node-agent Job -- the engine runs on some
        other node and cannot see this one's disk. See `node_agent` and `k8s.agent_jobs`.
        """
        from k8s import agent_jobs

        action = plan.actions[0]
        files: list[Candidate] = list(action.body["files"])
        before = {
            "node": plan.target_name,
            "files": [{"path": f["path"], "size_bytes": f["size_bytes"]} for f in files],
            "total_bytes": action.body["total_bytes"],
        }

        result = await agent_jobs.run(
            plan.target_name,
            {
                "op": "archive",
                "node": plan.target_name,
                "files": files,
                "roots": get_settings().disk_archive_roots,
            },
        )
        after = {
            "node": plan.target_name,
            "archived": result.get("archived", []),
            "freed_bytes": result.get("freed_bytes", 0),
        }
        return ExecutionResult(
            ok=bool(result.get("ok")),
            detail=str(result.get("detail", "")),
            state_before=before,
            # Nothing attempted (archive unreachable, agent unavailable) means nothing changed.
            state_after=after if result.get("attempted", True) and "archived" in result else before,
        )

    # --- compensate ------------------------------------------------------------------------------

    async def compensate(self, plan: RemediationPlan) -> ExecutionResult:
        """Pull the archived files back from S3, on the node they came from.

        Idempotent: a file already present is skipped. This restores the *data*, which is the part
        that matters; the deletion event itself cannot be unmade, and the plan says so by being
        classified `partially_reversible`.
        """
        from k8s import agent_jobs

        undo = plan.compensation[0]
        entries: list[dict[str, str]] = list(undo.body["files"])
        result = await agent_jobs.run(
            plan.target_name,
            {"op": "restore", "entries": entries, "roots": get_settings().disk_archive_roots},
        )
        return ExecutionResult(
            ok=bool(result.get("ok")),
            detail=str(result.get("detail", "")),
            state_before={"node": plan.target_name, "restoring": len(entries)},
            state_after={
                "restored": result.get("restored", []),
                "missing": result.get("missing", []),
            },
        )


def disk_used_query(node: str, paths: list[str]) -> str:
    """Utilisation of the filesystem the archived files actually lived on.

    Longest mountpoint first, through `or`: each term is a single unlabelled series, so the first
    filesystem that exists on the node is the one judged -- `/var/log` if it is its own mount,
    else `/var`, else `/`. Judging `/` alone measured a filesystem a kind node does not even
    expose, and on a real node with a separate `/var` it would judge the wrong disk entirely.
    """
    mounts: set[str] = {"/"}
    for path in paths:
        parts = [p for p in path.split("/") if p][:-1]  # the directories above the file
        mounts.update("/" + "/".join(parts[:i]) for i in range(1, len(parts) + 1))
    terms = []
    for mount in sorted(mounts, key=lambda m: (-len(m), m)):
        sel = f'{{node="{node}",mountpoint="{mount}",fstype!~"tmpfs|overlay|ramfs"}}'
        terms.append(
            f"max(1 - (node_filesystem_avail_bytes{sel} / node_filesystem_size_bytes{sel}))"
        )
    return " or ".join(terms)


def _candidates_from_bundle(bundle: IncidentBundle) -> list[Candidate]:
    """Archive candidates, captured into the bundle before planning.

    Read from the cause alert's annotations so the planner performs no I/O. The chaos harness and
    the live capture path both populate it the same way, which is what keeps the corpus honest.
    """
    raw = bundle.cause_alert.annotations.get("healops_archive_candidates")
    if not raw:
        return []
    import json

    try:
        parsed = json.loads(raw)
    except ValueError, TypeError:
        return []
    if not isinstance(parsed, list):
        return []

    # Validate each entry rather than trusting the shape. A malformed candidate would otherwise
    # raise deep inside planning, and a runbook that crashes is strictly worse than one that
    # refuses -- a crash produces no plan, no ledger entry and no explanation.
    candidates: list[Candidate] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            continue
        path, size, age = entry.get("path"), entry.get("size_bytes"), entry.get("age_days")
        if not isinstance(path, str):
            continue
        if not isinstance(size, int | float) or not isinstance(age, int | float):
            continue
        candidates.append(Candidate(path=path, size_bytes=int(size), age_days=float(age)))
    return candidates


def _key_for(node: str, path: str) -> str:
    """Content-addressed enough to be unique, readable enough to find by hand."""
    safe = path.strip("/").replace("/", "_")
    return f"logs/{node}/{safe}"


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _confidence(total_bytes: int, file_count: int) -> float:
    """More archivable data means more certainty that removing it relieves the pressure."""
    confidence = 0.85
    if total_bytes > 512 * 1024**2:
        confidence += 0.06
    if file_count >= 5:
        confidence += 0.03
    if total_bytes < 64 * 1024**2:
        # Barely enough to matter; the disk is probably full of something else.
        confidence -= 0.15
    return round(max(min(confidence, 0.99), 0.0), 3)


registry.register(DiskCleanup())
