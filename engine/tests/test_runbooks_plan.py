"""Runbook planning.

`plan()` is a pure function of the bundle, which makes it the most testable part of the system and
the part most worth testing: every decision the orchestrator later gates is made here.

The refusal cases carry most of the weight. A remediation engine that never refuses is a liability,
and refusal recall is graded at 100% in CI rather than averaged -- so each refusal has a test naming
the situation it declines.
"""

from __future__ import annotations

import json

import pytest

from bundle import (
    AutoscalerState,
    BundleAlert,
    ContainerState,
    IncidentBundle,
    Target,
)
from plan import Durability, Reversibility
from pricing import replica_cost
from runbooks import load_all
from runbooks.cpu_scale_up import CpuScaleUp
from runbooks.crashloop_rollback import CrashLoopRollback
from runbooks.disk_cleanup import DiskCleanup


@pytest.fixture(scope="module", autouse=True)
def _registry() -> None:
    load_all()


# --- helpers -------------------------------------------------------------------------------------


def alert(alertname: str, **annotations: str) -> BundleAlert:
    return BundleAlert(
        alertname=alertname,
        fingerprint="fp-1",
        severity="critical",
        namespace="shop",
        pod="api-6b8c9d7f5-ab12c",
        container="api",
        node="worker-1",
        annotations=annotations,
    )


def bundle(alertname: str, target: Target, **annotations: str) -> IncidentBundle:
    return IncidentBundle(
        incident_id=1,
        cause_alert=alert(alertname, **annotations),
        target=target,
    )


def deployment(**overrides: object) -> Target:
    base: dict[str, object] = {
        "kind": "Deployment",
        "name": "api",
        "namespace": "shop",
        "labels": {"app": "api"},
        "replicas": 3,
        "ready_replicas": 0,
        "containers": (
            ContainerState(
                name="api",
                image="ghcr.io/o/api:v2",
                cpu_limit_millis=200,
                memory_limit_mib=256,
                restart_count=7,
            ),
        ),
        "pods": ("api-6b8c9d7f5-ab12c",),
        "node": "worker-1",
        "revision": 4,
        "previous_revision": 3,
        "previous_image": "ghcr.io/o/api:v1",
    }
    return Target(**(base | overrides))  # type: ignore[arg-type]


# =================================================================================================
# Runbook 1 -- CrashLoop rollback
# =================================================================================================


