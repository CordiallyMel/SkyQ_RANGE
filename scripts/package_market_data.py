"""Package the local one-minute market archive for a GitHub release.

The raw archive is intentionally kept outside Git.  This script selects the
one-minute research data from the four local archive roots, records a SHA-256
manifest, creates a single tar (preserving hardlinks), and splits it into
release-sized assets.  The resulting directory is ready to upload with the
commands in ``docs/DATA_RELEASE.md``.

Example::

    python -m scripts.package_market_data --dry-run
    python -m scripts.package_market_data --split-mib 1800

The default output is ``../../release_assets/<tag>`` relative to this
repository, so the multi-gigabyte tar and parts never enter Git.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parents[1]
DEFAULT_TAG = "market-data-1m-2026-11-15"
ARCHIVE_PREFIX = "skyq-range-1m-archive"
SKIP_SUFFIXES = {".py", ".pyc", ".log", ".lock", ".tmp"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument(
        "--output-dir",
        default=str(PROJECT_ROOT / "release_assets" / DEFAULT_TAG),
        help="directory for the tar, parts, manifests and checksums",
    )
    parser.add_argument(
        "--split-mib",
        type=int,
        default=1800,
        help="maximum size of each release asset in MiB (default: 1800)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list the selected data and size without hashing or writing",
    )
    return parser


def _roots() -> list[tuple[str, Path]]:
    return [
        ("Data_For_Backtests", PROJECT_ROOT / "Data_For_Backtests"),
        ("SkyQ_Sigma_S/data", PROJECT_ROOT / "SkyQ_Sigma_S" / "data"),
        ("SkyQ_Scan/data", PROJECT_ROOT / "SkyQ_Scan" / "data"),
        ("Options_Edge/data", PROJECT_ROOT / "Options_Edge" / "data"),
    ]


def _is_one_minute(label: str, relative: str) -> bool:
    """Return True for a one-minute data file in one of the archive roots."""

    low = relative.lower()
    suffix = Path(relative).suffix.lower()
    if suffix in SKIP_SUFFIXES or any(part.startswith("__pycache__") for part in Path(relative).parts):
        return False

    # Canonical bars and every named 1m/1min/minute/M1 series.
    if any(token in low for token in ("1m", "1min", "minute", "m1_ohlc")):
        return True
    if "databento_premarket" in low:
        return True

    # The stock scanner stores its minute bars in date-partitioned raw files
    # whose names do not contain "1m".
    if label == "SkyQ_Scan/data":
        return low.startswith(("databento/", "cache/", "universes"))

    # Options raw files are named by contract, not by interval.  The panels and
    # underlying files do carry the interval; only 1m is included there.
    if label == "Options_Edge/data":
        if low.startswith("raw/"):
            return True
        if low.startswith("panels/") and "_1m" in low:
            return True
        if low.startswith("underlying/") and "_1m" in low:
            return True
        if low.startswith(("results/", "trades/")):
            return True
        if low.endswith(("manifest.csv", "legs_spy.csv", "legs_qqq.csv")):
            return True
    return False


def _select() -> list[tuple[str, Path, Path]]:
    selected: list[tuple[str, Path, Path]] = []
    for label, root in _roots():
        if not root.is_dir():
            print(f"warning: archive root not found: {root}", file=sys.stderr)
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if _is_one_minute(label, relative.as_posix()):
                selected.append((label, relative, path))
    selected.sort(key=lambda item: (item[0], item[1].as_posix()))
    return selected


def _sha256(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _archive_name(label: str, relative: Path) -> str:
    return f"{ARCHIVE_PREFIX}/{label}/{relative.as_posix()}"


def _restore_text(tag: str, owner: str = "CordiallyMel", repo: str = "SkyQ_RANGE") -> str:
    return f"""# SkyQ RANGE 1-minute data archive

Release tag: `{tag}`

This archive contains the local one-minute research data selected by
`scripts/package_market_data.py`: canonical ETF bars, futures and derived
1-minute indicators, forex/crypto 1-minute context, Databento stock-scanner
minute data, and option 1-minute panels/underlying/raw data.

