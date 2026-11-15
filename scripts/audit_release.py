"""Audit the published 1-minute release against the repository metadata.

This is an independent guard against a release asset drifting away from the
committed ``data/releases`` mirror.  It fetches the public GitHub release,
checks the part sizes and GitHub's SHA-256 asset digests, downloads the small
metadata files, and validates the per-file MANIFEST.

The large parts are not downloaded by default.  GitHub's asset ``digest`` is a
SHA-256 of the uploaded bytes, so the four part hashes can be audited without
spending 5.4 GB of runner bandwidth.  Use ``--download-parts`` for a full local
content audit when that bandwidth is acceptable.

Example::

    python -m scripts.audit_release
    python -m scripts.audit_release --download-parts
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAG = "market-data-1m-2026-11-15"
DEFAULT_REPO = "CordiallyMel/SkyQ_RANGE"
ARCHIVE_PREFIX = "skyq-range-1m-archive/"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class AuditError(RuntimeError):
    """Raised when the release and repository metadata disagree."""


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            raise AuditError(f"malformed checksum line: {raw_line!r}")
        digest, name = fields
        name = name.lstrip("*")
        if not SHA256_RE.fullmatch(digest.lower()):
            raise AuditError(f"invalid checksum for {name}: {digest!r}")
        if name in sums:
            raise AuditError(f"duplicate checksum entry: {name}")
        sums[name] = digest.lower()
    return sums


def _headers(token: str | None, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "skyq-range-release-audit",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _github_json(url: str, token: str | None, timeout: int) -> dict[str, Any]:
    request = Request(url, headers=_headers(token))
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise AuditError(f"GitHub API returned HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise AuditError(f"could not reach GitHub API: {exc.reason}") from exc
    if not isinstance(payload, dict):
        raise AuditError(f"unexpected GitHub API payload for {url}")
    return payload


def _download(
    url: str,
    destination: Path,
    token: str | None,
    timeout: int,
    retries: int = 3,
) -> None:
    """Download one public release asset, retrying transient failures."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        temporary = destination.with_name(destination.name + ".audit-download")
        temporary.unlink(missing_ok=True)
        try:
            request = Request(
                url,
                headers=_headers(token, accept="application/octet-stream"),
            )
            with urlopen(request, timeout=timeout) as response, temporary.open("wb") as handle:
                while chunk := response.read(8 * 1024 * 1024):
                    handle.write(chunk)
            temporary.replace(destination)
            return
        except (HTTPError, URLError, OSError) as exc:
            temporary.unlink(missing_ok=True)
            last_error = exc
            if attempt < retries:
                time.sleep(attempt * 2)
    raise AuditError(f"download failed for {url}: {last_error}")


def _asset_sha(asset: dict[str, Any], name: str) -> str:
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise AuditError(f"GitHub did not report a SHA-256 digest for {name}")
    value = digest.split(":", 1)[1].lower()
    if not SHA256_RE.fullmatch(value):
        raise AuditError(f"invalid GitHub digest for {name}: {digest!r}")
    return value


def _load_local(metadata_dir: Path, tag: str) -> tuple[dict[str, Any], dict[str, str]]:
    metadata_path = metadata_dir / f"{tag}.json"
    checksum_path = metadata_dir / f"{tag}.sha256"
    if not metadata_path.is_file() or not checksum_path.is_file():
        raise AuditError(
            f"repository release metadata is missing: {metadata_path}, {checksum_path}"
        )
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"cannot read {metadata_path}: {exc}") from exc
    sums = _parse_sums(checksum_path.read_text(encoding="utf-8"))
    _validate_metadata(metadata, sums, tag)
    return metadata, sums