class TestCrashLoopRollback:
    runbook = CrashLoopRollback()

    def test_plans_a_rollback_to_the_previous_revision(self) -> None:
        target = deployment()
        p = self.runbook.plan(bundle("KubePodCrashLooping", target), target)

        assert not p.is_refusal
        assert len(p.actions) == 1
        assert p.actions[0].verb == "rollout_undo"
        assert p.actions[0].body["to_revision"] == 3
        assert p.reversibility is Reversibility.REVERSIBLE
        assert "revision 4 to 3" in p.summary

    def test_compensation_rolls_forward_to_where_we_came_from(self) -> None:
        """The undo is decided BEFORE acting, not reconstructed afterwards.

        By the time compensation runs, current cluster state is the thing being undone -- so
        deriving the target revision from it would aim at the wrong place.
        """
        target = deployment()
        p = self.runbook.plan(bundle("KubePodCrashLooping", target), target)

        assert len(p.compensation) == 1
        assert p.compensation[0].body["to_revision"] == 4, "must return to the original revision"
        assert p.compensation[0].body["to_image"] == "ghcr.io/o/api:v2"

    def test_rollback_is_ephemeral_with_a_ttl(self) -> None:
        # On a GitOps-managed workload the next sync re-applies the bad image, so an imperative
        # rollback is explicitly a TTL'd mitigation. The PR is the real fix.
        p = self.runbook.plan(bundle("KubePodCrashLooping", deployment()), deployment())
        assert p.durability is Durability.EPHEMERAL
        assert p.ttl_seconds is not None and p.ttl_seconds > 0

    def test_refuses_when_there_is_no_previous_revision(self) -> None:
        """A workload on its first revision has nothing to roll back to."""
        target = deployment(revision=1, previous_revision=None, previous_image=None)
        p = self.runbook.plan(bundle("KubePodCrashLooping", target), target)

        assert p.is_refusal
        assert "no previous revision" in (p.refusal_reason or "")
        assert p.actions == ()

    def test_refuses_when_the_previous_revision_runs_the_same_image(self) -> None:
        """Defect D1, caught at plan time rather than discovered by verification.

        Rolling back onto an equally broken revision is the exact case Rev 1 recorded as a success.
        """
        target = deployment(
            previous_image="ghcr.io/o/api:v2",
            template_digest="a" * 64,
            previous_template_digest="a" * 64,
        )
        p = self.runbook.plan(bundle("KubePodCrashLooping", target), target)

        assert p.is_refusal
        assert "same pod template" in (p.refusal_reason or "")
        assert "equally broken revision" in (p.refusal_reason or "")

    def test_without_digests_the_same_image_is_still_refused(self) -> None:
        # The conservative fallback, for a bundle captured before template digests existed.
        target = deployment(previous_image="ghcr.io/o/api:v2")
        p = self.runbook.plan(bundle("KubePodCrashLooping", target), target)

        assert p.is_refusal

    def test_a_bad_config_on_the_same_image_is_rolled_back(self) -> None:
        """Regression: the image rule refused the commonest bad deploy -- an env or flag change.

        Found by the chaos suite: crashy-api's bad revision only adds CRASH_AFTER, so the old rule
        refused the very fault the runbook exists to fix.
        """
        target = deployment(
            previous_image="ghcr.io/o/api:v2",
            template_digest="a" * 64,
            previous_template_digest="b" * 64,
        )
        p = self.runbook.plan(bundle("KubePodCrashLooping", target), target)

        assert not p.is_refusal
        assert p.actions[0].body["to_revision"] == target.previous_revision

    def test_refuses_a_statefulset(self) -> None:
        # Policy also denies StatefulSets, but the runbook declines on its own too: only Deployments
        # carry the ReplicaSet revision history a rollback needs. Defence in depth.
        target = deployment(kind="StatefulSet", name="chaos-state")
        p = self.runbook.plan(bundle("KubePodCrashLooping", target), target)

        assert p.is_refusal
        assert "not rollable" in (p.refusal_reason or "")

    def test_blast_radius_counts_the_replicas(self) -> None:
        p = self.runbook.plan(bundle("KubePodCrashLooping", deployment()), deployment())
        assert p.blast_radius.pods == 3
        assert p.blast_radius.nodes == 0

    def test_user_facing_workload_scores_higher(self) -> None:
        plain = deployment(labels={"app": "api"})
        facing = deployment(labels={"app": "api", "tier": "frontend"})

        plain_plan = self.runbook.plan(bundle("KubePodCrashLooping", plain), plain)
        facing_plan = self.runbook.plan(bundle("KubePodCrashLooping", facing), facing)

        assert facing_plan.blast_radius.score() > plain_plan.blast_radius.score()

    def test_confidence_is_deterministic(self) -> None:
        """Deterministic rules, not a model -- so it can be unit-tested, replayed and hashed."""
        target = deployment()
        first = self.runbook.plan(bundle("KubePodCrashLooping", target), target)
        second = self.runbook.plan(bundle("KubePodCrashLooping", target), target)
        assert first.confidence == second.confidence
        assert first.digest() == second.digest()

    def test_gitops_managed_target_lowers_confidence(self) -> None:
        # A reconciliation race we cannot fully control from here is a real reason to be less sure.
        unmanaged = deployment()
        managed = deployment(gitops_manager="argocd")

        assert (
            self.runbook.plan(bundle("KubePodCrashLooping", managed), managed).confidence
            < self.runbook.plan(bundle("KubePodCrashLooping", unmanaged), unmanaged).confidence
        )

    def test_expected_signal_commits_to_restarts_going_flat(self) -> None:
        p = self.runbook.plan(bundle("KubePodCrashLooping", deployment()), deployment())
        assert p.expected_signal is not None
        assert p.expected_signal.alert_should_clear == "KubePodCrashLooping"
        assert p.expected_signal.thresholds["restarts"] == 0.0

    @pytest.mark.asyncio
    async def test_extra_verification_rejects_continued_restarts(self) -> None:
        from runbooks.base import SignalWindow

        p = self.runbook.plan(bundle("KubePodCrashLooping", deployment()), deployment())

        assert await self.runbook.extra_verification(p, SignalWindow(restarts=0)) is None
        assert await self.runbook.extra_verification(p, SignalWindow(restarts=3)) is not None

    @pytest.mark.asyncio
    async def test_extra_verification_treats_a_missing_signal_as_unproven(self) -> None:
        """Absence of evidence is never evidence of recovery."""
        from runbooks.base import SignalWindow

        p = self.runbook.plan(bundle("KubePodCrashLooping", deployment()), deployment())
        reason = await self.runbook.extra_verification(p, SignalWindow(restarts=None))
        assert reason is not None
        assert "unavailable" in reason


