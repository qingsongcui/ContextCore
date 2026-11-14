"""Safe setup bundle generation and configuration installer.

Provides:
1. Read-only configuration previews and manual installation scripts (generate_setup_bundle).
2. Explicit, safe installation behind confirmed=True (apply_host_integration).
   - Strict preflight parse of all affected files BEFORE any write.
   - Symlink rejection & strict home directory containment.
   - Rejection of pre-existing foreign 'contextcore' keys.
   - Owner manifest tracking with SHA256 hashes for exact ownership proof.
   - Atomic replacement via temp files and rollback on failure.
   - 0600 permissions on token and MCP config files containing literal tokens.
3. Idempotent uninstallation behind confirmed=True (uninstall_host_integration).
   - Verifies owner manifest proof before removal.
   - Preserves externally modified files/entries.
   - Removes only exact owned entries without affecting foreign tools.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.integrations.constants import COMMAND_HOOK_HOSTS, HOST_CONFIG_PATHS, LEGACY_HOST_SKILL_PATHS, MAX_CONNECTION_FILES, MCP_STARTUP_TIMEOUT_SEC, MCP_TOOL_TIMEOUT_SEC

try:
    import tomllib  # Python 3.11+
except ImportError:
    try:
        import tomli as tomllib  # Python 3.9/3.10
    except ImportError:
        tomllib = None  # type: ignore

from app.integrations.constants import (
    DEFAULT_BASE_URL,
    HOST_CAPABILITY_STATUS,
    HostId,
    SUPPORTED_HOSTS,
)
from app.integrations.templates import (
    CC_HOOK_PATH,
    render_hooks_config,
    render_mcp_config,
    render_skill_or_command,
)


@dataclass
class SetupBundle:
    agent_id: HostId
    base_url: str
    credential_path: str | None
    mcp_config: dict[str, Any]
    hooks_config: dict[str, Any]
    skill_or_command: dict[str, Any]
    install_script: str
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "base_url": self.base_url,
            "credential_path": self.credential_path,
            "mcp_config": self.mcp_config,
            "hooks_config": self.hooks_config,
            "skill_or_command": self.skill_or_command,
            "install_script": self.install_script,
            "notes": self.notes,
        }


@dataclass
class InstallResult:
    agent_id: str
    status: str
    token_path: str
    mcp_path: str
    hooks_path: str
    skill_path: str
    manifest_path: str
    details: list[str] = field(default_factory=list)
    prepared_files: dict[str, bytes] = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "token_path": self.token_path,
            "mcp_path": self.mcp_path,
            "hooks_path": self.hooks_path,
            "skill_path": self.skill_path,
            "manifest_path": self.manifest_path,
            "details": self.details,
        }


@dataclass
class UninstallResult:
    agent_id: str
    status: str
    files_modified: list[str] = field(default_factory=list)
    files_removed: list[str] = field(default_factory=list)
    details: list[str] = field(default_factory=list)
    skipped_modified: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "status": self.status,
            "files_modified": self.files_modified,
            "files_removed": self.files_removed,
            "details": self.details,
            "skipped_modified": self.skipped_modified,
        }


@dataclass
class _FileBackup:
    path: Path
    existed: bool
    original_bytes: bytes | None = None
    original_mode: int | None = None


def _sha256(text: str) -> str:
    """Return SHA256 hex digest of a string."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _entry_digest(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


def _matches_owned_entry(manifest: dict, current: Any) -> bool:
    fingerprint = manifest.get("owned_mcp_entry_sha256")
    if fingerprint:
        return fingerprint == _entry_digest(current)
    # Read legacy ownership without rewriting the host or duplicating its secret.
    legacy = manifest.get("owned_mcp_entry")
    return bool(legacy) and current == legacy


def _assert_path_safe(target_path: Path, home: Path) -> Path:
    """Ensure path resolves strictly within home directory and rejects any symlinks.

    Rejects target if it or any existing parent directory is a symlink or escapes home.
    """
    resolved_home = home.resolve()
    curr: Path | None = target_path
    while curr and curr != home and curr != resolved_home:
        if curr.is_symlink():
            raise ValueError(
                f"Security error: '{curr}' is a symlink. Symlinks within home are strictly rejected for safety."
            )
        parent = curr.parent
        if parent == curr:
            break
        curr = parent
    resolved_target = target_path.resolve()
    try:
        resolved_target.relative_to(resolved_home)
    except ValueError as err:
        raise ValueError(
            f"Security error: Path '{target_path}' resolves outside allowed home directory '{resolved_home}'."
        ) from err
    return resolved_target


