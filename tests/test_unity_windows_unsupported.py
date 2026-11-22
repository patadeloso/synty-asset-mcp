"""Native Windows is unsupported for the Unity cache provider, regardless of opt-in."""
import os

import pytest

from synty_mcp import unity_store
from synty_mcp.config import Settings


@pytest.mark.parametrize("enabled", [False, True])
def test_windows_reports_unsupported_without_cache_io(tmp_path, monkeypatch, enabled):
    if os.name != "nt":
        monkeypatch.setattr(Settings, "unity_cache_supported", property(lambda self: False))

    def unexpected(*args, **kwargs):
        pytest.fail("unsupported provider attempted cache I/O")

    monkeypatch.setattr(unity_store, "_ref_key", unexpected)
    monkeypatch.setattr(unity_store, "library_write_lock", unexpected)
    monkeypatch.setattr(unity_store.UnityStore, "_candidates", unexpected)
    settings = Settings(data_dir=tmp_path / "data", unity_asset_store_enabled=enabled,
                        unity_asset_store_roots=[tmp_path / "private-cache"])
    store = unity_store.UnityStore(settings, None)
    status = store.status()
    assert not status["available"] and status["enabled"] is enabled
    assert "not currently supported on Windows" in status["reason"]
    assert "private-cache" not in str(status)
    for operation in (store.scan, store.sync):
        result = operation(agent=True)
        assert result["items"] == [] and result["reason"] == status["reason"]
    assert store.import_package("a" * 64) == {"status": "failed", "error": status["reason"]}
    assert not settings.data_dir.exists()
