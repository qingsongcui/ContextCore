"""Isolated negative behavior and security tests for host integrations installer.

Verifies:
1. Preserve 0600 permissions on shared configs on uninstall (never widen to 0644).
2. Codex current owned entry compared exactly as JSON before deletion (modified preserved).
3. Nested hook matcher removes only owned command, leaving foreign siblings intact.
4. No substring cc_hook ownership (foreign hooks containing cc_hook preserved).
5. Reinstall with foreign or modified matching names rejected during preflight.
6. Uninstall retains modified OMP extension and modified skill files.
7. Missing or invalid manifest cannot delete files.
8. Failed preflight writes nothing to disk.
9. Failed multi-file write rolls back original bytes and mode, never deleting foreign files.
10. Hook runner paths with spaces or special characters are shell-quoted.

ALL tests operate on temporary directories only; never touch real home directories.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from app.integrations.constants import SUPPORTED_HOSTS
from app.integrations.installer import (
    InstallResult,
    SetupBundle,
    UninstallResult,
    apply_host_integration,
    generate_setup_bundle,
    uninstall_host_integration,
)
from app.integrations.templates import render_hooks_config


def test_uninstall_preserves_0600_mode_never_widens():
    """Critical 1: Shared MCP configs must retain 0600 permissions upon uninstall and never widen to 0644."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # 1. Setup Codex config.toml with 0600 mode and a foreign MCP server
        codex_config = temp_home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True, exist_ok=True)
        codex_config.write_text(
            "[model]\nname = 'gpt-5'\n\n[mcp_servers.private_foreign]\nurl = 'http://foreign.local'\nhttp_headers = { Authorization = 'Bearer foreign-token' }\n",
            encoding="utf-8",
        )
        if os.name == "posix":
            codex_config.chmod(0o600)

        # Apply Codex integration
        apply_res = apply_host_integration("codex", token="cc-tok-1", confirmed=True, home=temp_home)
        assert apply_res.status == "installed"

        if os.name == "posix":
            assert (stat.S_IMODE(codex_config.stat().st_mode) & 0o077) == 0

        # Uninstall Codex integration
        un_res = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert un_res.status == "uninstalled"

        # Verify config.toml still exists with foreign credentials and mode is STILL 0600 (not widened!)
        assert codex_config.is_file()
        if os.name == "posix":
            mode_after = stat.S_IMODE(codex_config.stat().st_mode)
            assert mode_after == 0o600, f"Expected mode 0600 but got {oct(mode_after)} (permission widened!)"

        post_toml = codex_config.read_text(encoding="utf-8")
        assert "[mcp_servers.private_foreign]" in post_toml
        assert "foreign-token" in post_toml
        assert "[mcp_servers.contextcore]" not in post_toml

        # 2. Test JSON host (Cursor) with 0600 mode
        cursor_mcp = temp_home / ".cursor" / "mcp.json"
        cursor_mcp.parent.mkdir(parents=True, exist_ok=True)
        cursor_mcp.write_text(
            json.dumps({"mcpServers": {"foreign_corp": {"url": "https://corp.mcp", "headers": {"token": "secret-123"}}}}),
            encoding="utf-8",
        )
        if os.name == "posix":
            cursor_mcp.chmod(0o600)

        apply_host_integration("cursor", token="cc-tok-cursor", confirmed=True, home=temp_home)
        uninstall_host_integration("cursor", confirmed=True, home=temp_home)

        assert cursor_mcp.is_file()
        if os.name == "posix":
            cursor_mode = stat.S_IMODE(cursor_mcp.stat().st_mode)
            assert cursor_mode == 0o600, f"Expected mode 0600 for cursor mcp but got {oct(cursor_mode)}"

        cursor_data = json.loads(cursor_mcp.read_text(encoding="utf-8"))
        assert "foreign_corp" in cursor_data["mcpServers"]
        assert "contextcore" not in cursor_data["mcpServers"]