def _atomic_write_file(target_path: Path, content: str | bytes, mode: int = 0o600) -> None:
    """Atomically write content to target_path using a sibling temporary file."""
    parent_dir = target_path.parent
    parent_dir.mkdir(parents=True, exist_ok=True)

    temp_name = f".{target_path.name}.tmp.{uuid4().hex[:8]}"
    temp_path = parent_dir / temp_name

    # Open with O_CREAT | O_WRONLY | O_TRUNC | O_NOFOLLOW
    open_flags = os.O_CREAT | os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        open_flags |= os.O_NOFOLLOW

    fd = os.open(str(temp_path), open_flags, mode)
    try:
        if isinstance(content, bytes):
            with open(fd, "wb") as f:
                f.write(content)
        else:
            with open(fd, "w", encoding="utf-8") as f:
                f.write(content)
        if os.name == "posix":
            try:
                os.chmod(str(temp_path), mode)
            except OSError:
                pass
        os.replace(str(temp_path), str(target_path))
        if os.name == "posix":
            try:
                os.chmod(str(target_path), mode)
            except OSError:
                pass
    finally:
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _validate_toml_syntax(text: str, context_label: str) -> dict[str, Any]:
    """Validate TOML syntax using tomllib if available."""
    if not text.strip():
        return {}
    if tomllib is not None:
        try:
            parsed = tomllib.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError(f"TOML top-level must be a table in {context_label}")
            return parsed
        except Exception as err:
            raise ValueError(f"Preflight error: Invalid TOML in {context_label}: {err}") from err
    return {}


def _validate_json_syntax(text: str, context_label: str) -> dict[str, Any]:
    """Validate JSON syntax and ensure top-level is a dictionary."""
    if not text.strip():
        return {}
    try:
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError(f"Preflight error: JSON top-level in {context_label} must be an object.")
        return parsed
    except json.JSONDecodeError as err:
        raise ValueError(f"Preflight error: Invalid JSON syntax in {context_label}: {err}") from err


def _merge_codex_toml(existing_toml: str, mcp_url: str, token: str) -> str:
    """Merge [mcp_servers.contextcore] into existing TOML with strict validation."""
    parsed_before = _validate_toml_syntax(existing_toml, "Codex config.toml")

    new_block = (
        "[mcp_servers.contextcore]\n"
        f'url = "{mcp_url}"\n'
        f'http_headers = {{ Authorization = "Bearer {token}" }}\n'
        f"startup_timeout_sec = {MCP_STARTUP_TIMEOUT_SEC}\n"
        f"tool_timeout_sec = {MCP_TOOL_TIMEOUT_SEC}\n"
        "enabled = true\n"
    )

    pattern = r"(?ms)^\[mcp_servers\.contextcore\].*?(?=(?:^\[|\Z))"
    if re.search(pattern, existing_toml):
        updated = re.sub(pattern, new_block + "\n", existing_toml)
    else:
        sep = "\n\n" if existing_toml and not existing_toml.endswith("\n\n") else ""
        updated = existing_toml + sep + new_block

    updated_clean = updated.strip() + "\n"

    # Validate output with tomllib
    parsed_after = _validate_toml_syntax(updated_clean, "Merged Codex config.toml")

    # Verify contextcore was added properly
    if "mcp_servers" not in parsed_after or "contextcore" not in parsed_after["mcp_servers"]:
        raise ValueError("Failed to cleanly merge [mcp_servers.contextcore] into Codex config.toml")

    # Verify pre-existing tables were preserved
    for k, v in parsed_before.items():
        if k != "mcp_servers":
            if k not in parsed_after or parsed_after[k] != v:
                raise ValueError(f"Preflight safety failed: Section '[{k}]' was corrupted during TOML merge.")
        elif isinstance(v, dict):
            for server_name, server_cfg in v.items():
                if server_name != "contextcore":
                    if server_name not in parsed_after["mcp_servers"] or parsed_after["mcp_servers"][server_name] != server_cfg:
                        raise ValueError(f"Preflight safety failed: Foreign MCP server '{server_name}' was corrupted.")

    return updated_clean


def _remove_codex_toml(existing_toml: str) -> str:
    """Remove [mcp_servers.contextcore] from TOML content with validation."""
    parsed_before = _validate_toml_syntax(existing_toml, "Codex config.toml")
    pattern = r"(?ms)^\[mcp_servers\.contextcore\].*?(?=(?:^\[|\Z))"
    updated = re.sub(pattern, "", existing_toml).strip() + "\n"
    parsed_after = _validate_toml_syntax(updated, "Uninstalled Codex config.toml")

    # Verify foreign tables preserved
    for k, v in parsed_before.items():
        if k != "mcp_servers":
            if k not in parsed_after or parsed_after[k] != v:
                raise ValueError(f"Uninstall safety failed: Section '[{k}]' was corrupted.")
        elif isinstance(v, dict):
            for server_name, server_cfg in v.items():
                if server_name != "contextcore":
                    if (
                        "mcp_servers" not in parsed_after
                        or server_name not in parsed_after["mcp_servers"]
                        or parsed_after["mcp_servers"][server_name] != server_cfg
                    ):
                        raise ValueError(f"Uninstall safety failed: Foreign MCP server '{server_name}' was corrupted.")

    return updated


