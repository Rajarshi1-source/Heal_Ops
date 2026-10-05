"""Runbook execution and compensation.

The Kubernetes and S3 layers are faked so these paths can be tested hermetically; the chaos suite
then proves the same runbooks against a real cluster. Two layers, because they answer different
questions -- these tests cover the *branching* (conflict, missing revision, partial failure,
idempotent re-entry), and the chaos tests cover whether the cluster actually ends up right.

The compensation tests matter most. Both the verifier and the watchdog may call `compensate()`, and
the watchdog may call it after `execute()` already rolled itself back -- so a second call must be a
no-op rather than a second mutation.
"""

from __future__ import annotations

from typing import Any

import pytest

from plan import ApiCall, BlastRadius, Durability, RemediationPlan, Reversibility
from runbooks.cpu_scale_up import CpuScaleUp
from runbooks.crashloop_rollback import CrashLoopRollback
from runbooks.disk_cleanup import DiskCleanup

pytestmark = pytest.mark.asyncio


# --- fakes ---------------------------------------------------------------------------------------


class FakeRevision:
    def __init__(self, revision: int, image: str = "img:v1") -> None:
        self.revision = revision
        self.template = {"spec": {"containers": [{"image": image}]}}
        self.images = (image,)


class FakeDeploymentOps:
    """Stands in for `k8s.deployment_ops`."""

    ConflictError = type("ConflictError", (RuntimeError,), {})
    REVISION_ANNOTATION = "deployment.kubernetes.io/revision"

    def __init__(self, revisions: list[int] | None = None, replicas: int = 3) -> None:
        self._revisions = [FakeRevision(r) for r in (revisions or [4, 3])]
        self.replicas = replicas
        self.rollbacks: list[int] = []
        self.scales: list[int] = []
        self.hpa_patches: list[dict[str, Any]] = []
        self.raise_conflict = False

    async def list_revisions(self, namespace: str, name: str) -> list[FakeRevision]:
        return self._revisions

    async def read_deployment(self, namespace: str, name: str) -> Any:
        class Meta:
            resource_version = "42"

        class Deployment:
            metadata = Meta()

        return Deployment()

    async def rollback_to(
        self, namespace: str, name: str, template: Any, resource_version: str
    ) -> None:
        if self.raise_conflict:
            raise self.ConflictError("changed while we were planning; re-planning")
        revision = next(r.revision for r in self._revisions if r.template is template)
        self.rollbacks.append(revision)
        # Roll the fake history forward so a second call sees the new current revision.
        self._revisions = [FakeRevision(revision)] + [
            r for r in self._revisions if r.revision != revision
        ]

    async def scale(
        self, namespace: str, name: str, replicas: int, expect_replicas: int | None = None
    ) -> None:
        if self.raise_conflict:
            raise self.ConflictError("scale changed under us; re-planning")
        self.scales.append(replicas)
        self.replicas = replicas

    async def patch_autoscaler(
        self,
        namespace: str,
        name: str,
        *,
        target_utilization: int | None = None,
        max_replicas: int | None = None,
    ) -> None:
        self.hpa_patches.append(
            {"target_utilization": target_utilization, "max_replicas": max_replicas}
        )


class FakeStateCapture:
    """Stands in for `k8s.state_capture`, returning whatever the fake ops currently hold."""

    def __init__(self, ops: FakeDeploymentOps) -> None:
        self.ops = ops

    async def capture_deployment(self, namespace: str, name: str) -> dict[str, Any]:
        return {
            "kind": "Deployment",
            "namespace": namespace,
            "name": name,
            "replicas": self.ops.replicas,
            "revision": str(self.ops._revisions[0].revision),
        }


@pytest.fixture
def k8s(monkeypatch: pytest.MonkeyPatch) -> FakeDeploymentOps:
    """Patch the k8s modules the runbooks import lazily inside their methods."""
    import sys

    ops = FakeDeploymentOps()
    capture = FakeStateCapture(ops)

    fake_k8s = type(sys)("k8s")
    fake_k8s.deployment_ops = ops  # type: ignore[attr-defined]
    fake_k8s.state_capture = capture  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "k8s", fake_k8s)
    monkeypatch.setitem(sys.modules, "k8s.deployment_ops", ops)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, "k8s.state_capture", capture)  # type: ignore[arg-type]
    return ops


