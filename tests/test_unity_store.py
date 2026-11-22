"""Local Unity Asset Store cache; synthetic packages only."""
import json
import os
import shutil
import socket

import fixtures
import httpx
import pytest
from conftest import _register_fake_pack

from synty_mcp.cli import main
from synty_mcp.config import load_settings
from synty_mcp.service import Service


@pytest.fixture
def cache(tmp_path):
    root = tmp_path / "private-unity-cache"
    root.mkdir()
    svc = Service(load_settings(data_dir=tmp_path / "data", unity_asset_store_enabled=True,
                                unity_asset_store_roots=[root]))
    return svc, root


def test_discovery_does_not_import_or_parse(cache):
    svc, root = cache
    package = root / "publisher" / "category" / "Test.UnityPackage"
    package.parent.mkdir(parents=True)
    package.write_bytes(b"not parsed on scan")
    (root / "unrelated.json").write_text("private")
    result = svc.unity_store.scan()
    assert [i["filename"] for i in result["items"]] == [package.name]
    assert root.name not in str(result)
    assert not svc.db.q("SELECT * FROM archives")
    assert not any(svc.settings.archives_dir.iterdir())
    assert result == svc.unity_store.scan()


def test_unavailable_roots_and_confinement(cache, tmp_path):
    svc, root = cache
    outside = tmp_path / "outside"
    hidden = fixtures.build_unitypackage(outside / "not-for-import.unitypackage")
    (root / "other").symlink_to(outside, target_is_directory=True)
    (root / hidden.name).symlink_to(hidden)
    svc.settings.unity_asset_store_roots += [tmp_path / "missing", svc.settings.secrets_dir]
    result = svc.unity_store.scan()
    assert result["items"] == []
    assert [r["status"] for r in result["roots"]] == ["ready", "missing or not a directory", "overlaps managed storage"]
    with pytest.raises(ValueError, match="not discovered"):
        svc.unity_store.import_package(str(hidden), allow_path=True)
    svc.settings.unity_asset_store_enabled = False
    fixtures.build_unitypackage(root / "valid.unitypackage")
    assert not svc.unity_store.sync()["items"]
    assert not svc.unity_store.status()["available"]
    assert not svc.db.q("SELECT * FROM archives")


def test_sync_is_offline_idempotent_and_preserves_cache(cache, monkeypatch):
    svc, root = cache
    _register_fake_pack(svc)
    pkg = fixtures.build_unitypackage(root / "nested" / "POLYGON_FakeVillage.UnityPackage")
    original = pkg.stat()
    other = root / "other.unitypackage"
    shutil.copyfile(pkg, other)
    svc.settings.unity_asset_store_roots.append(pkg.parent)  # overlapping roots, single discovery

    def forbid(*args, **kwargs):
        pytest.fail("local cache provider attempted network access")

    monkeypatch.setattr(socket.socket, "connect", forbid)
    monkeypatch.setattr(httpx.Client, "__init__", forbid)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", forbid)
    synced = svc.unity_store.sync()["items"]
    assert sorted(i["status"] for i in synced) == ["imported", "skipped_unmatched"]
    assert svc.db.one("SELECT asset_count FROM packs WHERE slug='polygon-fakevillage'")["asset_count"] > 0
    arc = svc.db.one("SELECT * FROM archives")
    assert arc["verified"] == "ok" and arc["origin"] == "unity_cache"
    assert not os.path.samefile(pkg, arc["path"])
    assert (pkg.stat().st_mode, pkg.stat().st_mtime_ns) == (original.st_mode, original.st_mtime_ns)

    # Explicit selection may import an unmatched file. SHA deduplication remains authoritative.
    assert svc.unity_store.import_package(str(other), allow_path=True)["archive_id"] == arc["id"]
    assert {i["status"] for i in svc.unity_store.sync()["items"]} == {"already_imported"}
    ref = next(i["id"] for i in synced if i["filename"] == pkg.name)
    os.utime(pkg, ns=(original.st_atime_ns, original.st_mtime_ns + 10**9))
    with pytest.raises(ValueError, match="not discovered"):
        svc.unity_store.import_package(ref)
    result = svc.unity_store.import_package(str(pkg), allow_path=True)
    assert result["archive_id"] == arc["id"]
    assert len(svc.db.q("SELECT id FROM archives")) == 1


def test_cli_workflow_persists_import_state(tmp_path, monkeypatch, capsys):
    root = tmp_path / "cache"
    pkg = fixtures.build_unitypackage(root / "POLYGON_FakeVillage.unitypackage")
    config = tmp_path / "config.yaml"
    config.write_text("data_dir: data\nunity_asset_store_enabled: true\nunity_asset_store_roots: [cache]\n")
    monkeypatch.setenv("SYNTY_MCP_CONFIG", str(config))

    def run(*args):
        assert main(["unity-cache", *args]) == 0
        return json.loads(capsys.readouterr().out)

    assert run("status")["roots"][0]["path"] == str(root)
    item, = run("scan")["items"]
    assert item["filename"] == pkg.name and item["archive_id"] is None
    assert run("sync")["items"][0]["status"] == "skipped_unmatched"
    result = run("import", item["id"])
    assert result["status"] == "imported" and result["pack"] == "polygon-fakevillage"
    assert run("sync")["items"][0]["status"] == "already_imported"


def test_failed_verification_does_not_mark_import_complete(cache):
    svc, root = cache
    _register_fake_pack(svc)
    bad = root / "POLYGON_FakeVillage.unitypackage"
    bad.write_bytes(b"incomplete download")
    outcome = svc.unity_store.sync()["items"][0]
    assert outcome["status"] == "failed" and "verification" in outcome["error"]
    item, = svc.unity_store.scan()["items"]
    assert item["archive_id"] is None
    assert not svc.db.q("SELECT * FROM archives") and not any(svc.settings.archives_dir.iterdir())
    assert svc.unity_store.sync()["items"][0]["status"] == "failed_unchanged"
    assert svc.unity_store.import_package(item["id"], retry=True)["status"] == "failed"
    fixtures.build_unitypackage(bad)
    assert svc.unity_store.sync()["items"][0]["status"] == "imported"