def _clean_hook_entry(entry: Any, owned_cmds: set[str]) -> tuple[Any | None, bool]:
    """Clean owned commands from a hook entry.

    Returns (cleaned_entry_or_None, was_modified).
    If cleaned_entry is None, the entry should be removed entirely.
    Preserves foreign sibling commands in nested hooks structures.
    Uses exact command matching; never substring matching.
    """
    if not isinstance(entry, dict):
        return entry, False

    # Case A: Top-level command
    if "command" in entry and isinstance(entry["command"], str):
        if entry["command"] in owned_cmds:
            return None, True
        return entry, False

    # Case B: Nested hooks array
    if "hooks" in entry and isinstance(entry["hooks"], list):
        orig_sub_hooks = entry["hooks"]
        cleaned_sub_hooks = []
        modified = False
        for sub_h in orig_sub_hooks:
            if isinstance(sub_h, dict) and isinstance(sub_h.get("command"), str) and sub_h["command"] in owned_cmds:
                modified = True
                continue
            cleaned_sub_hooks.append(sub_h)

        if not modified:
            return entry, False

        if not cleaned_sub_hooks:
            return None, True

        new_entry = dict(entry)
        new_entry["hooks"] = cleaned_sub_hooks
        return new_entry, True

    return entry, False


def _resolve_mcp_container(data: dict[str, Any], host: str) -> tuple[dict[str, Any], list[str]]:
    """Return the container that owns the contextcore MCP entry and its JSON path."""
    if host == "opencode":
        mcp = data.get("mcp")
        if mcp is None:
            data["mcp"] = {}
            return data["mcp"], ["mcp"]
        if not isinstance(mcp, dict):
            raise ValueError("OpenCode config 'mcp' must be an object; refusing to modify it.")
        servers = mcp.get("servers")
        if servers is not None:
            if not isinstance(servers, dict):
                raise ValueError("OpenCode config 'mcp.servers' must be an object; refusing to modify it.")
            return servers, ["mcp", "servers"]
        return mcp, ["mcp"]
    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError("MCP config 'mcpServers' must be an object; refusing to modify it.")
    return servers, ["mcpServers"]


def _container_by_path(data: dict[str, Any], path: list[str] | tuple[str, ...] | None) -> dict[str, Any] | None:
    if not path:
        return None
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, dict) else None


def _validate_manifest(agent_id: str, manifest: Any, resolved_home: Path) -> tuple[bool, str | None]:
    """Validate owner manifest integrity and security bounds."""
    if not isinstance(manifest, dict):
        return False, "Manifest must be a JSON object"
    if manifest.get("agent_id") != agent_id:
        return False, f"Manifest agent_id '{manifest.get('agent_id')}' does not match '{agent_id}'"
    for required_field in ("token_path", "mcp_target", "skill_target"):
        val = manifest.get(required_field)
        if not val or not isinstance(val, str):
            return False, f"Manifest missing required field '{required_field}'"
        try:
            _assert_path_safe(Path(val), resolved_home)
        except ValueError as err:
            return False, f"Manifest path '{val}' unsafe: {err}"
    return True, None


def _manifest_path(agent_id: str, home: Path) -> Path:
    return home / ".contextcore" / "manifests" / f"{agent_id}.json"


def _load_manifest(agent_id: str, home: Path) -> dict[str, Any] | None:
    path = _manifest_path(agent_id, home)
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _resolve_host_path(pattern: str, home: Path) -> Path:
    """Resolve tilde pattern relative to specified home."""
    if pattern.startswith("~/"):
        return home / pattern[2:]
    return Path(pattern)


def installed_hook_runner_path(home: Path) -> Path:
    """Private copy of the enqueue-only host hook. Hosts never call the tree path."""
    return Path(home) / ".contextcore" / "hooks" / "cc_hook.py"


