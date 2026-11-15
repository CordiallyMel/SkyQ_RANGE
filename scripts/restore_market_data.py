"""Verify and restore the split one-minute market-data release.

This is intentionally dependency-free.  It reads the release's ``SHA256SUMS``,
verifies every downloaded part, concatenates them, verifies the full tar,
rejects unsafe archive members, and extracts the data.

Examples::

    # Check downloads without extracting 6.8 GB.
    python -m scripts.restore_market_data --archive-dir release-assets --verify-only

    # Restore under data/restored/skyq-range-1m-archive/.
    python -m scripts.restore_market_data \
      --archive-dir release-assets --dest data/restored
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PREFIX = "market-data-1m-"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive-dir",
        required=True,
        help="directory containing *.part-*, SHA256SUMS and MANIFEST.csv",
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


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    archive_dir = Path(args.archive_dir).expanduser().resolve()
    sums_path = archive_dir / "SHA256SUMS"
    if not sums_path.is_file():
        print(f"missing {sums_path}", file=sys.stderr)
        return 2
    sums = _read_sums(sums_path)

    part_names = sorted(
        name
        for name in sums
        if name.startswith(DEFAULT_PREFIX) and ".part-" in name
    )
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

    full_name = next(
        (name for name in sums if name.startswith(DEFAULT_PREFIX) and name.endswith(".tar")),
        None,
    )
    if full_name is None:
        print("SHA256SUMS has no full tar entry", file=sys.stderr)
        return 2
    full_path = archive_dir / full_name
    if full_path.exists():
        print(f"using existing {full_path.name}", flush=True)
    else:
        _concatenate(parts, full_path)
    expected_full = sums[full_name]
    print("verifying full archive (this can take a minute) ...", flush=True)
    actual_full = _sha256(full_path)
    if actual_full != expected_full:
        print("full archive checksum mismatch", file=sys.stderr)
        print(f"  expected {expected_full}", file=sys.stderr)
        print(f"  actual   {actual_full}", file=sys.stderr)
        return 1

    destination = Path(args.dest).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(full_path, "r") as archive:
        members = archive.getmembers()
        for member in members:
            _safe_member(member)
        print(f"extracting {len(members):,} members into {destination} ...", flush=True)
        # Python 3.12+ has the PEP 706 data filter.  On 3.11 we have already
        # validated paths and refuse unsafe members above.
        if sys.version_info >= (3, 12):
            archive.extractall(destination, filter="data")
        else:
            archive.extractall(destination)
    print(f"restored archive to {destination}", flush=True)
    if args.remove_archive:
        full_path.unlink(missing_ok=True)
        print(f"removed reconstructed {full_path.name}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
