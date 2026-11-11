from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.adapters import DISCOVERY_PROFILES, HISTORY_READERS
from app.integrations.constants import (
    HOST_CONFIG_PATHS,
    HOST_LABELS,
    HOST_MCP_CONTAINER,
    SUPPORTED_HOSTS,
)
from app.integrations.installer import apply_host_integration, uninstall_host_integration
from app.services.connection_service import KNOWN_HOST_ROOTS
from app.services.handoff_service import ALLOWED_TARGET_AGENTS


def test_host_registries_stay_in_sync() -> None:
    expected = set(SUPPORTED_HOSTS)
    assert set(HOST_LABELS) == expected
    assert set(HOST_CONFIG_PATHS) == expected
    assert set(DISCOVERY_PROFILES) == expected
    assert set(HISTORY_READERS) == expected
    assert set(KNOWN_HOST_ROOTS) == expected
    assert ALLOWED_TARGET_AGENTS == expected
    assert set(HOST_MCP_CONTAINER) == expected - {"codex"}


def test_claude_install_preserves_unrelated_state(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".claude.json"
    config.write_text(json.dumps({"oauthAccount": {"id": "x"}, "projects": {"/repo": {"history": []}}}), encoding="utf-8")
    result = apply_host_integration("claude-code", confirmed=True, token="token", hook_runner_path="/bin/true")
    data = json.loads(config.read_text(encoding="utf-8"))
    assert result.status == "installed"
    assert data["oauthAccount"] == {"id": "x"}
    assert "/repo" in data["projects"]
    assert data["mcpServers"]["contextcore"]["type"] == "http"
    uninstall_host_integration("claude-code", confirmed=True)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["oauthAccount"] == {"id": "x"}
    assert "contextcore" not in data.get("mcpServers", {})


def test_opencode_install_merges_mcp_and_preserves_foreign_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"theme": "dark", "mcp": {"other": {"type": "remote"}}}), encoding="utf-8")
    apply_host_integration("opencode", confirmed=True, token="token", hook_runner_path="/bin/true")
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert "other" in data["mcp"]
    assert data["mcp"]["contextcore"]["type"] == "remote"
    uninstall_host_integration("opencode", confirmed=True)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert "other" in data["mcp"]
    assert "contextcore" not in data["mcp"]


def test_opencode_jsonc_refuses_automatic_merge(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".config/opencode").mkdir(parents=True)
    (tmp_path / ".config/opencode/opencode.jsonc").write_text("{ /* comment */ }", encoding="utf-8")
    with pytest.raises(ValueError, match="opencode.jsonc"):
        apply_host_integration("opencode", confirmed=True, token="token")


def test_opencode_v2_servers_shape_is_preserved(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    config = tmp_path / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcp": {"servers": {"other": {"type": "remote"}}, "theme": "dark"}}), encoding="utf-8")
    apply_host_integration("opencode", confirmed=True, token="token", hook_runner_path="/bin/true")
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "contextcore" in data["mcp"]["servers"]
    assert data["mcp"]["theme"] == "dark"
    uninstall_host_integration("opencode", confirmed=True)
    data = json.loads(config.read_text(encoding="utf-8"))
    assert "other" in data["mcp"]["servers"]
    assert "contextcore" not in data["mcp"]["servers"]


def test_claude_install_keeps_foreign_hooks(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = tmp_path / ".claude/settings.json"
    settings.parent.mkdir(parents=True)
    foreign = {"type": "command", "command": "/usr/bin/true foreign"}
    settings.write_text(json.dumps({"theme": "dark", "hooks": {"UserPromptSubmit": [{"hooks": [foreign]}]}}), encoding="utf-8")
    apply_host_integration("claude-code", confirmed=True, token="token", hook_runner_path="/bin/true")
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert foreign in data["hooks"]["UserPromptSubmit"][0]["hooks"]
    uninstall_host_integration("claude-code", confirmed=True)
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert foreign in data["hooks"]["UserPromptSubmit"][0]["hooks"]


def test_source_fingerprint_changes_with_file_state(tmp_path: Path) -> None:
    from app.services.connection_service import _source_fingerprint

    source = tmp_path / "session.jsonl"
    source.write_text('{"type":"session"}\n', encoding="utf-8")
    first = _source_fingerprint("omp", [str(tmp_path)])
    assert first
    source.write_text('{"type":"session"}\n{"type":"message"}\n', encoding="utf-8")
    second = _source_fingerprint("omp", [str(tmp_path)])
    assert second and second != first
