"""Defensive regressions for the archive layer shared by cache, CLI and MCP writers."""
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import fixtures
import pytest

from synty_mcp import library, write_lock
from synty_mcp.write_lock import LibraryWriteLockTimeout, library_write_lock


def test_shared_lock_reentry_and_process_exit(settings):
    # AI: Use the same Python installation as pytest. No platform-specific lock utilities.
    child = """
import sys
from pathlib import Path
from types import SimpleNamespace
from synty_mcp.write_lock import library_write_lock
s = SimpleNamespace(library_dir=Path(sys.argv[1]))
with library_write_lock(s), library_write_lock(s, timeout=0):
    print('ready', flush=True)
    sys.stdin.read()
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(write_lock.__file__).resolve().parents[1])
    with subprocess.Popen([sys.executable, "-c", child, str(settings.library_dir)], env=env, text=True,
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE) as proc:
        try:
            assert proc.stdout.readline().strip() == "ready"
            with pytest.raises(LibraryWriteLockTimeout), library_write_lock(settings, timeout=0.1):
                pytest.fail("two processes admitted together")
        finally:
            proc.kill()
            proc.wait(10)
    with library_write_lock(settings, timeout=1):
        pass


@pytest.mark.parametrize("subdir", ["unity", "unity_previews"])
def test_unregistered_tree_checked_before_inferring_new_pack(svc, subdir):
    pkg = fixtures.build_unitypackage(svc.settings.inbox_dir / "POLYGON_NewExample.unitypackage")
    stray = svc.settings.library_dir / "polygon-newexample" / subdir / "keep.txt"
    stray.parent.mkdir(parents=True)
    stray.write_text("keep")
    with pytest.raises(library.TreeConflict):
        library.import_archive(svc.settings, svc.db, pkg, protect_unity_tree=True)
    assert not svc.db.one("SELECT slug FROM packs WHERE slug='polygon-newexample'")
    assert not svc.db.q("SELECT * FROM archives") and stray.read_text() == "keep"


def test_tree_guard_also_checks_archive_dedup_hits(svc):
    a = fixtures.build_unitypackage(svc.settings.inbox_dir / "POLYGON_NewExample.unitypackage")
    b = fixtures.build_unitypackage(svc.settings.inbox_dir / "POLYGON_NewExample_v2.unitypackage", previews=False)
    first = library.import_archive(svc.settings, svc.db, a)
    library.import_archive(svc.settings, svc.db, b)
    library.extract_archive(svc.settings, svc.db, first["archive_id"])
    with pytest.raises(library.TreeConflict):
        library.import_archive(svc.settings, svc.db, b, protect_unity_tree=True)
    assert library.import_archive(svc.settings, svc.db, a, protect_unity_tree=True)["status"] == "already_imported"


def test_strict_verification_checks_gzip_footer_and_legacy_dedup(svc):
    pkg = fixtures.build_unitypackage(svc.settings.inbox_dir / "POLYGON_NewExample.unitypackage")
    pkg.write_bytes(pkg.read_bytes()[:-4])  # AI: tar end marker is intact, gzip footer is not
    with pytest.raises(library.InvalidArchive):
        library.import_archive(svc.settings, svc.db, pkg, require_valid=True)
    assert not svc.db.one("SELECT slug FROM packs WHERE slug='polygon-newexample'")
    assert not any(svc.settings.archives_dir.iterdir())
    library.import_archive(svc.settings, svc.db, pkg, verify=False)
    with pytest.raises(library.InvalidArchive):
        library.import_archive(svc.settings, svc.db, pkg, require_valid=True)


def test_staged_windows_copy_stays_readonly_after_cleanup(svc, monkeypatch):
    # AI: Check the archive-layer safeguard independently of platform support for the cache provider.
    monkeypatch.setattr(library, "_WINDOWS", True)
    stage = svc.settings.tmp_dir / "staging"
    pkg = fixtures.build_unitypackage(stage / "POLYGON_NewExample.unitypackage")
    res = library.import_archive(svc.settings, svc.db, pkg, origin="unity_cache")
    stored = Path(svc.db.one("SELECT path FROM archives WHERE id=?", (res["archive_id"],))["path"])
    assert not os.path.samefile(pkg, stored)
    shutil.rmtree(stage)
    assert stored.is_file() and not stat.S_IMODE(stored.stat().st_mode) & 0o222
