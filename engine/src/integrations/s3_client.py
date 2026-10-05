"""S3 client for log archival.

One rule governs everything here: **archive-then-delete, never delete-only.** The disk-cleanup
runbook must be able to prove the data still exists somewhere before it removes it, so
`archive_file` verifies the upload landed by reading back the object's size rather than trusting the
absence of an exception.

If the S3 write fails, the runbook **refuses**. Deleting logs because the archive destination is
unreachable would turn a disk-pressure incident into data loss -- the one outcome worse than a full
disk.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import structlog
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from config import get_settings

log = structlog.get_logger(__name__)


class ArchiveError(RuntimeError):
    """The archive did not land. Callers refuse rather than proceeding to delete."""


@dataclass(frozen=True, slots=True)
class ArchivedObject:
    key: str
    size_bytes: int
    source_path: str


def _client() -> Any:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        config=Config(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=5,
            read_timeout=30,
        ),
    )


async def ensure_bucket() -> bool:
    """Create the bucket when absent. Returns False when object storage is unusable."""
    settings = get_settings()

    def _ensure() -> bool:
        client = _client()
        try:
            client.head_bucket(Bucket=settings.s3_bucket)
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code not in ("404", "NoSuchBucket", "NotFound"):
                return False
            try:
                client.create_bucket(Bucket=settings.s3_bucket)
                return True
            except ClientError, BotoCoreError:
                return False
        except BotoCoreError:
            return False

    try:
        return await asyncio.to_thread(_ensure)
    except Exception:  # noqa: BLE001 - reachability check must report, never raise
        return False


async def ping() -> bool:
    return await ensure_bucket()


async def archive_file(local_path: str | Path, key: str) -> ArchivedObject:
    """Upload one file and **verify it landed**.

    The verification read is not paranoia: a silent partial upload followed by a local delete is
    exactly the failure this runbook must never produce, and `upload_file` returning without raising
    is not proof that the object is retrievable at full size.
    """
    settings = get_settings()
    path = Path(local_path)

    # The stat and the upload both block, so they run together in one worker thread rather than
    # stalling the event loop between them.
    def _upload() -> tuple[int, int]:
        if not path.is_file():
            raise FileNotFoundError(path)
        stat = path.stat()
        local = stat.st_size
        client = _client()
        client.upload_file(
            str(path), settings.s3_bucket, key, ExtraArgs={"Metadata": file_metadata(stat)}
        )
        head = client.head_object(Bucket=settings.s3_bucket, Key=key)
        return local, int(head["ContentLength"])

    try:
        local_size, remote_size = await asyncio.to_thread(_upload)
    except FileNotFoundError as exc:
        raise ArchiveError(f"{path} is not a file") from exc
    except (ClientError, BotoCoreError, OSError) as exc:
        raise ArchiveError(f"failed to archive {path} to s3://{settings.s3_bucket}/{key}") from exc

    if remote_size != local_size:
        raise ArchiveError(
            f"archive size mismatch for {key}: local {local_size} bytes, "
            f"remote {remote_size} bytes -- refusing to treat this as archived"
        )

    log.info("archived", key=key, bytes=remote_size)
    return ArchivedObject(key=key, size_bytes=remote_size, source_path=str(path))


async def object_exists(key: str) -> bool:
    """Used by compensation to confirm an archived object is still retrievable."""
    settings = get_settings()

    def _head() -> bool:
        try:
            _client().head_object(Bucket=settings.s3_bucket, Key=key)
            return True
        except ClientError, BotoCoreError:
            return False

    return await asyncio.to_thread(_head)


async def restore_file(key: str, local_path: str | Path) -> bool:
    """Pull an archived object back to local disk.

    This is the disk runbook's compensation. It is honest about what it can and cannot undo: the
    bytes come back, but the deletion itself already happened, which is why that runbook is
    classified `partially_reversible` rather than `reversible`.
    """
    settings = get_settings()
    path = Path(local_path)

    def _download() -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            client = _client()
            client.download_file(settings.s3_bucket, key, str(path))
            metadata = client.head_object(Bucket=settings.s3_bucket, Key=key).get("Metadata", {})
        except ClientError, BotoCoreError, OSError:
            return False
        apply_metadata(path, metadata)
        return True

    return await asyncio.to_thread(_download)


# The file's identity beyond its bytes, carried as object metadata. A log restored as a fresh,
# root-owned file is not the file that was archived: its writer can no longer append to it, and the
# 7-day floor would see it as new. Compensation restores all of it, or it is not compensation.
_META_MTIME, _META_MODE, _META_UID, _META_GID = (
    "healops-mtime",
    "healops-mode",
    "healops-uid",
    "healops-gid",
)


def file_metadata(stat: os.stat_result) -> dict[str, str]:
    return {
        _META_MTIME: repr(stat.st_mtime),
        _META_MODE: oct(stat.st_mode & 0o7777),
        _META_UID: str(stat.st_uid),
        _META_GID: str(stat.st_gid),
    }


def apply_metadata(path: Path, metadata: dict[str, str]) -> None:
    """Put mode, owner and mtime back. mtime last: chown and chmod would otherwise not touch it,
    but a later write would, and the order makes the intent plain."""
    if _META_MODE in metadata:
        os.chmod(path, int(metadata[_META_MODE], 8))
    if _META_UID in metadata and hasattr(os, "chown"):
        try:
            os.chown(path, int(metadata[_META_UID]), int(metadata[_META_GID]))
        except PermissionError:
            log.warning("restored without its owner", path=str(path))
    if _META_MTIME in metadata:
        mtime = float(metadata[_META_MTIME])
        os.utime(path, (mtime, mtime))
