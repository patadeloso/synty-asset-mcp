"""Library management: import archives (deduplicated by SHA-256), extract
them into an organised tree, and register existing folders in place."""

from __future__ import annotations

import os
import re
import shutil
import stat
from pathlib import Path

from . import archives, packs
from .config import Settings
from .db import StateDB, now
from .safepaths import confine, is_within, safe_name
from .write_lock import library_writer

ARCHIVE_SUFFIXES = (".zip", ".unitypackage")
UNITY_TREE_DIRS = ("unity", "unity_previews")
# Module-level so tests can exercise the Windows staging rule on any platform.
_WINDOWS = os.name == "nt"


class InvalidArchive(ValueError):
    """``require_valid`` import of an archive that fails its integrity check."""


class TreeConflict(RuntimeError):
    """``protect_unity_tree`` import whose pack already has ``unity/`` or
    ``unity_previews/`` content that this archive does not own."""


def _link_or_copy(src: Path, dst: Path, *, hardlink: bool = True) -> str:
    """Store an immutable copy of the original. Hard link when on the same
    filesystem (zero extra space), else a streamed copy. ``hardlink=False``
    forces the copy so ``dst`` never shares an inode with ``src``."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        return "exists"
    mode = None
    if hardlink:
        try:
            os.link(src, dst)
            mode = "hardlink"
        except OSError:
            pass
    if mode is None:
        tmp = dst.with_name(dst.name + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        mode = "copy"
    try:
        os.chmod(dst, 0o444)
    except OSError:
        pass
    return mode


def _staged_on_windows(settings: Settings, path: Path, origin: str) -> bool:
    """On Windows the read-only attribute is shared by every hard link. A staged
    temp file linked into the archive store would become read-only (breaking
    temp cleanup), and cleanup that clears the attribute to delete it would
    silently make the stored archive writable. Such files are copied instead."""
    return _WINDOWS and (origin == "unity_cache" or is_within(path, settings.tmp_dir))


def _populated(directory: Path, slug: str) -> bool:
    """True when ``directory`` holds anything. Anything that cannot be inspected,
    or is not a real directory, is a conflict rather than "empty"."""
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise TreeConflict(f"pack {slug!r}: cannot inspect {directory.name}/ ({type(exc).__name__})") from None
    if not stat.S_ISDIR(st.st_mode):
        return True
    try:
        with os.scandir(directory) as entries:
            return next(entries, None) is not None
    except OSError as exc:
        raise TreeConflict(f"pack {slug!r}: cannot inspect {directory.name}/ ({type(exc).__name__})") from None


def _guard_unity_tree(settings: Settings, db: StateDB, slug: str, archive_id: int | None) -> None:
    """Refuse when the pack's Unity tree already holds content that
    ``archive_id`` (None = an archive not yet registered) does not own."""
    pack_dir = settings.library_dir / slug
    populated = [name for name in UNITY_TREE_DIRS if _populated(pack_dir / name, slug)]
    if not populated:
        return
    unity_dest = pack_dir / "unity"
    owner = db.one("SELECT archive_id FROM trees WHERE pack_slug=? AND kind='unity' AND path=?",
                   (slug, str(unity_dest)))
    if archive_id is not None and owner and owner["archive_id"] == archive_id:
        return
    raise TreeConflict("an existing Unity tree requires owner confirmation")


@library_writer
def import_archive(
    settings: Settings,
    db: StateDB,
    path: Path,
    *,
    pack: str | None = None,
    origin: str = "manual",
    verify: bool = True,
    progress=None,
    log=print,
    require_valid: bool = False,
    protect_unity_tree: bool = False,
) -> dict:
    """Register one archive. Idempotent: the same bytes are never stored twice.

    ``require_valid``: verify before anything is registered or copied and raise
    :class:`InvalidArchive` on a corrupt archive (also when the same bytes were
    imported earlier and recorded as corrupt), instead of ``imported_but_corrupt``.

    ``protect_unity_tree``: raise :class:`TreeConflict`, before any pack/archive
    is registered or any file copied, when extracting this archive would write
    into a ``unity/`` / ``unity_previews/`` tree populated by another archive or
    by unknown files. Re-importing the archive that owns the tree is allowed.
    """
    path = Path(path).expanduser().resolve()
    if not path.is_file() or not path.name.lower().endswith(ARCHIVE_SUFFIXES):
        raise ValueError(f"not an archive (.zip/.unitypackage): {path}")
    log(f"hashing {path.name}")
    digest = archives.sha256_file(path, progress)
    existing = db.one("SELECT * FROM archives WHERE sha256=?", (digest,))
    if require_valid:
        ok, msg = archives.verify_archive(path, max_bytes=settings.max_extract_bytes, max_files=settings.max_extract_files)
        if not ok:
            raise InvalidArchive("archive failed verification")
        if existing and (existing["verified"] != "ok" or archives.sha256_file(Path(existing["path"])) != digest):
            raise ValueError("existing managed archive requires repair")
    if existing:
        if protect_unity_tree and existing["pack_slug"]:
            _guard_unity_tree(settings, db, existing["pack_slug"], existing["id"])
        return {"status": "already_imported", "archive": existing}
    if pack:
        row = packs.get_pack(db, pack)
        if row is None:
            raise KeyError(f"unknown pack {pack!r}")
        slug, score = row["slug"], 999
    else:
        slug, score = packs.match_pack(db, path.name)
    if protect_unity_tree:
        candidate = slug or (packs.inferred_pack(path.name) or {}).get("slug")
        if candidate:
            _guard_unity_tree(settings, db, candidate, None)
    if not slug:
        slug = packs.ensure_pack(db, path.name, source=f"archive:{path.name}")
        score = 1 if slug else 0
    if not require_valid:
        ok, msg = archives.verify_archive(path) if verify else (True, "not verified")
    kind = packs.guess_kind(path.name)
    version = packs.guess_version(path.name)
    dest_dir = settings.archives_dir / (slug or "_unmatched")
    stored = dest_dir / safe_name(path.name, 200)
    if stored.exists() and archives.sha256_file(stored) != digest:
        stored = dest_dir / f"{digest[:12]}_{safe_name(path.name, 180)}"
    mode = _link_or_copy(path, stored, hardlink=not _staged_on_windows(settings, path, origin))
    archive_id = db.x(
        "INSERT INTO archives(sha256, filename, path, size, pack_slug, kind, version, origin, verified, imported_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (digest, path.name, str(stored), path.stat().st_size, slug, kind, version, origin,
         "ok" if ok else f"failed: {msg}", now()),
    )
    if slug:
        real_id = packs.pack_id_from_filename(path.name)
        if real_id:
            db.x("UPDATE packs SET pack_id=?, pack_id_confirmed=1 WHERE slug=? AND pack_id_confirmed=0", (real_id, slug))
        db.x("UPDATE packs SET status='downloaded' WHERE slug=? AND status='owned_not_downloaded'", (slug,))
    return {
        "status": "imported" if ok else "imported_but_corrupt",
        "archive_id": archive_id,
        "pack": slug,
        "match_score": score,
        "kind": kind,
        "version": version,
        "sha256": digest,
        "verify": msg,
        "stored_as": mode,
        "warning": None if slug else "filename did not match an owned pack; pass pack= to assign it",
    }


def import_inbox(settings: Settings, db: StateDB, log=print) -> list[dict]:
    out = []
    for p in sorted(settings.inbox_dir.rglob("*")):
        if p.is_file() and p.name.lower().endswith(ARCHIVE_SUFFIXES):
            try:
                out.append({"file": p.name, **import_archive(settings, db, p, log=log)})
            except Exception as exc:  # noqa: BLE001 - report per file, keep going
                out.append({"file": p.name, "status": "error", "error": str(exc)})
    return out


def _register_tree(db: StateDB, slug: str, kind: str, path: Path, archive_id: int | None, external: bool) -> None:
    db.x(
        "INSERT OR REPLACE INTO trees(pack_slug, kind, path, archive_id, external, added_at) VALUES(?,?,?,?,?,?)",
        (slug, kind, str(path), archive_id, 1 if external else 0, now()),
    )


@library_writer
def extract_archive(settings: Settings, db: StateDB, archive_id: int, *, force: bool = False,
                    progress=None, log=print) -> dict:
    arc = db.one("SELECT * FROM archives WHERE id=?", (archive_id,))
    if arc is None:
        raise KeyError(f"unknown archive {archive_id}")
    if not arc["pack_slug"]:
        raise ValueError("archive is not assigned to a pack; re-import with pack=")
    if (arc["verified"] or "").startswith("failed"):
        raise ValueError(f"archive failed integrity check: {arc['verified']}")
    if arc["extracted_at"] and arc["extracted_path"] and Path(arc["extracted_path"]).exists() and not force:
        return {"status": "already_extracted", "path": arc["extracted_path"]}
    slug = arc["pack_slug"]
    src = Path(arc["path"])
    pack_dir = settings.library_dir / slug
    lim = dict(max_bytes=settings.max_extract_bytes, max_files=settings.max_extract_files)
    result: dict = {"archive_id": archive_id, "pack": slug, "trees": []}
    unity_dest = pack_dir / "unity"
    previews = pack_dir / "unity_previews"

    def unpack_unity(pkg: Path) -> None:
        log(f"unpacking unitypackage {pkg.name}")
        rep = archives.extract_unitypackage(pkg, unity_dest, previews, progress=progress, **lim)
        _register_tree(db, slug, "unity", unity_dest, archive_id, False)
        result["trees"].append({"kind": "unity", **rep.as_dict()})

    if src.name.lower().endswith(".unitypackage"):
        unpack_unity(src)
        extracted = unity_dest
    else:
        dest = pack_dir / f"{arc['kind'] or 'files'}_{archive_id}"
        if force and dest.exists():
            archives.remove_tree(dest)
        tmp = settings.tmp_dir / f"arc_{archive_id}"
        archives.remove_tree(tmp)
        log(f"extracting {src.name}")
        rep = archives.extract_zip(src, dest, divert_dir=tmp, progress=progress, **lim)
        nested = sorted(rep.nested_unitypackages)
        # Several render-pipeline variants may ship together; one is enough
        # for meshes/textures. Prefer Built-in, then URP.
        nested.sort(key=lambda p: (0 if "built" in p.lower() else 1 if "urp" in p.lower() else 2, p))
        for pkg in nested[:1]:
            unpack_unity(Path(pkg))
        if len(nested) > 1:
            result["skipped_unitypackages"] = [Path(p).name for p in nested[1:]]
        archives.remove_tree(tmp)
        if rep.files:
            kind = archives.classify_tree(dest)
            _register_tree(db, slug, kind, dest, archive_id, False)
            result["trees"].append({"kind": kind, **rep.as_dict()})
        elif dest.exists() and not any(dest.iterdir()):
            dest.rmdir()
        extracted = dest if rep.files else unity_dest
    db.x("UPDATE archives SET extracted_path=?, extracted_at=? WHERE id=?", (str(extracted), now(), archive_id))
    db.x("UPDATE packs SET status='extracted' WHERE slug=? AND status IN ('owned_not_downloaded','downloaded')", (slug,))
    result["status"] = "extracted"
    return result


def register_folder(settings: Settings, db: StateDB, path: Path, pack: str | None = None) -> dict:
    """Use an already-extracted folder in place (read-only). It must be inside a
    configured ``source_roots`` entry so the server never reads arbitrary paths."""
    path = confine(path, settings.source_roots + [settings.library_dir], must_exist=True)
    if pack:
        row = packs.get_pack(db, pack)
        if row is None:
            raise KeyError(f"unknown pack {pack!r}")
        slug = row["slug"]
    else:
        names = [path.name, path.parent.name] + [p.name for p in path.glob("MaterialList*.txt")]
        slug = None
        best = 0
        for n in names:
            s, score = packs.match_pack(db, n)
            if s and score > best:
                slug, best = s, score
        if not slug:
            # Source Files ship MaterialList_<PackName>.txt: "POLYGONCityCharacters" -> POLYGON_City_Characters
            for ml in path.glob("MaterialList*.txt"):
                raw = ml.stem.split("_", 1)[-1]
                name = "_".join(re.findall(r"[A-Z]+(?=[A-Z][a-z])|[A-Z][a-z]+|[A-Z]+|\d+", raw))
                slug = packs.ensure_pack(db, name, source=f"folder:{ml.name}")
                if slug:
                    break
        if not slug:
            raise ValueError("could not infer the pack from the folder; pass pack=")
    kind = archives.classify_tree(path)
    _register_tree(db, slug, kind, path, None, True)
    db.x("UPDATE packs SET status='extracted' WHERE slug=? AND status IN ('owned_not_downloaded','downloaded')", (slug,))
    return {"status": "registered", "pack": slug, "kind": kind, "path": str(path), "read_only": True}