def test_codex_uninstall_preserves_modified_mcp_entry():
    """Critical 2: If user modified [mcp_servers.contextcore], uninstall must preserve it like JSON does."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # Apply Codex integration
        apply_res = apply_host_integration("codex", token="original-tok", confirmed=True, home=temp_home)
        assert apply_res.status == "installed"

        codex_config = temp_home / ".codex" / "config.toml"
        assert codex_config.is_file()

        # User externally modified the contextcore entry to point to their custom server
        modified_toml = (
            "[mcp_servers.contextcore]\n"
            'url = "http://my-custom-proxy.internal/mcp/"\n'
            'http_headers = { Authorization = "Bearer custom-key" }\n'
            "startup_timeout_sec = 60\n"
            "tool_timeout_sec = 120\n"
            "enabled = true\n"
        )
        codex_config.write_text(modified_toml, encoding="utf-8")

        # Uninstall Codex integration
        un_res = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert un_res.status == "conflict"

        # The externally modified block MUST be preserved!
        content_after = codex_config.read_text(encoding="utf-8")
        assert "http://my-custom-proxy.internal/mcp/" in content_after
        assert "custom-key" in content_after
        assert codex_config.resolve() in {Path(path).resolve() for path in un_res.skipped_modified}


def test_nested_hook_matcher_leaves_foreign_siblings_codex():
    """Critical 3: Nested hook matcher with multiple commands must remove only owned command and leave foreign siblings."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # Pre-populate hooks.json with a matcher containing both a foreign command and a placeholder
        hooks_file = temp_home / ".codex" / "hooks.json"
        hooks_file.parent.mkdir(parents=True, exist_ok=True)

        foreign_sibling_cmd = "/usr/local/bin/enterprise-audit-logger --session"
        initial_hooks = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume|clear|compact|fork",
                        "hooks": [
                            {"type": "command", "command": foreign_sibling_cmd},
                        ],
                    }
                ]
            }
        }
        hooks_file.write_text(json.dumps(initial_hooks, indent=2), encoding="utf-8")

        # Install ContextCore (it will merge its owned hook into the same SessionStart)
        apply_res = apply_host_integration("codex", token="tok-hook-test", confirmed=True, home=temp_home)
        assert apply_res.status == "installed"

        # Verify both foreign and owned hooks exist
        merged_hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
        session_entries = merged_hooks["hooks"]["SessionStart"]
        all_cmds: list[str] = []
        for ent in session_entries:
            for sub in ent.get("hooks", []):
                all_cmds.append(sub.get("command", ""))
        assert foreign_sibling_cmd in all_cmds
        assert any("codex session-start" in c for c in all_cmds)

        # Now test nested sibling structure specifically by placing both in one matcher array
        owned_cmd = [c for c in all_cmds if "codex session-start" in c][0]
        coexisting_matcher = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume",
                        "hooks": [
                            {"type": "command", "command": foreign_sibling_cmd},
                            {"type": "command", "command": owned_cmd, "additionalContextLimit": 4000},
                        ],
                    }
                ]
            }
        }
        hooks_file.write_text(json.dumps(coexisting_matcher, indent=2), encoding="utf-8")

        # Uninstall ContextCore
        un_res = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert un_res.status == "uninstalled"

        # Check that foreign_sibling_cmd in the nested matcher survived intact!
        post_uninstall_hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
        post_entries = post_uninstall_hooks["hooks"]["SessionStart"]
        assert len(post_entries) == 1
        surviving_sub_hooks = post_entries[0]["hooks"]
        assert len(surviving_sub_hooks) == 1
        assert surviving_sub_hooks[0]["command"] == foreign_sibling_cmd


