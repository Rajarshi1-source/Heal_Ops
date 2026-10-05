"""A restored file is the file that was archived: its bytes, and its mtime, mode and owner.

Regression for a gap the chaos suite exposed: restore wrote the bytes back as a fresh, root-owned
file, so the log's writer could no longer append to it and the 7-day floor saw it as new.
"""

from __future__ import annotations

import os
from pathlib import Path

from integrations import s3_client


def test_metadata_round_trips_mtime_and_mode(tmp_path: Path) -> None:
    original = tmp_path / "archived-007.log"
    original.write_bytes(b"x" * 64)
    os.chmod(original, 0o640)
    os.utime(original, (1_700_000_000.25, 1_700_000_000.25))
    metadata = s3_client.file_metadata(original.stat())

    restored = tmp_path / "restored.log"
    restored.write_bytes(b"x" * 64)
    s3_client.apply_metadata(restored, metadata)

    assert restored.stat().st_mtime == original.stat().st_mtime
    if os.name != "nt":  # Windows has no POSIX permission bits to compare
        assert restored.stat().st_mode & 0o7777 == 0o640


def test_metadata_is_plain_strings_s3_can_carry() -> None:
    stat = os.stat(__file__)
    metadata = s3_client.file_metadata(stat)

    assert all(isinstance(v, str) for v in metadata.values())
    assert float(metadata["healops-mtime"]) == stat.st_mtime


def test_an_object_without_metadata_restores_as_is(tmp_path: Path) -> None:
    # Archives written before metadata existed must still restore.
    path = tmp_path / "old.log"
    path.write_bytes(b"y")
    s3_client.apply_metadata(path, {})
    assert path.read_bytes() == b"y"
