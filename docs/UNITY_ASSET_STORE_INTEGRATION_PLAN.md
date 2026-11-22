# Unity Asset Store integration plan

Tracking: [issue #1](https://github.com/patadeloso/synty-asset-mcp/issues/1). For the implemented commands,
configuration, and a worked example, see [Local Unity cache ingestion](../examples/unity-cache.md).

## Goal

Support Synty packages that the owner purchased through the **Unity Asset Store** without automating Unity authentication or relying on undocumented Unity download APIs.

```text
Unity Hub / Unity Editor (owner signs in and downloads normally)
  -> local Unity Asset Store cache containing .unitypackage files
  -> synty-mcp discovers those local files
  -> existing import / verify / extract / index pipeline
```

## Starting point

- The project already imports `.unitypackage` and `.zip` files, verifies archives, extracts them safely, detects packs, and indexes asset catalogs.
- Existing manual imports: `synty-mcp import <path-to-package>`, or copy packages into `inbox_dir` and run `synty-mcp inbox`.
- `source_roots` are read-only external folders that can be registered/scanned in place.
- `src/synty_mcp/store.py` supports the Synty Store after an owner-performed login. Unity cache ingestion should be separate: it is local discovery/import, not a remote store client.

## Security and product decision

**Do not implement a Unity username/password, browser-cookie, OAuth-token, or undocumented Unity API downloader.**

- Unity authentication/download interfaces may be unsupported or subject to terms and implementation changes.
- Automating a personal Unity session is fragile and increases credential/session risk.
- Unity Hub and the Unity Editor already provide the supported login and download UI.
- Local-cache ingestion provides the useful outcome without accessing credentials or downloading from Unity directly.

The MCP should never read, export, or expose Unity credentials, cookies, account data, or signed download URLs.

## Implementation outline

1. **Configuration**
   - Add `unity_asset_store_enabled: bool = False` and `unity_asset_store_roots: list[Path]` to `Settings` in `src/synty_mcp/config.py`.
   - Resolve configured paths. Do not assume a universal Unity cache location or add these roots to generic `source_roots`: the files are archives to be imported, not extracted trees to serve directly.

2. **Provider module**
   - Add `src/synty_mcp/unity_store.py`. Report errors for missing/non-directory roots as status, not crashes.
   - Recursively discover regular files ending in `.unitypackage` (case-insensitive). Do not follow symlinks outside configured roots.
   - Use a stable identity based on normalized path plus size and mtime; hash file contents only during import.
   - Ignore non-package files and avoid inspecting arbitrary file contents during the initial scan.
   - Return only safe metadata to agents: display name, opaque ID, size, mtime, inferred pack match and import status. Never return account information.

3. **Database state**
   - Track completed imports in SQLite using source path, size, mtime and archive ID. Discover candidates live so removed files do not remain eligible.
   - Preserve source paths locally; do not expose them over HTTP.
   - Keep a private ID key in managed secrets storage. Remember failed verification fingerprints until the file changes or the owner explicitly retries.
   - A changed size/mtime makes an entry eligible for review/re-import. Existing archive SHA-256 deduplication remains authoritative.

4. **Reuse existing import code**
   - Call `library.import_archive` for each explicitly selected/synced package. Stage a private snapshot to avoid hard-linking or chmod-ing a file in Unity's cache.
   - Do not duplicate archive parsing, extraction, safety checks, or deduplication.
   - Follow with the existing extraction/index workflow for verified archives assigned to a pack.

5. **Service, CLI, and MCP surface**
   - Add provider status to `Service.get_library_status()` beside `synty_store` and `syntydex`.
   - CLI: `unity-cache status`, `scan`, `import <id-or-path>`, `sync`.
   - MCP: `get_unity_asset_store_status()` and `scan_unity_asset_store()` are read-only; `import_unity_asset_store_package(ref)` and `sync_unity_asset_store()` write to managed storage. All four are idempotent and not open-world.
   - Bulk sync accepts only unambiguous exact pack-ID matches and never authorizes merging a new archive into a populated pack tree. Explicit owner confirmation for updates is local CLI only.
   - Status does not enumerate cache entries. Agent scan/sync omit non-exact matches unless the owner opts in, and agent imports accept keyed IDs only. MCP writes use the existing job manager.
   - Bound scan work and staged bytes; coordinate library writes across server and CLI. See the walkthrough for limits, flags and outcomes.

6. **Tests**
   - Use temp directories containing synthetic `.unitypackage` fixture files.
   - Test disabled state, missing root, nested discovery, symlink refusal, duplicate discovery, changed-file detection, and no files outside configured roots.
   - Test import delegation and archive deduplication.
   - Confirm no network client is created/called by the provider.
   - Confirm HTTP status/API output does not reveal secrets or Unity account details.

## Non-goals

- No Unity login automation, web scraping, use of undocumented Unity APIs, or automatic downloads.
- No handling or storage of Unity access tokens, cookies, passwords, MFA codes, or signed URLs.
- No modification of Unity’s cache or Unity project files.