# =================================================================================================
# Runbook 3 -- CPU scale-up (defect D4)
# =================================================================================================


class TestCpuScaleUp:
    runbook = CpuScaleUp()

    def test_scales_replicas_when_no_hpa_owns_the_workload(self) -> None:
        target = deployment(replicas=3, autoscaler=None)
        p = self.runbook.plan(bundle("HighPodCPU", target), target)

        assert not p.is_refusal
        assert p.actions[0].verb == "scale"
        # min(3*2.0, 3+2, ceiling 10) == 5
        assert p.actions[0].body["replicas"] == 5
        assert p.compensation[0].body["replicas"] == 3

    def test_respects_the_replica_ceiling(self) -> None:
        """A CPU alert must never become a cost incident."""
        target = deployment(replicas=9, autoscaler=None)
        p = self.runbook.plan(bundle("HighPodCPU", target), target)
        assert p.actions[0].body["replicas"] == 10

    def test_refuses_at_the_ceiling(self) -> None:
        target = deployment(replicas=10, autoscaler=None)
        p = self.runbook.plan(bundle("HighPodCPU", target), target)

        assert p.is_refusal
        assert "ceiling" in (p.refusal_reason or "")
        assert "capacity decision" in (p.refusal_reason or "")

    def test_patches_the_hpa_target_instead_of_replicas(self) -> None:
        """Act on the control loop, not on its output.

        The HPA owns `spec.replicas`. Patching replicas directly would be reverted within one sync,
        or oscillate against it -- which was defect D4.
        """
        target = deployment(
            replicas=4,
            autoscaler=AutoscalerState(
                name="api",
                metric="cpu",
                target_utilization=80,
                min_replicas=2,
                max_replicas=10,
                current_replicas=4,
            ),
        )
        p = self.runbook.plan(bundle("HighPodCPU", target), target)

        assert not p.is_refusal
        assert p.actions[0].verb == "hpa_patch"
        assert p.actions[0].body["target_utilization"] == 65  # 80 - 15
        assert p.compensation[0].body["target_utilization"] == 80
        assert "owns spec.replicas" in p.summary

    def test_raises_max_replicas_when_that_is_the_binding_constraint(self) -> None:
        # Lowering the utilisation target achieves nothing if the HPA is already pinned at its max.
        target = deployment(
            replicas=6,
            autoscaler=AutoscalerState(
                name="api",
                metric="cpu",
                target_utilization=70,
                min_replicas=2,
                max_replicas=6,
                current_replicas=6,
            ),
        )
        p = self.runbook.plan(bundle("HighPodCPU", target), target)

        assert p.actions[0].body["max_replicas"] == 8
        assert "target_utilization" not in p.actions[0].body

    def test_refuses_an_externally_scaled_workload(self) -> None:
        """Queue depth is the real signal; a CPU nudge would fight it."""
        target = deployment(
            autoscaler=AutoscalerState(
                name="worker", metric="external", max_replicas=20, current_replicas=4
            )
        )
        p = self.runbook.plan(bundle("HighPodCPU", target), target)

        assert p.is_refusal
        assert "external metric" in (p.refusal_reason or "")

    def test_refuses_when_the_hpa_is_maxed_at_the_ceiling(self) -> None:
        target = deployment(
            autoscaler=AutoscalerState(
                name="api",
                metric="cpu",
                target_utilization=60,
                max_replicas=10,
                current_replicas=10,
            )
        )
        p = self.runbook.plan(bundle("HighPodCPU", target), target)

        assert p.is_refusal
        assert "maxReplicas" in (p.refusal_reason or "")

    def test_refuses_when_the_hpa_target_is_already_at_the_floor(self) -> None:
        # Driving the target lower would keep the workload permanently oversized.
        target = deployment(
            replicas=4,
            autoscaler=AutoscalerState(
                name="api",
                metric="cpu",
                target_utilization=50,
                max_replicas=10,
                current_replicas=4,
            ),
        )
        p = self.runbook.plan(bundle("HighPodCPU", target), target)

        assert p.is_refusal
        assert "floor" in (p.refusal_reason or "")

    def test_cost_is_projected_onto_the_plan(self) -> None:
        """Cost is a first-class approval criterion, not an afterthought."""
        target = deployment(replicas=3, autoscaler=None)
        p = self.runbook.plan(bundle("HighPodCPU", target), target)
        assert p.cost_delta_inr_month == pytest.approx(2 * replica_cost(target.containers))

    def test_scale_up_expires_on_its_own(self) -> None:
        # Load spikes end. Automation that leaves a workload permanently oversized has made a
        # capacity decision nobody reviewed.
        p = self.runbook.plan(bundle("HighPodCPU", deployment(autoscaler=None)), deployment())
        assert p.durability is Durability.EPHEMERAL
        assert p.ttl_seconds == 7200

    def test_throttle_signal_is_a_ratio_not_a_counter(self) -> None:
        """This runbook changes the replica count, so a raw counter comparison is meaningless."""
        p = self.runbook.plan(bundle("HighPodCPU", deployment(autoscaler=None)), deployment())
        assert p.expected_signal is not None
        query = p.expected_signal.queries["throttle_ratio"]
        assert "throttle_ratio" in query
        assert "cfs_throttled_periods_total" not in query

    def test_single_replica_lowers_confidence(self) -> None:
        # Often a batch job or a singleton, where scaling out may not help at all.
        lonely = deployment(replicas=1, autoscaler=None)
        normal = deployment(replicas=3, autoscaler=None)

        assert (
            self.runbook.plan(bundle("HighPodCPU", lonely), lonely).confidence
            < self.runbook.plan(bundle("HighPodCPU", normal), normal).confidence
        )

    def test_refuses_a_non_deployment(self) -> None:
        target = deployment(kind="StatefulSet")
        p = self.runbook.plan(bundle("HighPodCPU", target), target)
        assert p.is_refusal


