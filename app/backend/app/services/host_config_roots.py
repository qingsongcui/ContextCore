"""Bind management operations to the configuration root used by this host surface."""
from pathlib import Path

from app.integrations.config_transactions import ConfigurationError
from app.integrations.host_management_native import effective_host

ROOTS = {"codex": ("CODEX_HOME", ".codex"), "claude-code": ("CLAUDE_CONFIG_DIR", ".claude")}
PATH_FIELDS = ("provider_path", "mcp_path", "skill_dir", "prompt_dir", "prompt_path")


def _path(home, raw):
    return home / raw[2:] if raw.startswith("~/") else Path(raw)


def _root(home, variable, raw):
    path = _path(home, raw)
    reason = None
    if not path.is_absolute() or ".." in path.parts or path == home or not path.is_relative_to(home):
        reason = "outside_home"
    else:
        try:
            for part in (path, *path.parents):
                if part.is_symlink() or (part.exists() and not part.is_dir()):
                    reason = "unsafe_path"
                    break
        except (OSError, ValueError):
            reason = "unsafe_path"
    return {"variable": variable, "path": str(path), "status": "blocked" if reason else "custom", "reason": reason}


def override_root(host_id, home, env):
    """Describe a native override, including unsupported relative or linked paths."""
    if host_id not in ROOTS:
        return None
    variable, default = ROOTS[host_id]
    if not env.get(variable):
        return None
    root = _root(home, variable, env[variable])
    if host_id == "codex" and root["status"] != "blocked" and Path(root["path"]) == home / default:
        return None
    return root


def require_integration_root(host_id, surface, home, env):
    # The connection credential, grants and ownership manifest belong to the
    # default profile. Desktop Codex still uses that profile when CLI moves.
    if host_id == "codex" and surface == "desktop":
        return
    root = override_root(host_id, home, env)
    if root:
        raise ConfigurationError(
            f"Connection integration requires the default host configuration root. "
            f"{root['variable']} selects {root['path']}. Relaunch the assistant and Senrelay "
            f"with {root['variable']} unset before connecting, removing or recovering default-profile files.",
            409)


def resolve(manager, host_id, definition, env, *, display=False):
    result = effective_host(definition, manager.home)
    home = manager.home
    cli_profile = override_root('codex', home, env) if host_id in ('codex',) else None
    result["shared_configuration"] = host_id == "codex" and cli_profile is None
    if host_id not in ROOTS:
        return result
    variable, default = ROOTS[host_id]
    # Claude's explicitly selected default directory has a different user MCP
    # file. Codex's explicitly selected default directory keeps the same file.
    custom = cli_profile if host_id == 'codex' else override_root(host_id, home, env)
    root = Path(custom["path"]) if custom else home / default
    if custom:
        result["config_root"] = custom
        prefix = root.relative_to(home) if root.is_relative_to(home) else root
        for key in PATH_FIELDS:
            if not result.get(key):
                continue
            # Claude's global user MCP database normally sits next to .claude.
            tail = Path('.claude.json') if key == "mcp_path" and host_id == "claude-code" else Path(result[key]).relative_to(default)
            result[key] = str(prefix / tail)
    # Persist the actual MCP path as scope: unique across default and custom
    # Claude layouts even when their settings.json lives in the same directory.
    scope = result['mcp_path']
    result['root_scope'] = scope
    if not custom or custom['status'] != 'blocked':
        with manager.connect() as conn:
            active = conn.execute("SELECT json_extract(data,'$.root_scope') FROM management_activations WHERE host_id=?", (host_id,)).fetchone()
        if active and (active[0] or definition['mcp_path']) != scope:
            previous = Path(active[0] or definition['mcp_path'])
            previous_root = home / (default if previous == Path('.claude.json') else previous.parent)
            result['config_root'] = {'variable': variable, 'path': str(root), 'status': 'blocked',
                                     'reason': 'previous_root', 'previous_path': str(previous_root),
                                     'previous_unset': str(previous) == definition['mcp_path']}
    if not display and result.get('config_root', {}).get('status') == 'blocked':
        raise ConfigurationError('Native configuration root is unsafe or changed. Review the configuration directory on the assistant page before changing files.', 409)
    return result


def shared_membership(manager, host_id, kind, resource_kind=None):
    """Definitions assigned through either entry belong to their shared configuration."""
    if kind in ("provider", "account", "asset") and host_id in ("codex",):
        if manager.host_config(host_id, display=True)["shared_configuration"]:
            return ("codex",)
    return (host_id,)


def check_plan(manager, plan):
    meta = plan['payload']['metadata']
    if meta.get('namespace') == 'connections':
        raise ConfigurationError('Use the selected assistant connection plan to apply or recover this change.', 409)
    host_id = meta.get('host_id')
    if host_id not in ROOTS:
        return
    from app.services.management_service import HOSTS
    host = manager.host_config(host_id)
    if meta.get('root_scope', HOSTS[host_id]['mcp_path']) != host['root_scope']:
        raise ConfigurationError('Native configuration root changed since preview. Reopen the correct profile and create a new plan.', 409)