def _validate_metadata(metadata: dict[str, Any], sums: dict[str, str], tag: str) -> None:
    if metadata.get("schema") != 1:
        raise AuditError("unsupported release metadata schema")
    if metadata.get("tag") != tag:
        raise AuditError("release metadata tag does not match the requested tag")
    archive = metadata.get("archive")
    if archive != f"{tag}.tar":
        raise AuditError("release metadata archive name is not the expected tag tar")
    if sums.get(str(archive)) != metadata.get("archive_sha256"):
        raise AuditError("archive SHA-256 disagrees between release.json and SHA256SUMS")

    parts = metadata.get("parts")
    if not isinstance(parts, list) or not parts:
        raise AuditError("release metadata has no parts")
    seen: set[str] = set()
    total_part_bytes = 0
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise AuditError(f"release metadata part {index} is not an object")
        name = part.get("name")
        size = part.get("bytes")
        digest = part.get("sha256")
        expected_name = f"{tag}.tar.part-{index:03d}"
        if name != expected_name:
            raise AuditError(f"part {index} name is {name!r}, expected {expected_name!r}")
        if name in seen:
            raise AuditError(f"duplicate part: {name}")
        seen.add(name)
        if not isinstance(size, int) or size <= 0:
            raise AuditError(f"part {name} has an invalid size")
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest.lower()):
            raise AuditError(f"part {name} has an invalid SHA-256")
        if sums.get(name) != digest.lower():
            raise AuditError(f"part {name} SHA-256 disagrees with SHA256SUMS")
        total_part_bytes += size

    if total_part_bytes != metadata.get("archive_bytes"):
        raise AuditError("part sizes do not add up to archive_bytes")
    if not isinstance(metadata.get("files"), int) or metadata["files"] <= 0:
        raise AuditError("release metadata has an invalid file count")
    if not isinstance(metadata.get("uncompressed_bytes"), int) or metadata["uncompressed_bytes"] <= 0:
        raise AuditError("release metadata has an invalid uncompressed size")
    if metadata.get("checksums") != "SHA256SUMS":
        raise AuditError("release metadata does not name SHA256SUMS")
    for key in ("manifest", "restore"):
        value = metadata.get(key)
        if not isinstance(value, str) or value not in sums:
            raise AuditError(f"release metadata {key} is missing from SHA256SUMS")
    if not isinstance(metadata.get("roots"), list) or not metadata["roots"]:
        raise AuditError("release metadata has no archive roots")


def _validate_manifest(path: Path, metadata: dict[str, Any]) -> None:
    roots = metadata.get("roots")
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        raise AuditError("release metadata roots are invalid")
    allowed_prefixes = tuple(f"{ARCHIVE_PREFIX}{root}/" for root in roots)
    seen: set[str] = set()
    rows = 0
    total_bytes = 0
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise AuditError("MANIFEST.csv is empty") from exc
        if header != ["archive_path", "source_label", "size_bytes", "sha256"]:
            raise AuditError(f"unexpected MANIFEST.csv header: {header!r}")
        for line_number, row in enumerate(reader, start=2):
            if len(row) != 4:
                raise AuditError(f"MANIFEST.csv row {line_number} has {len(row)} fields")
            archive_path, source_label, size_text, digest = row
            if archive_path in seen:
                raise AuditError(f"duplicate MANIFEST path: {archive_path}")
            seen.add(archive_path)
            if not archive_path.startswith(allowed_prefixes) or ".." in Path(archive_path).parts:
                raise AuditError(f"unsafe or unexpected MANIFEST path: {archive_path}")
            if source_label not in roots:
                raise AuditError(f"unexpected MANIFEST source label: {source_label!r}")
            try:
                size = int(size_text)
            except ValueError as exc:
                raise AuditError(f"invalid MANIFEST size at row {line_number}") from exc
            if size < 0:
                raise AuditError(f"negative MANIFEST size at row {line_number}")
            if not SHA256_RE.fullmatch(digest.lower()):
                raise AuditError(f"invalid MANIFEST SHA-256 at row {line_number}")
            rows += 1
            total_bytes += size

    if rows != metadata.get("files"):
        raise AuditError(f"MANIFEST has {rows:,} rows; release metadata says {metadata.get('files'):,}")
    if total_bytes != metadata.get("uncompressed_bytes"):
        raise AuditError("MANIFEST sizes do not add up to uncompressed_bytes")


def _download_and_check(
    asset: dict[str, Any],
    name: str,
    expected_sha: str | None,
    destination_dir: Path,
    token: str | None,
    timeout: int,
) -> Path:
    destination = destination_dir / name
    _download(str(asset["browser_download_url"]), destination, token, timeout)
    actual = _sha256(destination)
    expected_digest = _asset_sha(asset, name)
    if actual != expected_digest:
        raise AuditError(f"{name}: GitHub asset digest does not match downloaded bytes")
    if expected_sha is not None and actual != expected_sha:
        raise AuditError(f"{name}: SHA-256 disagrees with SHA256SUMS")
    return destination


