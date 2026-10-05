"""Runbook 2, disk_cleanup, on a real cluster: archive-then-delete on the node's own disk, a
restore that puts every file back exactly, and a refusal to delete anything while S3 is down.

One honest limit: kind cannot be driven into real disk pressure -- its nodes share the host's
Docker disk -- so the fix test delivers the `NodeDiskPressure` alert to the engine's webhook
itself. Everything after that is real: the scan and the archive run in node-agent Jobs on the
node, the objects land in the in-cluster S3, and the verifier judges the node's actual filesystem.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from tests.chaos import harness
from tests.chaos.harness import HEALOPS_NS, kubectl

RUNBOOK = "disk_cleanup"


def _spammer_node() -> str:
    [pod] = harness.pods_of("log-spammer")
    node: str = pod["spec"]["nodeName"]
    return node


def _files(node: str, paths: list[str]) -> dict[str, str | None]:
    """Each file as the node's own filesystem has it: bytes, size, mtime, owner, mode."""
    out: dict[str, str | None] = {}
    for path in paths:
        try:
            stat = harness.docker_exec(node, "stat", "-c", "%s %Y %u %g %a", path).strip()
            digest = harness.docker_exec(node, "sha256sum", path).split()[0]
            out[path] = f"{digest} {stat}"
        except Exception:  # noqa: BLE001 - absent is a value here, not an error
            out[path] = None
    return out


async def _plan(node: str) -> Any:
    import worker
    from planner import replan

    extras = await worker.scan_archive_candidates(node)
    target = await harness.live_target("NodeDiskPressure", name=node)
    plan = replan(harness.bundle_for("NodeDiskPressure", target, annotations=extras))
    assert plan is not None and not plan.is_refusal, plan and plan.refusal_reason
    return plan


def _paths(plan: Any) -> list[str]:
    return [str(f["path"]) for f in plan.actions[0].body["files"]]


@pytest.fixture
def node(cluster: None) -> str:
    return _spammer_node()


async def test_compensation_puts_every_file_back_exactly(node: str) -> None:
    """Archive, then restore, and compare bytes, size, mtime, owner and mode with `==`."""
    from runbooks import load_all

    plan = await _plan(node)
    paths = _paths(plan)
    before = _files(node, paths)
    assert all(before.values()), before

    runbook = load_all().get(RUNBOOK)
    assert runbook is not None
    executed = await runbook.execute(plan)
    assert executed.ok, executed.detail
    assert all(v is None for v in _files(node, paths).values()), "archived files must be gone"

    compensated = await runbook.compensate(plan)
    assert compensated.ok, compensated.detail
    assert _files(node, paths) == before


@pytest.fixture
def s3_down(cluster: None) -> Iterator[None]:
    kubectl("-n", HEALOPS_NS, "scale", "deployment/healops-s3", "--replicas=0")
    harness.wait_until(
        lambda: (
            not harness.kubectl_json(
                "-n", HEALOPS_NS, "get", "pods", "-l", "app.kubernetes.io/component=s3"
            )["items"]
        ),
        timeout=120,
        what="the S3 pod to stop",
    )
    yield
    kubectl("-n", HEALOPS_NS, "scale", "deployment/healops-s3", "--replicas=1")
    harness.rollout_settled("deployment/healops-s3", namespace=HEALOPS_NS)


async def test_refuses_to_delete_anything_while_the_archive_is_down(
    node: str, s3_down: None
) -> None:
    """No S3, no deletion: trading a disk-pressure incident for data loss is never the fix."""
    from runbooks import load_all

    plan = await _plan(node)
    paths = _paths(plan)
    before = _files(node, paths)

    runbook = load_all().get(RUNBOOK)
    assert runbook is not None
    result = await runbook.execute(plan)

    assert not result.ok
    assert "nothing was deleted" in result.detail
    assert result.state_after == result.state_before
    assert _files(node, paths) == before


def _disk_alert(node: str, status: str, fingerprint: str) -> dict[str, Any]:
    now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    labels = {
        "alertname": "NodeDiskPressure",
        "node": node,
        "severity": "warning",
        "healops_runbook": RUNBOOK,
    }
    return {
        "version": "4",
        "status": status,
        "receiver": "healops",
        "groupKey": f"chaos-suite:{fingerprint}",
        "groupLabels": {"alertname": "NodeDiskPressure"},
        "commonLabels": labels,
        "commonAnnotations": {},
        "externalURL": "http://chaos-suite",
        "alerts": [
            {
                "status": status,
                "labels": labels,
                "annotations": {"summary": f"chaos suite: disk pressure on {node}"},
                "startsAt": now,
                "endsAt": now if status == "resolved" else "0001-01-01T00:00:00Z",
                "fingerprint": fingerprint,
                "generatorURL": "http://chaos-suite",
            }
        ],
    }


def test_archives_old_logs_on_the_node_end_to_end(engine: harness.Engine, node: str) -> None:
    """The engine scans the node, plans, asks a human (a node action), archives, and verifies."""
    import asyncio

    mark = engine.high_water_mark()
    fingerprint = uuid.uuid4().hex[:16]
    # Node-scoped plans always need a human (`approve-node-scoped`), so the rung that lets this
    # runbook act at all is `approval` -- and this test is the human.
    with harness.promoted(RUNBOOK, "-", mode="approval"):
        engine.webhook(_disk_alert(node, "firing", fingerprint))
        incident = engine.wait_for_incident(
            after=mark, target_key=f"node/{node}", alertname="NodeDiskPressure", timeout=5 * 60
        )
        waiting = engine.wait_for_state(
            incident["id"], ("awaiting_approval", *harness.TERMINAL), timeout=5 * 60
        )
        assert waiting["state"] == "awaiting_approval", engine.evidence(incident["id"])["replay"]
        engine.approve(waiting["executions"][-1]["id"])
        detail = engine.wait_for_state(
            incident["id"], harness.TERMINAL, timeout=harness.FIX_TIMEOUT_SECONDS
        )
    engine.webhook(_disk_alert(node, "resolved", fingerprint))

    replay = engine.evidence(incident["id"])["replay"]
    assert detail["state"] == "remediated", replay
    assert detail["verifications"][-1]["outcome"] == "remediated", replay
    plan = detail["plans"][-1]
    paths = [f["path"] for f in plan["actions"][0]["body"]["files"]]
    assert paths and all(v is None for v in _files(node, paths).values())

    # Put the node's logs back, so the next run starts from the same disk.
    from config import get_settings
    from k8s import agent_jobs

    restored = asyncio.run(
        agent_jobs.run(
            node,
            {
                "op": "restore",
                "entries": plan["compensation"][0]["body"]["files"],
                "roots": get_settings().disk_archive_roots,
            },
        )
    )
    assert restored["ok"], restored
