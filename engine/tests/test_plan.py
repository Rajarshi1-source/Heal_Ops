"""The RemediationPlan.

The constructor validations are the interesting part. Each one turns a class of runbook bug into a
failure at plan time, where a unit test catches it, rather than at execution time where a cluster
does.
"""

from __future__ import annotations

import dataclasses

import pytest

from plan import (
    ApiCall,
    BlastRadius,
    Durability,
    ExpectedSignal,
    RemediationPlan,
    Reversibility,
    refuse,
)


def call(verb: str = "patch", **kw: object) -> ApiCall:
    return ApiCall(
        verb=verb,
        resource=kw.pop("resource", "apps/v1/Deployment"),  # type: ignore[arg-type]
        name=kw.pop("name", "api"),  # type: ignore[arg-type]
        namespace=kw.pop("namespace", "shop"),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )


def plan(**overrides: object) -> RemediationPlan:
    base: dict[str, object] = {
        "runbook": "crashloop_rollback",
        "incident_id": 1,
        "target_kind": "Deployment",
        "target_name": "api",
        "target_namespace": "shop",
        "actions": (call(),),
        "compensation": (call(),),
        "reversibility": Reversibility.REVERSIBLE,
        "durability": Durability.EPHEMERAL,
        "ttl_seconds": 7200,
        "blast_radius": BlastRadius(pods=3),
        "confidence": 0.94,
        "summary": "rollout undo deployment/api",
    }
    return RemediationPlan(**(base | overrides))  # type: ignore[arg-type]


# --- immutability -------------------------------------------------------------------------------


def test_plan_is_frozen() -> None:
    """A mutable plan would make the policy gate advisory.

    If a plan could change between the gate and execution, the decision the gate recorded would not
    describe the action that ran.
    """
    p = plan()
    with pytest.raises(AttributeError):
        p.confidence = 0.1  # type: ignore[misc]
    with pytest.raises(AttributeError):
        p.actions = ()  # type: ignore[misc]


def test_derivation_goes_through_replace() -> None:
    p = plan()
    derived = p.with_plan_id("01a02850-544d-7863-a3b0-778fc5dd5919")
    assert derived.plan_id is not None
    assert p.plan_id is None, "the original must be untouched"


# --- the invariants the constructor enforces ----------------------------------------------------


def test_ephemeral_plan_must_carry_a_ttl() -> None:
    """An imperative change with no expiry is a permanent hand-edit nobody is tracking."""
    with pytest.raises(ValueError, match="must set ttl_seconds"):
        plan(ttl_seconds=None)


def test_durable_plan_needs_no_ttl() -> None:
    # A durable fix lands in git, so it does not expire -- the PR is the lifecycle.
    p = plan(durability=Durability.DURABLE, ttl_seconds=None)
    assert p.requires_git_followup is True


def test_reversible_plan_must_describe_its_undo() -> None:
    """A runbook that cannot describe its own undo is not registered."""
    with pytest.raises(ValueError, match="must carry its compensation"):
        plan(compensation=())


def test_plan_with_no_actions_and_no_refusal_is_rejected() -> None:
    # A plan that does nothing but is not a refusal would be recorded as an action that succeeded
    # while changing nothing.
    with pytest.raises(ValueError, match="does nothing"):
        plan(actions=())


def test_confidence_must_be_a_probability() -> None:
    with pytest.raises(ValueError, match="confidence"):
        plan(confidence=1.5)
    with pytest.raises(ValueError, match="confidence"):
        plan(confidence=-0.1)


# --- refusals -----------------------------------------------------------------------------------


def test_refusal_is_a_first_class_plan() -> None:
    """Not an exception, not a None. A refusal is audited exactly like an action."""
    r = refuse("oom_memory_bump", 42, "already at ceiling 4096Mi -- this needs a code fix")

    assert r.is_refusal is True
    assert r.actions == ()
    assert r.refusal_reason is not None
    assert "ceiling" in r.refusal_reason
    assert r.summary.startswith("REFUSED:")
    # It survives the same serialisation path as any other plan.
    assert r.to_dict()["refusal_reason"] == r.refusal_reason


def test_refusal_is_confident_by_default() -> None:
    """The runbook is sure about its refusal.

    A low confidence here would make the calibration gate score a correct decision as a hesitant
    one, and would also trip `approve-low-confidence` for something that is not going to act.
    """
    assert refuse("r", 1, "reason").confidence == 1.0


