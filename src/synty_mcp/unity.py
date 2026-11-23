"""Read just enough Unity YAML to convert assets outside Unity:

* GUID index of a tree (``*.meta``),
* prefab -> source model (+ the node name inside a bundle FBX),
* material -> albedo texture,
* Synty ``MaterialList_*.txt`` (shipped with Source Files) -> per-mesh textures.

Text-level parsing only; no Unity Editor, no binary formats.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

GUID_RE = re.compile(r"^guid:\s*([0-9a-fA-F]{32})\s*$", re.MULTILINE)
REF_RE = re.compile(r"\{fileID:\s*(-?\d+),\s*guid:\s*([0-9a-fA-F]{32})")
MESH_REF_RE = re.compile(r"m_Mesh:\s*\{fileID:\s*(-?\d+),\s*guid:\s*([0-9a-fA-F]{32})")
SOURCE_PREFAB_RE = re.compile(r"m_SourcePrefab:\s*\{fileID:\s*(-?\d+),\s*guid:\s*([0-9a-fA-F]{32})")
MATERIAL_REF_RE = re.compile(r"-\s*\{fileID:\s*2100000,\s*guid:\s*([0-9a-fA-F]{32})")
MODEL_EXTS = {".fbx", ".obj", ".gltf", ".glb", ".blend", ".dae"}
TEXTURE_EXTS = {".png", ".tga", ".jpg", ".jpeg", ".psd", ".tif", ".tiff", ".exr"}
ALBEDO_KEYS = ("_basemap", "_maintex", "_albedo", "_base_texture", "_basecolormap", "_diffuse", "_texture", "_color")


def read_guid(meta: Path) -> str | None:
    try:
        m = GUID_RE.search(meta.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(1).lower() if m else None


def index_guids(root: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    for meta in root.rglob("*.meta"):
        g = read_guid(meta)
        if g:
            out[g] = meta.with_name(meta.name[:-5])
    return out


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def model_mesh_names(model: Path) -> dict[str, str]:
    """fileID -> object name for meshes inside a model, from its .meta
    (``internalIDToNameTable`` on Unity 2019+, ``fileIDToRecycleName`` before)."""
    text = _read(Path(str(model) + ".meta"))
    names: dict[str, str] = {}
    # Legacy: fileIDToRecycleName:\n    4300000: SM_Name
    legacy = re.search(r"fileIDToRecycleName:\n((?:\s+-?\d+:.*\n)+)", text)
    if legacy:
        for line in legacy.group(1).splitlines():
            k, _, v = line.strip().partition(":")
            names[k.strip()] = v.strip()
    # Modern: - first:\n      43: 4300000\n    second: SM_Name
    for m in re.finditer(r"-\s*first:\s*\n\s*(-?\d+):\s*(-?\d+)\s*\n\s*second:\s*(.+)", text):
        names[m.group(2)] = m.group(3).strip()
    return names


DOC_RE = re.compile(r"^--- !u!(\d+) &(-?\d+)", re.MULTILINE)
GAMEOBJECT_REF_RE = re.compile(r"m_GameObject:\s*\{fileID:\s*(-?\d+)\}")


def _documents(text: str) -> list[tuple[str, str, str]]:
    """(class id, file id, body) for each object of a Unity YAML file."""
    marks = list(DOC_RE.finditer(text))
    return [(m.group(1), m.group(2), text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)])
            for i, m in enumerate(marks)]


def _renderers(text: str) -> tuple[list[str], bool]:
    """Bodies of the mesh-carrying objects of a prefab (shown ones first) and
    whether any of them is switched off.

    Synty character prefabs embed every character of the pack's shared
    ``Characters.fbx`` and switch all but one GameObject off, so document
    order alone would pick an arbitrary (inactive) character.
    """
    docs = _documents(text)
    active = {fid: not re.search(r"^\s*m_IsActive:\s*0\s*$", body, re.MULTILINE)
              for cls, fid, body in docs if cls == "1"}
    # MeshFilter (mesh) and MeshRenderer (materials) are separate objects of one GameObject.
    parts: dict[str, list[str]] = {}
    disabled: set[str] = set()
    for cls, _fid, body in docs:
        go = GAMEOBJECT_REF_RE.search(body)
        if cls == "1" or not go:
            continue
        parts.setdefault(go.group(1), []).append(body)
        if cls in ("23", "137") and re.search(r"^\s*m_Enabled:\s*0\s*$", body, re.MULTILINE):
            disabled.add(go.group(1))  # MeshRenderer / SkinnedMeshRenderer switched off
    out = []
    for go, bodies in parts.items():
        body = "\n".join(bodies)
        if MESH_REF_RE.search(body):
            out.append((not active.get(go, True) or go in disabled, body))
    out.sort(key=lambda r: r[0])  # stable: keeps document order among equals
    return [body for _hidden, body in out], any(hidden for hidden, _body in out)


def prefab_model(prefab: Path, guids: dict[str, Path], depth: int = 0) -> tuple[Path | None, str | None]:
    """(model file, node name inside it) that a prefab renders, or (None, None)."""
    text = _read(prefab)
    if not text:
        return None, None
    sources = ["\n".join(_renderers(text)[0]) or text, text]
    for rx, src in zip((MESH_REF_RE, SOURCE_PREFAB_RE), sources):
        for m in rx.finditer(src):
            file_id, guid = m.group(1), m.group(2).lower()
            target = guids.get(guid)
            if target is None:
                continue
            if target.suffix.lower() in MODEL_EXTS:
                node = model_mesh_names(target).get(file_id)
                return target, node
            if target.suffix.lower() == ".prefab" and depth < 4:
                found = prefab_model(target, guids, depth + 1)
                if found[0]:
                    return found
    return None, None


def prefab_material_paths(prefab: Path, guids: dict[str, Path]) -> list[Path]:
    out: list[Path] = []
    text = _read(prefab)
    renderers, some_hidden = _renderers(text)
    # Only the shown renderer's materials when the prefab carries switched-off variants.
    for g in MATERIAL_REF_RE.findall(renderers[0] if some_hidden else text):
        p = guids.get(g.lower())
        if p is not None and p.suffix.lower() == ".mat":
            out.append(p)
    return out


def prefab_mesh_material_paths(prefab: Path, guids: dict[str, Path]) -> dict[str, list[Path | None]]:
    """Resolve renderer slots by model mesh name where Unity metadata identifies it."""
    renderers, some_hidden = _renderers(_read(prefab))
    result: dict[str, list[Path | None]] = {}
    for body in renderers[:1] if some_hidden else renderers:
        mesh = MESH_REF_RE.search(body)
        model = guids.get(mesh.group(2).lower()) if mesh else None
        if not model:
            continue
        name = model_mesh_names(model).get(mesh.group(1))
        if not name:
            continue
        slots = []
        for guid in MATERIAL_REF_RE.findall(body):
            path = guids.get(guid.lower())
            slots.append(path if path and path.suffix.lower() == ".mat" else None)
        if slots and name not in result:
            result[name] = slots
    return result


def material_albedo(mat: Path, guids: dict[str, Path]) -> Path | None:
    """The base-colour texture a Unity .mat samples (URP/Built-in/Synty shaders)."""
    text = _read(mat)
    m = re.search(r"m_TexEnvs:\n(.*?)(?:\n\s{4}m_Ints:|\n\s{4}m_Floats:|\Z)", text, re.S)
    block = m.group(1) if m else text
    entries: list[tuple[str, str]] = []
    current = None
    for line in block.splitlines():
        km = re.match(r"\s*-\s*([A-Za-z0-9_]+):\s*$", line)
        if km:
            current = km.group(1)
            continue
        tm = re.search(r"m_Texture:\s*\{fileID:\s*-?\d+,\s*guid:\s*([0-9a-fA-F]{32})", line)
        if tm and current:
            entries.append((current.lower(), tm.group(1).lower()))
    # AI: Only explicitly colour-like slots are safe; normal and emission maps are not albedo.
    for key in ALBEDO_KEYS:
        for name, g in entries:
            if key in name and "normal" not in name and "emiss" not in name:
                p = guids.get(g)
                if p is not None and p.suffix.lower() in TEXTURE_EXTS and p.is_file():
                    return p
    return None


def material_properties(mat: Path, guids: dict[str, Path]) -> dict:
    """Extract documented Unity Standard/URP inputs without guessing custom shader semantics."""
    text = _read(mat)
    floats = {m.group(1).lower(): float(m.group(2)) for m in re.finditer(
        r"^\s*-\s*(_\w+):\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*$", text, re.M)}
    colors = {}
    for m in re.finditer(r"^\s*-\s*(_\w+):\s*\{([^}]+)\}", text, re.M):
        values = dict(re.findall(r"([rgba]):\s*([-+\d.eE]+)", m.group(2)))
        if all(k in values for k in "rgb"):
            try:
                colors[m.group(1).lower()] = [float(values[k]) for k in "rgb"] + [float(values.get("a", 1))]
            except ValueError:
                pass
    props = {}
    for key in ("_basecolor", "_color"):
        if key in colors:
            props["base_color"] = colors[key]
            break
    if "_emissioncolor" in colors:
        props["emission"] = colors["_emissioncolor"][:3]
    if "_metallic" in floats:
        props["metallic"] = max(0.0, min(1.0, floats["_metallic"]))
    if "_roughness" in floats:
        props["roughness"] = max(0.0, min(1.0, floats["_roughness"]))
    else:
        for key in ("_smoothness", "_glossiness"):
            if key in floats:
                props["roughness"] = 1.0 - max(0.0, min(1.0, floats[key]))
                break
    if floats.get("_alphaclip", 0) > 0 or floats.get("_mode") == 1 or "_ALPHATEST_ON" in text:
        props["alpha_mode"] = "MASK"
        props["alpha_cutoff"] = max(0.0, min(1.0, floats.get("_cutoff", 0.5)))
    elif floats.get("_surface", 0) == 1 or floats.get("_mode") in (2, 3):
        props["alpha_mode"] = "BLEND"
    else:
        props["alpha_mode"] = "OPAQUE"
    texture = material_albedo(mat, guids)
    entries = re.findall(r"^\s*-\s*(_\w+):\s*\n\s*m_Texture:\s*\{([^}]*)\}", text, re.M)
    color_entries = [(name, ref) for name, ref in entries
                     if any(key in name.lower() for key in ALBEDO_KEYS)
                     and "normal" not in name.lower() and "emiss" not in name.lower()]
    refs = [re.search(r"guid:\s*([0-9a-fA-F]{32})", ref) for _, ref in color_entries]
    unresolved = next((m.group(1).lower() for m in refs if m and not texture), None)
    state = "resolved" if texture else "missing" if unresolved else "untextured" if color_entries else "unknown"
    unsupported = [name for name, ref in entries if "guid:" in ref and
                   any(key in name.lower() for key in ("normal", "bump", "emiss", "metallic"))]
    return {"properties": props, "texture": str(texture) if texture else None, "state": state,
            "unsupported_maps": unsupported,
            "expected_texture": f"GUID:{unresolved}" if unresolved else None,
            "shader_approximation": "Unity shader approximated using supported PBR inputs"}


# --- Source Files -------------------------------------------------------------

_ML_PREFAB = re.compile(r"^\s*Prefab Name:\s*(.+?)\s*$")
_ML_MESH = re.compile(r"^\s*Mesh Name:\s*(.+?)\s*$")
_ML_SLOT = re.compile(r"^\s*Slot:\s*(.+?)(?:\s*\((.+?)\))?\s*$")
_ML_ALBEDO = re.compile(r"^\s*Albedo:\s*(.+?)\s*\([^)]*\)\s*$")


def parse_material_list_meshes(path: Path) -> dict[str, dict[str, list[dict]]]:
    """Read compact or expanded material records, preserving per-mesh slots."""
    prefabs: dict[str, dict[str, list[dict]]] = {}
    prefab = None
    mesh = None
    for line in _read(path).splitlines():
        pm = _ML_PREFAB.match(line)
        if pm:
            prefab, mesh = pm.group(1), None
            prefabs.setdefault(prefab, {})
            continue
        mm = _ML_MESH.match(line)
        if mm and prefab:
            mesh = mm.group(1)
            prefabs[prefab].setdefault(mesh, [])
            continue
        sm = _ML_SLOT.match(line)
        if sm and prefab and mesh:
            hint = sm.group(2) or ""
            custom = hint.lower() == "uses custom shader"
            slot = {"material": sm.group(1), "texture": "" if custom else hint}
            if custom:
                slot["custom_shader"] = True
            prefabs[prefab][mesh].append(slot)
            continue
        am = _ML_ALBEDO.match(line)
        if am and prefab and mesh and prefabs[prefab][mesh]:
            prefabs[prefab][mesh][-1]["texture"] = Path(am.group(1)).stem
        if line.strip().lower() == "no textures assigned" and prefab and mesh and prefabs[prefab][mesh]:
            prefabs[prefab][mesh][-1]["untextured"] = True
        extra = re.match(r"^\s*(Normal|Emission|Metallic):\s*(.+)$", line, re.I)
        if extra and prefab and mesh and prefabs[prefab][mesh]:
            prefabs[prefab][mesh][-1].setdefault("unsupported_maps", []).append(extra.group(1).lower())
    return prefabs


def parse_material_list(path: Path) -> dict[str, list[dict]]:
    """Asset/mesh lookup for MaterialList; explicit flat slots retain their intent."""
    prefabs = parse_material_list_meshes(path)
    out: dict[str, list[dict]] = {}
    for meshes in prefabs.values():
        for mname, slots in meshes.items():
            out.setdefault(mname, slots)
    for pname, meshes in prefabs.items():
        if pname in out or not meshes:
            continue
        key = norm_name(pname)
        best = next((m for m in meshes if norm_name(m) == key), None)
        if best is None and len(meshes) == 1:
            best = next(iter(meshes))
        if best is not None:
            out[pname] = meshes[best]
    return out


_PREFIX = re.compile(r"^(sm|sk|chr|pf|fx)_", re.I)
_VARIANT = re.compile(r"_\d{1,3}$")


def norm_name(name: str) -> str:
    """Compare Synty names loosely: drop SM_/SK_ prefix and a trailing _01."""
    n = _PREFIX.sub("", name.strip())
    return _VARIANT.sub("", n).lower()


def lookup_material_list(ml: dict[str, list[dict]], *names: str) -> list[dict] | None:
    for n in names:
        if n and n in ml:
            return ml[n]
    wanted = {norm_name(n) for n in names if n}
    for k, v in ml.items():
        if norm_name(k) in wanted:
            return v
    return None


def resolve_material_slots(slots: list[dict], textures: dict[str, list[Path]]) -> dict:
    """Resolve exact texture stems; duplicate candidates are not guessed."""
    paths, states, expected, candidates, methods, shaders, unsupported, by_material, material_states = [], [], [], [], [], [], [], {}, {}
    intents: dict[str, set[tuple[str, str | None]]] = {}
    for slot in slots:
        declared = slot.get("texture") or ""
        name = declared or (slot.get("material", "") if slot.get("custom_shader") else "")
        found = textures.get(Path(name).stem.lower(), [])
        # AI: Accept the previous single-path index shape for callers outside the scanner.
        found = [found] if isinstance(found, Path) else found
        path = found[0] if len(found) == 1 and not slot.get("untextured") else None
        state = ("untextured" if slot.get("untextured") else "resolved" if path else
                 "ambiguous" if len(found) > 1 else "missing" if declared else "unknown")
        paths.append(str(path) if path else None)
        states.append(state)
        expected.append(name or None)
        candidates.append([p.name for p in found])
        methods.append("texture_name" if declared else "material_name" if name else None)
        shaders.append(bool(slot.get("custom_shader")))
        unsupported.append(slot.get("unsupported_maps", []))
        material = slot["material"]
        intents.setdefault(material, set()).add((state, str(path) if path else None))
    for material, choices in intents.items():
        if len(choices) == 1:
            state, path = next(iter(choices))
            material_states[material] = state
            if path:
                by_material[material] = path
    return {"slots": paths, "slot_states": states, "expected_textures": expected,
            "candidates": candidates, "resolution_methods": methods, "custom_shaders": shaders,
            "unsupported_maps": unsupported,
            "by_material": by_material, "material_states": material_states}


def texture_index(root: Path) -> dict[str, list[Path]]:
    """Lowercase stem to sorted image candidates under ``root``."""
    out: dict[str, list[Path]] = {}
    for p in sorted(root.rglob("*")):
        if p.suffix.lower() in TEXTURE_EXTS and p.is_file() and ".mayaSwatches" not in p.parts:
            out.setdefault(p.stem.lower(), []).append(p)
    return out


def deduplicate_textures(textures: dict[str, list[Path]]) -> dict[str, list[Path]]:
    """Collapse byte-identical copies, retaining distinct candidates in tree-priority order."""
    result = {}
    for name, paths in textures.items():
        if len(paths) < 2:
            result[name] = paths
            continue
        seen = set()
        result[name] = []
        for path in paths:
            try:
                digest = hashlib.sha256(path.read_bytes()).digest()
            except OSError:
                result[name].append(path)
                continue
            if digest not in seen:
                seen.add(digest)
                result[name].append(path)
    return result


def default_atlas(textures: dict[str, list[Path]]) -> Path | None:
    """Return a fallback only when exactly one atlas candidate exists."""
    candidates = [p for k, paths in textures.items()
                  if re.search(r"texture_0?1(_a)?$", k) and "normal" not in k and "emiss" not in k
                  for p in ([paths] if isinstance(paths, Path) else paths)]
    return candidates[0] if len(candidates) == 1 else None