# --- plans ---------------------------------------------------------------------------------------


def rollback_plan(to_revision: int = 3, from_revision: int = 4) -> RemediationPlan:
    action = ApiCall(
        verb="rollout_undo",
        resource="apps/v1/Deployment",
        name="api",
        namespace="shop",
        body={"to_revision": to_revision, "from_revision": from_revision, "to_image": "img:v1"},
    )
    undo = ApiCall(
        verb="rollout_undo",
        resource="apps/v1/Deployment",
        name="api",
        namespace="shop",
        body={"to_revision": from_revision, "from_revision": to_revision, "to_image": "img:v2"},
    )
    return RemediationPlan(
        runbook="crashloop_rollback",
        incident_id=1,
        target_kind="Deployment",
        target_name="api",
        target_namespace="shop",
        actions=(action,),
        compensation=(undo,),
        reversibility=Reversibility.REVERSIBLE,
        durability=Durability.EPHEMERAL,
        ttl_seconds=7200,
        blast_radius=BlastRadius(pods=3),
        confidence=0.94,
        summary="roll back",
    )


def scale_plan(to_replicas: int = 5, from_replicas: int = 3) -> RemediationPlan:
    action = ApiCall(
        verb="scale",
        resource="apps/v1/Deployment",
        name="api",
        namespace="shop",
        body={"replicas": to_replicas, "from_replicas": from_replicas},
    )
    undo = ApiCall(
        verb="scale",
        resource="apps/v1/Deployment",
        name="api",
        namespace="shop",
        body={"replicas": from_replicas, "from_replicas": to_replicas},
    )
    return RemediationPlan(
        runbook="cpu_scale_up",
        incident_id=1,
        target_kind="Deployment",
        target_name="api",
        target_namespace="shop",
        actions=(action,),
        compensation=(undo,),
        reversibility=Reversibility.REVERSIBLE,
        durability=Durability.EPHEMERAL,
        ttl_seconds=7200,
        blast_radius=BlastRadius(pods=2),
        confidence=0.9,
        summary="scale up",
    )


def hpa_plan() -> RemediationPlan:
    action = ApiCall(
        verb="hpa_patch",
        resource="autoscaling/v2/HorizontalPodAutoscaler",
        name="api",
        namespace="shop",
        body={"target_utilization": 65},
    )
    undo = ApiCall(
        verb="hpa_patch",
        resource="autoscaling/v2/HorizontalPodAutoscaler",
        name="api",
        namespace="shop",
        body={"target_utilization": 80},
    )
    return RemediationPlan(
        runbook="cpu_scale_up",
        incident_id=1,
        target_kind="Deployment",
        target_name="api",
        target_namespace="shop",
        actions=(action,),
        compensation=(undo,),
        reversibility=Reversibility.REVERSIBLE,
        durability=Durability.EPHEMERAL,
        ttl_seconds=7200,
        blast_radius=BlastRadius(pods=2),
        confidence=0.92,
        summary="lower HPA target",
    )


# =================================================================================================
# CrashLoop rollback
# =================================================================================================