def test_refusal_bypasses_the_action_and_ttl_invariants() -> None:
    # A refusal has no actions and needs no compensation, so the constructor must not demand them.
    r = refuse("r", 1, "nope")
    assert r.compensation == ()
    assert r.ttl_seconds == 0


def test_is_refusal_is_false_for_a_real_plan() -> None:
    assert plan().is_refusal is False


# --- blast radius -------------------------------------------------------------------------------


def test_blast_radius_score_is_arithmetic_and_explainable() -> None:
    """A rule a reviewer can evaluate in their head beats a weighting nobody trusts."""
    assert BlastRadius().score() == 0
    assert BlastRadius(pods=3).score() == 3
    assert BlastRadius(nodes=1).score() == 10
    assert BlastRadius(user_facing=True).score() == 20
    assert BlastRadius(pods=3, nodes=2, user_facing=True).score() == 3 + 20 + 20


def test_a_node_action_outscores_many_pods() -> None:
    # Draining one node affects everything on it, so it must rank above a handful of pods.
    assert BlastRadius(nodes=1).score() > BlastRadius(pods=9).score()


def test_user_facing_carries_a_flat_premium() -> None:
    assert BlastRadius(pods=1, user_facing=True).score() > BlastRadius(pods=15).score()


# --- serialisation and the determinism digest ---------------------------------------------------


def test_to_dict_flattens_the_blast_radius_score() -> None:
    # CEL cannot call Python methods, so the policy gate reads the computed scalar.
    assert plan().to_dict()["blast_radius_score"] == 3


def test_canonical_json_is_sorted_and_compact() -> None:
    text = plan().canonical_json()
    assert ", " not in text
    assert '": ' not in text


def test_digest_is_stable_for_the_same_decision() -> None:
    """The determinism anchor for the decision eval: same input, byte-identical plan."""
    assert plan().digest() == plan().digest()


def test_digest_ignores_the_plan_id() -> None:
    """`plan_id` is assigned per run, not derived from the decision.

    Including it would make every replay produce a different digest and the determinism gate could
    never pass.
    """
    p = plan()
    assert p.digest() == p.with_plan_id("01a02850-544d-7863-a3b0-778fc5dd5919").digest()


def test_digest_changes_when_the_decision_changes() -> None:
    p = plan()
    assert p.digest() != plan(confidence=0.5).digest()
    assert p.digest() != plan(summary="something else").digest()
    assert p.digest() != dataclasses.replace(p, actions=(call(verb="scale"),)).digest()


# --- descriptions -------------------------------------------------------------------------------


def test_api_call_describes_itself_readably() -> None:
    # Rendered into the Slack card, so it has to read as an action rather than a struct dump.
    assert call().describe() == "patch apps/v1/Deployment shop/api"
    assert (
        ApiCall(
            verb="resize", resource="v1/Pod", name="p1", namespace="shop", subresource="resize"
        ).describe()
        == "resize v1/Pod/resize shop/p1"
    )
    assert (
        ApiCall(verb="cordon", resource="v1/Node", name="worker-1").describe()
        == "cordon v1/Node worker-1"
    )


def test_target_key_matches_the_lock_and_recurrence_key_shape() -> None:
    assert plan().target_key == "shop/Deployment/api"
    assert (
        plan(target_kind="Node", target_name="worker-1", target_namespace=None).target_key
        == "-/Node/worker-1"
    )


def test_expected_signal_states_recovery_up_front() -> None:
    """Committing to the criterion up front stops verification hunting for any improved metric."""
    signal = ExpectedSignal(
        alert_should_clear="KubePodCrashLooping",
        queries={"restarts": "increase(kube_pod_container_status_restarts_total[5m])"},
        thresholds={"restarts": 0.0},
        window_seconds=300,
    )
    p = plan(expected_signal=signal)
    assert p.expected_signal is not None
    assert p.expected_signal.alert_should_clear == "KubePodCrashLooping"
    assert p.expected_signal.window_seconds == 300


# --- enums --------------------------------------------------------------------------------------


def test_reversibility_values_match_the_database_check_constraint() -> None:
    assert str(Reversibility.REVERSIBLE) == "reversible"
    assert str(Reversibility.PARTIAL) == "partially_reversible"
    assert str(Reversibility.IRREVERSIBLE) == "irreversible"


def test_durability_values_match_the_database_check_constraint() -> None:
    assert str(Durability.EPHEMERAL) == "ephemeral"
    assert str(Durability.DURABLE) == "durable"