def generate_setup_bundle(
    agent_id: str,
    base_url: str = DEFAULT_BASE_URL,
    credential_path: str | Path | None = None,
    token: str | None = None,
    hook_runner_path: str | None = None,
) -> SetupBundle:
    """Generate a complete setup bundle preview for the specified host agent.

    This function is purely analytical and does not modify any files on disk.
    """
    if agent_id not in SUPPORTED_HOSTS:
        raise ValueError(f"Unknown agent '{agent_id}'. Must be one of {SUPPORTED_HOSTS}")

    host: HostId = agent_id  # type: ignore

    resolved_token = token or "<agent-token>"
    resolved_cred_path = str(credential_path) if credential_path else f"~/.contextcore/credentials/{agent_id}.token"

    mcp = render_mcp_config(host, base_url=base_url, token=resolved_token)
    hooks = render_hooks_config(host, hook_runner_path=hook_runner_path, base_url=base_url)
    skill = render_skill_or_command(host)

    notes: list[str] = []
    caps = HOST_CAPABILITY_STATUS.get(host, {})
    for cap_name, cap_info in caps.items():
        if cap_info.get("status") in ("partial", "unsupported", "unavailable"):
            notes.append(f"[{cap_name.upper()} - {cap_info['status']}]: {cap_info['detail']}")

    if "trust_note" in hooks:
        notes.append(f"[SECURITY TRUST]: {hooks['trust_note']}")

    mcp_path = mcp["target_path"]
    hooks_path = hooks["target_path"]
    skill_path = skill["target_path"]

    script_lines = [
        "#!/usr/bin/env bash",
        f"# ContextCore Setup Script for {agent_id.upper()}",
        "# Review the operations below before executing in your terminal.",
        "set -euo pipefail",
        "",
        "# 1. Ensure target directories exist",
        f"mkdir -p $(dirname '{mcp_path}')",
        f"mkdir -p $(dirname '{hooks_path}')",
        f"mkdir -p $(dirname '{skill_path}')",
        "",
        f"# 2. Write MCP configuration to {mcp_path}",
        f"# Note: If file exists, merge manually or back up first.",
        f"cat << 'EOF' > '{mcp_path}.contextcore.preview'",
        mcp["content"],
        "EOF",
        "",
        f"# 3. Write Hooks/Extension configuration to {hooks_path}",
        f"cat << 'EOF' > '{hooks_path}.contextcore.preview'",
        hooks["content"],
        "EOF",
        "",
        f"# 4. Write Skill or Command to {skill_path}",
        f"cat << 'EOF' > '{skill_path}'",
        skill["content"],
        "EOF",
        "",
        f"echo 'ContextCore configuration files generated for {agent_id}.'",
        "echo 'Please review the .contextcore.preview files before merging.'",
    ]

    install_script = "\n".join(script_lines)

    return SetupBundle(
        agent_id=host,
        base_url=base_url,
        credential_path=resolved_cred_path,
        mcp_config=mcp,
        hooks_config=hooks,
        skill_or_command=skill,
        install_script=install_script,
        notes=notes,
    )