class TestCrashLoopExecution:
    runbook = CrashLoopRollback()

    async def test_executes_the_rollback(self, k8s: FakeDeploymentOps) -> None:
        result = await self.runbook.execute(rollback_plan())

        assert result.ok
        assert k8s.rollbacks == [3]
        assert "revision 3" in result.detail

    async def test_captures_before_and_after_state(self, k8s: FakeDeploymentOps) -> None:
        # What the dashboard diffs and what the compensation chaos test compares.
        result = await self.runbook.execute(rollback_plan())
        assert result.state_before["revision"] == "4"
        assert result.state_after["revision"] == "3"

    async def test_missing_revision_asks_for_a_replan_rather_than_guessing(
        self, k8s: FakeDeploymentOps
    ) -> None:
        """The revision was garbage-collected between planning and execution.

        Picking a different revision would be acting on a plan nobody gated.
        """
        result = await self.runbook.execute(rollback_plan(to_revision=99))

        assert not result.ok
        assert "no longer exists" in result.detail
        assert "re-plan required" in result.detail
        assert k8s.rollbacks == []

    async def test_conflict_asks_for_a_replan_not_a_retry(self, k8s: FakeDeploymentOps) -> None:
        """Optimistic concurrency: the object changed, so the gated plan may no longer be right.

        Retrying blindly would re-apply a decision made against stale state.
        """
        k8s.raise_conflict = True
        result = await self.runbook.execute(rollback_plan())

        assert not result.ok
        assert "re-planning" in result.detail

    async def test_compensation_rolls_forward(self, k8s: FakeDeploymentOps) -> None:
        await self.runbook.execute(rollback_plan())
        result = await self.runbook.compensate(rollback_plan())

        assert result.ok
        assert k8s.rollbacks == [3, 4], "must return to the original revision"

    async def test_compensation_is_idempotent(self, k8s: FakeDeploymentOps) -> None:
        """Both the verifier and the watchdog may call this, possibly in sequence."""
        await self.runbook.execute(rollback_plan())
        first = await self.runbook.compensate(rollback_plan())
        second = await self.runbook.compensate(rollback_plan())

        assert first.ok and second.ok
        assert "nothing to compensate" in second.detail
        assert k8s.rollbacks == [3, 4], "the second call must not mutate again"

    async def test_compensation_reports_a_vanished_revision(self, k8s: FakeDeploymentOps) -> None:
        # Honest failure: the undo target is gone, so escalate rather than silently doing nothing.
        result = await self.runbook.compensate(rollback_plan(to_revision=3, from_revision=77))
        assert not result.ok
        assert "is gone" in result.detail


# =================================================================================================
# CPU scale-up
# =================================================================================================


class TestCpuScaleUpExecution:
    runbook = CpuScaleUp()

    async def test_scales_the_deployment(self, k8s: FakeDeploymentOps) -> None:
        result = await self.runbook.execute(scale_plan())
        assert result.ok
        assert k8s.scales == [5]

    async def test_patches_the_hpa_when_that_is_the_plan(self, k8s: FakeDeploymentOps) -> None:
        result = await self.runbook.execute(hpa_plan())

        assert result.ok
        assert k8s.hpa_patches == [{"target_utilization": 65, "max_replicas": None}]
        assert k8s.scales == [], "must not touch replicas when an HPA owns them"

    async def test_scale_conflict_asks_for_a_replan(self, k8s: FakeDeploymentOps) -> None:
        k8s.raise_conflict = True
        result = await self.runbook.execute(scale_plan())
        assert not result.ok
        assert "re-planning" in result.detail

    async def test_compensation_scales_back(self, k8s: FakeDeploymentOps) -> None:
        await self.runbook.execute(scale_plan())
        result = await self.runbook.compensate(scale_plan())

        assert result.ok
        assert k8s.scales == [5, 3]

    async def test_compensation_is_idempotent(self, k8s: FakeDeploymentOps) -> None:
        await self.runbook.execute(scale_plan())
        await self.runbook.compensate(scale_plan())
        second = await self.runbook.compensate(scale_plan())

        assert second.ok
        assert "nothing to compensate" in second.detail
        assert k8s.scales == [5, 3]

    async def test_hpa_compensation_restores_the_original_target(
        self, k8s: FakeDeploymentOps
    ) -> None:
        await self.runbook.execute(hpa_plan())
        result = await self.runbook.compensate(hpa_plan())

        assert result.ok
        assert k8s.hpa_patches[-1]["target_utilization"] == 80


# =================================================================================================
# Disk cleanup -- the runbook that touches data
# =================================================================================================


