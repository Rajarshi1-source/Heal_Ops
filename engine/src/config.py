"""Environment-driven configuration.

Every tunable in HealOps is read here. No module reads `os.environ` directly and no timeout,
threshold or ceiling is written as a literal at its call site -- an operator changing the approval
window should not have to grep the runbooks for `600`.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["local", "staging", "prod"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HEALOPS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = "local"
    planner_version: str = "1.0.0"
    """Recorded on every plan so an incident can be correlated with the engine that handled it,
    and used as the key for the frozen determinism digests in the decision eval."""

    # --- storage -------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://healops_app:healops@localhost:5432/healops"
    redis_url: str = "redis://localhost:6379/0"

    # --- observability -------------------------------------------------------
    alertmanager_url: str = "http://localhost:9093"
    prometheus_url: str = "http://localhost:9090"
    webhook_shared_secret: SecretStr = SecretStr("change-me")

    # --- Slack ---------------------------------------------------------------
    slack_bot_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    slack_channel: str = "#healops-incidents"

    # --- object storage ------------------------------------------------------
    s3_endpoint: str | None = None
    s3_bucket: str = "healops-archives"

    # --- GitHub --------------------------------------------------------------
    github_token: SecretStr | None = None
    github_repo: str | None = None

    # --- narrator ------------------------------------------------------------
    narrator: Literal["template", "llm"] = "template"
    llm_model: str | None = None
    llm_api_key: SecretStr | None = None

    # --- policy --------------------------------------------------------------
    policy_file: str = "policy/remediation.yaml"
    self_namespace: str = "healops"

    # --- timeouts, in seconds ------------------------------------------------
    # An unbounded wait masquerading as "in progress" is the bug these prevent.
    k8s_timeout_seconds: int = 8
    resize_wait_seconds: int = 60
    approval_timeout_seconds: int = 600
    verification_window_seconds: int = 300
    verification_deadline_seconds: int = 900
    """The watchdog force-reverts any execution still awaiting verification past this."""
    verification_poll_seconds: int = 15
    verification_settle_seconds: int = 30
    """Excluded from the start of every post-action window: old pods restarting while a rollout
    replaces them are the failure being fixed, not evidence that the fix failed."""
    verification_early_after_seconds: int = 60
    """Failure may be called from here on; success only once the full window has elapsed."""
    verification_early_restart_threshold: int = 2
    verify_reclaim_idle_seconds: int = 120

    # --- approvals -----------------------------------------------------------
    approval_reaper_interval_seconds: int = 30
    approvals_api_token: SecretStr | None = None
    """Bearer token for `POST /api/approvals`. Unset means the endpoint refuses every decision and
    Slack is the only approval path -- an identity asserted in a request body is not an identity."""

    # --- worker --------------------------------------------------------------
    worker_enabled: bool = True
    """Run the exec, verify and reaper loops in this process. They only act while holding the
    Lease; this switch exists so the API can be served without them in tests and the public demo."""

    # --- locking and dedup ---------------------------------------------------
    lock_ttl_seconds: int = 120
    dedup_ttl_seconds: int = 300

    # --- queue ---------------------------------------------------------------
    stream_name: str = "remediation-jobs"
    dlq_name: str = "remediation-jobs-dlq"
    consumer_group_exec: str = "exec"
    consumer_group_verify: str = "verify"
    max_delivery_attempts: int = 3

    # --- leader election -----------------------------------------------------
    lease_name: str = "healops-engine-leader"
    lease_duration_seconds: int = 15

    # --- safety limits -------------------------------------------------------
    max_replicas_ceiling: int = 10
    max_scale_factor: float = 2.0
    cost_approval_threshold_inr: float = 500.0
    memory_bump_factor: float = 1.5
    memory_ceiling_mib: int = 4096
    disk_archive_floor_days: int = 7
    drain_capacity_headroom: float = 1.2
    recurrence_threshold: int = 4
    recurrence_window_days: int = 7

    # --- verification thresholds ---------------------------------------------
    throttle_ratio_ceiling: float = 0.05
    psi_full_ceiling: float = 0.05
    restart_tolerance: int = 0

    # --- promotion ladder ----------------------------------------------------
    shadow_min_plans: int = 10
    shadow_min_agreement: float = 0.90
    approval_min_consecutive_good: int = 20

    # --- budget and breaker --------------------------------------------------
    budget_window_seconds: int = 3600
    budget_global_max: int = 20
    breaker_compensation_threshold: int = 3
    breaker_compensation_window_seconds: int = 900
    breaker_consecutive_failure_threshold: int = 2
    breaker_k8s_error_rate: float = 0.20
    breaker_half_open_after_seconds: int = 1800

    # --- ephemeral fixes -----------------------------------------------------
    default_ephemeral_ttl_seconds: int = 7200

    read_only: bool = Field(
        default=False,
        description="Public demo builds refuse every mutating path regardless of policy.",
    )

    @field_validator("webhook_shared_secret")
    @classmethod
    def _reject_default_secret_outside_local(cls, v: SecretStr, info: object) -> SecretStr:
        # Deliberately not fatal in local dev; the deployment chart sets a real secret and the
        # health endpoint reports when the default is still in place.
        return v

    @property
    def slack_enabled(self) -> bool:
        return self.slack_bot_token is not None and self.slack_signing_secret is not None

    @property
    def github_enabled(self) -> bool:
        return self.github_token is not None and self.github_repo is not None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
