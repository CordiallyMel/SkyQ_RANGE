"""Turn a failed release audit into a GitHub issue.

The release-audit workflows capture the audit output in ``release-audit.log``
and run this module only after the audit step fails.  The issue contains the
run metadata and the captured drift/error details.  If an open issue with the
same title already exists, the new details are added as a comment instead of
opening a duplicate every week.

The token is read from ``GITHUB_TOKEN`` and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

DEFAULT_TAG = "market-data-1m-2026-11-15"
ISSUE_TITLE_PREFIX = "Release audit failed:"
MAX_LOG_CHARS = 50_000


class ReportError(RuntimeError):
    """Raised when the GitHub issue cannot be reported."""


def _api_base() -> str:
    return os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def _request(
    method: str,
    path: str,
    token: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    data = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "skyq-range-release-audit-reporter",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(_api_base() + path, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=60) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:2_000]
        raise ReportError(f"GitHub API returned HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ReportError(f"could not reach GitHub API: {exc.reason}") from exc
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReportError("GitHub API returned invalid JSON") from exc


def _read_log(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if len(text) > MAX_LOG_CHARS:
        text = "... log truncated ...\n" + text[-MAX_LOG_CHARS:]
    return text.strip()


def _error_line(log: str) -> str:
    for line in log.splitlines():
        if line.strip().startswith("release audit failed:"):
            return line.strip()
    return "The audit did not provide a one-line error summary."


def _issue_body(
    *,
    tag: str,
    repo: str,
    workflow: str,
    event: str,
    commit: str,
    run_url: str,
    log: str,
    repeat: bool = False,
) -> str:
    heading = "## Release audit failed again" if repeat else "## Release audit failure"
    log_block = log or "No audit output was captured; the job may have failed before the audit ran."
    return f"""{heading}

The published data release did not match the repository metadata, or the audit
could not complete. This needs investigation before the next restore.

| Detail | Value |
| --- | --- |
| Release tag | `{tag}` |
| Repository | `{repo}` |
| Workflow | `{workflow}` |
| Event | `{event}` |
| Commit | `{commit or "unknown"}` |
| Run | {run_url or "not available"} |

### Drift details

**{_error_line(log)}**

### Audit output

````text
{log_block}
````
"""


def _find_open_issue(repo: str, token: str, tag: str) -> dict[str, Any] | None:
    query = urlencode({"state": "open", "per_page": 100})
    issues = _request("GET", f"/repos/{repo}/issues?{query}", token)
    if not isinstance(issues, list):
        raise ReportError("GitHub returned an unexpected issue list")
    prefix = f"{ISSUE_TITLE_PREFIX} {tag}"
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("pull_request"):
            continue
        if str(issue.get("title") or "").startswith(prefix):
            return issue
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", default=os.environ.get("AUDIT_TAG", DEFAULT_TAG))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--log", type=Path, default=Path(os.environ.get("AUDIT_LOG", "release-audit.log")))
    parser.add_argument("--workflow", default=os.environ.get("AUDIT_WORKFLOW", "unknown"))
    parser.add_argument("--event", default=os.environ.get("AUDIT_EVENT", "unknown"))
    parser.add_argument("--commit", default=os.environ.get("AUDIT_SHA", "unknown"))
    parser.add_argument("--run-url", default=os.environ.get("AUDIT_RUN_URL", ""))
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        print("report failed: GITHUB_TOKEN is not set", file=sys.stderr)
        return 2
    if not args.repo or "/" not in args.repo:
        print("report failed: --repo must be owner/repository", file=sys.stderr)
        return 2

    try:
        log = _read_log(args.log)
        existing = _find_open_issue(args.repo, token, args.tag)
        details = _issue_body(
            tag=args.tag,
            repo=args.repo,
            workflow=args.workflow,
            event=args.event,
            commit=args.commit,
            run_url=args.run_url,
            log=log,
            repeat=existing is not None,
        )
        if existing is not None:
            number = int(existing["number"])
            _request(
                "POST",
                f"/repos/{args.repo}/issues/{number}/comments",
                token,
                {"body": details},
            )
            print(f"added failure details to {existing.get('html_url', f'issue #{number}')}")
            return 0
        issue = _request(
            "POST",
            f"/repos/{args.repo}/issues",
            token,
            {
                "title": f"{ISSUE_TITLE_PREFIX} {args.tag}",
                "body": details,
            },
        )
        if not isinstance(issue, dict):
            raise ReportError("GitHub returned an unexpected issue response")
        print(f"created issue {issue.get('html_url', '')}".rstrip())
        return 0
    except (ReportError, OSError) as exc:
        print(f"report failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
