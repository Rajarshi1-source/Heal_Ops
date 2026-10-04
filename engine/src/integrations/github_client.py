"""GitHub issues, for the anti-masking guard.

Credentials are a **fine-grained PAT scoped to one repository** -- issues now, contents and pull
requests once durable fixes land. Least privilege plus a documented rotation is the mitigation for
the token leaking.

Best-effort like every other notifier: `None` on any failure, never an exception. The masking guard
has already demoted the runbook and written the ledger entry by the time this runs; a GitHub outage
loses the convenience of the issue, not the guard.
"""

from __future__ import annotations

import httpx
import structlog

from config import get_settings

log = structlog.get_logger(__name__)

API = "https://api.github.com"
_TIMEOUT = httpx.Timeout(10.0, connect=3.0)


async def create_issue(title: str, body: str, labels: list[str]) -> str | None:
    """Open an issue and return its URL, or `None` when GitHub is unconfigured or unreachable."""
    settings = get_settings()
    if settings.github_token is None or settings.github_repo is None:
        log.info("github not configured; masking report not filed as an issue")
        return None

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
            response = await http.post(
                f"{API}/repos/{settings.github_repo}/issues",
                headers={
                    "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json={"title": title, "body": body, "labels": labels},
            )
            response.raise_for_status()
            url = response.json().get("html_url")
    except Exception as exc:  # noqa: BLE001
        log.warning("could not open github issue", error=str(exc))
        return None
    return str(url) if url else None
