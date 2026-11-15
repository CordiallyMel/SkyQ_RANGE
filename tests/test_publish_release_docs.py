"""Offline checks for the release documentation publisher."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from scripts import publish_release_docs as publish
from scripts.package_market_data import _restore_text

TAG = "market-data-1m-2026-11-15"
REPO = "CordiallyMel/SkyQ_RANGE"


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _mirror_text(restore_digest: str = "5" * 64) -> str:
    return (
        f"{'1' * 64}  {TAG}.tar\n"
        f"{'2' * 64}  {TAG}.tar.part-001\n"
        f"{'3' * 64}  MANIFEST.csv\n"
        f"{restore_digest}  RESTORE.md\n"
    )


def _metadata_dir(tmp_path: Path, restore_digest: str = "5" * 64) -> Path:
    metadata = tmp_path / "releases"
    metadata.mkdir()
    (metadata / f"{TAG}.sha256").write_text(
        _mirror_text(restore_digest), encoding="utf-8"
    )
    return metadata


class _FakeClient:
    def __init__(self, assets: list[dict[str, object]], remote_sums: bytes) -> None:
        self.assets = {str(asset["name"]): dict(asset) for asset in assets}
        self.remote_sums = remote_sums
        self._next_id = 100
        self.uploaded: list[str] = []
        self.renamed: list[tuple[int, str]] = []
        self.deleted: list[int] = []
        self.notes = "old release notes"
        self.updated_notes: list[str] = []

    def release(self, repo: str, tag: str) -> dict[str, object]:
        assert repo == REPO
        assert tag == TAG
        return {
            "id": 1,
            "tag_name": tag,
            "draft": False,
            "published_at": "2026-11-15T00:00:00Z",
            "body": self.notes,
            "upload_url": "https://uploads.example/releases/1/assets{?name,label}",
            "assets": [dict(asset) for asset in self.assets.values()],
        }

    def update_release(
        self, repo: str, release_id: int, body: str
    ) -> dict[str, object]:
        assert repo == REPO
        assert release_id == 1
        self.notes = body
        self.updated_notes.append(body)
        return {"id": release_id, "body": body}

    def download_asset(self, asset: dict[str, object]) -> bytes:
        if asset["name"] == "SHA256SUMS":
            return self.remote_sums
        raise AssertionError(f"unexpected asset download: {asset['name']}")

    def upload_asset(
        self,
        upload_url: str,
        name: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, object]:
        assert upload_url.startswith("https://uploads.example/")
        assert content_type
        self._next_id += 1
        asset: dict[str, object] = {
            "id": self._next_id,
            "name": name,
            "digest": f"sha256:{_digest(content)}",
        }
        self.assets[name] = asset
        self.uploaded.append(name)
        return asset

    def rename_asset(self, repo: str, asset_id: int, name: str) -> dict[str, object]:
        assert repo == REPO
        asset = next(asset for asset in self.assets.values() if asset["id"] == asset_id)
        del self.assets[str(asset["name"])]
        asset["name"] = name
        self.assets[name] = asset
        self.renamed.append((asset_id, name))
        return asset

    def delete_asset(self, repo: str, asset_id: int) -> None:
        assert repo == REPO
        name = next(
            str(asset["name"])
            for asset in self.assets.values()
            if asset["id"] == asset_id
        )
        del self.assets[name]
        self.deleted.append(asset_id)


def _old_assets() -> tuple[list[dict[str, object]], bytes]:
    old_sums = _mirror_text().encode("utf-8")
    return (
        [
            {
                "id": 1,
                "name": "RESTORE.md",
                "digest": f"sha256:{_digest(b'old restore')}",
            },
            {
                "id": 2,
                "name": "SHA256SUMS",
                "digest": f"sha256:{_digest(old_sums)}",
            },
        ],
        old_sums,
    )


def test_build_documents_updates_only_the_restore_line(tmp_path: Path) -> None:
    metadata = _metadata_dir(tmp_path)
    documents = publish._build_documents(TAG, metadata, REPO)

    assert documents.restore == _restore_text(TAG).encode("utf-8")
    assert b"warn when it differs from the active" in documents.restore
    assert documents.restore_digest == _digest(documents.restore)
    assert documents.notes == publish._release_notes_text(
        TAG, "CordiallyMel", "SkyQ_RANGE"
    ).encode("utf-8")
    assert b"warn when it differs from the active" in documents.notes
    assert b"RESTORE.md" in documents.notes
    assert documents.notes_digest == _digest(documents.notes)
    sums = publish._parse_sums(documents.sums.decode("utf-8"))
    assert sums["RESTORE.md"] == documents.restore_digest
    assert sums[f"{TAG}.tar"] == "1" * 64
    assert sums[f"{TAG}.tar.part-001"] == "2" * 64
    assert sums["MANIFEST.csv"] == "3" * 64


def test_publish_uploads_swaps_and_updates_mirror(tmp_path: Path) -> None:
    metadata = _metadata_dir(tmp_path)
    documents = publish._build_documents(TAG, metadata, REPO)
    assets, old_sums = _old_assets()
    client = _FakeClient(assets, old_sums)

    publish._publish_documents(client, REPO, TAG, documents)

    assert set(client.assets) == {"RESTORE.md", "SHA256SUMS"}
    assert client.assets["RESTORE.md"]["digest"] == f"sha256:{documents.restore_digest}"
    assert client.assets["SHA256SUMS"]["digest"] == f"sha256:{documents.sums_digest}"
    assert len(client.uploaded) == 2
    assert client.deleted == [1, 2]
    assert client.updated_notes == [documents.notes.decode("utf-8")]
    assert (metadata / f"{TAG}.sha256").read_bytes() == documents.sums


def test_publish_is_idempotent_when_assets_are_current(tmp_path: Path) -> None:
    metadata = _metadata_dir(tmp_path)
    documents = publish._build_documents(TAG, metadata, REPO)
    assets, _old_sums = _old_assets()
    client = _FakeClient(assets, documents.sums)
    client.assets["RESTORE.md"]["digest"] = f"sha256:{documents.restore_digest}"
    client.assets["SHA256SUMS"]["digest"] = f"sha256:{documents.sums_digest}"
    client.notes = documents.notes.decode("utf-8")

    publish._publish_documents(client, REPO, TAG, documents)

    assert client.uploaded == []
    assert client.deleted == []
    assert client.updated_notes == []
    assert (metadata / f"{TAG}.sha256").read_bytes() == documents.sums


def test_publish_refuses_remote_checksum_drift(tmp_path: Path) -> None:
    metadata = _metadata_dir(tmp_path)
    documents = publish._build_documents(TAG, metadata, REPO)
    assets, _old_sums = _old_assets()
    drifted_sums = _mirror_text().replace("3" * 64, "4" * 64).encode("utf-8")
    client = _FakeClient(assets, drifted_sums)

    with pytest.raises(publish.PublishError, match="disagrees"):
        publish._publish_documents(client, REPO, TAG, documents)

    assert client.uploaded == []
    assert client.updated_notes == []
    assert (metadata / f"{TAG}.sha256").read_text(encoding="utf-8") == _mirror_text()


def test_publish_updates_release_notes_when_assets_are_current(tmp_path: Path) -> None:
    metadata = _metadata_dir(tmp_path)
    documents = publish._build_documents(TAG, metadata, REPO)
    assets, _old_sums = _old_assets()
    client = _FakeClient(assets, documents.sums)
    client.assets["RESTORE.md"]["digest"] = f"sha256:{documents.restore_digest}"
    client.assets["SHA256SUMS"]["digest"] = f"sha256:{documents.sums_digest}"

    publish._publish_documents(client, REPO, TAG, documents)

    assert client.uploaded == []
    assert client.updated_notes == [documents.notes.decode("utf-8")]
    assert (metadata / f"{TAG}.sha256").read_bytes() == documents.sums


def test_publish_does_not_update_mirror_when_notes_update_fails(
    tmp_path: Path,
) -> None:
    metadata = _metadata_dir(tmp_path)
    documents = publish._build_documents(TAG, metadata, REPO)
    assets, _old_sums = _old_assets()

    class FailingNotesClient(_FakeClient):
        def update_release(
            self, repo: str, release_id: int, body: str
        ) -> dict[str, object]:
            raise publish.PublishError("simulated release notes failure")

    client = FailingNotesClient(assets, documents.sums)
    client.assets["RESTORE.md"]["digest"] = f"sha256:{documents.restore_digest}"
    client.assets["SHA256SUMS"]["digest"] = f"sha256:{documents.sums_digest}"

    with pytest.raises(publish.PublishError, match="simulated release notes failure"):
        publish._publish_documents(client, REPO, TAG, documents)

    assert client.uploaded == []
    assert (metadata / f"{TAG}.sha256").read_text(encoding="utf-8") == _mirror_text()


def test_publish_does_not_update_mirror_when_upload_fails(tmp_path: Path) -> None:
    metadata = _metadata_dir(tmp_path)
    documents = publish._build_documents(TAG, metadata, REPO)
    assets, old_sums = _old_assets()

    class FailingClient(_FakeClient):
        def upload_asset(
            self,
            upload_url: str,
            name: str,
            content: bytes,
            content_type: str,
        ) -> dict[str, object]:
            raise publish.PublishError("simulated upload failure")

    client = FailingClient(assets, old_sums)
    with pytest.raises(publish.PublishError, match="simulated upload failure"):
        publish._publish_documents(client, REPO, TAG, documents)

    assert (metadata / f"{TAG}.sha256").read_text(encoding="utf-8") == _mirror_text()


def test_publish_rejects_a_missing_restore_entry(tmp_path: Path) -> None:
    metadata = _metadata_dir(tmp_path)
    mirror = metadata / f"{TAG}.sha256"
    mirror.write_text(
        f"{'1' * 64}  {TAG}.tar\n"
        f"{'2' * 64}  {TAG}.tar.part-001\n"
        f"{'3' * 64}  MANIFEST.csv\n",
        encoding="utf-8",
    )

    with pytest.raises(publish.PublishError, match="RESTORE.md"):
        publish._build_documents(TAG, metadata, REPO)