def test_no_substring_cc_hook_ownership():
    """Critical 3 & ownership: Foreign hooks containing 'cc_hook' or 'cc-hook' in their path must not be removed."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        cursor_hooks = temp_home / ".cursor" / "hooks.json"
        cursor_hooks.parent.mkdir(parents=True, exist_ok=True)

        foreign_cc_hook_cmd = "/opt/security/cc_hook_monitor.sh"
        initial_cursor_hooks = {
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": foreign_cc_hook_cmd}],
            },
        }
        cursor_hooks.write_text(json.dumps(initial_cursor_hooks, indent=2), encoding="utf-8")

        # Install Cursor
        apply_res = apply_host_integration("cursor", token="cursor-tok-1", confirmed=True, home=temp_home)
        assert apply_res.status == "installed"

        # Verify foreign command is still in cursor_hooks
        data_merged = json.loads(cursor_hooks.read_text(encoding="utf-8"))
        session_cmds = [h["command"] for h in data_merged["hooks"]["sessionStart"]]
        assert foreign_cc_hook_cmd in session_cmds

        # Uninstall Cursor
        un_res = uninstall_host_integration("cursor", confirmed=True, home=temp_home)
        assert un_res.status == "uninstalled"

        # Foreign command containing cc_hook MUST NOT be removed by substring matching!
        data_after = json.loads(cursor_hooks.read_text(encoding="utf-8"))
        session_cmds_after = [h["command"] for h in data_after["hooks"]["sessionStart"]]
        assert foreign_cc_hook_cmd in session_cmds_after


def test_reinstall_foreign_matching_name_rejected():
    """Reinstall check: Pre-existing unowned 'contextcore' in config must be rejected and write nothing."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # Pre-existing foreign contextcore server in Codex config without manifest
        codex_config = temp_home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True, exist_ok=True)
        codex_config.write_text(
            "[mcp_servers.contextcore]\nurl = 'http://foreign.contextcore.org'\n",
            encoding="utf-8",
        )

        token_path = temp_home / ".contextcore" / "credentials" / "codex.token"

        with pytest.raises(ValueError) as exc:
            apply_host_integration("codex", token="new-tok", confirmed=True, home=temp_home)
        assert "Refusing to overwrite pre-existing unowned MCP server 'contextcore'" in str(exc.value)

        # Failed preflight writes nothing: no token, no manifest
        assert not token_path.exists()
        assert not (temp_home / ".contextcore" / "manifests" / "codex.json").exists()


def test_reinstall_modified_matching_name_rejected():
    """Reinstall check: If contextcore was installed but user externally changed it, reinstall is rejected."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # First install
        apply_res = apply_host_integration("cursor", token="c-tok-1", confirmed=True, home=temp_home)
        assert apply_res.status == "installed"

        cursor_mcp = temp_home / ".cursor" / "mcp.json"
        # User alters the contextcore entry
        data = json.loads(cursor_mcp.read_text(encoding="utf-8"))
        data["mcpServers"]["contextcore"]["url"] = "http://altered.example.com"
        cursor_mcp.write_text(json.dumps(data, indent=2), encoding="utf-8")

        # Attempt to reinstall
        with pytest.raises(ValueError) as exc:
            apply_host_integration("cursor", token="c-tok-2", confirmed=True, home=temp_home)
        assert "Refusing to overwrite externally modified or foreign MCP server 'contextcore'" in str(exc.value)


def test_omp_uninstall_modified_extension_retained():
    """OMP extension: modified contextcore.ts must be preserved on uninstall and not deleted."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        apply_res = apply_host_integration("omp", token="omp-tok-1", confirmed=True, home=temp_home)
        assert apply_res.status == "installed"

        omp_ext = temp_home / ".omp" / "agent" / "hooks" / "pre" / "contextcore.ts"
        assert omp_ext.is_file()

        # User modifies the extension code
        omp_ext.write_text("// user modified custom logic\nconsole.log('custom');\n", encoding="utf-8")

        un_res = uninstall_host_integration("omp", confirmed=True, home=temp_home)
        assert un_res.status == "conflict"
        assert Path(apply_res.manifest_path).exists()
        assert Path(apply_res.token_path).exists()

        # Extension file must STILL exist!
        assert omp_ext.is_file()
        assert "custom" in omp_ext.read_text(encoding="utf-8")
        assert omp_ext.resolve() in {Path(path).resolve() for path in un_res.skipped_modified}