def _audit(
    tag: str,
    repo: str,
    metadata_dir: Path,
    token: str | None,
    timeout: int,
    download_parts: bool,
) -> None:
    metadata, local_sums = _load_local(metadata_dir, tag)
    release = _github_json(
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
        token,
        timeout,
    )
    if release.get("tag_name") != tag:
        raise AuditError(f"GitHub returned tag {release.get('tag_name')!r}, expected {tag!r}")
    if release.get("draft"):
        raise AuditError("the release is still a draft")
    if not release.get("published_at"):
        raise AuditError("the release is not published")

    assets = {str(asset.get("name")): asset for asset in release.get("assets", [])}
    part_names = [part["name"] for part in metadata["parts"]]
    required = set(part_names) | {"SHA256SUMS", "MANIFEST.csv", "RESTORE.md", "release.json"}
    optional = {"catalog.json"}
    missing = sorted(required - set(assets))
    if missing:
        raise AuditError(f"release is missing assets: {', '.join(missing)}")
    unexpected = sorted(set(assets) - required - optional)
    if unexpected:
        raise AuditError(f"release has unexpected assets: {', '.join(unexpected)}")

    for part in metadata["parts"]:
        name = part["name"]
        asset = assets[name]
        if asset.get("state") != "uploaded":
            raise AuditError(f"{name}: GitHub asset state is {asset.get('state')!r}")
        if asset.get("size") != part["bytes"]:
            raise AuditError(f"{name}: asset size does not match release metadata")
        if _asset_sha(asset, name) != part["sha256"]:
            raise AuditError(f"{name}: GitHub digest does not match release metadata")

    with tempfile.TemporaryDirectory(prefix="skyq-release-audit-") as temporary:
        tmp = Path(temporary)
        # Download SHA256SUMS first so every later file can be checked against it.
        sums_asset = assets["SHA256SUMS"]
        _download_and_check(
            sums_asset,
            "SHA256SUMS",
            None,
            tmp,
            token,
            timeout,
        )
        checksum_text = (tmp / "SHA256SUMS").read_text(encoding="utf-8")
        remote_sums = _parse_sums(checksum_text)
        if remote_sums != local_sums:
            raise AuditError("downloaded SHA256SUMS disagrees with data/releases mirror")

        for name in ("MANIFEST.csv", "RESTORE.md", "release.json"):
            _download_and_check(
                assets[name],
                name,
                remote_sums.get(name),
                tmp,
                token,
                timeout,
            )

        if "catalog.json" in assets:
            _download_and_check(assets["catalog.json"], "catalog.json", None, tmp, token, timeout)
            try:
                catalog = json.loads((tmp / "catalog.json").read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise AuditError("catalog.json is not valid JSON") from exc
            if catalog.get("release", {}).get("tag") != tag:
                raise AuditError("catalog.json does not point at the audited release tag")

        try:
            remote_metadata = json.loads((tmp / "release.json").read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AuditError("release.json is not valid JSON") from exc
        for key in (
            "schema",
            "tag",
            "archive",
            "archive_bytes",
            "archive_sha256",
            "split_mib",
            "files",
            "uncompressed_bytes",
            "parts",
            "checksums",
            "manifest",
            "restore",
            "roots",
        ):
            if remote_metadata.get(key) != metadata.get(key):
                raise AuditError(f"release.json field {key!r} disagrees with the repository mirror")

        _validate_manifest(tmp / "MANIFEST.csv", metadata)
        restore_text = (tmp / "RESTORE.md").read_text(encoding="utf-8")
        if tag not in restore_text or f"{tag}.tar.part-*" not in restore_text:
            raise AuditError("RESTORE.md does not describe the audited tag")

        if download_parts:
            for part in metadata["parts"]:
                name = part["name"]
                part_path = tmp / name
                print(f"downloading and hashing {name} ...", flush=True)
                _download_and_check(
                    assets[name],
                    name,
                    part["sha256"],
                    tmp,
                    token,
                    timeout,
                )
                if _sha256(part_path) != part["sha256"]:
                    raise AuditError(f"{name}: content hash mismatch")

    print(
        f"release audit passed: {tag} | {len(part_names)} parts | "
        f"{metadata['files']:,} manifest rows | {metadata['archive_bytes'] / 1024**3:.2f} GiB"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=ROOT / "data" / "releases",
        help="directory containing <tag>.json and <tag>.sha256",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="network timeout in seconds (default: 120)",
    )
    parser.add_argument(
        "--download-parts",
        action="store_true",
        help="also download and hash all parts (full 5.4 GiB audit)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN") or None
    try:
        _audit(
            tag=args.tag,
            repo=args.repo,
            metadata_dir=args.metadata_dir,
            token=token,
            timeout=args.timeout,
            download_parts=args.download_parts,
        )
    except (AuditError, OSError) as exc:
        print(f"release audit failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
