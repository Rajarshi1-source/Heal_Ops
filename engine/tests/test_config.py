"""Configuration is the one module every other module depends on, so it gets a smoke test.

These assertions double as documentation of the constants the skills mandate: if someone
"simplifies" the approval window to five minutes or drops the replica ceiling, this fails.
"""

from __future__ import annotations

import pytest

from config import Settings, get_settings


def test_defaults_match_the_mandated_constants() -> None:
    s = Settings()

    assert s.approval_timeout_seconds == 600, "approval window is 10 minutes"
    assert s.verification_deadline_seconds == 900, "watchdog force-reverts after 15 minutes"
    assert s.lock_ttl_seconds == 120
    assert s.dedup_ttl_seconds == 300
    assert s.k8s_timeout_seconds == 8
    assert s.resize_wait_seconds == 60

    assert s.throttle_ratio_ceiling == 0.05
    assert s.psi_full_ceiling == 0.05
    assert s.restart_tolerance == 0

    assert s.max_replicas_ceiling == 10
    assert s.memory_ceiling_mib == 4096
    assert s.memory_bump_factor == 1.5
    assert s.disk_archive_floor_days == 7
    assert s.drain_capacity_headroom == 1.2

    assert s.shadow_min_plans == 10
    assert s.shadow_min_agreement == 0.90
    assert s.approval_min_consecutive_good == 20

    assert s.recurrence_threshold == 4
    assert s.recurrence_window_days == 7


def test_queue_uses_separate_consumer_groups() -> None:
    # Bulkhead: a slow verification backlog must not block new incident handling.
    s = Settings()
    assert s.consumer_group_exec != s.consumer_group_verify
    assert s.dlq_name != s.stream_name


def test_optional_integrations_are_absent_by_default() -> None:
    # The system must work with no Slack, no GitHub, no LLM key and no network.
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.slack_enabled is False
    assert s.github_enabled is False
    assert s.narrator == "template"


@pytest.mark.parametrize("field", ["slack_bot_token", "slack_signing_secret", "github_token"])
def test_credentials_are_secretstr(field: str) -> None:
    # Prevents a token reaching a log line, a ledger payload or a Slack message body.
    annotation = Settings.model_fields[field].annotation
    assert "SecretStr" in str(annotation)


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