def test_skill_uninstall_modified_retained():
    """Skill file: modified SKILL.md must be preserved on uninstall."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        apply_res = apply_host_integration("codex", token="tok-skill-test", confirmed=True, home=temp_home)
        assert apply_res.status == "installed"

        skill_path = Path(apply_res.skill_path)
        assert skill_path.is_file()

        # User customizes the skill
        skill_path.write_text("# My Customized Skill\nDo something special.\n", encoding="utf-8")

        un_res = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert un_res.status == "conflict"
        assert Path(apply_res.manifest_path).exists()
        assert Path(apply_res.token_path).exists()

        assert skill_path.is_file()
        assert "My Customized Skill" in skill_path.read_text(encoding="utf-8")
        assert str(skill_path) in un_res.skipped_modified


def test_uninstall_missing_or_invalid_manifest_cannot_delete():
    """Manifest safety: missing or corrupted manifest must refuse deletion."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # 1. Missing manifest
        un_res = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert un_res.status == "not_installed"
        assert len(un_res.files_removed) == 0
        assert len(un_res.files_modified) == 0

        # 2. Corrupt manifest JSON
        manifest_path = temp_home / ".contextcore" / "manifests" / "codex.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text("{ corrupt json ...", encoding="utf-8")

        un_res2 = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert un_res2.status == "not_installed" or un_res2.status == "error"
        assert len(un_res2.files_removed) == 0

        # 3. Invalid manifest schema (missing required fields)
        manifest_path.write_text(json.dumps({"agent_id": "codex"}), encoding="utf-8")
        un_res3 = uninstall_host_integration("codex", confirmed=True, home=temp_home)
        assert un_res3.status == "error"
        assert len(un_res3.files_removed) == 0
        assert any("Invalid or corrupt owner manifest" in d for d in un_res3.details)


def test_failed_preflight_writes_nothing():
    """Preflight atomicity: If preflight fails (e.g. symlink escape or pre-existing unowned skill), nothing is written."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # Pre-existing skill file that is unowned
        skill_path = temp_home / ".codex" / "skills" / "contextcore" / "SKILL.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text("# Existing unowned skill", encoding="utf-8")

        token_path = temp_home / ".contextcore" / "credentials" / "codex.token"

        with pytest.raises(ValueError) as exc:
            apply_host_integration("codex", token="tok-preflight", confirmed=True, home=temp_home)

        # Token was NOT written
        assert not token_path.exists()
        # Manifest was NOT written
        assert not (temp_home / ".contextcore" / "manifests" / "codex.json").exists()


def test_failed_multi_file_write_rolls_back_original_bytes_and_mode():
    """Rollback safety: If an error occurs midway through writing files, rollback restores original bytes and mode."""
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)

        # Setup an existing config.toml with specific mode and bytes
        codex_config = temp_home / ".codex" / "config.toml"
        codex_config.parent.mkdir(parents=True, exist_ok=True)
        orig_bytes = b"[model]\nname = 'gpt-4o'\n"
        codex_config.write_bytes(orig_bytes)
        if os.name == "posix":
            codex_config.chmod(0o600)

        # Force a failure during skill file writing (which happens after config.toml is written)
        real_atomic_write = apply_host_integration.__globals__["_atomic_write_file"]

        def mock_write(path: Path, content: str | bytes, mode: int = 0o600):
            if "skills" in str(path):
                raise IOError("Simulated disk error during skill write")
            return real_atomic_write(path, content, mode=mode)

        with patch("app.integrations.installer._atomic_write_file", side_effect=mock_write):
            with pytest.raises(IOError) as exc:
                apply_host_integration("codex", token="rollback-tok", confirmed=True, home=temp_home)
            assert "Simulated disk error" in str(exc.value)

        # Verify config.toml was rolled back to original bytes and mode
        assert codex_config.is_file()
        assert codex_config.read_bytes() == orig_bytes
        if os.name == "posix":
            assert stat.S_IMODE(codex_config.stat().st_mode) == 0o600

        # Newly created files like token must be unlinked
        token_path = temp_home / ".contextcore" / "credentials" / "codex.token"
        assert not token_path.exists()


def test_hook_runner_path_shell_quoted():
    """Security: Hook runner path with spaces or shell characters is properly quoted."""
    path_with_spaces = "/Users/developer/My Projects/ContextCore/cc_hook.py"
    config = render_hooks_config("codex", hook_runner_path=path_with_spaces)

    session_start = config["data"]["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    # Should contain quoted runner path
    assert f"'{path_with_spaces}'" in session_start or f'"{path_with_spaces}"' in session_start
    assert session_start.endswith("codex session-start")

    # Cursor
    cursor_cfg = render_hooks_config("cursor", hook_runner_path=path_with_spaces)
    cursor_cmd = cursor_cfg["data"]["hooks"]["sessionStart"][0]["command"]
    assert f"'{path_with_spaces}'" in cursor_cmd or f'"{path_with_spaces}"' in cursor_cmd

    # Antigravity
    ag_cfg = render_hooks_config("antigravity", hook_runner_path=path_with_spaces)
    ag_cmd = ag_cfg["data"]["contextcore-session"]["SessionStart"][0]["command"]
    assert f"'{path_with_spaces}'" in ag_cmd or f'"{path_with_spaces}"' in ag_cmd


def test_local_sync_preserves_owned_host_skill_and_manifest():
    """Importing local assets must never upgrade even an unmodified owned skill."""
    import hashlib
    from app.database import create_schema
    from app.services.management_service import ManagementService

    create_schema()
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir)
        installed = apply_host_integration("codex", token="cc-skill-refresh", confirmed=True, home=temp_home)
        assert installed.status == "installed"
        skill_path = temp_home / ".codex" / "skills" / "contextcore" / "SKILL.md"
        manifest_path = temp_home / ".contextcore" / "manifests" / "codex.json"
        old_bytes = b"# Legacy ContextCore Skill\nKeep this installed version.\n"
        skill_path.write_bytes(old_bytes)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["skill_hash"] = hashlib.sha256(old_bytes).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        before = {
            str(path.relative_to(temp_home)): path.read_bytes()
            for path in temp_home.rglob("*") if path.is_file()
        }

        ManagementService(home=temp_home).sync_local_environment()

        after = {
            str(path.relative_to(temp_home)): path.read_bytes()
            for path in temp_home.rglob("*") if path.is_file()
        }
        assert after == before


def test_legacy_contextcore_handoff_skill_migrates_cleanly_to_unified_contextcore():
    """Upgrading a legacy contextcore-handoff installation writes unified /contextcore and removes the old duplicate."""
    import hashlib

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_home = Path(temp_dir).resolve()
        installed = apply_host_integration("codex", token="cc-legacy-mig", confirmed=True, home=temp_home)
        assert installed.status == "installed"
        new_skill_path = temp_home / ".codex" / "skills" / "contextcore" / "SKILL.md"
        new_skill_path.unlink()

        legacy_skill_path = temp_home / ".agents" / "skills" / "contextcore-handoff" / "SKILL.md"
        legacy_skill_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_bytes = b"---\nname: contextcore-handoff\ndescription: legacy\n---\nLegacy skill body\n"
        legacy_skill_path.write_bytes(legacy_bytes)

        manifest_path = temp_home / ".contextcore" / "manifests" / "codex.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["skill_target"] = str(legacy_skill_path)
        manifest["skill_hash"] = hashlib.sha256(legacy_bytes).hexdigest()
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        migrated = apply_host_integration(
            "codex", token="cc-legacy-mig", confirmed=True, home=temp_home, components=["skill"]
        )
        assert migrated.status == "installed"
        assert new_skill_path.is_file()
        assert "name: contextcore\n" in new_skill_path.read_text(encoding="utf-8")
        assert not legacy_skill_path.exists()
        assert not legacy_skill_path.parent.exists()
        updated_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert updated_manifest["skill_target"] == str(new_skill_path)
