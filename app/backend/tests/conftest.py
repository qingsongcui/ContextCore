"""Every test starts inside a temporary home and runtime data directory.

Local fixtures may explicitly override these defaults. Database-only API tests
must never inspect or clean the user's launchd/PID/configuration metadata.
"""
import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_home(tmp_path, monkeypatch):
    home = tmp_path / "isolated-home"
    data = tmp_path / "isolated-runtime"
    home.mkdir()
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CONTEXTCORE_DATA_DIR", str(data))
    monkeypatch.setenv("CONTEXTCORE_DB", str(data / "test.db"))