def apply_host_integration(
    agent_id: str,
    *,
    token: str,
    confirmed: bool = False,
    home: Path | None = None,
    base_url: str = DEFAULT_BASE_URL,
    hook_runner_path: str | None = None,
    components: list[str] | None = None,
    prepare_only: bool = False,
) -> InstallResult:
    """Explicitly apply host integration files behind confirmed=True.

    Transactional & Secure:
    - Preflight parse on every affected file before any write.
    - Symlinks strictly rejected.
    - Existing unowned 'contextcore' MCP entries rejected (never overwritten).
    - Writes owner manifest for ownership proof.
    - Atomic write via temporary sibling files with automatic rollback on error.
    - Enforces 0600 POSIX permissions on token and MCP config containing literal token.
    """
    if not confirmed:
        raise ValueError("apply_host_integration requires confirmed=True. Refusing unconfirmed installation.")

    if agent_id not in SUPPORTED_HOSTS:
        raise ValueError(f"Unknown agent '{agent_id}'. Must be one of {SUPPORTED_HOSTS}")

    selected = set(components) if isinstance(components, list) else {"mcp", "hooks", "skill"} if components is None else set()
    if not selected or not selected <= {"mcp", "hooks", "skill"} or (components is not None and len(selected) != len(components)):
        raise ValueError("Select distinct supported integration components")
    needs_token = bool(selected & {"mcp", "hooks"})
    host: HostId = agent_id  # type: ignore
    resolved_home = Path(home).resolve() if home else Path.home().resolve()
    if "mcp" in selected and host == "opencode" and (resolved_home / ".config/opencode/opencode.jsonc").exists():
        raise ValueError(
            "OpenCode uses opencode.jsonc; automatic merge would drop comments. "
            "Use the setup preview snippet and merge it manually, or convert the file to opencode.json."
        )
    details: list[str] = []

    # 1. Resolve & validate all target paths against symlinks and home escapes
    token_path = resolved_home / ".contextcore" / "credentials" / f"{agent_id}.token"
    manifest_target = _manifest_path(agent_id, resolved_home)
    runner_target = None
    if "hooks" in selected and host in COMMAND_HOOK_HOSTS and hook_runner_path is None:
        runner_target = installed_hook_runner_path(resolved_home)
        _assert_path_safe(runner_target, resolved_home)
        hook_runner_path = str(runner_target)

    bundle = generate_setup_bundle(
        agent_id,
        base_url=base_url,
        credential_path=token_path,
        token=token,
        hook_runner_path=hook_runner_path,
    )

    mcp_target = _resolve_host_path(bundle.mcp_config["target_path"], resolved_home)
    hooks_target = _resolve_host_path(bundle.hooks_config["target_path"], resolved_home)
    skill_target = _resolve_host_path(bundle.skill_or_command["target_path"], resolved_home)

    _assert_path_safe(token_path, resolved_home)
    _assert_path_safe(mcp_target, resolved_home)
    _assert_path_safe(hooks_target, resolved_home)
    _assert_path_safe(skill_target, resolved_home)
    _assert_path_safe(manifest_target, resolved_home)
    existing_manifest = _load_manifest(agent_id, resolved_home)
    if manifest_target.exists() and not existing_manifest:
        raise ValueError("Unreadable ownership manifest; explicit recovery required")
    installed = set(existing_manifest.get("components", ["mcp", "hooks", "skill"])) if existing_manifest else set()
    if not installed <= {"mcp", "hooks", "skill"}:
        raise ValueError("Unknown installed component record")
    if needs_token and token_path.exists():
        if not existing_manifest or _sha256(token_path.read_text().strip()) != existing_manifest.get("token_hash"):
            raise ValueError("Existing credential is not owned; refusing replacement")
        if token_path.read_text().strip() != token.strip() and installed - selected:
            raise ValueError("Credential rotation would invalidate an unselected component")

    # 2. PREFLIGHT: Parse every existing file and check unowned collisions
    existing_mcp_raw = ""
    parsed_mcp_json: dict[str, Any] = {}
    if "mcp" in selected and mcp_target.is_file():
        existing_mcp_raw = mcp_target.read_text(encoding="utf-8")
        if host == "codex":
            parsed_codex_toml = _validate_toml_syntax(existing_mcp_raw, str(mcp_target))
            if "mcp_servers" in parsed_codex_toml and "contextcore" in parsed_codex_toml["mcp_servers"]:
                if not existing_manifest:
                    raise ValueError(
                        f"Refusing to overwrite pre-existing unowned MCP server 'contextcore' in {mcp_target}."
                    )
                current_entry = parsed_codex_toml["mcp_servers"]["contextcore"]
                if not _matches_owned_entry(existing_manifest, current_entry):
                    raise ValueError(
                        f"Refusing to overwrite externally modified or foreign MCP server 'contextcore' in {mcp_target}."
                    )
        else:
            parsed_mcp_json = _validate_json_syntax(existing_mcp_raw, str(mcp_target))
            try:
                mcp_servers, _mcp_path = _resolve_mcp_container(parsed_mcp_json, host)
            except ValueError as exc:
                raise ValueError(f"{exc} ({mcp_target})")
            if isinstance(mcp_servers, dict) and "contextcore" in mcp_servers:
                if not existing_manifest:
                    raise ValueError(
                        f"Refusing to overwrite pre-existing unowned MCP server 'contextcore' in {mcp_target}."
                    )
                current_entry = mcp_servers["contextcore"]
                if not _matches_owned_entry(existing_manifest, current_entry):
                    raise ValueError(
                        f"Refusing to overwrite externally modified or foreign MCP server 'contextcore' in {mcp_target}."
                    )

    existing_hooks_raw = ""
    parsed_hooks_json: dict[str, Any] = {}
    if "hooks" in selected and hooks_target.is_file():
        existing_hooks_raw = hooks_target.read_text(encoding="utf-8")
        if host not in ("omp", "opencode"):
            parsed_hooks_json = _validate_json_syntax(existing_hooks_raw, str(hooks_target))
        if host in ("omp", "opencode"):
            if not existing_manifest or "hooks" not in installed:
                raise ValueError(
                    f"Refusing to overwrite pre-existing unowned OMP extension at {hooks_target}."
                )
            current_hooks_hash = _sha256(existing_hooks_raw)
            expected_hooks_hash = existing_manifest.get("hooks_hash")
            if not expected_hooks_hash or current_hooks_hash != expected_hooks_hash:
                raise ValueError(
                    f"Refusing to overwrite externally modified extension at {hooks_target}."
                )
        elif host == "antigravity":
            owned_sections = ("contextcore-session", "contextcore-invoke", "contextcore-tool", "contextcore-stop")
            found_sections = [sec for sec in owned_sections if sec in parsed_hooks_json]
            if found_sections and (not existing_manifest or "hooks" not in installed):
                raise ValueError(
                    f"Refusing to overwrite pre-existing unowned hook sections {found_sections} in {hooks_target}."
                )
            if found_sections:
                from app.integrations.recovery import plan_recovery
                # Reuse exact legacy-section ownership proof before replacement.
                plan_recovery(host, resolved_home, components=["hooks"])

    legacy_skill_to_remove: Path | None = None
    if "skill" in selected:
        manifest_skill_target = (
            _assert_path_safe(Path(str(existing_manifest["skill_target"])), resolved_home)
            if existing_manifest and existing_manifest.get("skill_target")
            else None
        )
        allowed_legacy_skills = {
            _resolve_host_path(pattern, resolved_home).resolve()
            for pattern in LEGACY_HOST_SKILL_PATHS.get(host, ())
        }
        if manifest_skill_target and manifest_skill_target != skill_target.resolve():
            if manifest_skill_target not in allowed_legacy_skills:
                raise ValueError(
                    f"Unexpected legacy skill_target '{manifest_skill_target}' in manifest for {host}."
                )
            if manifest_skill_target.is_file():
                legacy_hash = _sha256(manifest_skill_target.read_text(encoding="utf-8"))
                if legacy_hash != existing_manifest.get("skill_hash"):
                    raise ValueError(
                        f"Refusing to remove externally modified legacy skill file at {manifest_skill_target}."
                    )
                legacy_skill_to_remove = manifest_skill_target
            if skill_target.is_file():
                raise ValueError(
                    f"Refusing to overwrite pre-existing unowned skill file at {skill_target}."
                )
        elif skill_target.is_file():
            if not existing_manifest:
                raise ValueError(
                    f"Refusing to overwrite pre-existing unowned skill file at {skill_target}."
                )
            current_skill_hash = _sha256(skill_target.read_text(encoding="utf-8"))
            if current_skill_hash != existing_manifest.get("skill_hash"):
                raise ValueError(
                    f"Refusing to overwrite externally modified skill file at {skill_target}."
                )
    # Transactional rollback tracking: stores exact original bytes and file mode
    backups: list[_FileBackup] = []

    def record_backup(p: Path):
        if p.is_file():
            st = p.stat()
            backups.append(
                _FileBackup(
                    path=p,
                    existed=True,
                    original_bytes=p.read_bytes(),
                    original_mode=stat.S_IMODE(st.st_mode),
                )
            )
        else:
            backups.append(
                _FileBackup(
                    path=p,
                    existed=False,
                    original_bytes=None,
                    original_mode=None,
                )
            )

    if needs_token: record_backup(token_path)
    for component, target in [("mcp", mcp_target), ("hooks", hooks_target), ("skill", skill_target)]:
        if component in selected: record_backup(target)
    if legacy_skill_to_remove is not None:
        record_backup(legacy_skill_to_remove)
    if runner_target is not None:
        record_backup(runner_target)
    record_backup(manifest_target)
    prepared_files = {}
    def write_target(path, content, mode=0o600):
        prepared_files[str(path)] = content.encode() if isinstance(content, str) else content
        if not prepare_only:
            _atomic_write_file(path, content, mode=mode)
    try:
        if needs_token:
            # 3. Write Token file securely (0600)
            write_target(token_path, token.strip() + "\n", mode=0o600)
            details.append(f"Saved token with 0600 permissions at {token_path}")

        if "mcp" in selected:
            # 4. Write / Merge MCP configuration (0600 because it contains literal token)
            owned_mcp_entry: Any = None
            if host == "codex":
                mcp_url = f"{base_url.rstrip('/')}/mcp/"
                updated_toml = _merge_codex_toml(existing_mcp_raw, mcp_url, token)
                write_target(mcp_target, updated_toml, mode=0o600)
                owned_mcp_entry = bundle.mcp_config["data"]["mcp_servers"]["contextcore"]
                details.append(f"Merged [mcp_servers.contextcore] into {mcp_target} (mode 0600)")
            else:
                if not isinstance(parsed_mcp_json, dict):
                    parsed_mcp_json = {}
                container, container_path = _resolve_mcp_container(parsed_mcp_json, host)
                if host == "opencode" and not existing_mcp_raw and "$schema" not in parsed_mcp_json:
                    parsed_mcp_json["$schema"] = bundle.mcp_config["data"].get("$schema", "https://opencode.ai/config.json")
                owned_mcp_entry = (
                    bundle.mcp_config["data"]["mcp"]["contextcore"]
                    if host == "opencode"
                    else bundle.mcp_config["data"]["mcpServers"]["contextcore"]
                )
                container["contextcore"] = owned_mcp_entry
                updated_json = json.dumps(parsed_mcp_json, indent=2, ensure_ascii=False) + "\n"
                write_target(mcp_target, updated_json, mode=0o600)
                details.append(f"Merged contextcore MCP server into {mcp_target} (mode 0600)")

        if runner_target is not None:
            runner_source = CC_HOOK_PATH.read_text(encoding="utf-8")
            main_body = runner_source.split("def main", 1)[-1].split("if __name__", 1)[0]
            if "enqueue_notification(" not in main_body or "post_to_contextcore(" in main_body:
                raise ValueError("Packaged host hook must enqueue a notification slot and must not POST from the host callback")
            write_target(runner_target, runner_source, mode=0o700)
            details.append(f"Installed enqueue-only host hook at {runner_target}")

        if "hooks" in selected:
            # 5. Write / Merge Hooks / Extension configuration
            owned_hook_commands: list[str] = []

            if host in ("omp", "opencode"):
                write_target(hooks_target, bundle.hooks_config["content"], mode=0o644)
                details.append(f"Wrote host extension to {hooks_target}")
            else:
                if host in ("codex", "claude-code"):
                    hooks_dict = parsed_hooks_json.setdefault("hooks", {})
                    new_hooks = bundle.hooks_config["data"]["hooks"]
                    for ent_list in new_hooks.values():
                        for ent in ent_list:
                            if isinstance(ent, dict):
                                if ent.get("command"):
                                    owned_hook_commands.append(ent["command"])
                                for sub_ent in ent.get("hooks", []):
                                    if isinstance(sub_ent, dict) and sub_ent.get("command"):
                                        owned_hook_commands.append(sub_ent["command"])

                    known_owned_cmds = set(owned_hook_commands)
                    if existing_manifest:
                        known_owned_cmds.update(existing_manifest.get("owned_hooks_commands", []))

                    for event_name, hook_entries in new_hooks.items():
                        existing_entries = hooks_dict.setdefault(event_name, [])
                        cleaned = []
                        for h in existing_entries:
                            cleaned_h, _ = _clean_hook_entry(h, known_owned_cmds)
                            if cleaned_h is not None:
                                cleaned.append(cleaned_h)
                        cleaned.extend(hook_entries)
                        hooks_dict[event_name] = cleaned

                    updated_hooks = json.dumps(parsed_hooks_json, indent=2, ensure_ascii=False) + "\n"
                    hooks_mode = stat.S_IMODE(hooks_target.stat().st_mode) if hooks_target.is_file() else 0o644
                    write_target(hooks_target, updated_hooks, mode=hooks_mode)
                    details.append(f"Merged owned hooks into {hooks_target}")

                elif host == "cursor":
                    hooks_dict = parsed_hooks_json.setdefault("hooks", {})
                    new_hooks = bundle.hooks_config["data"]["hooks"]
                    for ent_list in new_hooks.values():
                        for ent in ent_list:
                            if isinstance(ent, dict) and ent.get("command"):
                                owned_hook_commands.append(ent["command"])

                    known_owned_cmds = set(owned_hook_commands)
                    if existing_manifest:
                        known_owned_cmds.update(existing_manifest.get("owned_hooks_commands", []))

                    for event_name, hook_entries in new_hooks.items():
                        existing_entries = hooks_dict.setdefault(event_name, [])
                        cleaned = []
                        for h in existing_entries:
                            cleaned_h, _ = _clean_hook_entry(h, known_owned_cmds)
                            if cleaned_h is not None:
                                cleaned.append(cleaned_h)
                        cleaned.extend(hook_entries)
                        hooks_dict[event_name] = cleaned

                    updated_hooks = json.dumps(parsed_hooks_json, indent=2, ensure_ascii=False) + "\n"
                    hooks_mode = stat.S_IMODE(hooks_target.stat().st_mode) if hooks_target.is_file() else 0o644
                    write_target(hooks_target, updated_hooks, mode=hooks_mode)
                    details.append(f"Merged owned hooks into {hooks_target}")

                elif host == "antigravity":
                    new_sections = bundle.hooks_config["data"]
                    for sec_name, sec_val in new_sections.items():
                        parsed_hooks_json[sec_name] = sec_val
                        if isinstance(sec_val, dict):
                            for sub_v in sec_val.values():
                                if isinstance(sub_v, list):
                                    for item in sub_v:
                                        if isinstance(item, dict) and item.get("command"):
                                            owned_hook_commands.append(item["command"])

                    updated_hooks = json.dumps(parsed_hooks_json, indent=2, ensure_ascii=False) + "\n"
                    hooks_mode = stat.S_IMODE(hooks_target.stat().st_mode) if hooks_target.is_file() else 0o644
                    write_target(hooks_target, updated_hooks, mode=hooks_mode)
                    details.append(f"Merged owned hook sections into {hooks_target}")
        if "skill" in selected:
            # 6. Install Skill or Command (and clean up verified owned legacy skill path if migrating)
            write_target(skill_target, bundle.skill_or_command["content"], mode=0o644)
            details.append(f"Installed skill/command to {skill_target}")
            if legacy_skill_to_remove is not None:
                if len(prepared_files) + 2 <= MAX_CONNECTION_FILES:
                    prepared_files[str(legacy_skill_to_remove)] = None
                if not prepare_only:
                    legacy_skill_to_remove.unlink(missing_ok=True)
                    if legacy_skill_to_remove.parent.name == "contextcore-handoff":
                        try:
                            legacy_skill_to_remove.parent.rmdir()
                        except OSError:
                            pass
                details.append(f"Removed migrated legacy skill at {legacy_skill_to_remove}")
        # 7. Write Owner Manifest for ownership tracking
        manifest_data = {
            **(existing_manifest or {}),
            "components": sorted(installed | selected),
            "agent_id": agent_id,
            "version": "2.0.0",
            "token_path": str(token_path),
            "token_hash": _sha256(token.strip()) if needs_token else (existing_manifest or {}).get("token_hash"),
            "mcp_target": str(mcp_target),
            "mcp_format": bundle.mcp_config["format"],
            "mcp_container_path": (container_path if host != "codex" else ["mcp_servers"]) if "mcp" in selected else (existing_manifest or {}).get("mcp_container_path"),
            "mcp_shape": ("servers" if host == "opencode" and container_path == ["mcp", "servers"] else None) if "mcp" in selected else (existing_manifest or {}).get("mcp_shape"),
            "owned_mcp_entry_sha256": _entry_digest(owned_mcp_entry) if "mcp" in selected else (existing_manifest or {}).get("owned_mcp_entry_sha256"),
            "hooks_target": str(hooks_target),
            "owned_hooks_commands": sorted(set(owned_hook_commands)) if "hooks" in selected else (existing_manifest or {}).get("owned_hooks_commands", []),
            "hooks_hash": (_sha256(bundle.hooks_config["content"]) if host in ("omp", "opencode") else None) if "hooks" in selected else (existing_manifest or {}).get("hooks_hash"),
            "hook_runner_path": str(runner_target) if runner_target is not None else (existing_manifest or {}).get("hook_runner_path"),
            "hook_runner_hash": _sha256(CC_HOOK_PATH.read_text(encoding="utf-8")) if runner_target is not None else (existing_manifest or {}).get("hook_runner_hash"),
            "skill_target": str(skill_target),
            "skill_hash": _sha256(bundle.skill_or_command["content"]) if "skill" in selected else (existing_manifest or {}).get("skill_hash"),
        }
        if "owned_mcp_entry" in manifest_data:
            if "mcp" not in selected:
                manifest_data["owned_mcp_entry_sha256"] = _entry_digest(manifest_data["owned_mcp_entry"])
            del manifest_data["owned_mcp_entry"]
        write_target(
            manifest_target,
            json.dumps(manifest_data, indent=2, ensure_ascii=False) + "\n",
            mode=0o600,
        )
        details.append(f"Saved owner manifest at {manifest_target}")

        return InstallResult(
            agent_id=agent_id,
            status="prepared" if prepare_only else "installed",
            token_path=str(token_path),
            mcp_path=str(mcp_target),
            hooks_path=str(hooks_target),
            skill_path=str(skill_target),
            manifest_path=str(manifest_target),
            details=details,
            prepared_files=prepared_files,
        )

    except Exception:
        if prepare_only:
            raise
        # Rollback all changes upon any failure: restore original bytes and mode, never delete foreign
        for b in reversed(backups):
            try:
                if not b.existed:
                    b.path.unlink(missing_ok=True)
                else:
                    if b.original_bytes is not None:
                        _atomic_write_file(b.path, b.original_bytes, mode=b.original_mode or 0o600)
            except OSError:
                pass
        raise


