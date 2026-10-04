"""GitHub issues, for the anti-masking guard.

Credentials are a **fine-grained PAT scoped to one repository** -- issues now, contents and pull
requests once durable fixes land. Least privilege plus a documented rotation is the mitigation for
the token leaking.

`create_issue` is best-effort like every other notifier: `None` on any failure, never an exception.
The masking guard has already demoted the runbook by the time it runs, so a GitHub outage loses the
issue, not the guard. The pull-request primitives raise `GitHubError` instead, because opening a PR
is several dependent steps and `durable.py` needs to know which one failed.
"""

from __future__ import annotations

from typing import Any

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


# --- pull requests, for durable fixes -------------------------------------------------------------


class GitHubError(RuntimeError):
    """A step of opening a pull request failed; the message says which."""


async def _request(method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
    settings = get_settings()
    if settings.github_token is None or settings.github_repo is None:
        raise GitHubError("github is not configured")
    async with httpx.AsyncClient(timeout=_TIMEOUT) as http:
        response = await http.request(
            method,
            f"{API}/repos/{settings.github_repo}{path}",
            headers={
                "Authorization": f"Bearer {settings.github_token.get_secret_value()}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=json,
            params=params,
        )
    if response.status_code >= 400:
        raise GitHubError(f"{method} {path} -> {response.status_code}: {response.text[:200]}")
    return response.json() if response.content else None


async def get_file(path: str, ref: str) -> tuple[str, str]:
    """The file's text and blob sha at `ref`."""
    import base64

    body = await _request("GET", f"/contents/{path}", params={"ref": ref})
    return base64.b64decode(body["content"]).decode("utf-8"), str(body["sha"])


async def create_branch(branch: str, base: str) -> None:
    head = await _request("GET", f"/git/ref/heads/{base}")
    await _request(
        "POST", "/git/refs", json={"ref": f"refs/heads/{branch}", "sha": head["object"]["sha"]}
    )


async def put_file(path: str, *, branch: str, text: str, sha: str, message: str) -> None:
    import base64

    await _request(
        "PUT",
        f"/contents/{path}",
        json={
            "message": message,
            "content": base64.b64encode(text.encode("utf-8")).decode("ascii"),
            "sha": sha,
            "branch": branch,
        },
    )


async def create_pull(*, title: str, head: str, base: str, body: str, labels: list[str]) -> str:
    pull = await _request(
        "POST", "/pulls", json={"title": title, "head": head, "base": base, "body": body}
    )
    if labels:
        await _request("POST", f"/issues/{pull['number']}/labels", json={"labels": labels})
    return str(pull["html_url"])
