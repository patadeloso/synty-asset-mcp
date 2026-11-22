# Unity Asset Store: local cache ingestion

[Issue #1](https://github.com/patadeloso/synty-asset-mcp/issues/1). Use Unity to download purchased packages and
synty-mcp to import them. There is no Unity downloader, browser automation, credential handling or network access.

## Try it

1. Sign in to Unity yourself and download a purchased Synty package using **Package Manager → My Assets**.
2. Set the following in your local `config.yaml`. Replace the example folder with the package cache you actually
   use. There is no assumed platform path. Do not select a Unity project, account/profile directory or managed
   library directory. These roots are separate from `source_roots` (extracted trees) and are not served over HTTP.

   ```yaml
   unity_asset_store_enabled: true
   unity_asset_store_roots:
     - /path/to/downloaded/packages
   ```

3. Preview the cache. Both commands show live, metadata-only discovery; neither imports nor hashes package contents.

   ```sh
   export SYNTY_MCP_CONFIG="$PWD/config.yaml"
   synty-mcp unity-cache status
   synty-mcp unity-cache scan
   ```

4. Import one item using its scan ID (or its local file path). Alternatively, sync all files whose names match
   **known packs**. The filename match is a suggestion, not proof of publisher or ownership: review the scan first.

   ```sh
   printf 'Paste the package ID from scan: '
   read -r package_id
   synty-mcp unity-cache import "$package_id"
   # Or, to import all new matches:
   synty-mcp unity-cache sync
   ```

   Sync reports each file, including `skipped_unmatched`, `already_imported`, and failures. With an empty pack
   registry, start with explicit import: the existing importer can infer a pack from a Synty-style filename.
   A file that still cannot be assigned is archived but not extracted or indexed (`pack: null`).

## Design decisions

- Disabled by default. Only regular `.unitypackage` files are discovered (case-insensitive); discovery does not
  descend through symlinks. Missing roots are reported separately so other roots can still be used.
- Discovery is live rather than a database of potentially stale candidates. The scan ID fingerprints the normalized
  path, size and modification time. A changed size or timestamp requires a new selection.
- Only completed imports are recorded in local SQLite (`unity_store_imports`). A scan cannot identify identical
  content previously imported by another route without hashing. Import-time SHA-256 deduplication is authoritative.
- Cache files are staged one at a time in managed temporary storage **before** invoking the normal archive importer:
  its hard-link/permission changes must never affect Unity's files. Allow temporary space for one package in addition
  to managed archives and extracted trees. Verification, extraction and indexing reuse the existing library pipeline.
- MCP: `get_unity_asset_store_status` and `scan_unity_asset_store` are read-only;
  `import_unity_asset_store_package(ref)` and `sync_unity_asset_store` write to managed storage and may update
  extracted assets and catalogs. All four are local-only. Cache paths stay local: tools identify roots by their
  one-based configuration position and packages by scan ID and filename.
