# 1-minute market-data release

The raw research archive is too large for a normal Git clone, so it is published
as split GitHub release assets. The repository keeps the catalog, code, compact
published results and the verifier; the release holds the actual bars.

**Release tag:** `market-data-1m-2026-11-15`

Release page:

<https://github.com/CordiallyMel/SkyQ_RANGE/releases/tag/market-data-1m-2026-11-15>

Release metadata: [`data/releases/market-data-1m-2026-11-15.json`](../data/releases/market-data-1m-2026-11-15.json)

Published checksums: [`data/releases/market-data-1m-2026-11-15.sha256`](../data/releases/market-data-1m-2026-11-15.sha256)

## What the package contains

The package is built from the four archive roots documented in
[`data/catalog.json`](../data/catalog.json):

- `Data_For_Backtests/` — canonical SPY/QQQ 1m files, stitched ETF files,
  forex/crypto 1m context, leveraged ETF 1m files and Databento premarket data
- `SkyQ_Sigma_S/data/` — futures 1m bars and 1m indicator bundles
- `SkyQ_Scan/data/` — Databento stock-scanner raw/cache/universal files
- `Options_Edge/data/` — all option raw files, 1m panels, 1m underlying bars,
  option results and trade ledgers

It includes hard-linked copies in `Data_For_Backtests/store/` as hard links in
the tar, so the restored tree preserves those paths without uploading the same
bytes repeatedly.

It does **not** include credentials, broker statements, LSE live ticks, Python
caches/logs, or a duplicate of the small published dashboard bundle.

## Download and restore in one command

From a clone of the repository:

```bash
python -m scripts.restore_market_data \
  --download \
  --dest data/restored
```

This downloads the release assets into `release-assets/`, verifies every
SHA-256, reconstructs the tar, checks the full-archive hash, and extracts the
data. It checks free space before downloading and before extraction. The peak
working set is roughly **19 GB**: downloaded parts, reconstructed tar, and the
unpacked tree.

Set `GITHUB_TOKEN` to avoid anonymous GitHub API rate limits; the token is used
only for the API request and is never printed. `--verify-only` downloads and
checks the parts without reconstructing or extracting.

If a multi-gigabyte download is interrupted, run the same command again. The
helper resumes from the verified `.download` partial using an HTTP range
request, after confirming the source URL, asset size, ETag and expected
SHA-256 still match.

Extraction is also checkpointed. If the unpack is interrupted, rerun the same
command and it resumes after the last completed batch of members. The temporary
checkpoint is `<archive-name>.extract-state.json`; it is removed after a
successful extraction. Large extracts print member, byte and rate progress at
five-percent or ten-second intervals, including an estimated time remaining.
After extraction, including a resumed extraction, the helper verifies every
restored file's size and SHA-256 against `MANIFEST.csv` and fails if any entry
is missing or changed.

### Download separately

```bash
python -m scripts.restore_market_data --download --verify-only

# Or use GitHub CLI and verify the existing directory later:
gh release download market-data-1m-2026-11-15 \
  --repo CordiallyMel/SkyQ_RANGE \
  --dir release-assets
python -m scripts.restore_market_data \
  --archive-dir release-assets \
  --verify-only
```

### Browser

Open the release page and download every file whose name contains
`.tar.part-`, plus `SHA256SUMS`, `MANIFEST.csv` and `RESTORE.md`, into one
directory.

### Verify without extracting

```bash
python -m scripts.restore_market_data \
  --archive-dir release-assets \
  --verify-only
```

This checks every split part against `SHA256SUMS` and prints the verified count.
It does not need a market-data license or any credentials. The same checksums
and release metadata are mirrored in
[`data/releases/market-data-1m-2026-11-15.sha256`](../data/releases/market-data-1m-2026-11-15.sha256)
and [`data/releases/market-data-1m-2026-11-15.json`](../data/releases/market-data-1m-2026-11-15.json).

## Restore

```bash
python -m scripts.restore_market_data \
  --archive-dir release-assets \
  --dest data/restored
```

The verifier:

1. verifies every `.part-*` SHA-256;
2. concatenates the parts in order;
3. verifies the full tar SHA-256;
4. rejects absolute paths, `..` components and unsafe links;
5. extracts the archive;
6. verifies every restored file against `MANIFEST.csv`.

