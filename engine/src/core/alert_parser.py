"""Alertmanager **v2** webhook payload.

API v1 was removed in Alertmanager 0.27, so v2 is the only shape parsed here. The models are
deliberately permissive about extra fields and strict about the ones decisions depend on: a new
Alertmanager release adding a field must not 4xx a webhook, because Alertmanager would then retry
forever and the incident would never be recorded.

`extra="allow"` plus storing the raw payload verbatim in `JSONB` is the combination that makes an
incident reconstructable later. Reconstructing an incident from parsed fields only works until the
first field you did not think to parse.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from core.incident_correlator import AlertFacts, workload_from_pod

# Alertmanager sends this for an alert with no end time.
_NULL_TIMESTAMP = "0001-01-01T00:00:00Z"


class AlertmanagerAlert(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: str = "firing"
    labels: dict[str, str] = Field(default_factory=dict)
    annotations: dict[str, str] = Field(default_factory=dict)
    startsAt: datetime | None = None  # noqa: N815 - wire format is camelCase
    endsAt: datetime | None = None  # noqa: N815
    generatorURL: str | None = None  # noqa: N815
    fingerprint: str = ""

    @field_validator("endsAt", mode="before")
    @classmethod
    def _null_end_time(cls, v: Any) -> Any:
        # Alertmanager uses year 0001 as "no end time". Storing that as a real timestamp makes
        # "has this resolved?" queries silently wrong.
        return None if v in (_NULL_TIMESTAMP, "", None) else v

    @property
    def alertname(self) -> str:
        return self.labels.get("alertname", "")

    @property
    def severity(self) -> str:
        return self.labels.get("severity", "warning")

    @property
    def namespace(self) -> str | None:
        return self.labels.get("namespace")

    @property
    def pod(self) -> str | None:
        return self.labels.get("pod")

    @property
    def container(self) -> str | None:
        return self.labels.get("container")

    @property
    def node(self) -> str | None:
        # kube-state-metrics uses `node`; some exporters only carry `instance`.
        return self.labels.get("node") or self.labels.get("instance")

    @property
    def is_firing(self) -> bool:
        return self.status == "firing"

    def facts(self) -> AlertFacts:
        """Project onto the subset correlation is allowed to see.

        The workload name is a *guess* from the pod name here. The engine overwrites it with the
        real ownerReference when it can reach the API server, because a name is a guess and an
        ownerReference is a fact.
        """
        return AlertFacts(
            alertname=self.alertname,
            fingerprint=self.fingerprint,
            namespace=self.namespace,
            pod=self.pod,
            node=self.node,
            workload=workload_from_pod(self.pod),
            starts_at=self.startsAt or datetime.now(UTC),
        )


class AlertmanagerV2Payload(BaseModel):
    """The webhook body. Field names match the wire format exactly."""

    model_config = ConfigDict(extra="allow")

    version: str = "4"
    groupKey: str = ""  # noqa: N815
    truncatedAlerts: int = 0  # noqa: N815
    status: str = "firing"
    receiver: str = ""
    groupLabels: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    commonLabels: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    commonAnnotations: dict[str, str] = Field(default_factory=dict)  # noqa: N815
    externalURL: str = ""  # noqa: N815
    alerts: list[AlertmanagerAlert] = Field(default_factory=list)

    @property
    def firing(self) -> list[AlertmanagerAlert]:
        return [a for a in self.alerts if a.is_firing]

    @property
    def resolved(self) -> list[AlertmanagerAlert]:
        return [a for a in self.alerts if not a.is_firing]


def fingerprint_of(alert: AlertmanagerAlert) -> str:
    """Alertmanager always supplies a fingerprint; fall back to a label digest if it ever doesn't.

    A missing fingerprint would otherwise make every delivery look unique and defeat dedup.
    """
    if alert.fingerprint:
        return alert.fingerprint
    import hashlib

    canonical = ",".join(f"{k}={v}" for k, v in sorted(alert.labels.items()))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]
