"""Host-bridge checks for a down ContextCore and the installed enqueue hook."""
import json
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.integrations.constants import (
    COMMAND_HOOK_HOSTS,
    MCP_STARTUP_TIMEOUT_SEC,
    MCP_TOOL_TIMEOUT_SEC,
    SUPPORTED_HOSTS,
)
from app.integrations.installer import apply_host_integration, installed_hook_runner_path
from app.integrations.templates import render_mcp_config


def _silent_listener():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(0.1)
    return sock, sock.getsockname()[1]


def test_mcp_configs_declare_fail_open_timeouts():
    for host in SUPPORTED_HOSTS:
        rendered = render_mcp_config(host)
        assert str(MCP_TOOL_TIMEOUT_SEC) in rendered["content"]
        if host == "codex":
            assert f"startup_timeout_sec = {MCP_STARTUP_TIMEOUT_SEC}" in rendered["content"]
            assert f"tool_timeout_sec = {MCP_TOOL_TIMEOUT_SEC}" in rendered["content"]
        elif host == "antigravity":
            assert rendered["data"]["mcpServers"]["contextcore"]["timeoutSeconds"] == MCP_TOOL_TIMEOUT_SEC
        else:
            entry = rendered["data"].get("mcpServers", rendered["data"].get("mcp", {})).get("contextcore")
            if entry is None:
                entry = rendered["data"]["mcp"]["servers"]["contextcore"]
            assert entry["timeout"] == MCP_TOOL_TIMEOUT_SEC


def test_mcp_http_to_silent_server_fails_within_bound():
    sock, port = _silent_listener()
    import urllib.error
    import urllib.request
    started = time.monotonic()
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{port}/mcp/", timeout=0.4)
        pytest.fail("a silent MCP endpoint must not succeed")
    except (urllib.error.URLError, TimeoutError, OSError):
        assert time.monotonic() - started < 2
    finally:
        sock.close()


def test_installer_places_enqueue_hook_and_callback_ignores_hung_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    result = apply_host_integration("codex", token="synthetic-token", confirmed=True, home=tmp_path)
    assert result.status == "installed"
    runner = installed_hook_runner_path(tmp_path)
    assert runner.is_file()
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text(encoding="utf-8"))
    commands = [shlex.split(hook["command"])
                for groups in hooks["hooks"].values()
                for group in groups for hook in group["hooks"]]
    assert commands
    assert all(str(runner) in command for command in commands)
    assert all(str(Path(__file__).resolve()) not in command for command in commands)
    url = "http://127.0.0.1:65534"
    proc = subprocess.run(
        [sys.executable, str(runner), "--base-url", url, "codex", "prompt"],
        input='{"session_id":"synthetic","text":"local fixture"}',
        capture_output=True,
        text=True,
        timeout=8,
        env={**os.environ, "HOME": str(tmp_path)},
    )
    assert proc.returncode == 0
    assert proc.stdout == "{}"
    slots = list((tmp_path / ".contextcore" / "notifications").glob("codex-*.json"))
    assert len(slots) == 1 and slots[0].is_file() and slots[0].stat().st_size > 0


@pytest.mark.parametrize("host", COMMAND_HOOK_HOSTS)
def test_command_hosts_install_the_enqueue_runner(host, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    apply_host_integration(host, token="synthetic-token", confirmed=True, home=tmp_path)
    runner = installed_hook_runner_path(tmp_path)
    assert runner.is_file()
    hooks_path = {
        "codex": tmp_path / ".codex" / "hooks.json",
        "cursor": tmp_path / ".cursor" / "hooks.json",
        "antigravity": tmp_path / ".gemini" / "config" / "hooks.json",
        "claude-code": tmp_path / ".claude" / "settings.json",
    }[host]
    assert str(runner) in hooks_path.read_text(encoding="utf-8")
