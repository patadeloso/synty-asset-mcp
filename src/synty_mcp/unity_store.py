"""Offline discovery of owner-selected Unity Asset Store caches (never served or written)."""
from __future__ import annotations

import hmac
import os
import re
import secrets
import shutil
import stat
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory

from . import inventory, library, packs
from .config import Settings
from .db import StateDB
from .write_lock import library_write_lock

MAX_DEPTH = 32
WINDOWS_UNSUPPORTED = "Unity Asset Store cache ingestion is not currently supported on Windows."


def _ref_key(settings: Settings) -> bytes:
    """One private key per installation. Publish it atomically across CLI/server starts."""
    path = settings.secrets_dir / "unity-cache-id.key"
    try:
        if not path.exists():
            with library_write_lock(settings):
                if not path.exists():
                    with NamedTemporaryFile(dir=settings.secrets_dir, delete=False) as f:
                        tmp = Path(f.name)
                        f.write(secrets.token_bytes(32))
                    try:
                        tmp.replace(path)
                    finally:
                        tmp.unlink(missing_ok=True)
        key = path.read_bytes()
        if len(key) == 32:
            return key
    except OSError:
        pass
    raise ValueError("cannot initialize private Unity cache IDs")


def _file_id(path: Path):
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        return None
    return st.st_dev, st.st_ino


def _overlap(a: Path, b: Path) -> bool:
    # AI: Also catch aliases on case-insensitive volumes without assuming every volume is one.
    if a.is_relative_to(b) or b.is_relative_to(a):
        return True
    aid, bid = _file_id(a), _file_id(b)
    return ((aid is not None and any(_file_id(p) == aid for p in (b, *b.parents))) or
            (bid is not None and any(_file_id(p) == bid for p in a.parents)))


def _name_key(stem: str) -> str:
    return re.sub(r"[\s_-]+", "_", stem.casefold())


@dataclass(frozen=True)
class Item:
    path: Path
    root_number: int
    stat: os.stat_result
    pack: str | None
    exact: bool
    archive_id: int | None
    failed: bool
    ref: str

    def public(self) -> dict:
        return {"id": self.ref, "filename": self.path.name, "root": self.root_number,
                "size_bytes": self.stat.st_size, "pack": self.pack, "archive_id": self.archive_id,
                "match": "exact" if self.exact else "tentative" if self.pack else "unmatched"}

    @property
    def fingerprint(self) -> tuple:
        return str(self.path), self.stat.st_size, self.stat.st_mtime_ns


