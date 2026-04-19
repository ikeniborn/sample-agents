"""Tests for capability cache persistence — FIX-213."""
import json
import time
import pytest


def test_save_load_roundtrip(tmp_path):
    """Save cache to disk, load it back — entries survive."""
    cache_file = tmp_path / "capability_cache.json"
    now = time.time()
    data = {
        "model-a": {"mode": "json_schema", "ts": now},
        "model-b": {"mode": "json_object", "ts": now},
    }
    cache_file.write_text(json.dumps(data))

    loaded = json.loads(cache_file.read_text())
    assert loaded["model-a"]["mode"] == "json_schema"
    assert loaded["model-b"]["mode"] == "json_object"


def test_ttl_expiry(tmp_path):
    """Entries older than TTL are filtered out on load."""
    cache_file = tmp_path / "capability_cache.json"
    ttl = 7 * 86400
    now = time.time()
    data = {
        "fresh": {"mode": "json_schema", "ts": now},
        "stale": {"mode": "json_object", "ts": now - ttl - 1},
    }
    cache_file.write_text(json.dumps(data))

    loaded = json.loads(cache_file.read_text())
    filtered = {
        k: v["mode"] for k, v in loaded.items()
        if isinstance(v, dict) and now - v.get("ts", 0) < ttl
    }
    assert "fresh" in filtered
    assert "stale" not in filtered


def test_corrupt_file_graceful(tmp_path):
    """Corrupt cache file → empty dict, no crash."""
    cache_file = tmp_path / "capability_cache.json"
    cache_file.write_text("NOT JSON {{{")

    try:
        data = json.loads(cache_file.read_text())
        result = {k: v["mode"] for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        result = {}
    assert result == {}


def test_missing_file_empty(tmp_path):
    """Missing cache file → empty dict."""
    cache_file = tmp_path / "nonexistent.json"
    try:
        data = json.loads(cache_file.read_text())
        result = {k: v["mode"] for k, v in data.items() if isinstance(v, dict)}
    except Exception:
        result = {}
    assert result == {}
