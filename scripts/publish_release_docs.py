"""Publish refreshed release documentation assets to GitHub.

The release-facing ``RESTORE.md`` is generated from
``scripts.package_market_data._restore_text``.  After that template or the
checksum mirror changes, this command regenerates ``RESTORE.md``, rebuilds
``SHA256SUMS`` from ``data/releases/<tag>.sha256``, and replaces both assets on
the published GitHub release.  It also regenerates the release body, including
the checkpoint rate-window warning, so the release notes and ``RESTORE.md``
stay consistent.  The local checksum mirror is updated only after GitHub
confirms the replacement assets and release body.

Requires a token with ``Contents: write`` permission in ``GITHUB_TOKEN`` or
``GH_TOKEN``.

Examples::

    GITHUB_TOKEN=... python -m scripts.publish_release_docs
    GITHUB_TOKEN=... python -m scripts.publish_release_docs --tag OTHER_TAG
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from scripts.package_market_data import _restore_text

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TAG = "market-data-1m-2026-11-15"
DEFAULT_REPO = "CordiallyMel/SkyQ_RANGE"
ASSET_NAMES = ("RESTORE.md", "SHA256SUMS")
CONTENT_TYPES = {
    "RESTORE.md": "text/markdown; charset=utf-8",
    "SHA256SUMS": "text/plain; charset=utf-8",
}
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CHECKPOINT_WARNING = (
    "Resumed extractions print the checkpoint's recorded rate window and warn "
    "when it differs from the active `--rate-window`."
)


class PublishError(RuntimeError):
    """Raised when release documentation cannot be published safely."""


@dataclass(frozen=True)
class Documents:
    restore: bytes
    sums: bytes
    notes: bytes
    restore_digest: str
    sums_digest: str
    notes_digest: str
    mirror_path: Path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _release_notes_text(
    tag: str,
    owner: str = "CordiallyMel",
    repo: str = "SkyQ_RANGE",
) -> str:
    """Generate the release body from the same guidance as RESTORE.md."""

    return f"""# SkyQ RANGE 1-minute market data

Split release assets for the local 1-minute research archive. The code and compact published results remain in the repository; this release carries the bars and option panels.

## Download and restore in one command

From a clone of `{owner}/{repo}`:

```bash
python -m scripts.restore_market_data \\
  --download \\
  --dest data/restored
```

This downloads the release assets into `release-assets/`, verifies every SHA-256, reconstructs the full tar, checks the full-archive hash, rejects unsafe paths, extracts the archive, and checks every restored file against `MANIFEST.csv`. Set `GITHUB_TOKEN` to avoid anonymous GitHub API rate limits. Interrupted downloads and extractions resume when the command is rerun.

{CHECKPOINT_WARNING}

## Download separately

Download every `{tag}.tar.part-*` file plus `SHA256SUMS`, `MANIFEST.csv`, `RESTORE.md` and `release.json` into one directory.

With the GitHub CLI:

```bash
gh release download {tag} --repo {owner}/{repo} --dir release-assets
```

Then restore from that directory:

```bash
python -m scripts.restore_market_data \\
  --archive-dir release-assets \\
  --dest data/restored
```

The verifier checks every part SHA-256, reconstructs the full tar, checks the full-archive SHA-256, rejects unsafe paths, and extracts the archive. Use `--verify-only` to check the download without extracting.

## Asset layout

- `{tag}.tar.part-*` — split archive parts, each under GitHub's 2 GB limit
- `SHA256SUMS` — full-tar and per-part checksums
- `MANIFEST.csv` — per-file size and SHA-256
- `RESTORE.md` — full download and restore guide
- `release.json` — machine-readable sizes and checksums

The exact part count and sizes are recorded in `release.json`. The archive expands to `skyq-range-1m-archive/` with `Data_For_Backtests/`, `SkyQ_Sigma_S/data/`, `SkyQ_Scan/data/` and `Options_Edge/data/`.

The package was built from the four local archive roots and preserves hard-linked option/store paths as tar hardlinks. It includes 1-minute ETF, futures, forex/crypto, Databento scanner, options panel/underlying/raw data and 1m indicator bundles. It excludes credentials, broker statements, raw LSE tick streams, Python caches/logs and non-minute aggregate results.

Vendor data remains subject to the applicable vendor agreement; the MIT license covers the repository code and original notes, not third-party market data.