class UnityStore:
    def __init__(self, settings: Settings, db: StateDB):
        self.settings = settings
        self.db = db
        self._key = _ref_key(settings) if settings.unity_asset_store_enabled and settings.unity_cache_supported else None

    def status(self) -> dict:
        settings = self.settings
        if not settings.unity_cache_supported:
            return {"enabled": settings.unity_asset_store_enabled, "available": False, "reason": WINDOWS_UNSUPPORTED,
                    "roots": [{"root": n, "status": "unsupported"} for n in range(1, len(settings.unity_asset_store_roots) + 1)]}
        guarded = [settings.data_dir, settings.state_db, *settings.write_roots, *settings.source_roots]
        roots = []
        for n, root in enumerate(settings.unity_asset_store_roots, 1):
            state = "disabled"
            if settings.unity_asset_store_enabled:
                try:
                    if any(_overlap(p, root) for p in guarded):
                        state = "overlaps managed storage"
                    elif root != root.resolve() or not root.is_dir():
                        state = "missing or not a directory"
                    else:
                        state = "ready"
                except (OSError, RuntimeError):
                    state = "cannot access directory"
            roots.append({"root": n, "status": state})
        return {"enabled": settings.unity_asset_store_enabled, "available": any(r["status"] == "ready" for r in roots),
                "roots": roots}

    def _candidates(self, report: dict):
        remaining = self.settings.unity_asset_store_max_scan_entries
        for info, root in zip(report["roots"], self.settings.unity_asset_store_roots, strict=True):
            if info["status"] != "ready":
                continue
            dirs = [(root, 0)]
            while dirs:
                folder, depth = dirs.pop()
                try:
                    if folder.resolve() != folder:
                        continue
                    with os.scandir(folder) as entries:
                        for entry in entries:
                            if remaining <= 0:
                                report["truncated"] = True
                                return
                            remaining -= 1
                            if entry.is_symlink():
                                continue
                            path = Path(entry.path)
                            if entry.is_dir(follow_symlinks=False):
                                if depth < MAX_DEPTH:
                                    dirs.append((path, depth + 1))
                                else:
                                    report["truncated"] = True
                            elif path.suffix.lower() == ".unitypackage":
                                st = entry.stat(follow_symlinks=False)
                                if stat.S_ISREG(st.st_mode) and path.resolve() == path:
                                    yield info["root"], path, st
                except (OSError, RuntimeError):
                    info["status"] = "could not read all entries"

    def _discover(self) -> tuple[dict, list[Item]]:
        report = {**self.status(), "truncated": False}
        if not report["available"]:
            return report, []
        if self._key is None:  # AI: settings enabled after service construction
            self._key = _ref_key(self.settings)
        known = defaultdict(list)
        for row in self.db.q("SELECT slug, pack_id FROM packs"):
            known[_name_key(row["pack_id"])].append(row["slug"])
        items = {}
        for root_number, path, st in self._candidates(report):
            if path in items:
                continue
            args = (str(path), st.st_size, st.st_mtime_ns)
            where = " WHERE source_path=? AND size_bytes=? AND mtime_ns=?"
            previous = self.db.one("SELECT archive_id FROM unity_store_imports" + where, args)
            failed = self.db.one("SELECT 1 FROM unity_store_failures" + where, args)
            exact = known.get(_name_key(path.stem), [])
            slug = exact[0] if len(exact) == 1 else packs.match_pack(self.db, path.name)[0]
            stamp = os.fsencode(path) + f"\0{st.st_size}:{st.st_mtime_ns}".encode()
            ref = hmac.digest(self._key, stamp, "sha256").hex()
            items[path] = Item(path, root_number, st, slug, len(exact) == 1,
                               previous["archive_id"] if previous else None, bool(failed), ref)
        return report, [items[p] for p in sorted(items)]

    def _visible(self, item: Item, agent: bool) -> bool:
        return not agent or item.exact or self.settings.unity_asset_store_show_unmatched

    def scan(self, *, agent: bool = False) -> dict:
        report, items = self._discover()
        shown = [i.public() for i in items if self._visible(i, agent)]
        return {**report, "items": shown, "hidden_count": len(items) - len(shown)}

    def import_package(self, ref: str, *, allow_path: bool = False, allow_tree_update: bool = False,
                       retry: bool = False) -> dict:
        # AI: Never open a caller-supplied path. The local CLI may select by a discovered path;
        # an MCP client needs the opaque capability from a scan or handed to it by the owner.
        if not self.settings.unity_cache_supported:
            return {"status": "failed", "error": WINDOWS_UNSUPPORTED}
        if not allow_path and not re.fullmatch("[a-f0-9]{64}", ref):
            raise ValueError("expected a Unity cache scan ID")
        if not self.settings.unity_asset_store_enabled:
            raise ValueError("Unity cache provider is disabled")
        with library_write_lock(self.settings):
            _, items = self._discover()
            for item in items:
                if ref == item.ref or (allow_path and ref == str(item.path)):
                    return self._import(item, allow_tree_update=allow_tree_update, retry=retry)
        raise ValueError("package not discovered; scan configured Unity cache roots again")

    def sync(self, *, agent: bool = False) -> dict:
        if not self.settings.unity_cache_supported or not self.settings.unity_asset_store_enabled:
            return {**self.status(), "items": [], "truncated": False, "hidden_count": 0}
        with library_write_lock(self.settings):
            report, items = self._discover()
            # AI: Never act on an incomplete preview. A second exact candidate in another
            # publisher's folder also requires the owner to choose which one to import.
            if report["truncated"]:
                return {**report, "items": [], "error": "scan limit reached; narrow the configured roots"}
            counts = Counter(i.pack for i in items if i.exact and not i.archive_id)
            results = []
            hidden = 0
            for item in items:
                if not self._visible(item, agent):
                    hidden += 1
                    continue
                if item.archive_id or (item.exact and counts[item.pack] == 1):
                    results.append(self._import(item))
                else:
                    results.append({**item.public(), "status": "skipped_ambiguous" if item.pack else "skipped_unmatched"})
            return {**report, "items": results, "hidden_count": hidden}

    @staticmethod
    def _snapshot(item: Item, dest: Path) -> None:
        # AI: Do not stream until EOF: even a file growing concurrently cannot exceed the preflight size.
        def unchanged(st):
            return (stat.S_ISREG(st.st_mode) and os.path.samestat(st, item.stat)
                    and st.st_size == item.stat.st_size and st.st_mtime_ns == item.stat.st_mtime_ns)

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(item.path, flags), "rb") as src:
            if item.path.resolve() != item.path or not unchanged(os.fstat(src.fileno())):
                raise ValueError("cache entry changed")
            with dest.open("xb") as out:
                remaining = item.stat.st_size
                while remaining:
                    chunk = src.read(min(remaining, 1024 * 1024))
                    if not chunk:
                        raise ValueError("cache entry changed")
                    out.write(chunk)
                    remaining -= len(chunk)
            if not unchanged(os.fstat(src.fileno())) or not unchanged(item.path.lstat()) or item.path.resolve() != item.path:
                raise ValueError("cache entry changed")

    def _import(self, item: Item, *, allow_tree_update: bool = False, retry: bool = False) -> dict:
        result = item.public()
        if item.archive_id and not allow_tree_update:
            return {**result, "status": "already_imported"}
        if item.failed and not retry:
            return {**result, "status": "failed_unchanged", "error": "previous verification failed"}
        if item.stat.st_size > self.settings.unity_asset_store_max_package_bytes:
            return {**result, "status": "failed", "error": "package exceeds staging limit"}
        stage = "reading cache entry (check permissions or scan again)"
        quiet = lambda *_: None
        try:
            if shutil.disk_usage(self.settings.tmp_dir).free < item.stat.st_size:
                return {**result, "status": "failed", "error": "insufficient staging space"}
            with TemporaryDirectory(dir=self.settings.tmp_dir) as tmp:
                staged = Path(tmp) / item.path.name
                self._snapshot(item, staged)
                stage = "archive import"
                imported = library.import_archive(self.settings, self.db, staged, pack=item.pack, origin="unity_cache",
                                                  log=quiet, require_valid=True, protect_unity_tree=not allow_tree_update)
            arc_id = imported.get("archive_id") or imported["archive"]["id"]
            arc = self.db.one("SELECT pack_slug FROM archives WHERE id=?", (arc_id,))
            if slug := arc["pack_slug"]:
                stage = "archive extraction"
                library.extract_archive(self.settings, self.db, arc_id, force=allow_tree_update, log=quiet)
                stage = "pack indexing"
                inventory.scan_pack(self.settings, self.db, slug, log=quiet)
            stage = "recording completed import"
            with self.db.tx() as con:
                con.execute("INSERT OR REPLACE INTO unity_store_imports(source_path,size_bytes,mtime_ns,archive_id) "
                            "VALUES(?,?,?,?)", (*item.fingerprint, arc_id))
                con.execute("DELETE FROM unity_store_failures WHERE source_path=?", (str(item.path),))
            return {**result, "status": imported["status"], "archive_id": arc_id, "pack": slug}
        except library.TreeConflict:
            return {**result, "status": "requires_tree_confirmation", "error": "populated pack tree; confirm at the CLI"}
        except library.InvalidArchive:
            self.db.x("INSERT OR REPLACE INTO unity_store_failures VALUES(?,?,?)", item.fingerprint)
            return {**result, "status": "failed", "error": "archive verification failed"}
        except Exception as exc:
            return {**result, "status": "failed", "error": f"{stage} failed ({type(exc).__name__})"}
