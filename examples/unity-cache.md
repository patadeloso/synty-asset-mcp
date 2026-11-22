# Import a downloaded Unity Asset Store package

Download the package yourself in the Unity Editor (**Package Manager → My Assets**). The provider reads local files; it never logs in to Unity or contacts a store.

## Preview, then import

1. Add the following to your local `config.yaml`. Use the actual cache folder (or, better, just the publisher subfolder you want to import). Don't use a home directory, Unity project, managed data directory or configured source root.

   ```yaml
   unity_asset_store_enabled: true
   unity_asset_store_roots:
     - /path/to/downloaded/packages
   ```

2. Check that the root is available, then preview candidates. `status` does **not** enumerate packages. `scan` checks file metadata without hashing or importing contents.

   ```sh
   export SYNTY_MCP_CONFIG="$PWD/config.yaml"
   synty-mcp unity-cache status
   synty-mcp unity-cache scan
   ```

3. Select a package by its scan ID or cache file path. File paths are accepted **only at the CLI**. With no known packs, start here: the importer can infer a pack from a Synty-style filename. Unassignable files are archived, not extracted.

   ```sh
   printf 'Paste the ID from scan: '
   read -r package_id
   synty-mcp unity-cache import "$package_id"
   ```

4. Alternatively, `synty-mcp unity-cache sync` imports only **unique exact filename-stem matches** to known pack IDs (case and separators normalized). A filename is not proof of publisher or ownership. Token matches, competing candidates and unrelated files are skipped; select one explicitly if appropriate.

## Outcomes that need attention

| Outcome | What to do |
|---|---|
| `already_imported` | Nothing: this cache fingerprint or the archive content is already known. |
| `requires_tree_confirmation` | Review the package and back up the managed pack tree. Only if you intend to merge it into that tree, run `synty-mcp unity-cache import "$package_id" --allow-tree-update`. Extraction can overwrite same-named assets; it does not remove obsolete files. Sync never confirms an update. |
| `failed_unchanged` | Verification already failed for this path, size and mtime. Replace or finish downloading the file, then scan again. To explicitly recheck the same file, use `unity-cache import "$package_id" --retry`. |
| `truncated: true` | Narrow the configured cache roots and rescan. Bulk sync won't import from a truncated scan. |

Discovery is bounded (default 50,000 directory entries across all roots, maximum depth 32). Staging is limited to 8 GiB per file by default and checks available temporary space. Adjust `unity_asset_store_max_scan_entries` or `unity_asset_store_max_package_bytes` for a trusted cache if needed. Allow additional space for managed archives and extraction. Cache files are never used as the importer's hard-link source. SHA-256 deduplication remains authoritative, and a change in size or modification time gives a new selection ID.

## MCP privacy and jobs

`get_unity_asset_store_status` shows only numbered roots and their availability. `scan_unity_asset_store` shows exact matches; other filenames stay off the wire by default. To share them with connected agents, the **owner** must set `unity_asset_store_show_unmatched: true` locally. This also affects sync reports; a job result can retain filenames shared while opted in. The CLI always shows the full local scan. A scan ID is an opaque, locally keyed selection token; share one only when you want a client to select that package.

`import_unity_asset_store_package(ref)` and `sync_unity_asset_store()` return jobs. Poll `get_job(job_id)` or pass `wait: true` and check `result` for per-file outcomes. Neither tool accepts a cache path or authorizes merging a different archive into an existing tree. Other library tools keep their existing behavior; these safeguards are specific to the Unity cache workflow.
