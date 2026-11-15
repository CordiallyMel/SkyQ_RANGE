"""Verify and restore the split one-minute market-data release.

This is intentionally dependency-free.  It reads the release's ``SHA256SUMS``,
verifies every downloaded part, concatenates them, verifies the full tar,
rejects unsafe archive members, extracts the data, and verifies the restored
files against ``MANIFEST.csv``. Interrupted downloads resume from a checked
``.download`` partial when the remote asset supports HTTP range requests.
Extraction checkpoints member progress, reports member and byte progress for
large archives, and resumes after an interrupted unpack.

Examples::

    # Download the release, then verify it without extracting.
    python -m scripts.restore_market_data --download --verify-only

    # Download and restore under data/restored/skyq-range-1m-archive/.
    python -m scripts.restore_market_data \
      --download --dest data/restored

    # Verify a previously downloaded directory.
    python -m scripts.restore_market_data \
      --archive-dir release-assets --verify-only
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
import tarfile
import time
from collections.abc import Callable
from http.client import IncompleteRead
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAG = "market-data-1m-2026-11-15"
DEFAULT_REPO = "CordiallyMel/SkyQ_RANGE"
DEFAULT_ARCHIVE_DIR = "release-assets"
SMALL_ASSETS = {"SHA256SUMS", "MANIFEST.csv", "RESTORE.md", "release.json"}
SPACE_CUSHION = 512 * 1024 * 1024
EXTRACT_STATE_SCHEMA = 1
EXTRACT_CHECKPOINT_MEMBERS = 100
ARCHIVE_PREFIX = "skyq-range-1m-archive/"
MANIFEST_HEADER = ["archive_path", "source_label", "size_bytes", "sha256"]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-dir",
        default=None,
        help=(
            "directory containing *.part-*, SHA256SUMS and MANIFEST.csv "
            f"(default with --download: {DEFAULT_ARCHIVE_DIR})"
        ),
    )
    parser.add_argument(
        "--download",
        action="store_true",
        help="download the release assets from GitHub before verifying/extracting",
    )
    parser.add_argument(
        "--tag",
        default=DEFAULT_TAG,
        help=f"release tag to download (default: {DEFAULT_TAG})",
    )
    parser.add_argument(
        "--repo",
        default=DEFAULT_REPO,
        help=f"GitHub repository to download from (default: {DEFAULT_REPO})",
    )
    parser.add_argument(
        "--dest",
        default="data/restored",
        help="extraction destination (default: data/restored)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="verify the split parts but do not concatenate or extract",
    )
    parser.add_argument(
        "--remove-archive",
        action="store_true",
        help="delete the reconstructed full tar after extraction",
    )
    return parser


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _read_sums(path: Path) -> dict[str, str]:
    sums: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            raise SystemExit(f"malformed checksum line: {raw_line!r}")
        digest, name = parts
        sums[name.lstrip("*")] = digest.lower()
    return sums


class DownloadError(RuntimeError):
    """Raised when the optional release download cannot be completed safely."""


class ManifestError(RuntimeError):
    """Raised when extracted files do not match the release manifest."""


def _gib(value: int) -> float:
    return value / (1024**3)


def _headers(token: str | None, accept: str = "application/octet-stream") -> dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "skyq-range-release-restore",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _asset_sha(asset: dict[str, Any], name: str) -> str:
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        raise DownloadError(f"GitHub did not report a SHA-256 digest for {name}")
    return digest.split(":", 1)[1].lower()


def _github_release(repo: str, tag: str, timeout: int = 120) -> dict[str, Any]:
    token = os.environ.get("GITHUB_TOKEN") or None
    request = Request(
        f"https://api.github.com/repos/{repo}/releases/tags/{tag}",
        headers=_headers(token, accept="application/vnd.github+json"),
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            release = json.load(response)
    except HTTPError as exc:
        raise DownloadError(f"GitHub API returned HTTP {exc.code} for {tag}") from exc
    except (URLError, OSError) as exc:
        raise DownloadError(f"could not reach GitHub API: {exc}") from exc
    if release.get("tag_name") != tag:
        raise DownloadError(
            f"GitHub returned tag {release.get('tag_name')!r}, expected {tag!r}"
        )
    if release.get("draft") or not release.get("published_at"):
        raise DownloadError(f"release {tag} is not published")
    return release


def _partial_paths(destination: Path) -> tuple[Path, Path]:
    return (
        destination.with_name(destination.name + ".download"),
        destination.with_name(destination.name + ".download.json"),
    )


def _read_partial_meta(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_partial_meta(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value), encoding="utf-8")
    temporary.replace(path)


def _remove_partial(partial: Path, meta: Path) -> None:
    partial.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)


def _partial_state(
    destination: Path,
    url: str,
    expected_sha: str,
    size: int | None,
    partial: Path,
    meta: Path,
) -> int:
    """Return a resumable byte offset, or -1 when a complete partial was adopted."""
    if not partial.is_file():
        meta.unlink(missing_ok=True)
        return 0
    record = _read_partial_meta(meta)
    if (
        record.get("url") != url
        or record.get("expected_sha256") != expected_sha
        or (isinstance(size, int) and size > 0 and record.get("size") != size)
    ):
        _remove_partial(partial, meta)
        return 0
    length = partial.stat().st_size
    if length > (size or length):
        _remove_partial(partial, meta)
        return 0
    if isinstance(size, int) and size > 0 and length == size:
        if _sha256(partial) == expected_sha:
            partial.replace(destination)
            meta.unlink(missing_ok=True)
            print(f"adopted completed partial {destination.name}", flush=True)
            return -1
        _remove_partial(partial, meta)
        return 0
    return length


def _partial_remaining(
    destination: Path,
    url: str,
    expected_sha: str,
    size: int,
) -> int:
    """Estimate bytes that still need downloading for an interrupted asset."""
    partial, meta = _partial_paths(destination)
    if not partial.is_file():
        return size
    record = _read_partial_meta(meta)
    if (
        record.get("url") != url
        or record.get("expected_sha256") != expected_sha
        or record.get("size") != size
    ):
        return size
    length = partial.stat().st_size
    if length >= size:
        return 0 if length == size and _sha256(partial) == expected_sha else size
    return size - length


def _content_range_matches(value: str, offset: int) -> bool:
    return value.strip().lower().startswith(f"bytes {offset}-")


def _download_asset(
    asset: dict[str, Any],
    destination: Path,
    expected_sha: str,
    timeout: int = 120,
    retries: int = 3,
) -> Path:
    name = str(asset.get("name") or destination.name)
    if destination.is_file() and _sha256(destination) == expected_sha:
        print(f"using verified {name}", flush=True)
        return destination

    url = str(asset.get("browser_download_url") or "")
    if not url:
        raise DownloadError(f"release asset {name} has no download URL")
    token = os.environ.get("GITHUB_TOKEN") or None
    size = asset.get("size") if isinstance(asset.get("size"), int) else None
    size_text = f" ({_gib(size):.2f} GiB)" if isinstance(size, int) else ""
    partial, meta = _partial_paths(destination)
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        offset = _partial_state(destination, url, expected_sha, size, partial, meta)
        if offset == -1:
            return destination
        try:
            request_headers = _headers(token)
            record = _read_partial_meta(meta)
            if offset:
                request_headers["Range"] = f"bytes={offset}-"
                if record.get("etag"):
                    request_headers["If-Range"] = str(record["etag"])
                print(
                    f"resuming {name} at {_gib(offset):.2f} GiB{size_text} ...",
                    flush=True,
                )
            else:
                print(f"downloading {name}{size_text} ...", flush=True)
            request = Request(url, headers=request_headers)
            with urlopen(request, timeout=timeout) as response:
                status = response.getcode()
                content_range = response.headers.get("Content-Range", "")
                if offset and (status != 206 or not _content_range_matches(content_range, offset)):
                    print(
                        f"  {name}: server did not honor the resume range; restarting",
                        flush=True,
                    )
                    _remove_partial(partial, meta)
                    offset = 0
                    continue
                current_etag = response.headers.get("ETag")
                if (
                    offset
                    and record.get("etag")
                    and current_etag
                    and current_etag != record["etag"]
                ):
                    print(f"  {name}: remote ETag changed; restarting", flush=True)
                    _remove_partial(partial, meta)
                    offset = 0
                    continue
                _write_partial_meta(
                    meta,
                    {
                        "url": url,
                        "expected_sha256": expected_sha,
                        "size": size,
                        "etag": response.headers.get("ETag", ""),
                        "last_modified": response.headers.get("Last-Modified", ""),
                    },
                )
                downloaded = offset
                next_report = ((offset // (256 * 1024**2)) + 1) * 256 * 1024**2
                mode = "ab" if offset else "wb"
                with partial.open(mode) as handle:
                    while chunk := response.read(8 * 1024 * 1024):
                        handle.write(chunk)
                        downloaded += len(chunk)
                        if downloaded >= next_report:
                            print(f"  {name}: {_gib(downloaded):.2f} GiB", flush=True)
                            next_report += 256 * 1024**2
            partial_size = partial.stat().st_size
            if isinstance(size, int) and size > 0 and partial_size < size:
                last_error = DownloadError(
                    f"download interrupted for {name}: {partial_size} of {size} bytes"
                )
                if attempt < retries:
                    print(f"resuming {name} ({attempt}/{retries}) ...", flush=True)
                    time.sleep(attempt * 3)
                continue
            actual = _sha256(partial)
            if actual != expected_sha:
                raise DownloadError(
                    f"checksum mismatch for {name}: expected {expected_sha}, got {actual}"
                )
            if isinstance(size, int) and size > 0 and partial.stat().st_size != size:
                raise DownloadError(f"size mismatch for {name}: expected {size} bytes")
            partial.replace(destination)
            meta.unlink(missing_ok=True)
            return destination
        except HTTPError as exc:
            last_error = exc
            if exc.code in (400, 416):
                _remove_partial(partial, meta)
            if attempt < retries:
                print(f"retrying {name} ({attempt}/{retries}) ...", flush=True)
                time.sleep(attempt * 3)
        except (URLError, OSError, DownloadError, IncompleteRead) as exc:
            last_error = exc
            if isinstance(exc, DownloadError):
                # A checksum/size failure means the partial bytes are unusable.
                _remove_partial(partial, meta)
            if attempt < retries:
                print(f"retrying {name} ({attempt}/{retries}) ...", flush=True)
                time.sleep(attempt * 3)
    raise DownloadError(f"download failed for {name}: {last_error}")


def _nearest_existing(path: Path) -> Path:
    current = path.expanduser().resolve()
    while not current.exists():
        if current.parent == current:
            break
        current = current.parent
    return current


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(_nearest_existing(path)).free


def _same_filesystem(left: Path, right: Path) -> bool:
    return _nearest_existing(left).stat().st_dev == _nearest_existing(right).stat().st_dev


def _require_space(label: str, path: Path, needed: int) -> None:
    if needed <= 0:
        return
    required = needed + SPACE_CUSHION
    free = _free_bytes(path)
    if free < required:
        raise DownloadError(
            f"not enough free space for {label} at {path}: "
            f"need {_gib(required):.2f} GiB, have {_gib(free):.2f} GiB"
        )


def _validate_release_metadata(metadata: dict[str, Any], sums: dict[str, str], tag: str) -> None:
    if metadata.get("schema") != 1 or metadata.get("tag") != tag:
        raise DownloadError("release.json does not describe the requested tag")
    archive = metadata.get("archive")
    if archive != f"{tag}.tar" or sums.get(str(archive)) != metadata.get("archive_sha256"):
        raise DownloadError("release.json archive checksum is inconsistent")
    parts = metadata.get("parts")
    if not isinstance(parts, list) or not parts:
        raise DownloadError("release.json has no parts")
    total = 0
    for index, part in enumerate(parts, start=1):
        if not isinstance(part, dict):
            raise DownloadError(f"release.json part {index} is invalid")
        name = part.get("name")
        digest = part.get("sha256")
        size = part.get("bytes")
        if name != f"{tag}.tar.part-{index:03d}":
            raise DownloadError(f"unexpected part name in release.json: {name!r}")
        if not isinstance(size, int) or size <= 0 or not isinstance(digest, str):
            raise DownloadError(f"invalid part metadata for {name}")
        if sums.get(str(name)) != digest.lower():
            raise DownloadError(f"SHA256SUMS disagrees with release.json for {name}")
        total += size
    if total != metadata.get("archive_bytes"):
        raise DownloadError("release.json part sizes do not add up to archive_bytes")
    if not isinstance(metadata.get("files"), int) or not isinstance(
        metadata.get("uncompressed_bytes"), int
    ):
        raise DownloadError("release.json has invalid file/size totals")


def _download_release(
    archive_dir: Path,
    destination: Path,
    tag: str,
    repo: str,
    verify_only: bool,
    timeout: int = 120,
) -> None:
    release = _github_release(repo, tag, timeout=timeout)
    assets = {str(asset.get("name")): asset for asset in release.get("assets", [])}
    required_small = {"SHA256SUMS"} | (SMALL_ASSETS & set(assets))
    if "SHA256SUMS" not in assets:
        raise DownloadError("release has no SHA256SUMS asset")
    archive_dir.mkdir(parents=True, exist_ok=True)
    _require_space(
        "release metadata download",
        archive_dir,
        sum(int(assets[name].get("size") or 0) for name in required_small),
    )

    sha_asset = assets["SHA256SUMS"]
    _download_asset(sha_asset, archive_dir / "SHA256SUMS", _asset_sha(sha_asset, "SHA256SUMS"))
    sums = _read_sums(archive_dir / "SHA256SUMS")

    mirror_sums = ROOT / "data" / "releases" / f"{tag}.sha256"
    if mirror_sums.is_file() and _read_sums(mirror_sums) != sums:
        print(
            f"warning: downloaded SHA256SUMS differs from {mirror_sums}; "
            "using the release's own checksum file",
            file=sys.stderr,
            flush=True,
        )

    release_asset = assets.get("release.json")
    if release_asset is None:
        raise DownloadError("release has no release.json asset")
    _download_asset(
        release_asset,
        archive_dir / "release.json",
        _asset_sha(release_asset, "release.json"),
    )
    try:
        metadata = json.loads((archive_dir / "release.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DownloadError(f"cannot read downloaded release.json: {exc}") from exc
    _validate_release_metadata(metadata, sums, tag)

    part_names = [part["name"] for part in metadata["parts"]]
    sums_parts = {name for name in sums if ".part-" in name}
    if set(part_names) != sums_parts:
        raise DownloadError("release.json parts and SHA256SUMS parts differ")
    for name in part_names:
        asset = assets.get(name)
        part = next(part for part in metadata["parts"] if part["name"] == name)
        if asset is None:
            raise DownloadError(f"release is missing part asset {name}")
        if asset.get("size") != part["bytes"]:
            raise DownloadError(f"asset size for {name} disagrees with release.json")

    for name, expected_sha in (
        ("MANIFEST.csv", sums.get("MANIFEST.csv")),
        ("RESTORE.md", sums.get("RESTORE.md")),
    ):
        asset = assets.get(name)
        if asset is None or expected_sha is None:
            raise DownloadError(f"release is missing {name}")
        _download_asset(asset, archive_dir / name, expected_sha)

    if "catalog.json" in assets:
        _download_asset(
            assets["catalog.json"],
            archive_dir / "catalog.json",
            _asset_sha(assets["catalog.json"], "catalog.json"),
        )

    missing: list[tuple[str, int]] = []
    for part in metadata["parts"]:
        name = part["name"]
        path = archive_dir / name
        expected = part["sha256"]
        if path.is_file() and _sha256(path) == expected:
            print(f"using verified {name}", flush=True)
            continue
        asset = assets[name]
        remaining = _partial_remaining(
            path,
            str(asset.get("browser_download_url") or ""),
            expected,
            int(part["bytes"]),
        )
        missing.append((name, remaining))

    archive_path = archive_dir / metadata["archive"]
    archive_bytes = 0 if archive_path.is_file() or verify_only else int(metadata["archive_bytes"])
    extract_bytes = 0 if verify_only else int(metadata["uncompressed_bytes"])
    if not verify_only:
        already_extracted = _completed_extraction_bytes(
            _extract_state_path(archive_path),
            str(metadata["archive_sha256"]),
            destination,
        )
        extract_bytes = max(0, extract_bytes - min(already_extracted, extract_bytes))
    missing_bytes = sum(size for _, size in missing)
    if _same_filesystem(archive_dir, destination):
        _require_space(
            "download and restoration",
            archive_dir,
            missing_bytes + archive_bytes + extract_bytes,
        )
    else:
        _require_space("part download", archive_dir, missing_bytes + archive_bytes)
        _require_space("archive extraction", destination, extract_bytes)

    for name, _ in missing:
        asset = assets[name]
        _download_asset(asset, archive_dir / name, sums[name])


def _safe_member(member: tarfile.TarInfo) -> None:
    name = Path(member.name)
    if name.is_absolute() or ".." in name.parts:
        raise SystemExit(f"unsafe archive path: {member.name}")
    if member.issym() or member.islnk():
        target = Path(member.linkname)
        if target.is_absolute() or ".." in target.parts:
            raise SystemExit(f"unsafe link: {member.name} -> {member.linkname}")


def _concatenate(parts: list[Path], destination: Path) -> None:
    with destination.open("wb") as output:
        for part in parts:
            print(f"appending {part.name}", flush=True)
            with part.open("rb") as source:
                shutil.copyfileobj(source, output, length=8 * 1024 * 1024)


def _extract_state_path(archive_path: Path) -> Path:
    return archive_path.with_name(archive_path.name + ".extract-state.json")


def _read_extract_state(
    state_path: Path,
    archive_sha: str,
    destination: Path,
    member_count: int,
) -> int:
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(value, dict):
        state_path.unlink(missing_ok=True)
        return 0
    completed = value.get("completed")
    if (
        value.get("schema") != EXTRACT_STATE_SCHEMA
        or value.get("archive_sha256") != archive_sha
        or value.get("destination") != str(destination)
        or value.get("members") != member_count
        or not isinstance(completed, int)
        or completed < 0
        or completed > member_count
    ):
        state_path.unlink(missing_ok=True)
        return 0
    return completed


def _write_extract_state(
    state_path: Path,
    archive_sha: str,
    destination: Path,
    member_count: int,
    completed: int,
    completed_bytes: int,
) -> None:
    temporary = state_path.with_name(state_path.name + ".tmp")
    temporary.write_text(
        json.dumps(
            {
                "schema": EXTRACT_STATE_SCHEMA,
                "archive_sha256": archive_sha,
                "destination": str(destination),
                "members": member_count,
                "completed": completed,
                "completed_bytes": completed_bytes,
            }
        ),
        encoding="utf-8",
    )
    temporary.replace(state_path)


def _completed_extraction_bytes(
    state_path: Path, archive_sha: str, destination: Path
) -> int:
    """Return bytes already unpacked by a matching interrupted extraction."""
    try:
        value = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(value, dict):
        return 0
    completed = value.get("completed_bytes")
    if (
        value.get("schema") != EXTRACT_STATE_SCHEMA
        or value.get("archive_sha256") != archive_sha
        or value.get("destination") != str(destination)
        or not isinstance(completed, int)
        or completed < 0
    ):
        return 0
    return completed


def _member_present(destination: Path, member: tarfile.TarInfo) -> bool:
    path = destination / member.name
    try:
        if member.isdir():
            return path.is_dir()
        if member.issym():
            return path.is_symlink()
        if member.islnk():
            return path.is_file()
        if member.isfile():
            return path.is_file() and path.stat().st_size == member.size
        return path.exists()
    except OSError:
        return False


def _completed_members_present(
    destination: Path, members: list[tarfile.TarInfo], completed: int
) -> bool:
    return all(_member_present(destination, member) for member in members[:completed])


def _extract_member(
    archive: tarfile.TarFile,
    member: tarfile.TarInfo,
    destination: Path,
) -> None:
    # An interrupted batch may leave a symlink or hard link in place.  Remove
    # only link paths before retrying; regular files are safely overwritten.
    if member.issym() or member.islnk():
        path = destination / member.name
        try:
            if path.is_symlink() or path.exists():
                path.unlink()
        except OSError:
            # Let extract report the real error below.
            pass
    # Python 3.12+ has the PEP 706 data filter.  On 3.11 the members have
    # already been validated for absolute paths, ``..`` and unsafe links.
    if sys.version_info >= (3, 12):
        archive.extract(member, destination, filter="data")
    else:
        archive.extract(member, destination)


def _extract_members(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    destination: Path,
    on_member: Callable[[tarfile.TarInfo], None] | None = None,
) -> None:
    for member in members:
        _extract_member(archive, member, destination)
        if on_member is not None:
            on_member(member)


class _ExtractionProgress:
    def __init__(
        self, total_members: int, total_bytes: int, start_bytes: int = 0
    ) -> None:
        self.total_members = total_members
        self.total_bytes = total_bytes
        self.start_bytes = start_bytes
        self.started = time.monotonic()
        self.last_report = self.started
        self.next_percent = 5.0
        self.last_completed = -1

    def update(self, completed: int, completed_bytes: int, *, force: bool = False) -> None:
        if force and completed == self.last_completed:
            return
        now = time.monotonic()
        percent = (completed / self.total_members * 100) if self.total_members else 100.0
        if not force and now - self.last_report < 10 and percent < self.next_percent:
            return
        while percent >= self.next_percent:
            self.next_percent += 5.0
        if self.total_bytes:
            byte_percent = completed_bytes / self.total_bytes * 100
            bytes_text = (
                f", {_gib(completed_bytes):.2f}/{_gib(self.total_bytes):.2f} GiB "
                f"({byte_percent:.1f}%)"
            )
        else:
            bytes_text = ""
        elapsed = max(now - self.started, 0.001)
        run_bytes = max(0, completed_bytes - self.start_bytes)
        rate_text = (
            f" at {_gib(run_bytes) / elapsed * 1024:.1f} MiB/s"
            if run_bytes
            else ""
        )
        print(
            f"  extraction: {completed:,}/{self.total_members:,} members "
            f"({percent:.1f}%){bytes_text}{rate_text}",
            flush=True,
        )
        self.last_report = now
        self.last_completed = completed


def _extract_archive(archive_path: Path, destination: Path, archive_sha: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    state_path = _extract_state_path(archive_path)
    with tarfile.open(archive_path, "r") as archive:
        members = archive.getmembers()
        for member in members:
            _safe_member(member)
        completed = _read_extract_state(
            state_path, archive_sha, destination, len(members)
        )
        if completed and not _completed_members_present(
            destination, members, completed
        ):
            print(
                "previous extraction checkpoint is incomplete; restarting extraction",
                flush=True,
            )
            state_path.unlink(missing_ok=True)
            completed = 0
        if completed == len(members):
            print(f"extraction already complete at {destination}", flush=True)
            state_path.unlink(missing_ok=True)
            return
        completed_bytes = sum(member.size for member in members[:completed])
        total_bytes = sum(member.size for member in members)
        progress = _ExtractionProgress(len(members), total_bytes, completed_bytes)
        if completed:
            print(
                f"resuming extraction at member {completed:,}/{len(members):,} "
                f"into {destination} ...",
                flush=True,
            )
        else:
            print(f"extracting {len(members):,} members into {destination} ...", flush=True)
        for offset in range(completed, len(members), EXTRACT_CHECKPOINT_MEMBERS):
            batch = members[offset : offset + EXTRACT_CHECKPOINT_MEMBERS]
            batch_completed = 0
            batch_bytes = 0

            def report_member(
                member: tarfile.TarInfo,
                start: int = offset,
                base_bytes: int = completed_bytes,
            ) -> None:
                nonlocal batch_completed, batch_bytes
                batch_completed += 1
                batch_bytes += member.size
                progress.update(start + batch_completed, base_bytes + batch_bytes)

            _extract_members(archive, batch, destination, report_member)
            completed = offset + len(batch)
            completed_bytes += sum(member.size for member in batch)
            _write_extract_state(
                state_path,
                archive_sha,
                destination,
                len(members),
                completed,
                completed_bytes,
            )
        progress.update(completed, completed_bytes, force=True)
    state_path.unlink(missing_ok=True)


def _valid_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdefABCDEF" for character in value)


def _verify_restored_tree(destination: Path, manifest_path: Path) -> int:
    """Check every MANIFEST row against the restored tree."""
    if not manifest_path.is_file():
        raise ManifestError(f"missing manifest: {manifest_path}")

    destination_root = destination.resolve()
    errors: list[str] = []
    error_count = 0
    seen: set[str] = set()
    checked = 0
    checked_bytes = 0
    total_rows = 0
    started = time.monotonic()
    last_report = started

    def record_error(message: str) -> None:
        nonlocal error_count
        error_count += 1
        if len(errors) < 20:
            errors.append(message)

    print(f"verifying restored files against {manifest_path.name} ...", flush=True)
    with manifest_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ManifestError("MANIFEST.csv is empty") from exc
        if header != MANIFEST_HEADER:
            raise ManifestError(f"unexpected MANIFEST.csv header: {header!r}")

        for line_number, row in enumerate(reader, start=2):
            total_rows += 1
            if len(row) != 4:
                record_error(f"row {line_number}: expected 4 fields, got {len(row)}")
                continue
            archive_path, _source_label, size_text, digest = row
            try:
                size = int(size_text)
            except ValueError:
                record_error(f"row {line_number}: invalid size {size_text!r}")
                continue
            if size < 0:
                record_error(f"row {line_number}: negative size")
                continue
            if not _valid_sha256(digest):
                record_error(f"row {line_number}: invalid SHA-256")
                continue
            if archive_path in seen:
                record_error(f"row {line_number}: duplicate path {archive_path!r}")
                continue
            seen.add(archive_path)

            pure_path = PurePosixPath(archive_path)
            if (
                not archive_path.startswith(ARCHIVE_PREFIX)
                or pure_path.is_absolute()
                or ".." in pure_path.parts
            ):
                record_error(f"row {line_number}: unsafe path {archive_path!r}")
                continue

            path = destination.joinpath(*pure_path.parts)
            try:
                path.resolve(strict=False).relative_to(destination_root)
            except (OSError, ValueError):
                record_error(f"{archive_path}: path escapes the restore directory")
                continue
            if path.is_symlink():
                record_error(f"{archive_path}: expected a regular file, found a symlink")
                continue
            try:
                if not path.is_file():
                    record_error(f"{archive_path}: missing")
                    continue
                actual_size = path.stat().st_size
                if actual_size != size:
                    record_error(
                        f"{archive_path}: size mismatch (expected {size:,}, got {actual_size:,})"
                    )
                    continue
                actual_digest = _sha256(path)
            except OSError as exc:
                record_error(f"{archive_path}: cannot read: {exc}")
                continue

            checked += 1
            checked_bytes += actual_size
            if actual_digest != digest.lower():
                record_error(f"{archive_path}: SHA-256 mismatch")
                continue
            now = time.monotonic()
            if now - last_report >= 10 or checked % 1000 == 0:
                print(
                    f"  manifest: {checked:,} files, "
                    f"{_gib(checked_bytes):.2f} GiB checked",
                    flush=True,
                )
                last_report = now

    if error_count:
        for message in errors:
            print(f"  {message}", file=sys.stderr)
        if error_count > len(errors):
            print(f"  ... and {error_count - len(errors):,} more errors", file=sys.stderr)
        raise ManifestError(
            f"{error_count:,} of {total_rows:,} manifest entries failed verification"
        )
    print(
        f"verified {total_rows:,} restored files "
        f"({_gib(checked_bytes):.2f} GiB) against {manifest_path.name}",
        flush=True,
    )
    return total_rows


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.archive_dir:
        archive_dir = Path(args.archive_dir).expanduser().resolve()
    elif args.download:
        archive_dir = Path(DEFAULT_ARCHIVE_DIR).expanduser().resolve()
    else:
        parser.error("--archive-dir is required unless --download is used")

    destination = Path(args.dest).expanduser().resolve()
    if args.download:
        try:
            _download_release(
                archive_dir=archive_dir,
                destination=destination,
                tag=args.tag,
                repo=args.repo,
                verify_only=args.verify_only,
            )
        except (DownloadError, OSError) as exc:
            print(f"download failed: {exc}", file=sys.stderr)
            return 1

    sums_path = archive_dir / "SHA256SUMS"
    if not sums_path.is_file():
        print(f"missing {sums_path}", file=sys.stderr)
        return 2
    sums = _read_sums(sums_path)

    part_names = sorted(name for name in sums if ".part-" in name)
    if not part_names:
        print("no split parts listed in SHA256SUMS", file=sys.stderr)
        return 2

    parts: list[Path] = []
    for name in part_names:
        path = archive_dir / name
        if not path.is_file():
            print(f"missing part: {path}", file=sys.stderr)
            return 2
        expected = sums[name]
        print(f"verifying {name} ({path.stat().st_size / (1024**2):.1f} MiB) ...", flush=True)
        actual = _sha256(path)
        if actual != expected:
            print(f"checksum mismatch for {name}", file=sys.stderr)
            print(f"  expected {expected}", file=sys.stderr)
            print(f"  actual   {actual}", file=sys.stderr)
            return 1
        parts.append(path)
    print(f"verified {len(parts)} split parts", flush=True)
    if args.verify_only:
        return 0

    full_name = next((name for name in sums if name.endswith(".tar")), None)
    if full_name is None:
        print("SHA256SUMS has no full tar entry", file=sys.stderr)
        return 2
    full_path = archive_dir / full_name
    if full_path.exists():
        print(f"using existing {full_path.name}", flush=True)
    else:
        _concatenate(parts, full_path)
    manifest_path = archive_dir / "MANIFEST.csv"
    expected_manifest = sums.get("MANIFEST.csv")
    if not manifest_path.is_file():
        print(f"missing {manifest_path}", file=sys.stderr)
        return 2
    if expected_manifest is None:
        print("SHA256SUMS has no MANIFEST.csv entry", file=sys.stderr)
        return 2
    print("verifying MANIFEST.csv ...", flush=True)
    actual_manifest = _sha256(manifest_path)
    if actual_manifest != expected_manifest:
        print("MANIFEST.csv checksum mismatch", file=sys.stderr)
        print(f"  expected {expected_manifest}", file=sys.stderr)
        print(f"  actual   {actual_manifest}", file=sys.stderr)
        return 1
    expected_full = sums[full_name]
    print("verifying full archive (this can take a minute) ...", flush=True)
    actual_full = _sha256(full_path)
    if actual_full != expected_full:
        print("full archive checksum mismatch", file=sys.stderr)
        print(f"  expected {expected_full}", file=sys.stderr)
        print(f"  actual   {actual_full}", file=sys.stderr)
        return 1

    destination.mkdir(parents=True, exist_ok=True)
    _extract_archive(full_path, destination, expected_full)
    try:
        _verify_restored_tree(destination, manifest_path)
    except (ManifestError, OSError) as exc:
        print(f"restored tree verification failed: {exc}", file=sys.stderr)
        return 1
    print(f"restored archive to {destination}", flush=True)
    if args.remove_archive:
        full_path.unlink(missing_ok=True)
        print(f"removed reconstructed {full_path.name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