The result is:

```text
data/restored/skyq-range-1m-archive/
  Data_For_Backtests/
  SkyQ_Sigma_S/data/
  SkyQ_Scan/data/
  Options_Edge/data/
  MANIFEST.csv
  RESTORE.md
  data/catalog.json
  scripts/restore_market_data.py
```

Move the four roots to wherever your local archive lives, or use them in place:

```bash
python -m scripts.backtest_range --symbol SPY --quantity 1000 \
  --data data/restored/skyq-range-1m-archive/Data_For_Backtests/SPY_1m_24y.parquet \
  --start 2003-09-10 --end 2026-11-04

python -m scripts.import_market_data \
  --file data/restored/skyq-range-1m-archive/Data_For_Backtests/QQQ_1m_24y.parquet \
  --symbol QQQ --dataset etf-1m \
  --sqlite data/market/range_market.sqlite --replace
```

`MANIFEST.csv` contains the size and SHA-256 of every path, so you can verify a
single extracted file without re-downloading the archive. The restore helper
also performs this verification across the full restored tree automatically.

### Verify an existing restore

To re-check an already restored tree without downloading parts, concatenating
the tar or extracting again:

```bash
python -m scripts.restore_market_data \
  --verify-restored \
  --dest data/restored
```

The manifest is read from `skyq-range-1m-archive/MANIFEST.csv` under the
destination by default. Alternatively, pass a release directory containing
`MANIFEST.csv` (and optionally `SHA256SUMS`):

```bash
python -m scripts.restore_market_data \
  --verify-restored \
  --archive-dir release-assets \
  --dest data/restored
```

The command exits non-zero if any manifest entry is missing, the wrong size or
digest, or has an unsafe path.

## Release audit

The CI **Release audit** job runs:

```bash
python -m scripts.audit_release
```

The separate **Scheduled release audit** workflow runs the same command every
Monday at 06:17 UTC, and supports manual dispatch from the Actions tab. It
catches release or manifest drift even when no code is pushed.

When a push or scheduled audit fails, the workflow creates an issue titled
`Release audit failed: <tag>` containing the audit output, commit and run link.
Subsequent failures add a comment to the existing open issue instead of creating
a duplicate. Pull-request audits do not open issues because fork tokens are
read-only.

The scheduled workflow can also send an alert to a Slack-compatible incoming
webhook. Set the repository secret `RELEASE_AUDIT_WEBHOOK_URL` to enable it;
when the secret is absent the notification step skips cleanly. Keep the webhook
URL in GitHub Secrets, not in the repository or workflow file.

It checks the GitHub release against the committed metadata, verifies each
part's GitHub SHA-256 digest and size, downloads `SHA256SUMS`, `release.json`,
`MANIFEST.csv` and `RESTORE.md`, and validates the 12,269-row manifest and file
counts. It deliberately does **not** download the 5.4 GB of parts on every CI
run; GitHub's asset digest covers the uploaded bytes.

For a full local content audit, which downloads and hashes every part:

```bash
python -m scripts.audit_release --download-parts
```

That full audit needs the same multi-gigabyte bandwidth and disk headroom as a
normal download.

## Rebuild the package

The package is reproducible from a local archive:

```bash
# From the repository root; the data roots are expected two levels up.
python -m scripts.package_market_data --dry-run
python -m scripts.package_market_data --split-mib 1800
```

The output goes to `../../release_assets/<tag>/` by default. It writes:

- `<tag>.tar` — the full archive;
- `<tag>.tar.part-001` … — assets under GitHub's 2 GB per-asset limit;
- `MANIFEST.csv` — per-file SHA-256;
- `SHA256SUMS` — full tar and part checksums;
- `release.json` — sizes, part count, file count and checksums;
- `RESTORE.md` — the release-facing restore guide.

Upload the parts and the small manifest/checksum files with
`gh release upload <tag> <files> --repo CordiallyMel/SkyQ_RANGE`, or through the
GitHub release UI. Keep the full tar local; it is not uploaded as one asset.

## Licensing and safety

Vendor data remains subject to the applicable vendor agreement. The MIT license
covers the repository code and original notes, not third-party market data. Do
not redistribute the archive if your data agreement does not permit it.

The release is historical research data, not an execution guarantee. Stops can
gap through the modelled price; halts and LULD are not represented.