## Download

From a clone of `{owner}/{repo}` with the GitHub CLI:

```bash
gh release download {tag} --repo {owner}/{repo} --dir release-assets
```

Or download every `*.part-*`, `SHA256SUMS`, `MANIFEST.csv` and `RESTORE.md`
from the release page into one directory.

## Verify and restore

The restore command verifies each split part, concatenates them, verifies the
full tar checksum, rejects unsafe paths, and extracts the archive:

```bash
python -m scripts.restore_market_data \\
  --archive-dir release-assets \\
  --dest data/restored
```

The archive expands under
`data/restored/skyq-range-1m-archive/` with the original relative roots:

```text
skyq-range-1m-archive/
  Data_For_Backtests/
  SkyQ_Sigma_S/data/
  SkyQ_Scan/data/
  Options_Edge/data/
  MANIFEST.csv
  RESTORE.md
```

Use `--verify-only` to check checksums without extracting.  Use
`--remove-archive` to delete the reconstructed full tar after extraction.

## Per-file verification

`MANIFEST.csv` records the size and SHA-256 of every archived file.  The
release-level `SHA256SUMS` covers the full tar and every split part.  To check
the full archive manually:

```bash
cat {tag}.tar.part-* > {tag}.tar
sha256sum -c SHA256SUMS
tar -tf {tag}.tar | head
```

## Contents and exclusions

The package deliberately includes only data needed for one-minute research and
the option panels.  It excludes broker statements, credentials, raw LSE tick
streams, Python caches/logs, and the non-minute derived results that are already
documented in `data/catalog.json`.

