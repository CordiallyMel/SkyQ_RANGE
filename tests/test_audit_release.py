"""Checks for the release audit's restore-summary link validation."""

from __future__ import annotations

import pytest

from scripts.audit_release import AuditError, _validate_restore_summary
from scripts.package_market_data import _restore_text

TAG = "market-data-1m-2026-11-15"
SUMMARY_URL = (
    "https://github.com/CordiallyMel/SkyQ_RANGE/releases/download/"
    f"{TAG}/restore-summary.md"
)


def _summary_asset(url: str = SUMMARY_URL) -> dict[str, str]:
    return {"browser_download_url": url}


def test_generated_restore_text_links_the_published_summary() -> None:
    assert _validate_restore_summary(_restore_text(TAG), _summary_asset()) == SUMMARY_URL


def test_restore_summary_link_must_be_present() -> None:
    with pytest.raises(AuditError, match="exactly one"):
        _validate_restore_summary("# restore\n\nNo summary here.\n", _summary_asset())


def test_restore_summary_link_must_match_the_release_asset() -> None:
    text = _restore_text(TAG).replace(SUMMARY_URL, SUMMARY_URL.replace(TAG, "other-tag"))
    with pytest.raises(AuditError, match="does not match the release asset"):
        _validate_restore_summary(text, _summary_asset())