See `docs/DATA_RELEASE.md` in the repository for the full restore and rebuild workflow.
"""


def _parse_sums(text: str) -> dict[str, str]:
    sums: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split(maxsplit=1)
        if len(fields) != 2:
            raise PublishError(f"malformed checksum line: {raw_line!r}")
        digest, name = fields
        name = name.lstrip("*")
        if not SHA256_RE.fullmatch(digest.lower()):
            raise PublishError(f"invalid checksum for {name}: {digest!r}")
        if name in sums:
            raise PublishError(f"duplicate checksum entry: {name}")
        sums[name] = digest.lower()
    return sums


def _api_base() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _headers(token: str, *, accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "User-Agent": "skyq-range-release-docs",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _request(
    method: str,
    url: str,
    token: str,
    *,
    timeout: int,
    payload: dict[str, Any] | None = None,
    content: bytes | None = None,
    content_type: str | None = None,
    accept: str = "application/vnd.github+json",
) -> Any:
    headers = _headers(token, accept=accept)
    data = content
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif content is not None and content_type:
        headers["Content-Type"] = content_type
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        try:
            detail = exc.read(1_000).decode("utf-8", errors="replace").strip()
        except OSError:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise PublishError(f"GitHub API returned HTTP {exc.code} for {method} {url}{suffix}") from exc
    except (URLError, OSError) as exc:
        raise PublishError(f"could not reach the GitHub API: {exc}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PublishError(f"GitHub API returned invalid JSON for {method} {url}") from exc


def _download(url: str, token: str, timeout: int) -> bytes:
    request = Request(
        url,
        headers=_headers(token, accept="application/octet-stream"),
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read()
    except HTTPError as exc:
        raise PublishError(f"GitHub asset download returned HTTP {exc.code}") from exc
    except (URLError, OSError) as exc:
        raise PublishError(f"could not download the GitHub release asset: {exc}") from exc


class _GitHub:
    def __init__(self, token: str, *, timeout: int) -> None:
        self.token = token
        self.timeout = timeout

    def release(self, repo: str, tag: str) -> dict[str, Any]:
        release = _request(
            "GET",
            f"{_api_base()}/repos/{repo}/releases/tags/{quote(tag, safe='')}",
            self.token,
            timeout=self.timeout,
        )
        if not isinstance(release, dict):
            raise PublishError("GitHub returned an invalid release payload")
        if release.get("tag_name") != tag:
            raise PublishError(
                f"GitHub returned tag {release.get('tag_name')!r}, expected {tag!r}"
            )
        if release.get("draft") or not release.get("published_at"):
            raise PublishError(f"release {tag} is not published")
        return release

    def update_release(
        self,
        repo: str,
        release_id: int,
        body: str,
    ) -> dict[str, Any]:
        updated = _request(
            "PATCH",
            f"{_api_base()}/repos/{repo}/releases/{release_id}",
            self.token,
            timeout=self.timeout,
            payload={"body": body},
        )
        if not isinstance(updated, dict):
            raise PublishError(
                f"GitHub returned an invalid release update for {release_id}"
            )
        return updated

    def download_asset(self, asset: dict[str, Any]) -> bytes:
        url = str(asset.get("browser_download_url") or asset.get("url") or "")
        if not url:
            raise PublishError(f"release asset {asset.get('name')!r} has no download URL")
        return _download(url, self.token, self.timeout)

    def upload_asset(
        self,
        upload_url: str,
        name: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        base = upload_url.split("{", 1)[0]
        url = f"{base}?{urlencode({'name': name})}"
        uploaded = _request(
            "POST",
            url,
            self.token,
            timeout=self.timeout,
            content=content,
            content_type=content_type,
        )
        if not isinstance(uploaded, dict):
            raise PublishError(f"GitHub returned an invalid upload response for {name}")
        return uploaded

    def rename_asset(self, repo: str, asset_id: int, name: str) -> dict[str, Any]:
        renamed = _request(
            "PATCH",
            f"{_api_base()}/repos/{repo}/releases/assets/{asset_id}",
            self.token,
            timeout=self.timeout,
            payload={"name": name},
        )
        if not isinstance(renamed, dict):
            raise PublishError(f"GitHub returned an invalid rename response for asset {asset_id}")
        return renamed

    def delete_asset(self, repo: str, asset_id: int) -> None:
        _request(
            "DELETE",
            f"{_api_base()}/repos/{repo}/releases/assets/{asset_id}",
            self.token,
            timeout=self.timeout,
        )


def _build_documents(tag: str, metadata_dir: Path, repo: str) -> Documents:
    owner, repository = repo.split("/", 1)
    mirror_path = metadata_dir / f"{tag}.sha256"
    try:
        original = mirror_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(f"cannot read checksum mirror {mirror_path}: {exc}") from exc

    sums = _parse_sums(original)
    required = {f"{tag}.tar", "MANIFEST.csv", "RESTORE.md"}
    missing = sorted(required - set(sums))
    if missing:
        raise PublishError(f"checksum mirror is missing: {', '.join(missing)}")
    if not any(".part-" in name for name in sums):
        raise PublishError("checksum mirror has no split part entries")

    restore_text = _restore_text(tag, owner=owner, repo=repository)
    if not restore_text.endswith("\n"):
        restore_text += "\n"
    if tag not in restore_text or f"{tag}.tar.part-*" not in restore_text:
        raise PublishError("generated RESTORE.md does not describe the requested release")

    restore = restore_text.encode("utf-8")
    restore_digest = _sha256(restore)

    notes_text = _release_notes_text(tag, owner=owner, repo=repository)
    if not notes_text.endswith("\n"):
        notes_text += "\n"
    if (
        tag not in notes_text
        or "RESTORE.md" not in notes_text
        or CHECKPOINT_WARNING not in notes_text
    ):
        raise PublishError(
            "generated release notes do not describe the requested release"
        )
    notes = notes_text.encode("utf-8")
    notes_digest = _sha256(notes)

    updated_lines: list[str] = []
    replacements = 0
    for line in original.splitlines():
        fields = line.split(maxsplit=1)
        if (
            len(fields) == 2
            and fields[1].lstrip("*") == "RESTORE.md"
        ):
            replacements += 1
            updated_lines.append(f"{restore_digest}  RESTORE.md")
        else:
            updated_lines.append(line)
    if replacements != 1:
        raise PublishError("checksum mirror must contain exactly one RESTORE.md entry")

    sums_text = "\n".join(updated_lines) + "\n"
    if _parse_sums(sums_text).get("RESTORE.md") != restore_digest:
        raise PublishError("generated SHA256SUMS does not contain the RESTORE.md digest")
    sums_bytes = sums_text.encode("utf-8")
    return Documents(
        restore=restore,
        sums=sums_bytes,
        notes=notes,
        restore_digest=restore_digest,
        sums_digest=_sha256(sums_bytes),
        notes_digest=notes_digest,
        mirror_path=mirror_path,
    )


def _asset_digest(asset: dict[str, Any]) -> str | None:
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        return None
    value = digest.split(":", 1)[1].lower()
    return value if SHA256_RE.fullmatch(value) else None


def _actual_digest(client: _GitHub, asset: dict[str, Any]) -> str:
    digest = _asset_digest(asset)
    if digest is not None:
        return digest
    return _sha256(client.download_asset(asset))


def _asset_map(release: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(asset.get("name")): asset
        for asset in release.get("assets", [])
        if isinstance(asset, dict)
    }


def _write_mirror(path: Path, content: bytes) -> bool:
    try:
        if path.read_bytes() == content:
            return False
    except OSError:
        pass
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(path)
    return True


def _publish_notes(
    client: _GitHub,
    repo: str,
    release: dict[str, Any],
    documents: Documents,
) -> None:
    notes = documents.notes.decode("utf-8")
    if str(release.get("body") or "") == notes:
        print("release notes already current", flush=True)
        return

    release_id = release.get("id")
    if release_id is None:
        raise PublishError("release has no id")
    updated = client.update_release(repo, int(release_id), notes)
    if str(updated.get("body") or "") != notes:
        raise PublishError("GitHub did not preserve the generated release notes")
    print(
        f"updated release notes: {len(documents.notes):,} bytes | "
        f"sha256={documents.notes_digest}",
        flush=True,
    )


def _publish_documents(
    client: _GitHub,
    repo: str,
    tag: str,
    documents: Documents,
) -> None:
    release = client.release(repo, tag)
    assets = _asset_map(release)
    sums_asset = assets.get("SHA256SUMS")
    if sums_asset is None:
        raise PublishError("release has no SHA256SUMS asset")

    try:
        remote_sums = _parse_sums(client.download_asset(sums_asset).decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise PublishError("release SHA256SUMS is not valid UTF-8") from exc
    local_sums = _parse_sums(documents.sums.decode("utf-8"))
    if {
        name: digest for name, digest in remote_sums.items() if name != "RESTORE.md"
    } != {
        name: digest for name, digest in local_sums.items() if name != "RESTORE.md"
    }:
        raise PublishError(
            "release SHA256SUMS disagrees with the local mirror outside RESTORE.md; "
            "run python -m scripts.audit_release before publishing"
        )

    restore_asset = assets.get("RESTORE.md")
    if (
        restore_asset is not None
        and _actual_digest(client, restore_asset) == documents.restore_digest
        and _actual_digest(client, sums_asset) == documents.sums_digest
    ):
        print(f"release docs already current: {tag}", flush=True)
        _publish_notes(client, repo, release, documents)
        changed = _write_mirror(documents.mirror_path, documents.sums)
        if changed:
            print(f"updated local checksum mirror: {documents.mirror_path}", flush=True)
        return

    upload_url = str(release.get("upload_url") or "")
    if not upload_url:
        raise PublishError("release has no upload URL")

    suffix = uuid.uuid4().hex[:10]
    temporary_names = {
        name: f"{name}.publish-{suffix}" for name in ASSET_NAMES
    }
    temporary_assets: dict[str, dict[str, Any]] = {}
    try:
        for name in ASSET_NAMES:
            uploaded = client.upload_asset(
                upload_url,
                temporary_names[name],
                documents.restore if name == "RESTORE.md" else documents.sums,
                CONTENT_TYPES[name],
            )
            expected = (
                documents.restore_digest if name == "RESTORE.md" else documents.sums_digest
            )
            if _actual_digest(client, uploaded) != expected:
                raise PublishError(f"GitHub reported the wrong digest for uploaded {name}")
            temporary_assets[name] = uploaded

        backups: dict[str, tuple[dict[str, Any], str]] = {}
        moved: dict[str, dict[str, Any]] = {}
        try:
            for name in ASSET_NAMES:
                old = assets.get(name)
                if old is None:
                    continue
                backup_name = f"{name}.old-{suffix}"
                client.rename_asset(repo, int(old["id"]), backup_name)
                backups[name] = (old, backup_name)
            for name in ASSET_NAMES:
                client.rename_asset(repo, int(temporary_assets[name]["id"]), name)
                moved[name] = temporary_assets.pop(name)

            current = _asset_map(client.release(repo, tag))
            expected_digests = {
                "RESTORE.md": documents.restore_digest,
                "SHA256SUMS": documents.sums_digest,
            }
            for name, expected in expected_digests.items():
                asset = current.get(name)
                if asset is None or _actual_digest(client, asset) != expected:
                    raise PublishError(f"published {name} does not have the expected digest")
        except Exception:
            for name, asset in list(moved.items()):
                try:
                    client.rename_asset(repo, int(asset["id"]), temporary_names[name])
                    temporary_assets[name] = asset
                except PublishError:
                    pass
            for name, (old, _backup_name) in backups.items():
                try:
                    client.rename_asset(repo, int(old["id"]), name)
                except PublishError:
                    pass
            raise

        for old, _backup_name in backups.values():
            client.delete_asset(repo, int(old["id"]))
    finally:
        for asset in temporary_assets.values():
            try:
                client.delete_asset(repo, int(asset["id"]))
            except PublishError:
                pass

    _publish_notes(client, repo, release, documents)
    changed = _write_mirror(documents.mirror_path, documents.sums)
    print(f"published RESTORE.md and SHA256SUMS to {tag}", flush=True)
    print(
        f"RESTORE.md: {len(documents.restore):,} bytes | "
        f"sha256={documents.restore_digest}",
        flush=True,
    )
    print(
        f"SHA256SUMS: {len(documents.sums):,} bytes | "
        f"sha256={documents.sums_digest}",
        flush=True,
    )
    if changed:
        print(f"updated local checksum mirror: {documents.mirror_path}", flush=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=DEFAULT_TAG)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=ROOT / "data" / "releases",
        help="directory containing <tag>.sha256 (default: data/releases)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="network timeout in seconds (default: 120)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.repo.count("/") != 1:
        parser.error("--repo must be owner/repository")
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("GITHUB_TOKEN or GH_TOKEN is required to publish release docs", file=sys.stderr)
        return 2

    try:
        documents = _build_documents(args.tag, args.metadata_dir, args.repo)
        client = _GitHub(token, timeout=args.timeout)
        _publish_documents(client, args.repo, args.tag, documents)
    except (PublishError, OSError) as exc:
        print(f"release doc publish failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
