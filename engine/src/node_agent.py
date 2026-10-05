"""The node agent: disk cleanup's hands on the node it is cleaning.

Disk pressure is a property of one node's filesystem, and the engine runs as a Deployment on some
other node -- so archive-then-delete cannot happen in the engine process. It happens here, in a
short-lived Job pinned to the target node (`nodeName`), with the configured log roots mounted from
the host and nothing else. The Job tolerates the disk-pressure taint, because the node that needs
cleaning is exactly the node the scheduler is keeping new pods off.

Three operations, each a pure function of its task plus the filesystem:

    scan     list archive candidates under the roots, older than the floor -- run BEFORE planning,
             so `plan()` stays a pure function of the bundle and the dry-run listing is exact
    archive  for each planned file: upload, verify the size, only then delete (never delete-only)
    restore  pull archived files back; a file already present is skipped (idempotent)

Every path is checked against the roots before it is touched. A plan cannot name a file outside
them, however the plan was produced.

The same functions run in-process (`node_agent_mode=local`) for development and the unit suite, and
inside the Job in a cluster. The Job writes one `HEALOPS_AGENT_RESULT` line, which the engine reads
back from the pod log.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Final

import structlog

log = structlog.get_logger(__name__)

RESULT_MARKER: Final[str] = "HEALOPS_AGENT_RESULT "
TASK_ENV: Final[str] = "HEALOPS_AGENT_TASK"


class OutsideRoots(ValueError):
    """A path the plan named is not under any configured log root."""


def host_path(path: str, prefix: str) -> Path:
    """Where a host path is visible to this process: under the mount prefix inside the Job."""
    return Path(prefix + path) if prefix else Path(path)


def check(path: str, roots: list[str]) -> str:
    """Refuse anything outside the roots, including `..` escapes and symlinked directories."""
    normalised = os.path.normpath(path)
    for root in roots:
        base = os.path.normpath(root)
        try:
            if os.path.commonpath([normalised, base]) == base:
                return normalised
        except ValueError:  # different drives on Windows: certainly not under this root
            continue
    raise OutsideRoots(f"{path} is outside the archive roots {roots}")


def scan(
    roots: list[str], floor_days: float, *, prefix: str = "", now: float | None = None
) -> list[dict[str, Any]]:
    """Regular files under the roots older than the floor. Symlinks are never followed."""
    now = time.time() if now is None else now
    found: list[dict[str, Any]] = []
    for root in roots:
        base = host_path(root, prefix)
        if not base.is_dir():
            continue
        for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
            for filename in filenames:
                local = Path(dirpath) / filename
                if local.is_symlink() or not local.is_file():
                    continue
                stat = local.stat()
                age_days = (now - stat.st_mtime) / 86400
                if age_days < floor_days:
                    continue
                visible = str(local)
                path = visible[len(prefix) :] if prefix and visible.startswith(prefix) else visible
                found.append(
                    {"path": path, "size_bytes": stat.st_size, "age_days": round(age_days, 2)}
                )
    return sorted(found, key=lambda c: c["path"])


async def archive(
    node: str, files: list[dict[str, Any]], roots: list[str], *, prefix: str = ""
) -> dict[str, Any]:
    """Archive each file to S3, verify it, then delete it. Stops at the first archive failure."""
    from integrations import s3_client
    from runbooks.disk_cleanup import _human, _key_for

    if not await s3_client.ensure_bucket():
        # Refuse at execution time too: deleting because the archive is down would be data loss.
        return {
            "ok": False,
            "detail": (
                "object storage is unreachable, so nothing was deleted -- refusing to trade a "
                "disk-pressure incident for data loss"
            ),
            "archived": [],
            "freed_bytes": 0,
            "attempted": False,
        }

    archived: list[dict[str, Any]] = []
    freed = 0
    for entry in files:
        try:
            path = check(str(entry["path"]), roots)
        except OutsideRoots as exc:
            return {"ok": False, "detail": str(exc), "archived": archived, "freed_bytes": freed}
        local = host_path(path, prefix)
        key = _key_for(node, path)
        try:
            obj = await s3_client.archive_file(local, key)
        except s3_client.ArchiveError as exc:
            # Everything already archived stays archived and its original is already gone, so the
            # result reports partial progress rather than pretending the whole action failed.
            return {
                "ok": False,
                "detail": (
                    f"archived {len(archived)} of {len(files)} files before failing on {path}: "
                    f"{exc}. No further deletions were attempted."
                ),
                "archived": archived,
                "freed_bytes": freed,
            }

        # Only now is deletion safe: the object exists in S3 at the right size.
        try:
            await asyncio.to_thread(local.unlink)
        except OSError as exc:
            log.warning("archived but could not delete", path=path, error=str(exc))
            continue
        archived.append({"path": path, "key": obj.key, "size_bytes": obj.size_bytes})
        freed += obj.size_bytes

    return {
        "ok": True,
        "detail": f"archived and removed {len(archived)} files, freeing {_human(freed)}",
        "archived": archived,
        "freed_bytes": freed,
    }


async def restore(
    entries: list[dict[str, Any]], roots: list[str], *, prefix: str = ""
) -> dict[str, Any]:
    """Pull archived files back. Idempotent: a file already on disk is left alone."""
    from integrations import s3_client

    restored: list[str] = []
    missing: list[str] = []
    for entry in entries:
        try:
            path = check(str(entry["path"]), roots)
        except OutsideRoots:
            missing.append(str(entry.get("key")))
            continue
        local = host_path(path, prefix)
        if await asyncio.to_thread(local.exists):
            continue
        key = str(entry["key"])
        if not await s3_client.object_exists(key) or not await s3_client.restore_file(key, local):
            missing.append(key)
            continue
        restored.append(path)
    return {
        "ok": not missing,
        "detail": f"restored {len(restored)} files from s3"
        + (f"; {len(missing)} could not be retrieved" if missing else ""),
        "restored": restored,
        "missing": missing,
    }


async def perform(task: dict[str, Any], *, prefix: str = "") -> dict[str, Any]:
    op = task["op"]
    roots = list(task["roots"])
    if op == "scan":
        return {"ok": True, "candidates": scan(roots, float(task["floor_days"]), prefix=prefix)}
    if op == "archive":
        return await archive(task["node"], list(task["files"]), roots, prefix=prefix)
    if op == "restore":
        return await restore(list(task["entries"]), roots, prefix=prefix)
    return {"ok": False, "detail": f"unknown agent operation {op!r}"}


def encode(task: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(task, sort_keys=True).encode("utf-8")).decode("ascii")


def decode_result(logs: str) -> dict[str, Any] | None:
    """The agent's result is its last marker line; anything else in the log is diagnostics."""
    for line in reversed(logs.splitlines()):
        if line.startswith(RESULT_MARKER):
            parsed: dict[str, Any] = json.loads(line[len(RESULT_MARKER) :])
            return parsed
    return None


def main() -> int:
    """Job entrypoint: the task arrives base64-encoded in the environment; the result, on stdout."""
    raw = os.environ.get(TASK_ENV)
    if not raw:
        print(f"{TASK_ENV} is not set", file=sys.stderr)
        return 2
    task = json.loads(base64.b64decode(raw))
    result = asyncio.run(perform(task, prefix=os.environ.get("HEALOPS_AGENT_HOST_PREFIX", "/host")))
    print(RESULT_MARKER + json.dumps(result, sort_keys=True), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