class FakeS3:
    def __init__(self, *, reachable: bool = True, fail_on: str | None = None) -> None:
        self.reachable = reachable
        self.fail_on = fail_on
        self.archived: list[str] = []
        self.restored: list[str] = []
        self.objects: set[str] = set()

    async def ensure_bucket(self) -> bool:
        return self.reachable

    async def archive_file(self, path: Any, key: str) -> Any:
        if self.fail_on and self.fail_on in str(path):
            raise self.ArchiveError(f"failed to archive {path}")
        self.archived.append(str(path))
        self.objects.add(key)

        class Archived:
            def __init__(self, key: str) -> None:
                self.key = key
                self.size_bytes = 1024

        return Archived(key)

    async def object_exists(self, key: str) -> bool:
        return key in self.objects

    async def restore_file(self, key: str, path: Any) -> bool:
        self.restored.append(str(path))
        return key in self.objects

    ArchiveError = type("ArchiveError", (RuntimeError,), {})


def disk_plan(paths: list[str]) -> RemediationPlan:
    action = ApiCall(
        verb="s3_archive_then_delete",
        resource="v1/Node",
        name="worker-1",
        namespace=None,
        body={
            "files": [{"path": p, "size_bytes": 1024, "age_days": 30.0} for p in paths],
            "total_bytes": 1024 * len(paths),
            "floor_days": 7,
            "bucket": "healops-archives",
        },
    )
    undo = ApiCall(
        verb="s3_restore",
        resource="v1/Node",
        name="worker-1",
        namespace=None,
        body={
            "files": [
                {"path": p, "key": f"logs/worker-1/{p.strip('/').replace('/', '_')}"} for p in paths
            ],
            "bucket": "healops-archives",
        },
    )
    return RemediationPlan(
        runbook="disk_cleanup",
        incident_id=1,
        target_kind="Node",
        target_name="worker-1",
        target_namespace=None,
        actions=(action,),
        compensation=(undo,),
        reversibility=Reversibility.PARTIAL,
        durability=Durability.EPHEMERAL,
        ttl_seconds=7200,
        blast_radius=BlastRadius(nodes=1),
        confidence=0.9,
        summary="archive logs",
    )


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> FakeS3:
    import sys

    fake = FakeS3()
    module = type(sys)("integrations.s3_client")
    module.ensure_bucket = fake.ensure_bucket  # type: ignore[attr-defined]
    module.archive_file = fake.archive_file  # type: ignore[attr-defined]
    module.object_exists = fake.object_exists  # type: ignore[attr-defined]
    module.restore_file = fake.restore_file  # type: ignore[attr-defined]
    module.ArchiveError = fake.ArchiveError  # type: ignore[attr-defined]

    package = type(sys)("integrations")
    package.s3_client = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "integrations", package)
    monkeypatch.setitem(sys.modules, "integrations.s3_client", module)
    return fake