Vendor data remains subject to the applicable vendor agreement.  The MIT
license covers the repository code and original notes, not third-party market
data.  Do not redistribute this archive if your data agreement does not allow
it.
"""


def _write_manifest(
    output: Path,
    selected: list[tuple[str, Path, Path]],
    progress: bool = True,
) -> tuple[Path, int, int]:
    manifest = output / "MANIFEST.csv"
    total_bytes = 0
    with manifest.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["archive_path", "source_label", "size_bytes", "sha256"])
        for index, (label, relative, path) in enumerate(selected, start=1):
            size = path.stat().st_size
            total_bytes += size
            writer.writerow(
                [_archive_name(label, relative), label, size, _sha256(path)]
            )
            if progress and (index % 250 == 0 or index == len(selected)):
                print(
                    f"hashed {index:,}/{len(selected):,} files "
                    f"({total_bytes / (1024**3):.2f} GiB)",
                    flush=True,
                )
    return manifest, len(selected), total_bytes


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str) -> None:
    info = tar.gettarinfo(str(path), arcname)
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.mtime = int(path.stat().st_mtime)
    with path.open("rb") as handle:
        tar.addfile(info, handle)


def _build_tar(
    output: Path,
    selected: list[tuple[str, Path, Path]],
    manifest: Path,
    restore: Path,
    catalog: Path,
    restore_script: Path,
    tag: str,
) -> Path:
    tar_path = output / f"{tag}.tar"
    first_inode: dict[tuple[int, int], str] = {}
    with tarfile.open(tar_path, "w", format=tarfile.PAX_FORMAT) as tar:
        for index, (label, relative, path) in enumerate(selected, start=1):
            arcname = _archive_name(label, relative)
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            if stat.st_nlink > 1 and key in first_inode:
                info = tarfile.TarInfo(arcname)
                info.type = tarfile.LNKTYPE
                info.linkname = first_inode[key]
                info.size = 0
                tar.addfile(info)
            else:
                if stat.st_nlink > 1:
                    first_inode[key] = arcname
                _add_file(tar, path, arcname)
            if index % 500 == 0 or index == len(selected):
                print(f"archived {index:,}/{len(selected):,} files", flush=True)
        _add_file(tar, manifest, f"{ARCHIVE_PREFIX}/MANIFEST.csv")
        _add_file(tar, restore, f"{ARCHIVE_PREFIX}/RESTORE.md")
        if catalog.is_file():
            _add_file(tar, catalog, f"{ARCHIVE_PREFIX}/data/catalog.json")
        if restore_script.is_file():
            _add_file(
                tar,
                restore_script,
                f"{ARCHIVE_PREFIX}/scripts/{restore_script.name}",
            )
    return tar_path


def _split(tar_path: Path, output: Path, split_bytes: int) -> list[Path]:
    parts: list[Path] = []
    with tar_path.open("rb") as source:
        index = 1
        while True:
            part = output / f"{tar_path.name}.part-{index:03d}"
            written = 0
            with part.open("wb") as target:
                while written < split_bytes:
                    chunk = source.read(min(8 * 1024 * 1024, split_bytes - written))
                    if not chunk:
                        break
                    target.write(chunk)
                    written += len(chunk)
            if written == 0:
                part.unlink(missing_ok=True)
                break
            parts.append(part)
            print(f"wrote {part.name} ({written / (1024**2):.1f} MiB)", flush=True)
            index += 1
    return parts


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    selected = _select()
    total_apparent = sum(path.stat().st_size for _, _, path in selected)
    if not selected:
        print("no one-minute files found", file=sys.stderr)
        return 2
    print(
        f"selected {len(selected):,} files | "
        f"{total_apparent / (1024**3):.2f} GiB apparent",
        flush=True,
    )
    if args.dry_run:
        for label, relative, _ in selected[:20]:
            print(f"  {label}/{relative.as_posix()}")
        if len(selected) > 20:
            print(f"  ... and {len(selected) - 20:,} more")
        return 0

    output = Path(args.output_dir).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        print(f"output directory is not empty: {output}", file=sys.stderr)
        print("remove or choose a different --output-dir", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)

    restore_text = _restore_text(args.tag)
    restore = output / "RESTORE.md"
    restore.write_text(restore_text, encoding="utf-8")
    manifest, count, total_bytes = _write_manifest(output, selected)
    print(f"wrote {manifest} | {count:,} files | {total_bytes / (1024**3):.2f} GiB", flush=True)

    tar_path = _build_tar(
        output,
        selected,
        manifest,
        restore,
        ROOT / "data" / "catalog.json",
        ROOT / "scripts" / "restore_market_data.py",
        args.tag,
    )
    full_sha = _sha256(tar_path)
    print(f"full tar: {tar_path.name} | {tar_path.stat().st_size / (1024**3):.2f} GiB | sha256={full_sha}", flush=True)

    parts = _split(tar_path, output, max(1, args.split_mib) * 1024 * 1024)
    checksums: list[tuple[str, str]] = [(full_sha, tar_path.name)]
    part_rows: list[dict[str, object]] = []
    for part in parts:
        part_sha = _sha256(part)
        checksums.append((part_sha, part.name))
        part_rows.append(
            {"name": part.name, "bytes": part.stat().st_size, "sha256": part_sha}
        )
        print(f"part checksum: {part.name} | sha256={part_sha}", flush=True)

    for extra in (manifest, restore):
        checksums.append((_sha256(extra), extra.name))
    sums = output / "SHA256SUMS"
    sums.write_text(
        "".join(f"{digest}  {name}\n" for digest, name in checksums),
        encoding="utf-8",
        newline="\n",
    )

    release = {
        "schema": 1,
        "tag": args.tag,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "archive": tar_path.name,
        "archive_bytes": tar_path.stat().st_size,
        "archive_sha256": full_sha,
        "split_mib": args.split_mib,
        "files": count,
        "uncompressed_bytes": total_bytes,
        "parts": part_rows,
        "checksums": sums.name,
        "manifest": manifest.name,
        "restore": restore.name,
        "roots": [label for label, root in _roots() if root.is_dir()],
    }
    (output / "release.json").write_text(
        json.dumps(release, indent=2) + "\n",
        encoding="utf-8",
    )
    if (ROOT / "data" / "catalog.json").is_file():
        shutil.copy2(ROOT / "data" / "catalog.json", output / "catalog.json")
    print(f"ready to upload: {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
