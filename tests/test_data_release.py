"""Offline checks for the split data-release metadata and manifest rules."""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import pytest

from scripts.audit_release import AuditError, _load_local, _validate_manifest
from scripts.restore_market_data import DownloadError, _require_space


def test_committed_release_metadata_and_checksums_agree() -> None:
    metadata, sums = _load_local(Path("data/releases"), "market-data-1m-2026-11-15")
    assert metadata["files"] == 12_269
    assert len(metadata["parts"]) == 4
    assert sums[f"{metadata['tag']}.tar"] == metadata["archive_sha256"]
    assert sums["MANIFEST.csv"] == "3476aba28b73997614ec745317121032dc75dbb9f808ff7433962050e567b2d9"


def _manifest(rows: list[list[str]]) -> Path:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".csv", delete=False, encoding="utf-8", newline=""
    ) as handle:
        writer = csv.writer(handle)
        writer.writerow(["archive_path", "source_label", "size_bytes", "sha256"])
        writer.writerows(rows)
        name = handle.name
    return Path(name)


def test_manifest_accepts_a_valid_row() -> None:
    path = _manifest(
        [
            [
                "skyq-range-1m-archive/Data_For_Backtests/SPY.csv",
                "Data_For_Backtests",
                "3",
                "a" * 64,
            ]
        ]
    )
    try:
        _validate_manifest(
            path,
            {
                "roots": ["Data_For_Backtests"],
                "files": 1,
                "uncompressed_bytes": 3,
            },
        )
    finally:
        path.unlink(missing_ok=True)


def test_manifest_rejects_unsafe_paths() -> None:
    path = _manifest(
        [
            [
                "skyq-range-1m-archive/../escape.csv",
                "Data_For_Backtests",
                "3",
                "a" * 64,
            ]
        ]
    )
    try:
        with pytest.raises(AuditError, match="unsafe or unexpected"):
            _validate_manifest(
                path,
                {
                    "roots": ["Data_For_Backtests"],
                    "files": 1,
                    "uncompressed_bytes": 3,
                },
            )
    finally:
        path.unlink(missing_ok=True)


def test_manifest_rejects_count_mismatch() -> None:
    path = _manifest(
        [
            [
                "skyq-range-1m-archive/Data_For_Backtests/SPY.csv",
                "Data_For_Backtests",
                "3",
                "a" * 64,
            ]
        ]
    )
    try:
        with pytest.raises(AuditError, match="rows"):
            _validate_manifest(
                path,
                {
                    "roots": ["Data_For_Backtests"],
                    "files": 2,
                    "uncompressed_bytes": 3,
                },
            )
    finally:
        path.unlink(missing_ok=True)


def test_disk_guard_refuses_impossible_space(tmp_path: Path) -> None:
    with pytest.raises(DownloadError, match="not enough free space"):
        _require_space("test download", tmp_path, 10**18)
