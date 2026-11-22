"""Policy boundaries common to local and MCP cache operations."""
import hashlib
import os
import shutil
from pathlib import Path

import fixtures
import pytest
from conftest import _register_fake_pack

from synty_mcp.config import load_settings
from synty_mcp.service import Service

pytestmark = pytest.mark.skipif(os.name == "nt", reason="Unity cache ingestion is unsupported on native Windows")


@pytest.fixture
def store(svc, tmp_path):
    root = tmp_path / "cache"
    root.mkdir()
    svc.settings.unity_asset_store_enabled = True
    svc.settings.unity_asset_store_roots = [root]
    _register_fake_pack(svc)
    return svc.unity_store, root


def test_disclosure_requires_boolean_opt_in(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text('unity_asset_store_show_unmatched: "false"\n')
    with pytest.raises(ValueError, match="must be a boolean"):
        load_settings(config, data_dir=tmp_path)


def test_id_is_private_and_survives_restart(store):
    provider, root = store
    pkg = fixtures.build_unitypackage(root / "private.unitypackage")
    item, = provider.scan()["items"]
    stamp = f"\0{pkg.stat().st_size}:{pkg.stat().st_mtime_ns}".encode()
    assert item["id"] != hashlib.sha256(os.fsencode(pkg) + stamp).hexdigest()
    assert item["id"] == Service(provider.settings).unity_store.scan()["items"][0]["id"]
    assert "mtime_ns" not in item
    with pytest.raises(ValueError, match="scan ID"):
        provider.import_package(str(pkg))
    assert not provider.db.q("SELECT * FROM archives")


def test_fuzzy_and_competing_matches_require_selection(store):
    provider, root = store
    for name in ("A/POLYGON_FakeVillage.unitypackage", "B/POLYGON_FakeVillage.unitypackage",
                 "B/POLYGON_FakeVillage Extras.unitypackage"):
        fixtures.build_unitypackage(root / name)
    results = provider.sync()["items"]
    assert len(results) == 3 and {i["status"] for i in results} == {"skipped_ambiguous"}
    assert not provider.db.q("SELECT * FROM archives")
    fuzzy, = [i for i in results if i["match"] == "tentative"]
    assert provider.import_package(fuzzy["id"])["status"] == "imported"


def test_sync_does_not_mix_trees_and_confirmation_is_explicit(store):
    provider, root = store
    pkg = fixtures.build_unitypackage(root / "POLYGON_FakeVillage.unitypackage")
    assert provider.sync()["items"][0]["status"] == "imported"
    # AI: Same pack, different valid archive bytes. A shared tree already exists.
    fixtures.build_unitypackage(pkg, previews=False)
    tree = provider.settings.library_dir / "polygon-fakevillage" / "unity"
    before = {p: p.stat().st_mtime_ns for p in tree.rglob("*") if p.is_file()}
    conflict, = provider.sync()["items"]
    assert conflict["status"] == "requires_tree_confirmation"
    assert provider.import_package(conflict["id"])["status"] == "requires_tree_confirmation"
    assert {p: p.stat().st_mtime_ns for p in before} == before and before
    assert len(provider.db.q("SELECT id FROM archives")) == 1
    assert provider.import_package(conflict["id"], allow_tree_update=True)["status"] == "imported"


def test_incomplete_scan_never_imports(store):
    provider, root = store
    fixtures.build_unitypackage(root / "POLYGON_FakeVillage.unitypackage")
    (root / "other-file").touch()
    provider.settings.unity_asset_store_max_scan_entries = 1
    assert provider.scan()["truncated"]
    assert provider.sync()["truncated"] and not provider.db.q("SELECT * FROM archives")


def test_staging_limits_before_copy(store, monkeypatch):
    provider, root = store
    fixtures.build_unitypackage(root / "POLYGON_FakeVillage.unitypackage")
    monkeypatch.setattr(provider, "_snapshot", lambda *_: pytest.fail("should not start copying"))
    provider.settings.unity_asset_store_max_package_bytes = 1
    assert "limit" in provider.sync()["items"][0]["error"]
    provider.settings.unity_asset_store_max_package_bytes = 1024**3
    usage = shutil.disk_usage(root)._replace(free=0)
    monkeypatch.setattr(shutil, "disk_usage", lambda *_: usage)
    assert "space" in provider.sync()["items"][0]["error"]
    assert not any(provider.settings.tmp_dir.iterdir())


def test_snapshot_checks_for_source_change(store, monkeypatch):
    provider, root = store
    pkg = fixtures.build_unitypackage(root / "POLYGON_FakeVillage.unitypackage")
    original = provider._snapshot

    def changed(item, dest):
        pkg.rename(root / "download-replaced")
        pkg.write_bytes(b"replacement")
        original(item, dest)

    monkeypatch.setattr(provider, "_snapshot", changed)
    result = provider.sync(agent=True)["items"][0]
    assert result["status"] == "failed" and str(root) not in str(result)
    assert not provider.db.q("SELECT * FROM archives")


def test_overlapping_sources_and_data_refused(store):
    provider, root = store
    provider.settings.source_roots = [root / "nested-source"]
    assert not provider.status()["available"]
    provider.settings.unity_asset_store_roots = [provider.settings.data_dir / "custom"]
    assert not provider.status()["available"]


def test_case_alias_overlap_on_case_insensitive_volume(store):
    provider, root = store
    alias = Path(str(root).swapcase())
    if not alias.exists() or not alias.samefile(root):
        pytest.skip("requires case-insensitive volume")
    provider.settings.source_roots = [alias / "nested"]
    assert not provider.status()["available"]
