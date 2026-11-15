"""Send a failure alert for the weekly release audit.

The scheduled workflow runs this after a failed audit.  Configure the
repository secret ``RELEASE_AUDIT_WEBHOOK_URL`` with a Slack-compatible
incoming webhook URL.  If the secret is absent, the step is skipped without
turning the existing audit failure into a second failure.

The webhook URL is read only from the environment and is never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scripts.report_release_audit_failure import _error_line, _read_log

DEFAULT_TAG = "market-data-1m-2026-11-15"


class NotifyError(RuntimeError):
    """Raised when the webhook cannot deliver a notification."""


def _message(
    *,
    tag: str,
    repo: str,
    workflow: str,
    event: str,
    commit: str,
    run_url: str,
    log: str,
) -> str:
    return "\n".join(
        [
            "*Release audit failed*",
            f"Tag: `{tag}`",
            f"Repository: `{repo}`",
            f"Workflow: `{workflow}` ({event})",
            f"Commit: `{commit or 'unknown'}`",
            f"Run: {run_url or 'not available'}",
            f"Error: {_error_line(log)}",
        ]
    )


def _post_webhook(url: str, payload: dict[str, Any], retries: int = 3) -> None:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "skyq-range-release-audit-notifier",
    }
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = Request(url, data=data, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=15) as response:
                status = response.getcode()
                response.read(1_000)
            if status >= 300:
                raise NotifyError(f"webhook returned HTTP {status}")
            return
        except HTTPError as exc:
            last_error = exc
            if 400 <= exc.code < 500:
                raise NotifyError(f"webhook returned HTTP {exc.code}") from exc
        except (URLError, OSError, NotifyError) as exc:
            last_error = exc
        if attempt < retries:
            time.sleep(attempt * 2)
    raise NotifyError(f"webhook notification failed: {last_error}")


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

    webhook_url = os.environ.get("RELEASE_AUDIT_WEBHOOK_URL", "").strip()
    if not webhook_url:
        print("notification skipped: RELEASE_AUDIT_WEBHOOK_URL is not configured")
        return 0
    if not args.repo or "/" not in args.repo:
        print("notification failed: --repo must be owner/repository", file=sys.stderr)
        return 2

    try:
        message = _message(
            tag=args.tag,
            repo=args.repo,
            workflow=args.workflow,
            event=args.event,
            commit=args.commit,
            run_url=args.run_url,
            log=_read_log(args.log),
        )
        _post_webhook(webhook_url, {"text": message})
    except (NotifyError, OSError) as exc:
        print(f"notification failed: {exc}", file=sys.stderr)
        return 1
    print("release audit notification sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
