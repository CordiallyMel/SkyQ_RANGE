"""Offline checks for the split data-release metadata and manifest rules."""

from __future__ import annotations

import csv
import hashlib
import http.server
import json
import tarfile
import tempfile
import threading
from pathlib import Path

import pytest

from scripts import restore_market_data as restore
from scripts.audit_release import AuditError, _load_local, _validate_manifest
from scripts.restore_market_data import (
    DownloadError,
    _completed_extraction_bytes,
    _download_asset,
    _extract_archive,
    _extract_state_path,
    _partial_paths,
    _partial_remaining,
    _require_space,
)


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


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    data = b"0123456789" * 1_000
    honor_range = True
    truncate_first = False
    requests = 0

    def do_GET(self) -> None:
        type(self).requests += 1
        if type(self).truncate_first and type(self).requests == 1:
            self.send_response(200)
            self.send_header("Content-Length", str(len(type(self).data)))
            self.send_header("ETag", '"test-etag"')
            self.end_headers()
            self.wfile.write(type(self).data[:4096])
            self.wfile.flush()
            self.close_connection = True
            return
        range_header = self.headers.get("Range", "")
        start = 0
        if range_header and self.honor_range:
            start = int(range_header.split("=", 1)[1].split("-", 1)[0])
            body = self.data[start:]
            self.send_response(206)
            self.send_header(
                "Content-Range", f"bytes {start}-{len(self.data) - 1}/{len(self.data)}"
            )
        else:
            body = self.data
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"test-etag"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


def _serve(handler: type[_RangeHandler]) -> tuple[http.server.ThreadingHTTPServer, str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}/asset"


def _partial_server_asset(tmp_path: Path, url: str) -> tuple[Path, dict[str, object], str]:
    data = _RangeHandler.data
    digest = hashlib.sha256(data).hexdigest()
    destination = tmp_path / "asset.bin"
    partial, meta = _partial_paths(destination)
    partial.write_bytes(data[:4096])
    meta.write_text(
        json.dumps(
            {
                "url": url,
                "expected_sha256": digest,
                "size": len(data),
                "etag": '"test-etag"',
            }
        ),
        encoding="utf-8",
    )
    return destination, {
        "name": destination.name,
        "browser_download_url": url,
        "size": len(data),
    }, digest


def test_partial_remaining_counts_only_missing_bytes(tmp_path: Path) -> None:
    destination = tmp_path / "asset.bin"
    partial, meta = _partial_paths(destination)
    data = b"x" * 1000
    digest = hashlib.sha256(data).hexdigest()
    partial.write_bytes(data[:400])
    meta.write_text(
        json.dumps(
            {
                "url": "https://example.invalid/asset",
                "expected_sha256": digest,
                "size": len(data),
            }
        ),
        encoding="utf-8",
    )
    assert _partial_remaining(destination, "https://example.invalid/asset", digest, 1000) == 600
    assert _partial_remaining(destination, "https://example.invalid/other", digest, 1000) == 1000


def test_download_resumes_a_partial_file(tmp_path: Path) -> None:
    _RangeHandler.requests = 0
    _RangeHandler.honor_range = True
    server, url = _serve(_RangeHandler)
    try:
        destination, asset, digest = _partial_server_asset(tmp_path, url)
        _download_asset(asset, destination, digest)
        assert destination.read_bytes() == _RangeHandler.data
        assert _RangeHandler.requests == 1
        partial, meta = _partial_paths(destination)
        assert not partial.exists()
        assert not meta.exists()
    finally:
        server.shutdown()
        server.server_close()


def test_download_restarts_when_server_ignores_range(tmp_path: Path) -> None:
    _RangeHandler.requests = 0
    _RangeHandler.honor_range = False
    _RangeHandler.truncate_first = False
    server, url = _serve(_RangeHandler)
    try:
        destination, asset, digest = _partial_server_asset(tmp_path, url)
        _download_asset(asset, destination, digest)
        assert destination.read_bytes() == _RangeHandler.data
        assert _RangeHandler.requests == 2
    finally:
        server.shutdown()
        server.server_close()


