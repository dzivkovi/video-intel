"""Tests for vector_db_dir resolution and the LanceDB-oracle probe.

Guards two failure modes:
  1. The LanceDB path silently colocating with a cloud-synced output_dir
     (as in the 2026-04-18 GDrive File Stream incident - see ADR-0016).
  2. build_search_index wasting Voyage tokens before the probe catches an
     incompatible filesystem.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from video_intel import probe_atomic_writes, resolve_vector_db_dir


def test_resolve_vector_db_dir_defaults_to_output_dir_lancedb(tmp_path):
    out = tmp_path / "out"
    config = {"output_dir": str(out)}
    assert resolve_vector_db_dir(config, out) == out / ".lancedb"


def test_resolve_vector_db_dir_config_override_wins(tmp_path):
    out = tmp_path / "out"
    local = tmp_path / "cache" / "db"
    config = {"output_dir": str(out), "vector_db_dir": str(local)}
    assert resolve_vector_db_dir(config, out) == local


def test_resolve_vector_db_dir_expands_tilde(tmp_path):
    config = {"vector_db_dir": "~/cache/db"}
    result = resolve_vector_db_dir(config, tmp_path / "out")
    assert "~" not in str(result)
    assert result.is_absolute()


def test_probe_atomic_writes_succeeds_on_writable_dir(tmp_path):
    """Real LanceDB round-trip against a local tmp_path must succeed."""
    target = tmp_path / "probe_target"
    ok, reason = probe_atomic_writes(target)
    assert ok is True
    assert reason is None
    assert not list(target.glob("_probe_*"))  # probe subdir cleaned up


def test_probe_atomic_writes_returns_reason_on_lancedb_commit_failure(tmp_path, monkeypatch):
    """When LanceDB create_table raises (the actual GDFS failure shape), probe
    returns (False, reason). The reason names vector_db_dir as the fix."""
    import video_intel as vi

    class _FakeDB:
        def create_table(self, *a, **kw):
            raise RuntimeError("lance error: Incorrect function. (os error 1)")

        def drop_table(self, *a, **kw):
            pass

    fake_lancedb = SimpleNamespace(connect=lambda *a, **kw: _FakeDB())
    monkeypatch.setattr(vi, "require_lancedb", lambda: fake_lancedb)

    target = tmp_path / "probe_target"
    ok, reason = probe_atomic_writes(target)
    assert ok is False
    assert reason is not None
    assert "lancedb" in reason.lower() or "lance" in reason.lower()
    assert "vector_db_dir" in reason


def test_probe_uses_integration_oracle_not_mechanism_probe(tmp_path, monkeypatch):
    """Regression guard against reverting to an os.replace-based probe.

    2026-04-18 smoke test showed that GDrive File Stream permits Python-level
    os.replace (even rename-over-existing) but still fails LanceDB's Rust-side
    commit. A mechanism probe based on os.replace produces false negatives and
    burns Voyage tokens. The probe MUST call into LanceDB to be predictive.
    If someone 'simplifies' the probe back to a mechanism probe, this test fails.
    """
    import video_intel as vi

    lancedb_was_called = {"value": False}

    def tracking_connect(*a, **kw):
        lancedb_was_called["value"] = True

        class _OK:
            def create_table(self, *a, **kw):
                return None

            def drop_table(self, *a, **kw):
                pass

        return _OK()

    fake_lancedb = SimpleNamespace(connect=tracking_connect)
    monkeypatch.setattr(vi, "require_lancedb", lambda: fake_lancedb)

    ok, _ = probe_atomic_writes(tmp_path / "probe_target")
    assert ok is True
    assert lancedb_was_called["value"] is True, (
        "probe must call lancedb.connect as the oracle - a mechanism probe "
        "(os.replace / tempfile / shutil only) produces false negatives on GDFS"
    )


def test_build_search_index_aborts_before_voyage_when_probe_fails(tmp_path, monkeypatch):
    """Wiring test: probe failure short-circuits before any Voyage client is built."""
    import video_intel as vi

    bad_path = tmp_path / "bad"
    config = {"output_dir": str(tmp_path / "out"), "vector_db_dir": str(bad_path)}

    monkeypatch.setattr(vi, "probe_atomic_writes", lambda p: (False, "simulated failure"))

    sentinel = {"voyage_called": False}

    class _ShouldNotBeConstructed:
        def __init__(self, *a, **kw):
            sentinel["voyage_called"] = True
            raise AssertionError("Voyage client built despite probe failure")

    def _fake_require_voyageai():
        return SimpleNamespace(Client=_ShouldNotBeConstructed)

    def _fake_require_lancedb():
        return SimpleNamespace(
            connect=lambda *a, **kw: (_ for _ in ()).throw(
                AssertionError("lancedb.connect called despite probe failure")
            )
        )

    monkeypatch.setattr(vi, "require_voyageai", _fake_require_voyageai)
    monkeypatch.setattr(vi, "require_lancedb", _fake_require_lancedb)
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-key")

    with pytest.raises(SystemExit):
        vi.build_search_index(Path(config["output_dir"]), config=config)

    assert sentinel["voyage_called"] is False
