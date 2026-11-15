"""Tests for release-audit failure issue reporting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Self

from scripts import report_release_audit_failure as reporter


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._raw


def _recording_urlopen(
    responses: list[Any], calls: list[tuple[str, str, dict[str, Any] | None]]
):
    def fake(request: Any, timeout: int) -> _FakeResponse:
        del timeout
        payload = json.loads(request.data) if request.data else None
        calls.append((request.get_method(), request.full_url, payload))
        return _FakeResponse(responses.pop(0))

    return fake


def test_report_creates_an_issue_with_the_audit_details(
    tmp_path: Path, monkeypatch
) -> None:
    log = tmp_path / "release-audit.log"
    log.write_text(
        "release audit failed: MANIFEST.csv disagrees with SHA256SUMS\n",
        encoding="utf-8",
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        reporter,
        "urlopen",
        _recording_urlopen(
            [
                [],
                {"number": 7, "html_url": "https://github.com/example/issues/7"},
            ],
            calls,
        ),
    )
    code = reporter.main(
        [
            "--tag",
            "market-data-test",
            "--repo",
            "owner/repo",
            "--log",
            str(log),
            "--workflow",
            "Scheduled release audit",
            "--event",
            "schedule",
            "--commit",
            "abc123",
        ]
    )
    assert code == 0
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert calls[1][1].endswith("/repos/owner/repo/issues")
    issue = calls[1][2]
    assert issue is not None
    assert issue["title"] == "Release audit failed: market-data-test"
    assert "MANIFEST.csv disagrees" in issue["body"]
    assert "Scheduled release audit" in issue["body"]
    assert "abc123" in issue["body"]


def test_report_comments_on_an_existing_open_issue(
    tmp_path: Path, monkeypatch
) -> None:
    log = tmp_path / "release-audit.log"
    log.write_text("release audit failed: part digest mismatch\n", encoding="utf-8")
    calls: list[tuple[str, str, dict[str, Any] | None]] = []
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")
    monkeypatch.setattr(
        reporter,
        "urlopen",
        _recording_urlopen(
            [
                [
                    {
                        "number": 5,
                        "title": "Release audit failed: market-data-test",
                        "html_url": "https://github.com/example/issues/5",
                    }
                ],
                {"id": 99},
            ],
            calls,
        ),
    )
    code = reporter.main(
        [
            "--tag",
            "market-data-test",
            "--repo",
            "owner/repo",
            "--log",
            str(log),
        ]
    )
    assert code == 0
    assert calls[1][1].endswith("/repos/owner/repo/issues/5/comments")
    comment = calls[1][2]
    assert comment is not None
    assert "failed again" in comment["body"]
    assert "part digest mismatch" in comment["body"]


def test_report_without_a_token_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert reporter.main(["--repo", "owner/repo"]) == 2