def _test_archive(tmp_path: Path, count: int = 10) -> tuple[Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    archive_path = tmp_path / "market-data.tar"
    with tarfile.open(archive_path, "w") as archive:
        for index in range(count):
            member = source / f"file-{index:02d}.txt"
            member.write_text(f"member {index}\n", encoding="utf-8")
            archive.add(member, arcname=f"skyq-range-1m-archive/{member.name}")
    return archive_path, hashlib.sha256(archive_path.read_bytes()).hexdigest()


def test_completed_extraction_bytes_only_uses_a_matching_state(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "archive.tar.extract-state.json"
    destination = tmp_path / "restored"
    state_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "archive_sha256": "abc",
                "destination": str(destination),
                "completed_bytes": 1234,
            }
        ),
        encoding="utf-8",
    )
    assert _completed_extraction_bytes(state_path, "abc", destination) == 1234
    assert _completed_extraction_bytes(state_path, "different", destination) == 0
    assert _completed_extraction_bytes(state_path, "abc", tmp_path / "other") == 0


def test_extract_resumes_after_an_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path, digest = _test_archive(tmp_path)
    destination = tmp_path / "restored"
    state_path = _extract_state_path(archive_path)
    original_extract = restore._extract_members
    calls = 0

    def interrupting_extract(
        archive: tarfile.TarFile,
        members: list[tarfile.TarInfo],
        target: Path,
    ) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption")
        original_extract(archive, members, target)

    monkeypatch.setattr(restore, "EXTRACT_CHECKPOINT_MEMBERS", 3)
    monkeypatch.setattr(restore, "_extract_members", interrupting_extract)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        _extract_archive(archive_path, destination, digest)

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["completed"] == 3
    assert state["completed_bytes"] == sum(
        (tmp_path / "source" / f"file-{index:02d}.txt").stat().st_size
        for index in range(3)
    )
    first_path = destination / "skyq-range-1m-archive" / "file-00.txt"
    assert first_path.is_file()
    first_path.write_text("keep 000\n", encoding="utf-8")

    monkeypatch.setattr(restore, "_extract_members", original_extract)
    _extract_archive(archive_path, destination, digest)
    assert first_path.read_text(encoding="utf-8") == "keep 000\n"
    for index in range(1, 10):
        path = destination / "skyq-range-1m-archive" / f"file-{index:02d}.txt"
        assert path.read_text(encoding="utf-8") == f"member {index}\n"
    assert not state_path.exists()


def test_extracting_a_batch_twice_replaces_hard_links(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    original = source / "original.txt"
    original.write_text("linked content\n", encoding="utf-8")
    linked = source / "linked.txt"
    linked.hardlink_to(original)
    archive_path = tmp_path / "links.tar"
    with tarfile.open(archive_path, "w") as archive:
        archive.add(original, arcname="skyq-range-1m-archive/original.txt")
        archive.add(linked, arcname="skyq-range-1m-archive/linked.txt")
    destination = tmp_path / "restored"
    destination.mkdir()
    with tarfile.open(archive_path) as archive:
        members = archive.getmembers()
        restore._extract_members(archive, members, destination)
        restore._extract_members(archive, members, destination)
    assert (
        destination / "skyq-range-1m-archive" / "linked.txt"
    ).read_text(encoding="utf-8") == "linked content\n"


def test_extract_restarts_when_checkpoint_files_are_missing(tmp_path: Path) -> None:
    archive_path, digest = _test_archive(tmp_path)
    destination = tmp_path / "restored"
    destination.mkdir()
    state_path = _extract_state_path(archive_path)
    state_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "archive_sha256": digest,
                "destination": str(destination),
                "members": 10,
                "completed": 3,
            }
        ),
        encoding="utf-8",
    )
    _extract_archive(archive_path, destination, digest)
    for index in range(10):
        path = destination / "skyq-range-1m-archive" / f"file-{index:02d}.txt"
        assert path.is_file()
    assert not state_path.exists()


def test_download_resumes_after_an_interrupted_response(tmp_path: Path) -> None:
    _RangeHandler.requests = 0
    _RangeHandler.honor_range = True
    _RangeHandler.truncate_first = True
    server, url = _serve(_RangeHandler)
    try:
        destination = tmp_path / "asset.bin"
        asset = {
            "name": destination.name,
            "browser_download_url": url,
            "size": len(_RangeHandler.data),
        }
        digest = hashlib.sha256(_RangeHandler.data).hexdigest()
        _download_asset(asset, destination, digest)
        assert destination.read_bytes() == _RangeHandler.data
        assert _RangeHandler.requests >= 2
    finally:
        _RangeHandler.truncate_first = False
        server.shutdown()
        server.server_close()