def uninstall_host_integration(
    agent_id: str,
    *,
    confirmed: bool = False,
    home: Path | None = None,
    components: list[str] | None = None,
) -> UninstallResult:
    """Idempotently uninstall host integration entries behind confirmed=True.

    Owner Proof & Safety:
    - Consults owner manifest to verify ownership.
    - Preserves externally modified entries and files.
    - Never deletes foreign keys or configs.
    """
    if not confirmed:
        raise ValueError("uninstall_host_integration requires confirmed=True.")

    if agent_id not in SUPPORTED_HOSTS:
        raise ValueError(f"Unknown agent '{agent_id}'. Must be one of {SUPPORTED_HOSTS}")

    host: HostId = agent_id  # type: ignore
    resolved_home = Path(home).resolve() if home else Path.home().resolve()

    manifest = _load_manifest(agent_id, resolved_home)
    if not manifest:
        if _manifest_path(agent_id, resolved_home).exists():
            return UninstallResult(agent_id=agent_id, status="error",
                                   details=["Unreadable ownership manifest; explicit recovery required"])
        return UninstallResult(
            agent_id=agent_id,
            status="not_installed",
            details=["No owner manifest found. Refusing to guess unowned files."],
            skipped_modified=[],
        )

    is_valid, validation_err = _validate_manifest(agent_id, manifest, resolved_home)
    if not is_valid:
        return UninstallResult(
            agent_id=agent_id,
            status="error",
            details=[f"Invalid or corrupt owner manifest found: {validation_err}. Refusing to delete unverified files."],
            skipped_modified=[],
        )

    from app.integrations.recovery import RecoveryConflict, apply_recovery, plan_recovery

    try:
        plan = plan_recovery(host, resolved_home, components=components)
        backup = resolved_home / ".contextcore" / "recovery" / str(uuid4())
        apply_recovery(plan, backup, confirmed=True)
    except RecoveryConflict as exc:
        return UninstallResult(
            agent_id=agent_id, status="conflict", details=[str(exc)],
            skipped_modified=[str(manifest.get(key, "")) for key in
                              ("mcp_target", "hooks_target", "skill_target", "token_path")],
        )
    return UninstallResult(
        agent_id=agent_id, status="uninstalled",
        files_modified=[str(c.path) for c in plan.changes if c.before != c.after and c.after is not None],
        files_removed=[str(c.path) for c in plan.changes if c.before != c.after and c.after is None],
        details=[f"Verified owned entries removed; private recovery receipt: {backup / 'receipt.json'}"],
    )