# =================================================================================================
# Runbook 2 -- disk cleanup
# =================================================================================================


def node_target(**overrides: object) -> Target:
    base: dict[str, object] = {
        "kind": "Node",
        "name": "worker-1",
        "namespace": None,
        "node": "worker-1",
    }
    return Target(**(base | overrides))  # type: ignore[arg-type]


def candidates(count: int, size_mib: int, age_days: float = 30.0) -> str:
    return json.dumps(
        [
            {
                "path": f"/var/log/chaos/archived-{i:03d}.log",
                "size_bytes": size_mib * 1024**2,
                "age_days": age_days,
            }
            for i in range(count)
        ]
    )


class TestDiskCleanup:
    runbook = DiskCleanup()

    def test_plans_archive_then_delete(self) -> None:
        target = node_target()
        b = bundle("NodeDiskPressure", target, healops_archive_candidates=candidates(10, 100))
        p = self.runbook.plan(b, target)

        assert not p.is_refusal
        assert p.actions[0].verb == "s3_archive_then_delete"
        assert p.compensation[0].verb == "s3_restore"

    def test_plan_carries_the_dry_run_listing(self) -> None:
        """A human approving this sees the exact files. The plan IS the listing."""
        target = node_target()
        b = bundle("NodeDiskPressure", target, healops_archive_candidates=candidates(4, 100))
        p = self.runbook.plan(b, target)

        files = p.actions[0].body["files"]
        assert len(files) == 4
        assert all("path" in f and "size_bytes" in f and "age_days" in f for f in files)

    def test_is_only_partially_reversible(self) -> None:
        """The bytes come back from S3, but the deletion happened.

        Claiming full reversibility here would make every other classification untrustworthy -- and
        it is what routes this runbook to the approval path rather than the auto path.
        """
        target = node_target()
        b = bundle("NodeDiskPressure", target, healops_archive_candidates=candidates(4, 100))
        p = self.runbook.plan(b, target)
        assert p.reversibility is Reversibility.PARTIAL

    def test_refuses_when_nothing_is_old_enough(self) -> None:
        """Recent logs are what an engineer needs while the incident is live."""
        target = node_target()
        p = self.runbook.plan(bundle("NodeDiskPressure", target), target)

        assert p.is_refusal
        assert "older than" in (p.refusal_reason or "")

    def test_refuses_when_the_archivable_total_is_trivial(self) -> None:
        # Churning the disk for a few megabytes is noise, and the space is being used by something
        # this runbook does not manage.
        target = node_target()
        b = bundle("NodeDiskPressure", target, healops_archive_candidates=candidates(1, 1))
        p = self.runbook.plan(b, target)

        assert p.is_refusal
        assert "will not relieve disk pressure" in (p.refusal_reason or "")

    def test_caps_the_amount_archived_in_one_action(self) -> None:
        # A single invocation that moves tens of gigabytes is a migration, not a remediation.
        target = node_target()
        b = bundle("NodeDiskPressure", target, healops_archive_candidates=candidates(20, 1024))
        p = self.runbook.plan(b, target)

        assert p.actions[0].body["total_bytes"] <= 5 * 1024**3

    def test_oldest_files_are_selected_first(self) -> None:
        target = node_target()
        mixed = json.dumps(
            [
                {"path": "/var/log/new.log", "size_bytes": 4 * 1024**3, "age_days": 8},
                {"path": "/var/log/old.log", "size_bytes": 4 * 1024**3, "age_days": 60},
            ]
        )
        b = bundle("NodeDiskPressure", target, healops_archive_candidates=mixed)
        p = self.runbook.plan(b, target)

        chosen = [f["path"] for f in p.actions[0].body["files"]]
        assert chosen == ["/var/log/old.log"], "the cap must keep the least useful data"

    def test_malformed_candidate_listing_refuses(self) -> None:
        # Unparseable input must not be guessed at; refusing is the safe reading.
        target = node_target()
        b = bundle("NodeDiskPressure", target, healops_archive_candidates="not json{{{")
        assert self.runbook.plan(b, target).is_refusal

    def test_blast_radius_is_node_scoped(self) -> None:
        target = node_target()
        b = bundle("NodeDiskPressure", target, healops_archive_candidates=candidates(10, 100))
        p = self.runbook.plan(b, target)
        assert p.blast_radius.nodes == 1
        assert p.blast_radius.pods == 0


