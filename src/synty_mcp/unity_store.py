"""Offline discovery of owner-selected Unity Asset Store caches (never served or written)."""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from . import inventory, library, packs
from .config import Settings
from .db import StateDB


@dataclass(frozen=True)
class Item:
    path: Path
    root_number: int
    stat: os.stat_result
    pack: str | None
    archive_id: int | None

    @property
    def ref(self) -> str:
        stamp = f"\0{self.stat.st_size}:{self.stat.st_mtime_ns}".encode()
        return hashlib.sha256(os.fsencode(self.path) + stamp).hexdigest()

    def public(self) -> dict:
        return {"id": self.ref, "filename": self.path.name, "root": self.root_number,
                "size_bytes": self.stat.st_size, "mtime_ns": self.stat.st_mtime_ns,
                "pack": self.pack, "archive_id": self.archive_id}


class UnityStore:
    def __init__(self, settings: Settings, db: StateDB):
        self.settings = settings
        self.db = db
        self._writes = threading.Lock()

    def status(self) -> dict:
        settings = self.settings
        enabled = settings.unity_asset_store_enabled
        roots = []
        for n, root in enumerate(settings.unity_asset_store_roots, 1):
            state = "disabled"
            if enabled:
                if any(p.is_relative_to(root) or root.is_relative_to(p)
                       for p in [*settings.write_roots, settings.state_db]):
                    state = "overlaps managed storage"
                elif root != root.resolve() or not root.is_dir():
                    state = "missing or not a directory"
                else:
                    state = "ready"
            roots.append({"root": n, "status": state})
        return {"enabled": enabled, "available": any(r["status"] == "ready" for r in roots), "roots": roots}

    def _discover(self) -> tuple[dict, list[Item]]:
        report = self.status()
        seen = set()
        items = []
        for info, root in zip(report["roots"], self.settings.unity_asset_store_roots, strict=True):
            if info["status"] != "ready":
                continue

            def unreadable(_exc, info=info):
                info["status"] = "could not read all entries"

            for folder, dirs, files in os.walk(root, onerror=unreadable, followlinks=False):
                parent = Path(folder)
                if parent.resolve() != parent:
                    dirs.clear()
                    continue
                dirs.sort()
                for name in sorted(files):
                    path = parent / name
                    if not name.lower().endswith(".unitypackage") or path in seen:
                        continue
                    try:
                        st = path.lstat()
                    except OSError as exc:
                        unreadable(exc)
                        continue
                    if not stat.S_ISREG(st.st_mode):
                        continue
                    seen.add(path)
                    previous = self.db.one("SELECT archive_id FROM unity_store_imports "
                                           "WHERE source_path=? AND size_bytes=? AND mtime_ns=?",
                                           (str(path), st.st_size, st.st_mtime_ns))
                    slug, _ = packs.match_pack(self.db, name)
                    items.append(Item(path, info["root"], st, slug, previous["archive_id"] if previous else None))
        return report, items

    def scan(self) -> dict:
        report, items = self._discover()
        return {**report, "items": [i.public() for i in items]}

    def import_package(self, ref: str) -> dict:
        # Never open a caller-supplied path: select only from this live scan.
        with self._writes:
            report, items = self._discover()
            if not report["enabled"]:
                raise ValueError("Unity cache provider is disabled")
            for item in items:
                if ref in (item.ref, str(item.path)):
                    return self._import(item)
        raise ValueError("package not discovered; scan configured Unity cache roots again")

    def sync(self) -> dict:
        with self._writes:
            report, items = self._discover()
            results = [self._import(i) if i.pack or i.archive_id else {**i.public(), "status": "skipped_unmatched"}
                       for i in items]
            return {**report, "items": results}

    @staticmethod
    def _snapshot(item: Item, dest: Path) -> None:
        # The generic importer may hard-link and chmod its input. Feed it a private
        # snapshot, never the cache inode. Check the opened file before reading any bytes.
        def unchanged(st):
            return (stat.S_ISREG(st.st_mode) and os.path.samestat(st, item.stat)
                    and st.st_size == item.stat.st_size and st.st_mtime_ns == item.stat.st_mtime_ns)

        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(item.path, flags), "rb") as src:
            if item.path.resolve() != item.path or not unchanged(os.fstat(src.fileno())):
                raise ValueError("cache entry changed")
            with dest.open("xb") as out:
                shutil.copyfileobj(src, out, length=1024 * 1024)
            if not unchanged(os.fstat(src.fileno())):
                raise ValueError("cache entry changed")

    def _import(self, item: Item) -> dict:
        result = item.public()
        if item.archive_id:
            return {**result, "status": "already_imported"}
        stage = "reading cache entry (check permissions or scan again)"
        quiet = lambda *_: None
        try:
            with TemporaryDirectory(dir=self.settings.tmp_dir) as tmp:
                staged = Path(tmp) / item.path.name
                self._snapshot(item, staged)
                stage = "archive import"
                imported = library.import_archive(self.settings, self.db, staged, pack=item.pack,
                                                  origin="unity_cache", log=quiet)
            arc_id = imported.get("archive_id") or imported["archive"]["id"]
            arc = self.db.one("SELECT pack_slug, verified FROM archives WHERE id=?", (arc_id,))
            if arc["verified"] != "ok":
                return {**result, "status": "failed", "error": "archive verification failed"}
            if slug := arc["pack_slug"]:
                stage = "archive extraction"
                library.extract_archive(self.settings, self.db, arc_id, log=quiet)
                stage = "pack indexing"
                inventory.scan_pack(self.settings, self.db, slug, log=quiet)
            stage = "recording completed import"
            self.db.x("INSERT OR REPLACE INTO unity_store_imports(source_path,size_bytes,mtime_ns,archive_id) "
                      "VALUES(?,?,?,?)", (str(item.path), item.stat.st_size, item.stat.st_mtime_ns, arc_id))
            return {**result, "status": imported["status"], "archive_id": arc_id, "pack": slug}
        except Exception as exc:
            # Continue bulk imports and do not return exceptions containing local paths or package contents.
            return {**result, "status": "failed", "error": f"{stage} failed ({type(exc).__name__})"}
