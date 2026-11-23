# Plan: GLB material fallback and atlas discovery

**Tracking:** https://github.com/patadeloso/synty-asset-mcp/issues/3

**Status:** Implemented for the supported PBR subset; shader-specific texture maps and baking remain out of scope. See validation notes below.

## Goal

Convert ordinary Synty atlas materials to GLBs with embedded, bound base-colour textures when those textures exist locally. For Unity-specific shaders, preserve interpretable material properties in a predictable glTF-compatible PBR approximation, and explain missing bindings and simplifications in conversion metadata.

## Starting points

- `src/synty_mcp/unity.py`: Unity GUID and material parsing, MaterialList parsing, texture indexing and slot resolution. The texture index currently retains one path per lowercase stem; `material_albedo` can fall back to any resolved image, even one that is not necessarily albedo.
- `src/synty_mcp/inventory.py`: builds pack-local file indexes and persists texture bindings in `asset_files`. It combines Unity prefab and MaterialList data and chooses a default atlas.
- `src/synty_mcp/convert/blender_job.py`: resolves bindings per mesh and slot, wires Blender materials and emits conversion warnings. `_wire` currently forces matte roughness and zero metallic.
- `src/synty_mcp/convert/__init__.py` and `src/synty_mcp/diagnostics.py`: conversion results and exposed diagnostics; trace the full metadata path before changing it.
- `tests/test_unity.py`, `tests/test_convert.py`, `tests/test_diagnostics.py`, and `tests/fixtures.py`: existing test and fixture entry points. See `docs/TESTING_STATUS.md` for test requirements.

## Work sequence

1. **Reproduce and characterize.** Inspect the reported Mech ArcRifle, Cyber City Laser Gun and Sci-Fi Horror Mining Laser assets if their packs are available. Record the declared materials, shader properties, available textures, conversion diagnostics and actual GLB bindings. Treat the issue's observations as a starting point, not proof that a texture exists. Preserve Mech as a working regression case. Do not commit proprietary assets or private local paths.
2. **Define binding and diagnostic contracts.** Trace scan → persisted `asset_files` → conversion job → Blender report → API diagnostics. Decide on backwards-compatible fields for expected texture, candidates/search locations, selected path, resolution method, ambiguity, material state and fallback reason. Distinguish explicitly untextured, missing, ambiguous and unknown bindings, and distinguish missing textures from shader simplification. Consider path privacy in surfaced diagnostics.
3. **Improve discovery.** Preserve duplicate texture candidates instead of discarding all but one stem match. Resolve using explicit Unity GUID references, declared texture names, material names, slot order and pack-local context, with a documented deterministic precedence. Do not silently select unrelated or ambiguous atlases; avoid interpreting normal or emission maps as base colour. Preserve per-mesh and per-slot intent and existing correct bindings.
4. **Implement PBR approximation.** Parse and propagate reliably interpretable Unity material properties, including available base colour/atlas, alpha and cutout, emission, and metallic/roughness (including any required smoothness conversion). Update Blender wiring to retain those values rather than imposing generic defaults over known data. Preserve existing imported materials where appropriate; document which shader features cannot be translated. Texture baking is optional and out of initial scope unless investigation justifies it.
5. **Expose actionable results.** Report what was expected, where resolution searched, how any texture was selected, and why a binding or shader feature fell back. Ensure warnings accurately reflect the resulting material and that existing API consumers continue to work. Determine whether rescanning indexed packs and forcing cached conversions are needed, and document those steps.
6. **Test and document.** Add synthetic, redistributable fixtures and unit tests for exact and duplicate names, precedence, slot ordering, missing and explicitly untextured slots, custom shader inputs and PBR property handling. Add Blender integration tests that inspect the exported GLB material references and embedded images, including fallback properties, not merely successful conversion. Run existing tests and lint; where available, validate the three real examples and note any unavailable dependencies or packs. Update user-facing documentation with limitations and refresh instructions.

## Acceptance checks

- An ordinary atlas present locally is embedded and bound to the correct GLB material; ambiguous or absent candidates are not misreported as successful bindings.
- Unsupported shader features produce a documented PBR approximation using available supported properties, rather than an unexplained flat-colour material.
- Conversion metadata identifies unresolved bindings, resolution methods and fallback choices, and clearly separates missing textures from shader simplification.
- Existing successful material bindings and intentionally untextured materials remain correct; regression tests cover both.
- Report precisely which automated and real-pack checks were run, and what could not be verified.

## Validation notes

- Synthetic tests exercise duplicate and missing names, identical copies, custom-shader hints, Unity PBR properties, and actual Blender GLB material and embedded-image references.
- Local real-pack checks converted the Mech ArcRifle with its atlas bound, Cyber City Laser Gun with a reported missing declared albedo (only its emissive image was found), and Sci-Fi Horror Mining Laser with its exact material-name atlas and screen textures bound. The latter reports a custom-shader approximation. These checks used local files read-only and temporary outputs.
- Rescan indexed packs to refresh persisted bindings. The converter version invalidates old cache entries; use `force=true` when file contents change without path changes.
- Normal, emission and metallic texture maps, arbitrary shader graphs and baking are not implemented. Unity material properties are extracted from text `.mat` files, not reconstructed from custom shader code.

## Repository conventions

Follow `AGENTS.md`: new explanatory source or configuration comments written by AI start with `AI:`. Any GitHub issue or PR descriptions, comments or review replies written by AI require a visible **AI-authored** attribution. The GitHub repository for this issue is `patadeloso/synty-asset-mcp`, not the upstream parent.