# =================================================================================================
# The registry contract
# =================================================================================================


class TestRegistry:
    def test_every_runbook_declares_a_chaos_fixture(self) -> None:
        """A runbook that cannot prove itself is not registered."""
        registry = load_all()
        for runbook in registry.all():
            assert runbook.chaos_fixture, f"{runbook.name} names no fixture"

    def test_fixtures_exist_in_the_chaos_app(self) -> None:
        """The fixture name is a CONTRACT with the chaos app, so it is checked, not assumed."""
        # Mirrors chaos-app/entrypoint.py plus the flaky-node injector.
        available = {"crashy-api", "hungry-worker", "cpu-burner", "log-spammer", "flaky-node"}
        for runbook in load_all().all():
            assert runbook.chaos_fixture in available, (
                f"{runbook.name} names fixture {runbook.chaos_fixture!r}, "
                "which the chaos app does not ship"
            )

    def test_each_alert_maps_to_exactly_one_runbook(self) -> None:
        # Two runbooks answering one alert would make selection non-deterministic and break the
        # determinism digest.
        registry = load_all()
        for trigger in registry.triggers:
            assert registry.for_alert(trigger) is not None

    def test_duplicate_registration_is_rejected(self) -> None:
        from runbooks.base import RunbookRegistry

        fresh = RunbookRegistry()
        fresh.register(CrashLoopRollback())
        with pytest.raises(ValueError, match="duplicate runbook name"):
            fresh.register(CrashLoopRollback())

    def test_conflicting_triggers_are_rejected(self) -> None:
        from runbooks.base import RunbookRegistry

        class Impostor(CrashLoopRollback):
            name = "impostor"

        fresh = RunbookRegistry()
        fresh.register(CrashLoopRollback())
        with pytest.raises(ValueError, match="already answered by"):
            fresh.register(Impostor())

    def test_describe_exposes_the_model_card_fields(self) -> None:
        described = load_all().describe()
        assert described
        for entry in described:
            assert {"name", "triggers", "chaos_fixture"} <= set(entry)

    def test_unknown_alert_has_no_runbook(self) -> None:
        # An unrecognised alert escalates rather than being force-fitted to a runbook.
        assert load_all().for_alert("SomethingNobodyPlannedFor") is None


def test_disk_recovery_judges_the_filesystem_the_logs_lived_on() -> None:
    """Regression, found on the kind cluster: the check read only `mountpoint="/"`, which a kind
    node does not expose, so a cleanup that freed 60 MiB was judged inconclusive and undone."""
    from runbooks.disk_cleanup import disk_used_query

    query = disk_used_query("w2", ["/var/log/chaos/a.log", "/var/log/b.log"])
    order = [m.split('"')[0] for m in query.split('mountpoint="')[1:]]
    # Longest first, each term once per filesystem (avail and size), every parent considered.
    assert order[::2] == ["/var/log/chaos", "/var/log", "/var", "/"]
    assert query.count(" or ") == 3
    assert 'node="w2"' in query