class TestDiskCleanupExecution:
    runbook = DiskCleanup()

    @pytest.fixture(autouse=True)
    def _roots(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        """The archive roots are the only place the agent may touch; here, the test's own dir."""
        from config import get_settings

        monkeypatch.setattr(get_settings(), "disk_archive_roots", [str(tmp_path)])

    async def test_a_path_outside_the_roots_is_refused(self, s3: FakeS3, tmp_path: Any) -> None:
        """Whatever produced the plan, the agent only deletes under the configured roots."""
        outside = tmp_path.parent / "elsewhere.log"
        outside.write_text("keep me", encoding="utf-8")
        try:
            result = await self.runbook.execute(disk_plan([str(outside)]))
            assert not result.ok
            assert "outside the archive roots" in result.detail
            assert outside.exists()
        finally:
            outside.unlink()

    async def test_archives_then_deletes(self, s3: FakeS3, tmp_path: Any) -> None:
        first = tmp_path / "a.log"
        second = tmp_path / "b.log"
        first.write_text("x" * 100, encoding="utf-8")
        second.write_text("y" * 100, encoding="utf-8")

        result = await self.runbook.execute(disk_plan([str(first), str(second)]))

        assert result.ok
        assert len(s3.archived) == 2
        # The originals are gone only after the archive was verified.
        assert not first.exists()
        assert not second.exists()

    async def test_refuses_when_object_storage_is_unreachable(
        self, s3: FakeS3, tmp_path: Any
    ) -> None:
        """Deleting because the archive destination is down would be data loss.

        That is strictly worse than the disk-pressure incident being remediated.
        """
        target = tmp_path / "a.log"
        target.write_text("x" * 100, encoding="utf-8")
        s3.reachable = False

        result = await self.runbook.execute(disk_plan([str(target)]))

        assert not result.ok
        assert "unreachable" in result.detail
        assert "nothing was deleted" in result.detail
        assert target.exists(), "the file must survive an unreachable archive destination"

    async def test_stops_at_the_first_archive_failure(self, s3: FakeS3, tmp_path: Any) -> None:
        """A failure mid-run must not cascade into deleting files that were never archived."""
        good = tmp_path / "good.log"
        bad = tmp_path / "bad.log"
        also = tmp_path / "also.log"
        for path in (good, bad, also):
            path.write_text("x" * 100, encoding="utf-8")
        s3.fail_on = "bad.log"

        result = await self.runbook.execute(disk_plan([str(good), str(bad), str(also)]))

        assert not result.ok
        assert "No further deletions were attempted" in result.detail
        assert not good.exists(), "the file archived before the failure was safely removed"
        assert bad.exists(), "the file that failed to archive must remain"
        assert also.exists(), "files after the failure must be untouched"

    async def test_reports_the_space_it_freed(self, s3: FakeS3, tmp_path: Any) -> None:
        target = tmp_path / "a.log"
        target.write_text("x" * 100, encoding="utf-8")

        result = await self.runbook.execute(disk_plan([str(target)]))
        assert result.state_after["freed_bytes"] == 1024

    async def test_compensation_restores_archived_files(self, s3: FakeS3, tmp_path: Any) -> None:
        target = tmp_path / "a.log"
        target.write_text("x" * 100, encoding="utf-8")
        plan = disk_plan([str(target)])

        await self.runbook.execute(plan)
        assert not target.exists()

        result = await self.runbook.compensate(plan)
        assert result.ok
        assert s3.restored == [str(target)]

    async def test_compensation_is_idempotent(self, s3: FakeS3, tmp_path: Any) -> None:
        # A file already back on disk needs no restore. The watchdog may call this after the
        # verifier already did.
        target = tmp_path / "a.log"
        target.write_text("x" * 100, encoding="utf-8")
        plan = disk_plan([str(target)])

        await self.runbook.execute(plan)
        target.write_text("restored", encoding="utf-8")

        result = await self.runbook.compensate(plan)
        assert result.ok
        assert s3.restored == [], "an existing file must not be re-downloaded"

    async def test_compensation_reports_unretrievable_objects(
        self, s3: FakeS3, tmp_path: Any
    ) -> None:
        """Honest about what it could not restore, rather than claiming success."""
        target = tmp_path / "a.log"
        target.write_text("x" * 100, encoding="utf-8")
        plan = disk_plan([str(target)])

        await self.runbook.execute(plan)
        s3.objects.clear()  # the archive is gone

        result = await self.runbook.compensate(plan)
        assert not result.ok
        assert result.state_after["missing"]


async def test_a_renumbered_revision_is_still_found_by_its_template() -> None:
    """Regression, found on the kind cluster: Kubernetes renumbers a reused ReplicaSet, so the
    revision 8 an undo was registered against is revision 10 by the time it runs. By digest, the
    undo still finds the template it means; by number it reported 'revision 8 is gone'."""
    from types import SimpleNamespace

    from k8s.state_capture import template_digest
    from runbooks.crashloop_rollback import _find

    bad = {
        "metadata": {"labels": {"app": "api"}},
        "spec": {"containers": [{"name": "api", "env": [{"name": "X"}]}]},
    }
    good = {"metadata": {"labels": {"app": "api"}}, "spec": {"containers": [{"name": "api"}]}}
    revisions = [
        SimpleNamespace(revision=10, template=bad),
        SimpleNamespace(revision=9, template=good),
    ]
    undo = {"to_revision": 8, "to_template": template_digest(bad)}

    assert _find(revisions, undo).revision == 10
    assert _find(revisions, {"to_revision": 8}) is None  # the number alone really is gone
