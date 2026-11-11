"""Unit tests for ContextCore Transport layer and Host Integrations.

Tests:
1. Credential security (0600 permissions, log and header redaction, URL sanitization).
2. Codex app-server JSON-RPC client and session reading.
3. CLI structured memory distillation (safe arg construction, stdin prompt, metadata-only error).
4. Host integrations, setup bundle previews, explicit apply installer (isolated home),
   and idempotent uninstaller preserving foreign keys.
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any
import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"

from app.integrations.constants import (
    HOST_CAPABILITY_STATUS,
    MCP_TOOLS,
    SUPPORTED_HOSTS,
)
from app.integrations.installer import (
    apply_host_integration,
    generate_setup_bundle,
    uninstall_host_integration,
)
from app.integrations.templates import (
    render_hooks_config,
    render_mcp_config,
    render_skill_or_command,
)
from app.integrations.cc_hook import format_host_response
from app.transport import (
    CodexAppServerClient,
    CodexAppServerUnavailableError,
    CredentialSecurityError,
    DistillExecutionError,
    DistillResult,
    DistillUsage,
    _resolve_omp_default_model_from_config,
    build_distill_command,
    fetch_codex_sessions,
    fetch_codex_sessions_sync,
    is_path_under_roots,
    parse_distill_output,
    read_credential_securely,
    redact_token,
    run_distill,
    run_distill_sync,
    sanitize_headers,
    sanitize_url,
)

# ---------------------------------------------------------------------------
# 1. Credential Security Tests
# ---------------------------------------------------------------------------


def test_read_credential_securely_valid():
    with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
        tf.write("secret-agent-token-12345\n")
        tf_path = Path(tf.name)

    try:
        if os.name == "posix":
            tf_path.chmod(0o600)
        token = read_credential_securely(tf_path)
        assert token == "secret-agent-token-12345"
    finally:
        tf_path.unlink(missing_ok=True)


def test_read_credential_securely_insecure_permissions():
    if os.name != "posix":
        pytest.skip("Permission bits check only enforced on POSIX")

    with tempfile.NamedTemporaryFile("w+", delete=False) as tf:
        tf.write("insecure-token\n")
        tf_path = Path(tf.name)

    try:
        tf_path.chmod(0o666)
        with pytest.raises(CredentialSecurityError) as exc_info:
            read_credential_securely(tf_path, enforce_permissions=True)
        assert "insecure permissions" in str(exc_info.value)
    finally:
        tf_path.unlink(missing_ok=True)


def test_read_credential_securely_nonexistent():
    with pytest.raises(CredentialSecurityError):
        read_credential_securely(Path("/tmp/nonexistent-contextcore-token.token"))


def test_redact_token():
    assert redact_token(None) == "[EMPTY]"
    assert redact_token("") == "[EMPTY]"
    assert redact_token("short") == "***"
    assert redact_token("sk-test-secret-123456789") == "sk-t...6789"


def test_sanitize_headers():
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer sk-1234567890abcdef",
        "X-Agent-Token": "secret_agent_998877",
        "User-Agent": "ContextCore/1.0",
    }
    sanitized = sanitize_headers(headers)
    assert sanitized["Content-Type"] == "application/json"
    assert sanitized["User-Agent"] == "ContextCore/1.0"
    assert sanitized["Authorization"].startswith("Bearer sk-1...")
    assert "..." in sanitized["X-Agent-Token"]


def test_sanitize_url():
    clean = "http://127.0.0.1:8420/api/connections/codex"
    assert sanitize_url(clean) == clean

    leaky = "http://127.0.0.1:8420/mcp/?token=secret123&user=alice"
    sanitized = sanitize_url(leaky)
    assert "token=[REDACTED]" in sanitized
    assert "user=alice" in sanitized


# ---------------------------------------------------------------------------
# 2. Codex App-Server JSON-RPC Client Tests
# ---------------------------------------------------------------------------


def test_is_path_under_roots():
    with tempfile.TemporaryDirectory() as root_dir, tempfile.TemporaryDirectory() as outside_dir:
        sub_dir = Path(root_dir) / "subproject"
        sub_dir.mkdir()
        # Workspace path check
        assert is_path_under_roots(str(sub_dir), [root_dir])
        assert not is_path_under_roots(outside_dir, [root_dir])

        # Exact canonical host-level installation grant (~/.codex)
        canonical_codex = str(Path.home() / ".codex")
        assert is_path_under_roots(outside_dir, [canonical_codex])

        # Insecure arbitrary .codex path (/tmp/.codex/one-session) MUST NOT grant outside paths
        insecure_codex = "/tmp/.codex/one-session"
        assert not is_path_under_roots(outside_dir, [insecure_codex])

@pytest.mark.anyio
async def test_fetch_codex_sessions_unauthorized_guard():
    # When allow_execution is False, must raise CodexAppServerUnavailableError
    with pytest.raises(CodexAppServerUnavailableError) as exc_info:
        await fetch_codex_sessions(roots=["/some/path"], allow_execution=False)
    assert "not authorized" in str(exc_info.value)


def test_fetch_codex_sessions_sync_unauthorized():
    with pytest.raises(CodexAppServerUnavailableError):
        fetch_codex_sessions_sync(roots=["/some/path"], allow_execution=False)




# ---------------------------------------------------------------------------
# 3. CLI Distillation Tests
# ---------------------------------------------------------------------------


def test_build_distill_command():
    # OMP with verified official constrained flags per v18.10.4 and settings.md
    omp_cmd, omp_safe = build_distill_command("omp", "/tmp/omp_overlay.yaml")
    assert omp_safe is True
    assert "--no-tools" in omp_cmd
    assert "--no-extensions" in omp_cmd
    assert "--no-skills" in omp_cmd
    assert "--no-rules" in omp_cmd
    assert "--no-lsp" in omp_cmd
    assert "--no-session" in omp_cmd
    assert "--no-title" in omp_cmd
    assert "--config" in omp_cmd
    assert "/tmp/omp_overlay.yaml" in omp_cmd

    # Codex, Cursor, Antigravity lack official no-tools sandbox flags
    codex_cmd, codex_safe = build_distill_command("codex")
    assert codex_safe is False

    cursor_cmd, cursor_safe = build_distill_command("cursor")
    assert cursor_safe is False

    agy_cmd, agy_safe = build_distill_command("antigravity")
    assert agy_safe is False


@pytest.mark.anyio
async def test_run_distill_unsafe_host_reporting():
    # Must report unavailable for hosts without documented read-only sandbox or no-tools
    res_codex = await run_distill("codex", {"text": "hello"}, allow_execution=True)
    assert res_codex.status == "unavailable"
    assert "Codex CLI lacks documented no-tools or strict data boundary" in res_codex.error

    res_cursor = await run_distill("cursor", {"text": "hello"}, allow_execution=True)
    assert res_cursor.status == "unavailable"
    assert "lacks documented read-only sandbox" in res_cursor.error

    res_agy = await run_distill("antigravity", {"text": "hello"}, allow_execution=True)
    assert res_agy.status == "unavailable"
    assert "lacks documented read-only sandbox" in res_agy.error


@pytest.mark.anyio
async def test_run_distill_pending_when_unauthorized():
    res = await run_distill("omp", {"text": "some prompt"}, allow_execution=False)
    assert res.status == "pending"
    assert "pending explicit user grant" in res.error

def test_parse_distill_output_codex():
    codex_raw = (
        '{"event":"turn.started"}\n'
        '{"event":"turn.completed","usage":{"input_tokens":120,"output_tokens":45,"total_tokens":165}}\n'
        '{"memories":[{"category":"decision","title":"Use loopback","content":"Restricted to 127.0.0.1","tags":["sec"]}],"summary":"Configured loopback."}\n'
    )
    parsed, usage = parse_distill_output("codex", codex_raw)
    assert len(parsed.get("memories", [])) == 1
    assert parsed["summary"] == "Configured loopback."
    assert usage.tokens == 165
    assert usage.input_tokens == 120
    assert usage.output_tokens == 45
    assert usage.note is None


def test_parse_distill_output_omp():
    result = {
        "memories": [{"category": "preference", "title": "Theme", "content": "Dark mode", "tags": ["ui"]}],
        "summary": "User prefers dark mode.",
    }
    message = {
        "role": "assistant", "stopReason": "stop",
        "usage": {"input": 200, "output": 80, "totalTokens": 280},
        "content": [{"type": "thinking", "thinking": "Not memory"},
                    {"type": "text", "text": json.dumps(result)}],
    }
    omp_raw = "\n".join(json.dumps(event) for event in [
        {"type": "session", "id": "synthetic"},
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "partial"}},
        {"type": "message_end", "message": message},
        {"type": "agent_end", "messages": [message]},
    ])
    parsed, usage = parse_distill_output("omp", omp_raw)
    assert len(parsed.get("memories", [])) == 1
    assert parsed["summary"] == "User prefers dark mode."
    assert usage.tokens == 280
    assert usage.input_tokens == 200
    assert usage.output_tokens == 80
    assert usage.note is None


@pytest.mark.parametrize("usage_block", [{}, {"input": 20}, {"output": 5}])
def test_omp_partial_usage_does_not_report_false_total(usage_block):
    _, usage = parse_distill_output("omp", json.dumps({
        "type": "message_end",
        "message": {"role": "assistant", "content": [{"type": "text", "text": "{\"memories\": []}"}], "usage": usage_block},
    }))
    assert usage.tokens is None


@pytest.mark.parametrize("stop_reason", ["error", "aborted"])
def test_omp_failed_message_does_not_apply_partial_memory(stop_reason):
    result = {"memories": [{"category": "decision", "title": "Partial", "content": "Must not apply"}]}
    raw = json.dumps({"type": "message_end", "message": {
        "role": "assistant", "stopReason": stop_reason,
        "content": [{"type": "text", "text": json.dumps(result)}],
    }})
    parsed, _ = parse_distill_output("omp", raw)
    assert parsed == {}


@pytest.mark.anyio
@pytest.mark.parametrize("valid", [True, False])
async def test_real_subprocess_requires_completed_structured_output(tmp_path, monkeypatch, valid):
    memory = {"category": "decision", "title": "Loopback", "content": "Bind to 127.0.0.1"}
    text = json.dumps({"memories": [memory]}) if valid else "unstructured response"
    event = {"type": "message_end", "message": {
        "role": "assistant", "stopReason": "stop",
        "content": [{"type": "text", "text": text}],
    }}
    executable = tmp_path / "omp"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nprint({json.dumps(event)!r})\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = await run_distill("omp", {"text": "Synthetic decision"}, allow_execution=True)
    if valid:
        assert result.status == "success"
        assert result.memories == [memory]
    else:
        assert result.status == "failed"
        assert result.memories == []


@pytest.mark.anyio
async def test_resolve_omp_default_model_from_config_and_overlay(tmp_path, monkeypatch):
    home1 = tmp_path / "home1"
    omp_config = home1 / ".omp" / "agent" / "config.yml"
    omp_config.parent.mkdir(parents=True)
    omp_config.write_text(
        "modelRoles:\n"
        "  smol: commandcode/deepseek/deepseek-v4.1-flash:max\n"
        "  task: google-antigravity/gemini-3.8-flash:high\n",
        encoding="utf-8",
    )
    assert (
        _resolve_omp_default_model_from_config(home1)
        == "commandcode/deepseek/deepseek-v4.1-flash:max"
    )

    home2 = tmp_path / "home2"
    omp_config_with_default = home2 / ".omp" / "agent" / "config.yml"
    omp_config_with_default.parent.mkdir(parents=True)
    omp_config_with_default.write_text(
        "modelRoles:\n"
        "  default: google-antigravity/gemini-3.8-flash:high\n"
        "  smol: commandcode/deepseek/deepseek-v4.1-flash:max\n",
        encoding="utf-8",
    )
    assert (
        _resolve_omp_default_model_from_config(home2)
        == "google-antigravity/gemini-3.8-flash:high"
    )

    monkeypatch.setattr(
        "app.transport._resolve_omp_default_model_from_config",
        lambda home: "commandcode/deepseek/deepseek-v4.1-flash:max",
    )
    event = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "stop",
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "memories": [
                                {
                                    "category": "decision",
                                    "title": "Overlay model",
                                    "content": "Verified modelRoles.default in overlay",
                                }
                            ]
                        }
                    ),
                }
            ],
        },
    }
    captured_overlay = tmp_path / "captured_overlay.yaml"
    executable = tmp_path / "omp"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "sys.stdin.read()\n"
        "args = sys.argv[1:]\n"
        "cfg_idx = args.index('--config')\n"
        f"pathlib.Path({str(captured_overlay)!r}).write_text(pathlib.Path(args[cfg_idx + 1]).read_text(encoding='utf-8'), encoding='utf-8')\n"
        f"print({json.dumps(event)!r})\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    result = await run_distill("omp", {"text": "Overlay model check"}, allow_execution=True)
    assert result.status == "success"
    overlay_text = captured_overlay.read_text(encoding="utf-8")
    assert "modelFallback: false" in overlay_text
    assert "default: commandcode/deepseek/deepseek-v4.1-flash:max" in overlay_text


def test_parse_distill_output_cursor_honest_usage():
    cursor_raw = json.dumps({
        "result": json.dumps({
            "memories": [{"category": "task", "title": "Implement adapter", "content": "Done", "tags": []}],
            "summary": "Completed adapter implementation.",
        }),
        "duration_ms": 1200,
    })
    parsed, usage = parse_distill_output("cursor", cursor_raw)
    assert len(parsed.get("memories", [])) == 1
    assert usage.tokens is None
    assert usage.note == "未知（未透出）"


# ---------------------------------------------------------------------------
# 4. Integrations, Setup Preview, and Explicit Installer Tests
# ---------------------------------------------------------------------------


def test_generate_setup_bundle_all_hosts():
    for host in SUPPORTED_HOSTS:
        bundle = generate_setup_bundle(host, base_url="http://127.0.0.1:8420")
        assert bundle.agent_id == host
        assert "contextcore" in bundle.mcp_config["content"]
        assert bundle.hooks_config["content"] is not None
        assert bundle.skill_or_command["content"] is not None
        assert "#!/usr/bin/env bash" in bundle.install_script
        assert len(bundle.notes) > 0




def test_apply_and_uninstall_host_integration_in_isolated_home():
    """Verify that apply_host_integration and uninstall_host_integration operate safely

    in an isolated directory, preserving foreign keys and setting 0600 token permissions.
    """
    with tempfile.TemporaryDirectory() as temp_home_str:
        temp_home = Path(temp_home_str)

        # 1. Test unconfirmed guard
        with pytest.raises(ValueError) as exc:
            apply_host_integration("codex", token="tok123", confirmed=False, home=temp_home)
        assert "requires confirmed=True" in str(exc.value)

        # 2. Setup pre-existing config.toml with foreign table
        codex_config = temp_home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True, exist_ok=True)
        codex_config.write_text(
            "[model]\nname = 'gpt-5'\n\n[mcp_servers.other_tool]\nurl = 'http://localhost:9000'\n",
            encoding="utf-8",
        )

        # Pre-existing hooks.json with foreign hook
        codex_hooks = temp_home / ".codex" / "hooks.json"
        codex_hooks.write_text(
            json.dumps({"hooks": {"UserPromptSubmit": [{"command": "echo user-custom-hook"}]}}),
            encoding="utf-8",
        )

        # 3. Apply integration with confirmed=True
        install_res = apply_host_integration(
            "codex",
            token="secret_token_val_999",
            confirmed=True,
            home=temp_home,
        )
        assert install_res.status == "installed"

        # Check token file permissions 0600 on POSIX
        token_path = Path(install_res.token_path)
        assert token_path.is_file()
        if os.name == "posix":
            mode = token_path.stat().st_mode
            assert (mode & 0o077) == 0  # no group or other access

        # Verify foreign tables preserved in config.toml
        updated_toml = codex_config.read_text(encoding="utf-8")
        assert "[model]" in updated_toml
        assert "name = 'gpt-5'" in updated_toml
        assert "[mcp_servers.other_tool]" in updated_toml
        assert "[mcp_servers.contextcore]" in updated_toml
        assert "secret_token_val_999" in updated_toml

        # Verify foreign hooks preserved in hooks.json
        updated_hooks = json.loads(codex_hooks.read_text(encoding="utf-8"))
        entries = updated_hooks["hooks"]["UserPromptSubmit"]
        assert any(e.get("command") == "echo user-custom-hook" for e in entries)

        # 4. Uninstall with confirmed=True
        uninstall_res = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert uninstall_res.status == "uninstalled"

        # Token removed
        assert not token_path.exists()

        # [mcp_servers.contextcore] removed, foreign tables still intact!
        post_uninstall_toml = codex_config.read_text(encoding="utf-8")
        assert "[mcp_servers.contextcore]" not in post_uninstall_toml
        assert "[model]" in post_uninstall_toml
        assert "[mcp_servers.other_tool]" in post_uninstall_toml


def test_apply_and_uninstall_json_host_isolated():
    """Verify JSON-based host (Cursor / OMP) install & uninstall preserves foreign servers."""
    with tempfile.TemporaryDirectory() as temp_home_str:
        temp_home = Path(temp_home_str)

        cursor_mcp = temp_home / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
        cursor_mcp.write_text(
            json.dumps({"mcpServers": {"github": {"url": "https://github.com/mcp"}}}),
            encoding="utf-8",
        )

        apply_host_integration("cursor", token="cursor_tok", confirmed=True, home=temp_home)

        # Check merged JSON
        data = json.loads(cursor_mcp.read_text(encoding="utf-8"))
        assert "github" in data["mcpServers"]
        assert "contextcore" in data["mcpServers"]

        # Uninstall
        uninstall_host_integration("cursor", confirmed=True, home=temp_home)
        data_after = json.loads(cursor_mcp.read_text(encoding="utf-8"))
        assert "contextcore" not in data_after["mcpServers"]
        assert "github" in data_after["mcpServers"]


def test_apply_host_integration_preflight_malformed_json():
    """Preflight must reject malformed JSON before any write and leave no partial changes."""
    with tempfile.TemporaryDirectory() as temp_home_str:
        temp_home = Path(temp_home_str)
        cursor_mcp = temp_home / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
        cursor_mcp.write_text("{malformed: json: not_valid,", encoding="utf-8")

        with pytest.raises(ValueError) as exc:
            apply_host_integration("cursor", token="test_token", confirmed=True, home=temp_home)
        assert "Preflight error" in str(exc.value)

        # Assert no files created or overwritten
        token_file = temp_home / ".contextcore" / "credentials" / "cursor.token"
        manifest_file = temp_home / ".contextcore" / "manifests" / "cursor.json"
        assert not token_file.exists()
        assert not manifest_file.exists()
        # Original malformed file preserved without corruption
        assert cursor_mcp.read_text(encoding="utf-8") == "{malformed: json: not_valid,"


def test_apply_host_integration_preflight_malformed_toml():
    """Preflight must reject malformed TOML before any write."""
    with tempfile.TemporaryDirectory() as temp_home_str:
        temp_home = Path(temp_home_str)
        codex_config = temp_home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True, exist_ok=True)
        codex_config.write_text("[section\nkey = = = bad toml", encoding="utf-8")

        with pytest.raises(ValueError) as exc:
            apply_host_integration("codex", token="test_token", confirmed=True, home=temp_home)
        assert "Preflight error" in str(exc.value)
        assert not (temp_home / ".contextcore" / "credentials" / "codex.token").exists()


def test_apply_host_integration_symlink_rejection():
    """Symlinks inside home must be rejected to prevent escaping or arbitrary file clobbering."""
    with tempfile.TemporaryDirectory() as temp_home_str, tempfile.TemporaryDirectory() as outside_dir_str:
        temp_home = Path(temp_home_str)
        outside_target = Path(outside_dir_str) / "target.json"
        outside_target.write_text("{}", encoding="utf-8")

        # Create symlinked cursor config dir
        cursor_dir = temp_home / ".cursor"
        cursor_dir.mkdir(parents=True, exist_ok=True)
        symlink_mcp = cursor_dir / "mcp.json"
        symlink_mcp.symlink_to(outside_target)

        with pytest.raises(ValueError) as exc:
            apply_host_integration("cursor", token="tok", confirmed=True, home=temp_home)
        assert "Security error" in str(exc.value)
        assert "symlink" in str(exc.value).lower()


def test_apply_host_integration_foreign_same_key_rejected():
    """Pre-existing unowned 'contextcore' MCP key must be rejected to prevent clobbering."""
    with tempfile.TemporaryDirectory() as temp_home_str:
        temp_home = Path(temp_home_str)
        cursor_mcp = temp_home / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
        foreign_content = {
            "mcpServers": {
                "contextcore": {"url": "http://foreign-unowned-server.com"},
                "other_tool": {"url": "http://other.com"},
            }
        }
        cursor_mcp.write_text(json.dumps(foreign_content), encoding="utf-8")

        with pytest.raises(ValueError) as exc:
            apply_host_integration("cursor", token="new_tok", confirmed=True, home=temp_home)
        assert "Refusing to overwrite pre-existing unowned MCP server 'contextcore'" in str(exc.value)

        # Foreign contextcore entry remains intact
        after = json.loads(cursor_mcp.read_text(encoding="utf-8"))
        assert after["mcpServers"]["contextcore"]["url"] == "http://foreign-unowned-server.com"


def test_apply_host_integration_mcp_config_0600_permissions():
    """MCP config file containing literal token must have 0600 permissions."""
    if os.name != "posix":
        pytest.skip("File permissions test applies to POSIX")

    with tempfile.TemporaryDirectory() as temp_home_str:
        temp_home = Path(temp_home_str)
        install_res = apply_host_integration("codex", token="secret_tok_123", confirmed=True, home=temp_home)
        mcp_path = Path(install_res.mcp_path)
        mode = mcp_path.stat().st_mode
        assert (mode & 0o077) == 0  # 0600


def test_uninstall_preserves_externally_modified_entries():
    """If user modified the contextcore MCP entry or skill, uninstall preserves it."""
    with tempfile.TemporaryDirectory() as temp_home_str:
        temp_home = Path(temp_home_str)
        apply_host_integration("cursor", token="tok1", confirmed=True, home=temp_home)

        cursor_mcp = temp_home / ".cursor" / "mcp.json"
        data = json.loads(cursor_mcp.read_text(encoding="utf-8"))
        # User modifies contextcore entry
        data["mcpServers"]["contextcore"]["custom_user_field"] = "preserved_value"
        cursor_mcp.write_text(json.dumps(data), encoding="utf-8")

        uninst_res = uninstall_host_integration("cursor", confirmed=True, home=temp_home)
        assert uninst_res.status == "conflict"
        assert (temp_home / ".contextcore/manifests/cursor.json").exists()
        assert (temp_home / ".contextcore/credentials/cursor.token").exists()

        # Entry was preserved because it was modified
        after = json.loads(cursor_mcp.read_text(encoding="utf-8"))
        assert "contextcore" in after["mcpServers"]
        assert after["mcpServers"]["contextcore"]["custom_user_field"] == "preserved_value"
        assert any("externally modified" in d for d in uninst_res.details)
        assert uninst_res.files_modified == []
        assert uninst_res.files_removed == []

def test_cc_hook_format_response():
    resp_data = {"injection": "Verified ContextCore Card", "card": "Verified ContextCore Card"}

    # Codex format
    codex_resp = format_host_response("codex", "prompt", resp_data)
    assert codex_resp == {}

    # Cursor format
    cursor_resp = format_host_response("cursor", "sessionStart", resp_data)
    assert cursor_resp == {}

    cursor_prompt_resp = format_host_response("cursor", "beforeSubmitPrompt", resp_data)
    assert cursor_prompt_resp == {"continue": True}

    # Antigravity format
    agy_resp = format_host_response("antigravity", "SessionStart", resp_data)
    assert agy_resp == {}


@pytest.mark.anyio
async def test_completed_cli_parser_bug_propagates_without_unknown_send(tmp_path, monkeypatch):
    executable = tmp_path / "omp"
    executable.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdin.read()\nprint('{{}}')\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))

    def broken_parser(*args):
        raise TypeError("synthetic parser implementation failure")

    monkeypatch.setattr("app.transport.parse_distill_output", broken_parser)
    with pytest.raises(DistillExecutionError) as error:
        await run_distill("omp", {"text": "Synthetic source only"}, allow_execution=True)
    assert error.value.kind == "implementation"
    assert error.value.send_started is True
    assert error.value.result_received is True
    assert isinstance(error.value.__cause__, TypeError)


@pytest.mark.anyio
async def test_cli_timeout_is_indeterminate_and_reaps_owned_process(tmp_path, monkeypatch):
    executable = tmp_path / "omp"
    executable.write_text(
        f"#!{sys.executable}\nimport sys,time\nsys.stdin.read()\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    started = []
    create_process = asyncio.create_subprocess_exec

    async def record_process(*args, **kwargs):
        process = await create_process(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", record_process)
    with pytest.raises(DistillExecutionError) as error:
        await run_distill("omp", {"text": "Synthetic source only"}, allow_execution=True, timeout=0.5)
    assert error.value.kind == "transport"
    assert error.value.send_started is True
    assert error.value.result_received is False
    assert started[0].returncode is not None
