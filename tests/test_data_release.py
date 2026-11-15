"""Offline checks for the split data-release metadata and manifest rules."""

from __future__ import annotations

import csv
import hashlib
import http.server
import json
import tempfile
import threading
from pathlib import Path

import pytest

from scripts.audit_release import AuditError, _load_local, _validate_manifest
from scripts.restore_market_data import (
    DownloadError,
    _download_asset,
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
