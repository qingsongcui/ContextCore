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


@pytest.fixture(autouse=True)
def offline_test_dns(monkeypatch):
    """Unit fixtures use synthetic upstreams. DNS-specific tests supply their own answers."""
    import ipaddress
    import socket

    resolve = socket.getaddrinfo

    def resolve_locally(host, *args, **kwargs):
        name = host.decode() if isinstance(host, bytes) else host
        if name and name.lower() != "localhost":
            try:
                ipaddress.ip_address(name)
            except ValueError:
                raise socket.gaierror(socket.EAI_NONAME, "No external DNS in unit tests") from None
        return resolve(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", resolve_locally)
