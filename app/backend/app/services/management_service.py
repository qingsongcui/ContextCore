"""Model, account and tool assets, kept separate from compute authorization.

Saving metadata never applies host configuration or executes an MCP/Skill.
Only reviewed, hash-bound plans mutate explicitly selected host files.
"""
from __future__ import annotations

import base64
import hashlib
import copy
import io
import json
import os
import re
import sqlite3
import stat
import tomllib
import zipfile
import unicodedata
from contextlib import contextmanager
from functools import wraps
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit
from uuid import uuid4
from typing import Any

from app.config import settings
from app.integrations.config_transactions import (
    ConfigurationError, ConfigurationTransactions, SECRET_REF, SENSITIVE, canonical, redact, reject_secrets, configuration_lock,
)
from app.integrations.secret_store import KeychainSecretStore, SecretStore, SecretUnavailable

# Only verified native formats are rendered. Discovery/registration is not proof
# that a particular installed version loaded a configuration.
HOSTS = {
    "codex": {"name": "Codex CLI", "mcp_path": ".codex/config.toml", "format": "toml", "skill_dir": ".codex/skills", "prompt_path": ".codex/AGENTS.md"},
    "claude-code": {"name": "Claude Code", "mcp_path": ".claude.json", "format": "json", "container": "mcpServers", "provider_path": ".claude/settings.json", "skill_dir": ".claude/skills", "prompt_path": ".claude/CLAUDE.md"},
    "opencode": {"name": "OpenCode", "mcp_path": ".config/opencode/opencode.json", "format": "json", "container": "mcp", "provider_path": ".config/opencode/opencode.json", "skill_dir": ".config/opencode/skills"},
    "claude-desktop": {"name": "Claude Desktop", "mcp_path": "Library/Application Support/Claude/claude_desktop_config.json", "format": "json", "container": "mcpServers"},
    "gemini-cli": {"name": "Gemini CLI", "mcp_path": ".gemini/settings.json", "format": "json", "container": "mcpServers"},
    "grok-build": {"name": "Grok Build", "unsupported_reason": "Native configuration format has not been verified"},
    "openclaw": {"name": "OpenClaw", "unsupported_reason": "JSON5/plugin configuration requires a lossless native renderer"},
    "hermes": {"name": "Hermes", "unsupported_reason": "YAML provider and tool format requires a lossless native renderer"},
    "pi": {"name": "Pi", "unsupported_reason": "Pi and Oh My Pi are separate hosts; renderer not verified"},
    "minimax-code": {"name": "MiniMax Code", "unsupported_reason": "Native configuration format has not been verified"},
    "kimi-code": {"name": "Kimi Code", "unsupported_reason": "Native configuration format has not been verified"},
}
from app.integrations.host_formats_v2 import HOST_FORMATS, render_mcp_specs, render_provider_specs, render_activation_specs, validate_prompt
HOSTS.update(HOST_FORMATS)
from app.integrations.host_management_native import (
    NATIVE_HOSTS, effective_host, omp_provider_specs, omp_activation_specs,
    cursor_prompt_spec, antigravity_prompt_spec, native_skill,
    require_omp_models_migration, require_omp_settings_migration,
)
HOSTS.update(NATIVE_HOSTS)
HOSTS["codex"]["configuration_notice"] = NATIVE_HOSTS["codex-desktop"]["configuration_notice"]

ENTITY_KINDS = {"provider", "model", "binding", "account", "asset", "bundle"}


def _has_entity_reference(conn, kind, entity_id):
    if kind in ('provider', 'account'):
        field = 'provider_id' if kind == 'provider' else 'account_id'
        predicate = f"json_extract(e.data,'$.{field}')=?"
        params = [entity_id]
    elif kind == 'model':
        predicate = "e.kind='binding' AND json_extract(e.data,'$.model_id')=?"
        params = [entity_id]
    elif kind == 'asset':
        # Older definitions may have accepted a keyed membership object;
        # preserve its keys and the old text-membership behavior as references.
        predicate = """e.kind='bundle' AND (
            (json_type(e.data,'$.asset_ids')='text' AND instr(json_extract(e.data,'$.asset_ids'),?)>0)
            OR EXISTS(SELECT 1 FROM json_each(e.data,'$.asset_ids') member
                WHERE CASE WHEN json_type(e.data,'$.asset_ids')='object' THEN member.key ELSE member.value END=?))"""
        params = [entity_id, entity_id]
    else:
        return False
    return conn.execute('SELECT 1 FROM management_entities e WHERE e.id<>? AND (' + predicate + ') LIMIT 1', [entity_id, *params]).fetchone() is not None
_VAULT: SecretStore | None = None


def set_secret_store(store: SecretStore | None) -> None:
    """Dependency injection for tests/integrations. Never returns credentials."""
    global _VAULT
    _VAULT = store


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    if conn is None:
        settings.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(settings.db_path) as local:
            ensure_schema(local)
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS management_entities (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_management_kind ON management_entities(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_management_kind_page ON management_entities(kind,created_at,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_management_asset_kind ON management_entities(kind,json_extract(data,'$.kind'),created_at,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_management_provider_ref ON management_entities(kind,json_extract(data,'$.provider_id'))")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_provider_setups (
        request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
        provider_id TEXT NOT NULL, model_id TEXT NOT NULL, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_provider_revisions (
        request_id TEXT PRIMARY KEY, request_hash TEXT NOT NULL,
        provider_id TEXT NOT NULL, model_id TEXT NOT NULL, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_activations (
        host_id TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_routing_profiles (
        host_id TEXT PRIMARY KEY, version INTEGER NOT NULL, data TEXT NOT NULL,
        pending TEXT, updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_secret_cleanup (
        id TEXT PRIMARY KEY, credential_ref TEXT NOT NULL, reason TEXT NOT NULL,
        status TEXT NOT NULL, created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_asset_versions (
        asset_id TEXT NOT NULL, version INTEGER NOT NULL, data TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(asset_id,version))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_plans (
        id TEXT PRIMARY KEY, version INTEGER NOT NULL, status TEXT NOT NULL,
        payload TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_management_plan_host ON management_plans(json_extract(payload,'$.metadata.host_id'),created_at,id)")
    existing_idx = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_management_plan_host_updated'"
    ).fetchone()
    if existing_idx and existing_idx[0] and "WHERE" in existing_idx[0].upper():
        conn.execute("DROP INDEX IF EXISTS idx_management_plan_host_updated")
    conn.execute("""CREATE INDEX IF NOT EXISTS idx_management_plan_host_updated
        ON management_plans(
            json_extract(payload,'$.metadata.host_id'),
            updated_at DESC,
            id DESC,
            status,
            length(coalesce(json_extract(payload,'$.metadata'),''))
        )""")
    from app.services.selection_receipts import EVENT_FILTER
    conn.execute("CREATE INDEX IF NOT EXISTS idx_management_selection_events ON management_plans(json_extract(payload,'$.metadata.host_id'),updated_at DESC,id DESC) WHERE " + EVENT_FILTER)
    conn.execute("""CREATE TABLE IF NOT EXISTS management_owned (
        path TEXT NOT NULL,item_key TEXT NOT NULL,value_hash TEXT NOT NULL,
        owner TEXT NOT NULL,PRIMARY KEY(path,item_key))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_management_owned_owner ON management_owned(owner)")


def audited_plan_operation(fn):
    @wraps(fn)
    def wrapped(self, plan_id, *args, **kwargs):
        from app.database import db, log_audit
        target = plan_id if isinstance(plan_id, str) and re.fullmatch(r'plan_[a-f0-9]{32}', plan_id) else 'invalid-plan-id'
        action = 'management.plan.' + fn.__name__
        def record(result):
            with db(self.db_path) as conn:
                log_audit('local-management', action, target, result, conn=conn)
        record('attempted')
        try:
            result = fn(self, plan_id, *args, **kwargs)
        except Exception as failure:
            code = 'credential_unavailable' if isinstance(failure, SecretUnavailable) else 'filesystem_error' if isinstance(failure, OSError) else 'database_error' if isinstance(failure, sqlite3.Error) else 'operation_failed'
            if isinstance(failure, ConfigurationError):
                code = 'validation_rejected' if failure.status_code == 422 else 'configuration_conflict'
                for prefix, reason in [('Plan expired', 'plan_expired'), ('Apply was interrupted', 'recovery_required'), ('Explicit confirmation', 'confirmation_required')]:
                    if str(failure).startswith(prefix):
                        code = reason
                        break
                if code == 'recovery_required':
                    cause = self.transactions.get(plan_id)['payload'].get('failure', {}).get('code')
                    if cause in ('filesystem_error', 'database_error', 'configuration_validation_failed', 'apply_interrupted'):
                        code += ':' + cause
            record('failed:' + code)
            raise
        record(result.get('status', 'unknown') if result.get('status') in ('applied', 'recovered') else 'unknown')
        return result
    return wrapped


def configuration_locked(fn):
    from functools import wraps
    @wraps(fn)
    def guarded(*args, **kwargs):
        with configuration_lock:
            return fn(*args, **kwargs)
    return guarded


class ManagementService:
    def __init__(self, db_path: Path | None = None, home: Path | None = None, vault: SecretStore | None = None):
        self.db_path = Path(db_path or settings.db_path)
        self.home = Path(home or Path.home()).absolute()
        self.isolated_home = home is not None
        self.vault = vault or _VAULT or KeychainSecretStore()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            ensure_schema(conn)
            conn.execute('CREATE TABLE IF NOT EXISTS audit_logs (id TEXT PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL)')
        self.transactions = ConfigurationTransactions(self.db_path, self.db_path.parent / "configuration-snapshots", [self.home], self.vault)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 5000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def status(self) -> dict:
        with self.connect() as conn:
            counts = {row["kind"]: row["n"] for row in conn.execute("SELECT kind,COUNT(*) n FROM management_entities GROUP BY kind")}
            recovery = conn.execute("SELECT COUNT(*) FROM management_plans WHERE status IN ('applying','recovery_required')").fetchone()[0]
        return {"counts": counts, "recovery_required": recovery, "credential_backend": "macos_keychain", "compute_authorized_by_management": False,
                "oauth": {"status": "configurable_pkce", "reason": "Requires an explicitly registered official public client and exact local callback"},
                "capabilities": {"plan_apply_readback": True, "secret_free_export": True, "automatic_host_reload": False, "mcp_tools_probe": True}}

    def hosts(self) -> list[dict]:
        from app.services import host_registry
        registry = {entry["id"]: entry for entry in host_registry.discover_hosts(home=self.home, env={} if self.isolated_home else dict(os.environ))}
        result = []
        for host_id, host in HOSTS.items():
            host = effective_host(host, self.home)
            result.append({"id": host_id, "name": host["name"], "surface": "cli" if host_id in ("codex", "claude-code", "opencode", "omp", "gemini-cli", "grok-build", "kimi-code", "minimax-code", "pi", "antigravity-cli") else "desktop_or_service",
                           "configuration_status": "supported_format" if any(host.get(key) for key in ("mcp_path", "provider_path", "skill_dir", "prompt_path")) else "unsupported",
                           "verification_status": "not_verified", "unsupported_reason": host.get("unsupported_reason"), "provider_unsupported_reason": host.get("provider_unsupported_reason"), "provider_mode": host.get("provider_mode", "coexist_named_entries"),
                           "configuration_notice": host.get("configuration_notice"), "prompt_notice": host.get("prompt_notice"), "format_sources": host.get("sources", []),
                           "selection_config_only": host_id in ("omp", "codex-desktop"),
                           "operation_limits": {category: {"status": "external_host_ui_required", "operations": ["install", "update", "switch", "restore", "uninstall"], "reason": host["provider_unsupported_reason"], "sources": host.get("sources", [])} for category in ("providers",) if host.get("provider_unsupported_reason")},
                           "capabilities": {"mcp": bool(host.get("mcp_path")), "providers": bool(host.get("provider_path")) or host_id == "codex",
                                            "skills": bool(host.get("skill_dir")), "prompts": bool(host.get("prompt_path") or host.get("prompt_dir")), "oauth": False},
                           "paths": {key: str(self.home / value) for key, value in host.items() if key.endswith("_path") or key in ("skill_dir", "prompt_dir")}})
        for item in result:
            item["capabilities"]["native_activation"] = item["id"] in ("codex", "codex-desktop", "omp", "claude-code", "opencode", "pi", "kimi-code", "hermes", "minimax-code", "openclaw")
            item["provider_protocols"] = ["openai"] if item["id"] in ("codex", "codex-desktop") else ["openai", "anthropic", "gemini"] if item["id"] == "omp" else ["anthropic"] if item["id"] == "claude-code" else ["openai", "anthropic"] if item["capabilities"]["providers"] else []
            dependency = HOSTS[item["id"]].get("runtime_dependency")
            if dependency:
                from app.integrations.structured_config import runtime_status
                format_name = "yaml" if dependency == "ruamel.yaml" else "json5"
                item["format_runtime"] = runtime_status(format_name)
            identity = "antigravity" if item["id"] == "antigravity-cli" else "codex" if item["id"] == "codex-desktop" else item["id"]
            item["registry_host_id"] = identity
            item["configuration_surface"] = item["surface"]
            if identity in registry:
                discovery = registry[identity]["discovery"]
                if self.db_path == settings.db_path and not self.isolated_home:
                    try:
                        from app.services.connection_service import get_connection
                        discovery = get_connection(identity).get("discovery", discovery)
                    except Exception:
                        pass
                item["discovery"] = discovery
                with self.connect() as conn:
                    try:
                        item["observations"] = host_registry.get_observations(identity, conn=conn)
                    except sqlite3.OperationalError:
                        item["observations"] = []
                item["context_status"] = "shared_registry_capability"
            else:
                evidence = []
                for kind, raw_path in item["paths"].items():
                    path = Path(raw_path)
                    state = "symlink_requires_authorization" if any(part.is_symlink() for part in [path, *path.parents]) else "present" if path.exists() else "missing"
                    evidence.append({"kind": kind, "path": raw_path, "status": state})
                item["discovery"] = {"mode": "metadata", "execution_performed": False, "history_read": False, "credentials_read": False, "health": "not_probed", "authorization": "required", "evidence": evidence}
                item["observations"] = []
                item["context_status"] = "unsupported"
        return result

    def _row(self, row) -> dict:
        if not row:
            raise ConfigurationError("Managed object was not found", 404)
        return {**json.loads(row["data"]), "id": row["id"], "version": row["version"], "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def get(self, kind: str, entity_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM management_entities WHERE kind=? AND id=?", (kind, entity_id)).fetchone()
        return self._row(row)

    def list(self, kind: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM management_entities WHERE kind=? ORDER BY COALESCE(CASE WHEN json_type(data,'$.sort_index')='integer' THEN json_extract(data,'$.sort_index') END, 999999), created_at, id",
                (kind,),
            ).fetchall()
        return [self._public(self._row(row)) for row in rows]

    def catalog_page(self, kind: str, **options) -> dict:
        from app.services.management_catalog import catalog_page
        return catalog_page(self, kind, **options)

    def plan_page(self, host_id=None, limit=50, offset=0):
        if host_id is not None and host_id not in HOSTS:
            raise ConfigurationError('Unknown assistant', 422)
        return self.transactions.list_page(host_id, limit, offset)

    def assign_agent(self, kind, entity_id, host_id, selected, expected_version):
        if kind not in ('provider', 'account', 'asset', 'bundle') or host_id not in HOSTS or type(selected) is not bool:
            raise ConfigurationError('Choose a resource, assistant and explicit membership', 422)
        old = self.get(kind, entity_id)
        if type(expected_version) is not int or expected_version != old['version']:
            raise ConfigurationError('Object changed before updating assistant membership', 409)
        if isinstance(old.get('agent_ids'), list):
            agents = list(old['agent_ids'])
        else:
            agents = list(old.get('host_ids') or [])
            if kind == 'provider':
                with self.connect() as conn:
                    agents += [row[0] for row in conn.execute("SELECT DISTINCT json_extract(data,'$.host_id') FROM management_entities WHERE kind='binding' AND json_extract(data,'$.provider_id')=?", (entity_id,))]
        agents = list(dict.fromkeys(agent for agent in agents if agent in HOSTS and agent != host_id))
        if selected:
            agents.append(host_id)
        patch = {'agent_ids': agents}
        if kind == 'bundle' and selected:
            patch['host_ids'] = list(dict.fromkeys([*(old.get('host_ids') or []), host_id]))
        saved = self.save(kind, patch, entity_id, expected_version)
        return {'id': saved['id'], 'version': saved['version'], 'agent_ids': saved['agent_ids']}

    @configuration_locked
    def setup_provider(self, body: dict) -> dict:
        from app.services.management_experience import setup_provider
        return setup_provider(self, body)

    @configuration_locked
    def revise_provider_setup(self, body: dict) -> dict:
        from app.services.management_experience import revise_provider_setup
        return revise_provider_setup(self, body)

    @configuration_locked
    def host_state(self, host_id: str) -> dict:
        from app.services.management_experience import host_state
        return host_state(self, host_id)

    def check_env_conflicts(self, host_id: str | None = None) -> dict:
        from app.services.management_experience import check_env_conflicts
        return check_env_conflicts(self, host_id)

    @configuration_locked
    def reorder_providers(self, ordered_ids: list[str]) -> list[dict]:
        if not isinstance(ordered_ids, list) or not ordered_ids or len(ordered_ids) > 200 or any(not isinstance(pid, str) or not pid for pid in ordered_ids):
            raise ConfigurationError("ordered_ids must be a non-empty list of provider IDs", 422)
        unique_ids = list(dict.fromkeys(ordered_ids))
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for idx, pid in enumerate(unique_ids):
                row = conn.execute("SELECT data, version FROM management_entities WHERE kind='provider' AND id=?", (pid,)).fetchone()
                if not row:
                    raise ConfigurationError(f"Provider '{pid}' was not found", 404)
                data = json.loads(row["data"])
                data["sort_index"] = idx
                conn.execute(
                    "UPDATE management_entities SET data=?, version=version+1, updated_at=? WHERE id=? AND kind='provider'",
                    (canonical(data), now, pid),
                )
        return self.list("provider")

    @configuration_locked
    def quick_switch_provider(self, host_id: str, provider_id: str, model_id: str | None = None, *, auto_apply: bool = False) -> dict:
        """Prepare a switch; when auto_apply=True, apply immediately unless an external conflict requires diff review."""
        if host_id not in HOSTS:
            raise ConfigurationError("Unknown host", 422)
        provider = self.get("provider", provider_id)
        if auto_apply and host_id not in (provider.get("agent_ids") or []):
            self.assign_agent("provider", provider_id, host_id, True, provider.get("version"))
        selection = self.host_state(host_id).get("selection") or {}
        resolved_model = model_id or (selection.get("model_id") if selection.get("provider_id") == provider_id else None)
        if not resolved_model and auto_apply:
            provider_models = [m for m in self.list("model") if m.get("provider_id") == provider_id]
            default_mapped = (provider.get("model_map") or {}).get("default") or next(iter((provider.get("model_map") or {}).values()), None)
            if provider_models:
                matched = next((m["id"] for m in provider_models if default_mapped and m.get("model_id") == default_mapped), None)
                resolved_model = matched or provider_models[0]["id"]
            elif isinstance(default_mapped, str) and default_mapped.strip():
                resolved_model = default_mapped.strip()
        if not resolved_model:
            raise ConfigurationError("Select an explicit model before previewing a provider switch", 422)
        request = {"host_id": host_id, "provider_id": provider_id,
                   "activate_provider": True, "model_id": resolved_model}
        if host_id == "claude-code" and selection.get("provider_id") and selection["provider_id"] != provider_id:
            request["replace_provider_id"] = selection["provider_id"]
        plan = self.plan(request)
        if auto_apply:
            try:
                applied = self.apply(plan["id"], plan["version"], True)
                return {"ok": True, "status": "applied", "host_id": host_id, "provider_id": provider_id,
                        "model_id": resolved_model, "plan_id": applied["id"], "plan": applied}
            except ConfigurationError as exc:
                return {"ok": True, "status": "conflict_preview", "conflict_error": str(exc), "host_id": host_id,
                        "provider_id": provider_id, "model_id": resolved_model, "plan_id": plan["id"], "plan": plan}
        return {"ok": True, "status": "planned", "host_id": host_id, "provider_id": provider_id,
                "model_id": resolved_model, "plan_id": plan["id"], "plan": plan}

    @configuration_locked
    def quick_toggle_asset(self, host_id: str, asset_id: str, enabled: bool = True, *, auto_apply: bool = False) -> dict:
        """Prepare an owned-resource install/removal; when auto_apply=True, apply immediately unless a conflict occurs."""
        if type(enabled) is not bool:
            raise ConfigurationError("enabled must be a boolean", 422)
        if auto_apply and enabled:
            asset = self.get("asset", asset_id)
            if host_id not in (asset.get("agent_ids") or []):
                self.assign_agent("asset", asset_id, host_id, True, asset.get("version"))
        operation = "install" if enabled else "uninstall"
        plan = self.plan({"host_id": host_id, "asset_ids": [asset_id], "operation": operation})
        if auto_apply:
            try:
                applied = self.apply(plan["id"], plan["version"], True)
                return {"ok": True, "status": "applied", "host_id": host_id, "asset_id": asset_id,
                        "enabled": enabled, "operation": operation, "plan_id": applied["id"], "plan": applied}
            except ConfigurationError as exc:
                return {"ok": True, "status": "conflict_preview", "conflict_error": str(exc), "host_id": host_id,
                        "asset_id": asset_id, "enabled": enabled, "operation": operation, "plan_id": plan["id"], "plan": plan}
        return {"ok": True, "status": "planned", "host_id": host_id, "asset_id": asset_id,
                "enabled": enabled, "operation": operation, "plan_id": plan["id"], "plan": plan}

    @configuration_locked
    def sync_to_hosts(self, kind: str, entity_id: str, host_ids: list[str],
                      operation: str = "install", model_id: str | None = None, *, auto_apply: bool = False) -> dict:
        """Prepare separate host plans; when auto_apply=True, apply each host plan immediately if conflict-free."""
        if kind not in ("provider", "asset"):
            raise ConfigurationError("sync_to_hosts supports 'provider' or 'asset'", 422)
        if not isinstance(host_ids, list) or not host_ids or any(not isinstance(h, str) or h not in HOSTS for h in host_ids):
            raise ConfigurationError("Select at least one valid target host", 422)
        if operation not in ("install", "uninstall"):
            raise ConfigurationError("operation must be 'install' or 'uninstall'", 422)
        self.get(kind, entity_id)
        results, planned, conflicts, failed = {}, [], [], {}
        for host_id in dict.fromkeys(host_ids):
            try:
                if kind == "provider" and operation == "install":
                    result = self.quick_switch_provider(host_id, entity_id, model_id, auto_apply=auto_apply)
                elif kind == "asset":
                    result = self.quick_toggle_asset(host_id, entity_id, operation == "install", auto_apply=auto_apply)
                else:
                    plan = self.plan({"host_id": host_id, "provider_id": entity_id, "operation": operation})
                    if auto_apply:
                        try:
                            applied = self.apply(plan["id"], plan["version"], True)
                            result = {"ok": True, "status": "applied", "host_id": host_id, "plan_id": applied["id"], "plan": applied}
                        except ConfigurationError as exc:
                            result = {"ok": True, "status": "conflict_preview", "conflict_error": str(exc), "host_id": host_id, "plan_id": plan["id"], "plan": plan}
                    else:
                        result = {"ok": True, "status": "planned", "host_id": host_id, "plan_id": plan["id"], "plan": plan}
                results[host_id] = result
                if result.get("status") == "conflict_preview":
                    conflicts.append(host_id)
                else:
                    planned.append(host_id)
            except ConfigurationError as exc:
                failed[host_id] = str(exc)
                results[host_id] = {"ok": False, "status": "failed", "error": str(exc)}
        if not failed and not conflicts:
            overall_status = "applied" if auto_apply else "planned"
        elif conflicts and not planned and not failed:
            overall_status = "conflict_preview"
        else:
            overall_status = "partial_plan"
        return {
            "ok": not failed and not conflicts,
            "status": overall_status,
            "kind": kind,
            "entity_id": entity_id,
            "operation": operation,
            "planned": planned,
            "conflicts": conflicts,
            "failed": failed,
            "results": results,
        }

    def _public(self, data: dict) -> dict:
        value = copy.deepcopy(data)
        if value.get('kind') == 'oauth':
            from app.services.provider_credentials import account_status
            if value.get('status') == 'authorizing' and value.get('authorization_expires_at') is None and value.get('oauth_flow'):
                # Older pending flows predate the account's deadline metadata.
                with self.connect() as conn:
                    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE name='management_oauth_flows'").fetchone()
                    flow = conn.execute('SELECT expires_at FROM management_oauth_flows WHERE state_hash=? AND account_id=?', (value['oauth_flow'], value['id'])).fetchone() if exists else None
                if flow:
                    value['authorization_expires_at'] = flow['expires_at']
            value['status'] = account_status(value)
        if "protocol" in value or value.get("kind") in ("api_key", "oauth", "host_owned") or "credential_ref" in value:
            value["has_credential"] = bool(value.get("credential_ref"))
        if value.get("kind") in ("api_key", "oauth", "host_owned"):
            value.setdefault("quota", {"status": "unknown"})
        return value

    @staticmethod
    def _endpoint(value: str) -> str:
        try:
            url = urlsplit(value)
            _ = url.port
        except (ValueError, TypeError):
            raise ConfigurationError("Invalid provider endpoint", 422) from None
        if url.username or url.password or url.query or url.fragment or not url.hostname:
            raise ConfigurationError("Endpoint must not contain credentials, query or fragment", 422)
        if url.scheme != "https" and not (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")):
            raise ConfigurationError("HTTPS is required except for explicit loopback model endpoints", 422)
        from app.services.provider_checks import _blocked_special_ip
        if _blocked_special_ip(url.hostname, allow_loopback=True):
            raise ConfigurationError("Endpoint must not target a private or link-local address", 422)
        return value.rstrip("/")

    def _validate(self, kind: str, data: dict, *, entity_id=None) -> dict:
        if kind not in ENTITY_KINDS or not isinstance(data, dict):
            raise ConfigurationError("Unsupported managed object", 422)
        reject_secrets(data)
        if len(canonical(data).encode()) > 2 * 1024 * 1024:
            raise ConfigurationError("Managed object exceeds 2 MiB", 422)
        value = copy.deepcopy(data)
        for key in ("id", "version", "created_at", "updated_at", "expected_version", "has_credential"):
            value.pop(key, None)
        if kind in ("provider", "account", "asset", "model", "bundle"):
            name = value.get("name")
            if not isinstance(name, str) or not name.strip() or len(name) > 200:
                raise ConfigurationError("A name of 1–200 characters is required", 422)
            value["name"] = name.strip()
        if "agent_ids" in value:
            ids = value["agent_ids"]
            if not isinstance(ids, list) or any(not isinstance(item, str) or item not in HOSTS for item in ids):
                raise ConfigurationError("agent_ids must be a list of known assistants", 422)
            value["agent_ids"] = list(dict.fromkeys(ids))
        if "credential_ref" in value and value["credential_ref"] is not None:
            ref = value["credential_ref"]
            if not isinstance(ref, str) or not SECRET_REF.fullmatch("{{credential:" + ref + "}}"):
                raise ConfigurationError("Invalid Keychain reference", 422)
        if kind == "provider":
            value["base_url"] = self._endpoint(value.get("base_url", ""))
            if value.get("protocol") not in ("openai", "anthropic", "gemini"):
                raise ConfigurationError("Supported protocols: openai, anthropic, gemini", 422)
            from app.services.provider_endpoints import validate_endpoints
            validate_endpoints(value)
            if value.get('credential_source') not in (None, 'provider', 'account'):
                raise ConfigurationError('Choose provider or account as credential_source', 422)
            if value.get('credential_source') == 'account' and not value.get('account_id'):
                raise ConfigurationError('Select a bound account before using account credentials', 422)
            if value.get("oauth") is not None:
                if not isinstance(value["oauth"], dict) or not isinstance(value["oauth"].get("enabled"), bool):
                    raise ConfigurationError("OAuth config requires an explicit enabled boolean", 422)
                if value["oauth"]["enabled"]:
                    from app.services.oauth_service import OAuthService
                    OAuthService.validate_config(value["oauth"])
            if value.get("account_id"):
                account = self.get("account", value["account_id"])
                if account.get("provider_id") and account["provider_id"] != entity_id:
                    raise ConfigurationError("Account belongs to another provider", 422)
            value.setdefault("enabled", True)
            value.setdefault("model_map", {})
            if "sort_index" in value and value["sort_index"] is not None:
                if type(value["sort_index"]) is not int or not -1000000 <= value["sort_index"] <= 1000000:
                    raise ConfigurationError("sort_index must be an integer", 422)
            if not isinstance(value["enabled"], bool) or not isinstance(value["model_map"], dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value["model_map"].items()):
                raise ConfigurationError("Invalid provider enabled flag or model map", 422)
        if kind == "model":
            self.get("provider", value.get("provider_id", ""))
            if not isinstance(value.get("model_id"), str) or not value["model_id"].strip() or len(value['model_id']) > 200 or any(ord(char) < 32 for char in value['model_id']):
                raise ConfigurationError("A native model_id is required", 422)
            value['model_id'] = value['model_id'].strip()
            capacity = value.get('context_window')
            if capacity is not None and (type(capacity) is not int or not 0 < capacity <= 9223372036854775807):
                raise ConfigurationError('context_window must be a positive integer or unknown', 422)
        if kind == "binding":
            if value.get("host_id") not in HOSTS:
                raise ConfigurationError("Unknown host", 422)
            self.get("provider", value.get("provider_id", ""))
            if value.get("model_id"):
                model = self.get("model", value["model_id"])
                if model["provider_id"] != value["provider_id"]:
                    raise ConfigurationError("Model belongs to another provider", 422)
            self._check_project(value.get("project_id"))
        if kind == "account":
            if value.get("kind") not in ("api_key", "oauth", "host_owned"):
                raise ConfigurationError("Account kind must be api_key, oauth or host_owned", 422)
            if value.get("provider_id"):
                self.get("provider", value["provider_id"])
            value.setdefault("status", "unconfigured" if value["kind"] == "api_key" else "unsupported")
            if value.get("credential_ref") and value["kind"] == "api_key":
                value["status"] = "configured"
            elif value["status"] in ("configured", "authorized") and not value.get("credential_ref"):
                raise ConfigurationError("Account has no official credential evidence", 422)
            if value["kind"] != "api_key" and value["status"] not in ("unsupported", "cancelled", "logged_out", "expired", "authorized", "authorizing", "refreshing", "error"):
                raise ConfigurationError("No official OAuth/host login flow is configured", 422)
            if value["status"] not in ("unconfigured", "configured", "unsupported", "cancelled", "logged_out", "expired", "authorized", "authorizing", "refreshing", "error"):
                raise ConfigurationError("Account state has no official authorization evidence", 422)
        if kind == "asset":
            if value.get("kind") not in ("mcp", "skill", "prompt"):
                raise ConfigurationError("Asset kind must be mcp, skill or prompt", 422)
            if value["kind"] == "mcp":
                self._validate_mcp(value.get("config", {}))
            if value["kind"] == "prompt" and (not isinstance(value.get("content"), str) or len(value["content"]) > 200000):
                raise ConfigurationError("Prompt content must be text under 200000 characters", 422)
            if value["kind"] == "skill":
                if value.get("install_mode", "copy") not in ("copy", "symlink"):
                    raise ConfigurationError("Skill install_mode must be copy or symlink", 422)
                value.setdefault("install_mode", "copy")
                self._validate_skill(value.get("files", {}))
            if value["kind"] in ("prompt", "skill") and SECRET_REF.search(canonical(value)):
                raise ConfigurationError("Credentials cannot be embedded into prompts or Skills", 422)
            value.setdefault("source", {"type": "manual"})
        if kind == "bundle":
            self._check_project(value.get("project_id"))
            for asset_id in value.setdefault("asset_ids", []):
                self.get("asset", asset_id)
            for host in value.setdefault("host_ids", []):
                if host not in HOSTS:
                    raise ConfigurationError("Unknown bundle host", 422)
            if value.get("provider_id"):
                self.get("provider", value["provider_id"])
        return value

    def _check_project(self, project_id):
        if not project_id:
            return
        with self.connect() as conn:
            exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='projects'").fetchone()
            if not exists or not conn.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise ConfigurationError("Project was not found", 404)

    @staticmethod
    def _validate_mcp(config: dict):
        if not isinstance(config, dict):
            raise ConfigurationError("MCP config must be an object", 422)
        if config.get("transport", "stdio") == "stdio":
            if not isinstance(config.get("command"), str) or not config["command"].strip():
                raise ConfigurationError("stdio MCP requires a command", 422)
            if not isinstance(config.get("args", []), list) or not all(isinstance(arg, str) for arg in config.get("args", [])):
                raise ConfigurationError("MCP args must be a string array", 422)
            if not isinstance(config.get("env", {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in config.get("env", {}).items()):
                raise ConfigurationError("MCP env must map names to strings", 422)
        elif config["transport"] in ("http", "sse"):
            ManagementService._endpoint(config.get("url", ""))
            if not isinstance(config.get("headers", {}), dict):
                raise ConfigurationError("MCP headers must be an object", 422)
        else:
            raise ConfigurationError("Unknown MCP transport", 422)

    @staticmethod
    def _validate_skill_path(raw: str):
        if not isinstance(raw, str):
            raise ConfigurationError("Unsafe Skill archive path", 422)
        path = PurePosixPath(raw)
        if not raw or "\x00" in raw or raw != path.as_posix() or path.is_absolute() or ".." in path.parts or "\\" in raw or raw in (".", ".."):
            raise ConfigurationError("Unsafe Skill archive path", 422)

    @staticmethod
    def _validate_skill(files: dict):
        if not isinstance(files, dict) or "SKILL.md" not in files or not files or len(files) > 100:
            raise ConfigurationError("A Skill requires SKILL.md and at most 100 text files", 422)
        canonical_paths = set()
        for raw, text in files.items():
            ManagementService._validate_skill_path(raw)
            normalized = unicodedata.normalize("NFC", raw).casefold()
            if normalized in canonical_paths:
                raise ConfigurationError("Skill paths collide on macOS", 422)
            canonical_paths.add(normalized)
            if not isinstance(text, str) or len(text.encode()) > 500000:
                raise ConfigurationError("Skills accept bounded UTF-8 text files only", 422)
        names = canonical_paths
        if any(parent.as_posix() in names for name in names for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")):
            raise ConfigurationError("Skill files cannot also be directories", 422)

    @configuration_locked
    def save(self, kind: str, data: dict, entity_id: str | None = None, expected_version: int | None = None) -> dict:
        old = self.get(kind, entity_id) if entity_id else None
        if old and expected_version != old["version"]:
            raise ConfigurationError("Object version changed; reload before editing")
        if kind == "account" and data.get("status") in ("authorizing", "authorized", "refreshing", "error"):
            raise ConfigurationError("Account authorization state is controlled by the official flow", 422)
        merged = {**old, **data} if old else data
        if old and kind in ("asset", "account") and data.get("kind", old["kind"]) != old["kind"]:
            raise ConfigurationError("Object kind is immutable; create a separate object", 422)
        # Credential refs may only be created by this service or inherited.
        if "credential_ref" in data and (not old or data["credential_ref"] != old.get("credential_ref")):
            raise ConfigurationError("Change credentials through the credential endpoint", 422)
        value = self._validate(kind, merged, entity_id=entity_id)
        entity_id = entity_id or kind + "_" + uuid4().hex[:16]
        if kind == "provider" and value.get("account_id"):
            account = self.get("account", value["account_id"])
            if account.get("provider_id") and account["provider_id"] != entity_id:
                raise ConfigurationError("Account belongs to another provider", 422)
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            # Recheck references while holding the write claim; a concurrent
            # provider/account deletion cannot leave a dangling new binding.
            self._validate(kind, merged, entity_id=entity_id)
            if old:
                changed = conn.execute("UPDATE management_entities SET data=?,version=version+1,updated_at=? WHERE id=? AND kind=? AND version=?", (canonical(value), now, entity_id, kind, expected_version))
                if changed.rowcount != 1:
                    raise ConfigurationError("Object changed concurrently")
            else:
                conn.execute("INSERT INTO management_entities VALUES(?,?,?,1,?,?)", (entity_id, kind, canonical(value), now, now))
            if kind == "asset":
                conn.execute("INSERT INTO management_asset_versions VALUES(?,?,?,?)", (entity_id, old["version"] + 1 if old else 1, canonical(value), now))
        return self._public(self.get(kind, entity_id))

    def _store_secret(self, value: str) -> str:
        try:
            return self.vault.put(value)
        except SecretUnavailable as exc:
            if getattr(exc, "reference", None):
                with self.connect() as conn:
                    self._cleanup_receipt(conn, exc.reference, "unverified_keychain_write")
            raise

    @staticmethod
    def _cleanup_receipt(conn, reference: str, reason: str) -> str:
        receipt = "cleanup_" + uuid4().hex
        conn.execute("INSERT INTO management_secret_cleanup VALUES(?,?,?,'pending',?)", (receipt, reference, reason, datetime.now(timezone.utc).isoformat()))
        return receipt

    def retry_credential_cleanup(self, receipt_id: str | None = None) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM management_secret_cleanup WHERE status='pending'" + (" AND id=?" if receipt_id else "") + " LIMIT 100", (receipt_id,) if receipt_id else ()).fetchall()
        results = []
        for row in rows:
            try:
                self.vault.delete(row["credential_ref"])
            except SecretUnavailable:
                results.append({"id": row["id"], "status": "pending", "reason": "Keychain cleanup unavailable; reference is retained for recovery"})
                continue
            with self.connect() as conn:
                conn.execute("UPDATE management_secret_cleanup SET status='completed' WHERE id=?", (row["id"],))
            results.append({"id": row["id"], "status": "completed"})
        return results

    @configuration_locked
    def credential(self, kind: str, entity_id: str, secret: str) -> dict:
        if kind not in ("provider", "account"):
            raise ConfigurationError("Credentials are only supported for providers/accounts", 422)
        old = self.get(kind, entity_id)
        if kind == "account" and old["kind"] != "api_key":
            raise ConfigurationError("OAuth and host-owned credentials require an official flow", 422)
        reference = self._store_secret(secret)
        value = {key: item for key, item in old.items() if key not in ("id", "version", "created_at", "updated_at")}
        if kind == 'provider':
            from app.services.provider_credentials import selected_source
            value['credential_source'] = selected_source(old)
            value['credential_reattach_required'] = False
        value["credential_ref"] = reference
        if kind == "account":
            value["status"] = "configured"
        cleanup_id = None
        try:
            with self.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                changed = conn.execute("UPDATE management_entities SET data=?,version=version+1,updated_at=? WHERE id=? AND kind=? AND version=?", (canonical(value), datetime.now(timezone.utc).isoformat(), entity_id, kind, old["version"]))
                if changed.rowcount != 1:
                    raise ConfigurationError("Object changed while saving credential")
                if old.get("credential_ref"):
                    cleanup_id = self._cleanup_receipt(conn, old["credential_ref"], "credential_replaced")
        except Exception:
            # Persist the unadopted Keychain reference before attempting cleanup.
            with self.connect() as conn:
                receipt = self._cleanup_receipt(conn, reference, "credential_not_adopted")
            self.retry_credential_cleanup(receipt)
            raise
        cleanup = self.retry_credential_cleanup(cleanup_id) if cleanup_id else []
        return {**self._public(self.get(kind, entity_id)), "previous_credential_cleanup": cleanup[0]["status"] if cleanup else "not_needed"}

    @configuration_locked
    def delete(self, kind: str, entity_id: str, expected_version: int | None = None) -> dict:
        cleanup_ids = []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT version, json_extract(data,'$.credential_ref') AS credential_ref FROM management_entities WHERE id=? AND kind=?",
                (entity_id, kind),
            ).fetchone()
            if not row:
                raise ConfigurationError("Managed object was not found", 404)
            if expected_version is not None and row["version"] != expected_version:
                raise ConfigurationError("Object version changed")
            from app.services.host_routing import HostRouting
            HostRouting.assert_not_referenced(conn, entity_id)
            if conn.execute("SELECT 1 FROM management_owned WHERE owner=? LIMIT 1", (entity_id,)).fetchone():
                raise ConfigurationError("Uninstall owned host entries before deleting this object")
            if kind == "provider":
                from app.services.selection_receipts import HEADER_SQL, selected_state
                for activation in conn.execute(f"SELECT host_id,{HEADER_SQL} data FROM management_activations"):
                    selection = selected_state(conn, activation['host_id'], json.loads(activation['data']))
                    if selection.get('selection_receipt_unknown'):
                        raise ConfigurationError('Legacy selection receipt is incomplete; explicitly activate a model before deleting providers', 409)
                    if selection.get("provider_id") == entity_id:
                        raise ConfigurationError("Restore the active host selection before deleting its provider")
            if _has_entity_reference(conn, kind, entity_id):
                raise ConfigurationError("Object is referenced; remove bindings and dependent objects first")
            if row["credential_ref"]:
                cleanup_ids.append(self._cleanup_receipt(conn, row["credential_ref"], "object_deleted"))
            if kind == "account" and conn.execute("SELECT 1 FROM sqlite_master WHERE name='management_oauth_flows'").fetchone():
                flows = conn.execute("SELECT verifier_ref FROM management_oauth_flows WHERE account_id=? AND status IN ('pending','exchanging')", (entity_id,)).fetchall()
                for flow in flows:
                    cleanup_ids.append(self._cleanup_receipt(conn, flow["verifier_ref"], "oauth_account_deleted"))
                conn.execute("UPDATE management_oauth_flows SET status='cancelled' WHERE account_id=? AND status IN ('pending','exchanging')", (entity_id,))
            changed = conn.execute("DELETE FROM management_entities WHERE kind=? AND id=? AND version=?", (kind, entity_id, row["version"]))
            if changed.rowcount != 1:
                raise ConfigurationError("Object changed before deletion")
            if kind == "asset":
                conn.execute("DELETE FROM management_asset_versions WHERE asset_id=?", (entity_id,))
        # Cleanup occurs after the entity CAS/claim commits. A concurrent
        # credential update now either precedes the version check or fails CAS
        # and records its own cleanup receipt; it cannot become orphaned.
        cleanup = [result for receipt in cleanup_ids for result in self.retry_credential_cleanup(receipt)]
        return {"deleted": True, "host_configuration": "unchanged", "credential_cleanup": cleanup}

    def account_action(self, entity_id: str, action: str) -> dict:
        account = self.get("account", entity_id)
        if account["kind"] == "oauth":
            from app.services.oauth_service import OAuthService
            oauth = OAuthService(self)
            if action == "authorize":
                if not account.get("provider_id"):
                    return {"account_id": entity_id, "status": "unsupported", "reason": "No official OAuth provider/client is configured", "authorization_url": None}
                return oauth.authorize(entity_id)
            if action == "cancel":
                return oauth.cancel(entity_id)
            if action == "logout":
                return oauth.logout(entity_id)
        if action in ("authorize", "refresh"):
            return {"account_id": entity_id, "status": "unsupported", "reason": "This account has no configured official OAuth flow", "authorization_url": None}
        if action not in ("cancel", "logout"):
            raise ConfigurationError("Unsupported account action", 422)
        if action == "logout" and account.get("credential_ref"):
            self.remove_credential("account", entity_id)
            account = self.get("account", entity_id)
        patch = {"status": "cancelled" if action == "cancel" else "logged_out"}
        return self.save("account", patch, entity_id, account["version"])

    @configuration_locked
    def remove_credential(self, kind: str, entity_id: str) -> dict:
        if kind not in ("provider", "account"):
            raise ConfigurationError("Object has no credential lifecycle", 422)
        cleanup_id = None
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM management_entities WHERE kind=? AND id=?", (kind, entity_id)).fetchone()
            self._row(row)
            data = json.loads(row["data"])
            if data.get("credential_ref"):
                cleanup_id = self._cleanup_receipt(conn, data["credential_ref"], "credential_detached")
                if kind == 'provider':
                    from app.services.provider_credentials import selected_source
                    if selected_source(data) == 'provider':
                        data['credential_source'] = 'provider'
                        data['credential_reattach_required'] = True
            data["credential_ref"] = None
            if kind == "account":
                data["status"] = "unconfigured"
            conn.execute("UPDATE management_entities SET data=?,version=version+1,updated_at=? WHERE id=? AND version=?", (canonical(data), datetime.now(timezone.utc).isoformat(), entity_id, row["version"]))
        cleanup = self.retry_credential_cleanup(cleanup_id) if cleanup_id else []
        return {**self._public(self.get(kind, entity_id)), "credential_cleanup": cleanup}

    @staticmethod
    def _clean_mcp_config_for_export(config: dict) -> tuple[dict, bool]:
        cleaned = copy.deepcopy(config)
        had_credentials = False
        if "env" in cleaned and isinstance(cleaned["env"], dict):
            new_env = {}
            for k, v in cleaned["env"].items():
                if isinstance(v, str) and SECRET_REF.search(v):
                    had_credentials = True
                else:
                    new_env[k] = v
            cleaned["env"] = new_env
        if "headers" in cleaned and isinstance(cleaned["headers"], dict):
            new_headers = {}
            for k, v in cleaned["headers"].items():
                if isinstance(v, str) and SECRET_REF.search(v):
                    had_credentials = True
                else:
                    new_headers[k] = v
            cleaned["headers"] = new_headers
        if "args" in cleaned and isinstance(cleaned["args"], list):
            new_args = []
            skip_next = False
            for i, arg in enumerate(cleaned["args"]):
                if skip_next:
                    skip_next = False
                    continue
                if isinstance(arg, str):
                    if SECRET_REF.search(arg):
                        had_credentials = True
                        continue
                    if i + 1 < len(cleaned["args"]):
                        next_arg = cleaned["args"][i + 1]
                        if isinstance(next_arg, str) and SECRET_REF.search(next_arg):
                            had_credentials = True
                            skip_next = True
                            continue
                new_args.append(arg)
            cleaned["args"] = new_args
        return cleaned, had_credentials

    def export(self, kind: str | None = None) -> dict:
        collections = [kind] if kind else ["asset", "provider", "account", "model", "bundle", "binding"]
        objects = []
        for collection in collections:
            if collection not in ENTITY_KINDS:
                raise ConfigurationError("Unknown export kind", 422)
            for obj in self.list(collection):
                value = copy.deepcopy(obj)
                credential_required = bool(value.get('credential_ref') or value.get('account_id') or value.get('credential_source') == 'account')
                value.pop("credential_ref", None)
                value.pop("has_credential", None)
                value.pop("previous_credential_cleanup", None)
                value.pop("cc_switch_current_hosts", None)
                if isinstance(value.get("source"), dict) and value["source"].get("type") == "cc-switch":
                    value["source"] = {"type": "local-sync"}
                if collection == "account":
                    for private_metadata in ("oauth_flow", "token_type", "expires_at", "authorization_expires_at"):
                        value.pop(private_metadata, None)
                    value["status"] = "unconfigured" if value.get("kind") == "api_key" else "unsupported"
                elif collection == 'provider' and credential_required:
                    value['credential_reattach_required'] = True
                elif collection == "asset":
                    if value.get("kind") == "mcp" and isinstance(value.get("config"), dict):
                        cleaned_cfg, had_creds = self._clean_mcp_config_for_export(value["config"])
                        value["config"] = cleaned_cfg
                        if had_creds:
                            value["credential_reattach_required"] = True
                    elif SECRET_REF.search(canonical(value)):
                        value = json.loads(SECRET_REF.sub("", canonical(value)))
                        value["credential_reattach_required"] = True
                objects.append({"entity_kind": collection, "data": value})
        return {
            "format": "senrelay-management",
            "version": 1,
            "managed_credentials_excluded": True,
            "embedded_text_review_required": True,
            "review_notice": "Saved managed credentials are excluded, but embedded text in skills, prompts, and configurations is unverified and requires review before sharing.",
            "objects": objects,
        }

    @staticmethod
    def _download_source(url: str, limit: int) -> bytes:
        from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
        from urllib.error import URLError
        try:
            parsed = urlsplit(url)
            _ = parsed.port
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ConfigurationError("Registry/archive sources require explicit credential-free HTTPS URLs", 422)
            from app.services.provider_checks import _blocked_special_ip
            if _blocked_special_ip(parsed.hostname, allow_loopback=False):
                raise ConfigurationError("Registry/archive sources must not target loopback, private, or link-local addresses", 422)
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            with build_opener(ProxyHandler({}), NoRedirect()).open(Request(url, headers={"User-Agent": "Senrelay-Skill-Import"}), timeout=15) as response:
                content = response.read(limit + 1)
            if len(content) > limit:
                raise ConfigurationError("Registry/archive download exceeds the size limit", 422)
            return content
        except (URLError, OSError, ValueError, TypeError):
            raise ConfigurationError("Selected registry/archive could not be read without redirects or credentials", 422) from None

    def import_objects(self, payload: dict) -> dict:
        agent_id = payload.get("agent_id")
        if agent_id is not None and (not isinstance(agent_id, str) or agent_id not in HOSTS):
            raise ConfigurationError("Select a known assistant before importing", 422)
        scope = {"agent_ids": [agent_id]} if agent_id and not payload.get("target_id") else {}
        format_name, raw = payload.get("format", "senrelay"), payload.get("data")
        if format_name in ("skill-local", "skill-zip", "skill-github", "skill-registry"):
            # Source updates inherit mode unless explicitly changed; never
            # silently turn a linked installation back into a copy.
            target = self.get("asset", payload["target_id"]) if payload.get("target_id") else {}
            payload = {**payload, "install_mode": payload.get("install_mode", target.get("install_mode", "copy"))}
            if payload["install_mode"] not in ("copy", "symlink"):
                raise ConfigurationError("Skill install_mode must be copy or symlink", 422)
        if format_name == "skill-registry":
            if not isinstance(raw, dict) or not isinstance(raw.get("manifest_url"), str):
                raise ConfigurationError("Select an explicit HTTPS registry manifest URL", 422)
            try:
                manifest = json.loads(self._download_source(raw["manifest_url"], 128 * 1024))
            except (ValueError, UnicodeDecodeError):
                raise ConfigurationError("Registry manifest must be bounded JSON", 422) from None
            if not isinstance(manifest, dict) or set(manifest) - {"name", "version", "source", "description"} or not isinstance(manifest.get("source"), dict):
                raise ConfigurationError("Registry manifest contains unsupported fields or executable actions", 422)
            reject_secrets(manifest)
            source = manifest["source"]
            options = {**payload, "name": payload.get("name") or manifest.get("name", "Registry Skill")}
            if source.get("type") == "github":
                if set(source) - {"type", "url", "ref", "path"}:
                    raise ConfigurationError("Unsupported registry GitHub source fields", 422)
                result = self.import_objects({**options, "format": "skill-github", "data": {key: value for key, value in source.items() if key != "type"}})
            elif source.get("type") == "zip":
                if set(source) - {"type", "url", "sha256"} or not isinstance(source.get("sha256"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", source["sha256"]):
                    raise ConfigurationError("Registry ZIP requires a pinned SHA-256", 422)
                content = self._download_source(source.get("url"), 2 * 1024 * 1024)
                import hashlib
                if hashlib.sha256(content).hexdigest() != source["sha256"].lower():
                    raise ConfigurationError("Registry archive hash did not match; nothing was imported", 422)
                result = self.import_objects({**options, "format": "skill-zip", "data": base64.b64encode(content).decode()})
            else:
                raise ConfigurationError("Registry source must be a fixed GitHub commit or hash-pinned ZIP; scripts are never executed", 422)
            for item in result["imported"]:
                updated = self.save("asset", {"source": {"type": "registry", "manifest_url": raw["manifest_url"], "version": manifest.get("version"), "origin": item.get("source")}}, item["id"], item["version"])
                item.update(updated)
            return result
        if format_name == "skill-local":
            if not isinstance(raw, str) or "\x00" in raw or not Path(raw).is_absolute() or ".." in Path(raw).parts:
                raise ConfigurationError("Select an explicit absolute Skill source directory", 422)
            root = Path(raw)
            if root.is_symlink() or not root.is_dir():
                raise ConfigurationError("Skill source must be a regular directory, not a symlink", 422)
            for parent in root.parents:
                if parent.is_symlink():
                    raise ConfigurationError("Symlink source ancestors are not supported", 422)
            files, total = {}, 0
            for directory, subdirs, names in os.walk(root, followlinks=False):
                if any((Path(directory) / name).is_symlink() for name in subdirs + names):
                    raise ConfigurationError("Skill sources cannot contain symlinks", 422)
                for name in names:
                    path = Path(directory) / name
                    if len(files) >= 100 or any(part.is_symlink() for part in [path, *path.parents]):
                        raise ConfigurationError("Skill source exceeds limits or contains symlinks", 422)
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                        with os.fdopen(fd, "rb") as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode) or info.st_size > 500000:
                                raise ConfigurationError("Skill sources require bounded regular text files", 422)
                            content = stream.read(500001)
                            if len(content) > 500000:
                                raise ConfigurationError("Skill source exceeds bounded text limits", 422)
                            text = content.decode("utf-8")
                    except (UnicodeDecodeError, OSError):
                        raise ConfigurationError("Skill source contains unreadable, linked or binary files", 422) from None
                    total += len(text.encode())
                    if total > 2 * 1024 * 1024:
                        raise ConfigurationError("Skill source exceeds 2 MiB", 422)
                    files[path.relative_to(root).as_posix()] = text
            asset = {**scope, "kind": "skill", "name": payload.get("name") or root.name, "install_mode": payload["install_mode"], "files": files, "source": {"type": "local", "path": str(root)}}
            return {"imported": [self.save("asset", asset, payload.get("target_id"), payload.get("expected_version"))], "unsupported": []}
        if format_name == "skill-github":
            if not isinstance(raw, dict):
                raise ConfigurationError("GitHub source requires url, pinned 40-character commit and optional path", 422)
            parsed = urlsplit(raw.get("url", ""))
            parts = parsed.path.strip("/").split("/")
            ref = raw.get("ref", "")
            if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username or parsed.password or parsed.query or parsed.fragment or len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) and part not in (".", "..") for part in parts) or not re.fullmatch(r"[a-fA-F0-9]{40}", ref):
                raise ConfigurationError("Only public github.com owner/repository at a pinned commit is supported", 422)
            from urllib.request import Request, build_opener, HTTPRedirectHandler
            from urllib.error import URLError
            class NoRedirect(HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None
            download = f"https://codeload.github.com/{parts[0]}/{parts[1]}/zip/{ref}"
            try:
                with build_opener(NoRedirect()).open(Request(download, headers={"User-Agent": "Senrelay-Skill-Import"}), timeout=15) as response:
                    content = response.read(2 * 1024 * 1024 + 1)
                if len(content) > 2 * 1024 * 1024:
                    raise ConfigurationError("GitHub archive exceeds 2 MiB", 422)
                subpath = PurePosixPath(raw.get("path", ""))
                if subpath.is_absolute() or ".." in subpath.parts or "\\" in str(subpath):
                    raise ConfigurationError("Unsafe GitHub Skill path", 422)
                output = io.BytesIO()
                with zipfile.ZipFile(io.BytesIO(content)) as original, zipfile.ZipFile(output, "w") as selected:
                    expected_prefix = parts[1] + "-" + ref + "/"
                    selected_prefix = expected_prefix + (str(subpath).rstrip("/") + "/" if str(subpath) not in ("", ".") else "")
                    count, total = 0, 0
                    for item in original.infolist():
                        if not item.filename.startswith(selected_prefix):
                            continue
                        relative = item.filename[len(selected_prefix):].rstrip("/")
                        if relative:
                            self._validate_skill_path(relative)
                        if stat.S_ISLNK(item.external_attr >> 16):
                            raise ConfigurationError("GitHub Skill sources cannot contain symlinks", 422)
                        if item.is_dir():
                            continue
                        count += 1
                        total += item.file_size
                        if total > 2 * 1024 * 1024 or count > 100 or item.file_size > 500000 or stat.S_ISLNK(item.external_attr >> 16):
                            raise ConfigurationError("GitHub Skill exceeds limits or contains symlinks", 422)
                        selected.writestr(item.filename[len(selected_prefix):], original.read(item))
                result = self.import_objects({**payload, "format": "skill-zip", "data": base64.b64encode(output.getvalue()).decode()})
                for item in result["imported"]:
                    updated = self.save("asset", {"source": {"type": "github", "url": raw["url"], "ref": ref, "path": raw.get("path", "")}}, item["id"], item["version"])
                    item.update(updated)
                return result
            except (URLError, OSError, zipfile.BadZipFile, ValueError):
                raise ConfigurationError("Public pinned GitHub Skill could not be imported; no credentials or redirects were used", 422) from None
        if format_name == "skill-zip":
            try:
                archive_bytes = base64.b64decode(raw, validate=True)
                if len(archive_bytes) > 2 * 1024 * 1024:
                    raise ValueError()
                files = {}
                with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
                    if len(archive.infolist()) > 300:
                        raise ValueError()
                    total = 0
                    for item in archive.infolist():
                        self._validate_skill_path(item.filename.rstrip("/"))
                        mode = item.external_attr >> 16
                        if stat.S_ISLNK(mode) or item.flag_bits & 1 or item.file_size > 500000:
                            raise ValueError()
                        if item.is_dir():
                            continue
                        total += item.file_size
                        if total > 2 * 1024 * 1024:
                            raise ValueError()
                        # Finder ZIP metadata is not Skill content. Validate its
                        # paths, type, size and total budget before ignoring it.
                        if item.filename.startswith('__MACOSX/') or PurePosixPath(item.filename).name == '.DS_Store':
                            continue
                        if item.filename in files:
                            raise ValueError()
                        files[item.filename] = archive.read(item).decode("utf-8")
                wrapper = None
                if 'SKILL.md' not in files:
                    roots = [name[:-len('SKILL.md')] for name in files if name.endswith('/SKILL.md')]
                    if len(roots) != 1 or not all(name.startswith(roots[0]) for name in files):
                        raise ConfigurationError("ZIP must contain one unambiguous Skill directory with SKILL.md", 422)
                    wrapper = roots[0].rstrip('/').split('/')[-1]
                    files = {name[len(roots[0]):]: text for name, text in files.items()}
                inferred_name = wrapper or 'Imported Skill'
                lines = files.get('SKILL.md', '').splitlines()[:80]
                if lines and lines[0].strip() == '---':
                    for line in lines[1:]:
                        if line.strip() == '---':
                            break
                        if line.startswith('name:') and line[5:].strip():
                            inferred_name = line[5:].strip().strip('\"\'')
                            break
                return {"imported": [self.save("asset", {**scope, "kind": "skill", "name": payload.get("name") or inferred_name, "install_mode": payload["install_mode"], "files": files, "source": {"type": "zip"}}, payload.get("target_id"), payload.get("expected_version"))], "unsupported": []}
            except (ValueError, TypeError, zipfile.BadZipFile, UnicodeDecodeError, RuntimeError):
                raise ConfigurationError("Invalid or unsafe Skill ZIP", 422) from None
        if format_name in ("cc-switch", "local-sync", "compatible"):
            # Explicit supplied copy only. Never read external auth storage.
            if not isinstance(raw, dict):
                raise ConfigurationError("Supply a compatible config JSON copy", 422)
            source_type = "cc-switch" if format_name == "cc-switch" else "local-sync"
            rejected = []
            objects = []
            supported_cc_keys = {"mcpServers", "providers", "prompts", "skills"}
            for name, config in (raw.get("mcpServers") or {}).items():
                if isinstance(config, dict):
                    objects.append({"entity_kind": "asset", "data": {"name": name, "kind": "mcp", "config": {"transport": "http" if config.get("url") else "stdio", **config}, "source": {"type": source_type}}})
            raw_providers = raw.get("providers")
            if isinstance(raw_providers, dict):
                raw_providers = [{"name": k, **(v if isinstance(v, dict) else {})} for k, v in raw_providers.items()]
            if isinstance(raw_providers, list):
                for idx, prov in enumerate(raw_providers):
                    if isinstance(prov, dict):
                        p_name = str(prov.get("name") or prov.get("id") or f"Provider {idx + 1}")
                        p_proto = str(prov.get("protocol") or prov.get("api_type") or "openai").lower()
                        if p_proto not in ("openai", "anthropic", "gemini"):
                            p_proto = "anthropic" if "claude" in p_proto or "anthropic" in p_proto else ("gemini" if "gemini" in p_proto else "openai")
                        p_url = str(prov.get("base_url") or prov.get("endpoint") or prov.get("api_url") or "https://api.openai.com/v1")
                        raw_map = prov.get("model_map") if isinstance(prov.get("model_map"), dict) else {}
                        model_map = {
                            str(k).strip(): str(v).strip()
                            for k, v in raw_map.items()
                            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()
                        }
                        for m_field in ("model", "default_model", "model_id"):
                            m_val = prov.get(m_field)
                            if isinstance(m_val, str) and m_val.strip():
                                model_map.setdefault(m_val.strip(), m_val.strip())
                        if isinstance(prov.get("models"), list):
                            for m_item in prov["models"]:
                                if isinstance(m_item, str) and m_item.strip():
                                    model_map.setdefault(m_item.strip(), m_item.strip())
                                elif isinstance(m_item, dict):
                                    mid = m_item.get("model_id") or m_item.get("id") or m_item.get("name")
                                    if isinstance(mid, str) and mid.strip():
                                        model_map.setdefault(mid.strip(), mid.strip())
                        p_data: dict[str, Any] = {
                            "name": p_name,
                            "protocol": p_proto,
                            "base_url": p_url,
                            "enabled": bool(prov.get("enabled", True)),
                            "sort_index": prov.get("sort_index", idx) if type(prov.get("sort_index", idx)) is int else idx,
                            "model_map": model_map,
                        }
                        if isinstance(prov.get("wire_api"), str) and prov["wire_api"] in ("chat", "responses") and p_proto == "openai":
                            p_data["wire_api"] = prov["wire_api"]
                        if isinstance(prov.get("supported_endpoints"), list):
                            p_data["supported_endpoints"] = prov["supported_endpoints"]
                        objects.append({
                            "entity_kind": "provider",
                            "data": p_data,
                        })
            raw_prompts = raw.get("prompts")
            if isinstance(raw_prompts, dict):
                raw_prompts = [{"name": k, **(v if isinstance(v, dict) else {"content": str(v)})} for k, v in raw_prompts.items()]
            if isinstance(raw_prompts, list):
                for idx, prm in enumerate(raw_prompts):
                    if isinstance(prm, dict):
                        objects.append({
                            "entity_kind": "asset",
                            "data": {
                                "name": str(prm.get("name") or f"Prompt {idx + 1}"),
                                "kind": "prompt",
                                "content": str(prm.get("content") or prm.get("prompt") or ""),
                                "description": str(prm.get("description") or ""),
                                "source": {"type": source_type},
                            },
                        })
            raw_skills = raw.get("skills")
            if isinstance(raw_skills, dict):
                raw_skills = [{"name": k, **(v if isinstance(v, dict) else {"content": str(v)})} for k, v in raw_skills.items()]
            if isinstance(raw_skills, list):
                for idx, skl in enumerate(raw_skills):
                    if isinstance(skl, dict):
                        files = skl.get("files") if isinstance(skl.get("files"), dict) else {"SKILL.md": str(skl.get("content") or f"# {skl.get('name') or 'Skill'}\n")}
                        objects.append({
                            "entity_kind": "asset",
                            "data": {
                                "name": str(skl.get("name") or f"Skill {idx + 1}"),
                                "kind": "skill",
                                "install_mode": str(skl.get("install_mode") or "copy"),
                                "files": files,
                                "source": {"type": source_type},
                            },
                        })
            for key in raw:
                if key not in supported_cc_keys:
                    rejected.append({"field": key, "reason": "Unsupported config field/version; not silently migrated"})
            raw = {"objects": objects}
        else:
            rejected = []
            if format_name != "senrelay":
                raise ConfigurationError("Unsupported import format", 422)
        if not isinstance(raw, dict) or not isinstance(raw.get("objects"), list) or len(raw["objects"]) > 200:
            raise ConfigurationError("Import requires at most 200 explicit objects", 422)
        imported, id_map = [], {}
        # Prevalidate secret-free objects; refs are never trusted from a share.
        for entry in raw["objects"]:
            if not isinstance(entry, dict) or not isinstance(entry.get('entity_kind'), str) or not isinstance(entry.get('data'), dict):
                raise ConfigurationError('Each imported object requires a data object', 422)
            for key in ('id', 'provider_id', 'model_id', 'account_id'):
                reference = entry['data'].get(key)
                if reference is not None and (not isinstance(reference, str) or len(reference) > 100):
                    raise ConfigurationError('Imported references must be resource identifiers', 422)
            reject_secrets(entry)
            if "keychain:senrelay:" in canonical(entry):
                raise ConfigurationError("Imported credentials must be reattached locally", 422)

        pending = []
        for index, entry in enumerate(raw["objects"]):
            kind = entry.get("entity_kind")
            data = copy.deepcopy(entry.get("data", {}))
            data.update(scope)
            if scope and kind == "bundle":
                data["host_ids"] = [agent_id]
            if scope and kind == "binding":
                data["host_id"] = agent_id
            original_id = data.pop("id", None)
            data.pop("version", None)
            data.pop("created_at", None)
            data.pop("updated_at", None)
            pending.append({
                "index": index,
                "kind": kind,
                "original_id": original_id,
                "data": data,
                "last_error": None,
            })

        deferred_links = []
        imported_by_id = {}
        source_ids = {item['original_id'] for item in pending if isinstance(item['original_id'], str)}

        while pending:
            progress = False
            remaining = []
            for item in pending:
                kind = item["kind"]
                value = copy.deepcopy(item["data"])

                for key in ("provider_id", "model_id", "account_id"):
                    if value.get(key) in id_map:
                        value[key] = id_map[value[key]]
                if "asset_ids" in value and isinstance(value["asset_ids"], list):
                    value["asset_ids"] = [id_map.get(item_id, item_id) for item_id in value["asset_ids"]]

                try:
                    unresolved = [item['data'].get(key) for key in ('provider_id', 'model_id', 'account_id') if item['data'].get(key) in source_ids and item['data'].get(key) not in id_map]
                    if unresolved or (kind == 'provider' and value.get('account_id') and value['account_id'] not in id_map.values()):
                        raise ConfigurationError('Imported references must be rebound to the imported objects', 422)
                    created = self.save(kind, value)
                    imported_idx = len(imported)
                    imported.append({"entity_kind": kind, **created})
                    imported_by_id[created["id"]] = imported_idx
                    if item["original_id"]:
                        id_map[item["original_id"]] = created["id"]
                    progress = True
                    continue
                except ConfigurationError as exc:
                    item["last_error"] = str(exc)

                # Staged deferrals for circular or forward dependencies:
                # 1. Provider referencing an account that does not exist yet
                if kind == "provider" and value.get("account_id"):
                    raw_account_ref = value.pop("account_id")
                    source = value.pop('credential_source', None)
                    value['credential_reattach_required'] = True
                    try:
                        created = self.save(kind, value)
                        imported_idx = len(imported)
                        imported.append({"entity_kind": kind, **created})
                        imported_by_id[created["id"]] = imported_idx
                        if item["original_id"]:
                            id_map[item["original_id"]] = created["id"]
                        deferred_links.append({
                            "kind": "provider",
                            "id": created["id"],
                            "field": "account_id",
                            "target_ref": raw_account_ref,
                            'credential_source': source,
                        })
                        progress = True
                        continue
                    except ConfigurationError:
                        value["account_id"] = raw_account_ref
                        if source is not None:
                            value['credential_source'] = source

                # 2. Account referencing a provider that does not exist yet
                if kind == "account" and value.get("provider_id"):
                    raw_provider_ref = value.pop("provider_id")
                    try:
                        created = self.save(kind, value)
                        imported_idx = len(imported)
                        imported.append({"entity_kind": kind, **created})
                        imported_by_id[created["id"]] = imported_idx
                        if item["original_id"]:
                            id_map[item["original_id"]] = created["id"]
                        deferred_links.append({
                            "kind": "account",
                            "id": created["id"],
                            "field": "provider_id",
                            "target_ref": raw_provider_ref,
                        })
                        progress = True
                        continue
                    except ConfigurationError:
                        value["provider_id"] = raw_provider_ref

                item["data"] = value
                remaining.append(item)

            if not progress:
                break
            pending = remaining

        for item in pending:
            rejected.append({
                "entity_kind": item["kind"],
                "name": item["data"].get("name"),
                "reason": item["last_error"] or "Managed object dependency could not be resolved",
            })

        for link in deferred_links:
            target_id = id_map.get(link['target_ref']) or (link['target_ref'] if link['target_ref'] in id_map.values() else None)
            try:
                if target_id is None:
                    raise ConfigurationError('Referenced object was not imported; attach a local account/provider explicitly', 422)
                current_entity = self.get(link["kind"], link["id"])
                updated = self.save(
                    link["kind"],
                    {link["field"]: target_id, **({'credential_source': link['credential_source']} if link.get('credential_source') is not None else {})},
                    entity_id=link["id"],
                    expected_version=current_entity["version"],
                )
                if link["id"] in imported_by_id:
                    imported[imported_by_id[link["id"]]].update(updated)
            except ConfigurationError as exc:
                rejected.append({
                    "entity_kind": link["kind"],
                    "name": self.get(link["kind"], link["id"]).get("name"),
                    "reason": f"Deferred link for {link['field']} failed: {exc}",
                })

        return {
            "imported": imported,
            "unsupported": rejected,
            "managed_credentials_excluded": True,
            "embedded_text_review_required": True,
        }

    @configuration_locked
    def plan(self, request: dict) -> dict:
        from app.services.host_routing import HostRouting
        routing = HostRouting(self)
        profile = routing.private_state(request.get("host_id"))
        if profile.get('pending'):
            raise ConfigurationError('Recover the pending assistant connection change first', 409)
        if profile["mode"] != "direct" and (request.get("activate_provider") is True or request.get("operation") == "restore"):
            return routing.direct_plan(request, profile)
        if request.get("operation") == "restore":
            return self._restore_activation(request.get("host_id"))
        specs, metadata = self._configuration_specs(request)
        return self.transactions.create(specs, metadata)

    def _configuration_specs(self, request: dict, *, provider_override=None):
        host_id = request.get("host_id")
        if host_id not in HOSTS:
            raise ConfigurationError("Unknown host", 422)
        host = effective_host(HOSTS[host_id], self.home)
        operation = request.get("operation", "install")
        if operation not in ("install", "uninstall"):
            raise ConfigurationError("Operation must be install, uninstall or restore", 422)
        asset_ids = list(request.get("asset_ids", []))
        provider_id = request.get("provider_id")
        if request.get("bundle_id"):
            bundle = self.get("bundle", request["bundle_id"])
            if bundle.get("host_ids") and host_id not in bundle["host_ids"]:
                raise ConfigurationError("Host is not selected in the project bundle", 422)
            asset_ids = list(dict.fromkeys(asset_ids + bundle["asset_ids"]))
            provider_id = provider_id or bundle.get("provider_id")
        if provider_id and host.get("provider_unsupported_reason") and not host.get("provider_path"):
            raise ConfigurationError(host["provider_unsupported_reason"], 422)
        if request.get("activate_provider") is True and not provider_id:
            raise ConfigurationError("An explicit provider is required for activation", 422)
        specs = []
        configuration_changes = []
        model_selection = None
        provider_dependencies = {}
        versions = {bundle["id"]: bundle["version"]} if request.get("bundle_id") else {}
        for asset_id in asset_ids:
            asset = self.get("asset", asset_id)
            versions[asset_id] = asset["version"]
            configuration_changes.append({'name': asset['name'], 'kind': asset['kind'], 'operation': operation})
            specs += self._asset_specs(host_id, host, asset, operation)
        if provider_id:
            provider = copy.deepcopy(provider_override) if provider_override else self.get("provider", provider_id)
            if not provider["enabled"] and operation == "install":
                raise ConfigurationError("Provider is disabled")
            configuration_changes.append({'name': provider['name'], 'kind': 'provider', 'operation': operation})
            if not provider_override:
                versions[provider_id] = provider["version"]
            # The saved catalog must reach the native provider configuration,
            # rather than changing only the default-model selector.
            provider = copy.deepcopy(provider)
            with self.connect() as conn:
                catalog = [] if provider_override or operation == 'uninstall' else [self._row(row) for row in conn.execute("SELECT * FROM management_entities WHERE kind='model' AND json_extract(data,'$.provider_id')=?", (provider_id,))]
            known_models = set(provider["model_map"].values())
            dependencies = {}
            capacities = {}
            for model in catalog:
                versions[model["id"]] = model["version"]
                dependencies[model['id']] = model['version']
                if model["model_id"] not in known_models:
                    provider["model_map"][model["id"]] = model["model_id"]
                    known_models.add(model["model_id"])
                if host_id in ('kimi-code', 'omp') and model.get('context_window') is not None:
                    capacity = model['context_window']
                    if model['model_id'] in capacities and capacities[model['model_id']] != capacity:
                        raise ConfigurationError('Saved models declare conflicting context_window values for the same native model', 422)
                    capacities[model['model_id']] = capacity
            if capacities:
                windows = provider.get('model_context_windows')
                if windows is not None and not isinstance(windows, dict):
                    raise ConfigurationError('model_context_windows must be a model-to-capacity object', 422)
                provider['model_context_windows'] = {**(windows or {}), **capacities}
            choice = None
            if operation == 'install' and not provider_override:
                from app.services.provider_credentials import binding
                choice = binding(self, provider)
                provider['credential_ref'] = choice['reference']
                versions.update(choice['dependencies'])
                dependencies.update(choice['dependencies'])
            if dependencies:
                provider_dependencies[provider_id] = list(dependencies)
            provider_specs = self._provider_specs(host_id, host, provider, operation, replacing_selected=request.get("activate_provider") is True)
            if choice and choice['projection']:
                for spec in provider_specs:
                    spec['credential_projection'] = {choice['reference']: choice['projection']}
            if request.get("activate_provider") is True:
                if operation != "install":
                    raise ConfigurationError("Activation is an explicit install operation", 422)
                native_model = self._selected_model(provider, request.get("model_id"))
                model_selection = {'provider_name': provider['name'], 'native_model': native_model}
                if host_id == "claude-code":
                    # The reviewed activation owns exactly these native fields,
                    # retaining their original values only in private snapshots.
                    for spec in provider_specs:
                        spec.update(owner="activation:" + host_id, takeover_selected_field=True, activation_field=True)
                    if not provider.get("credential_ref"):
                        provider_specs.append({"path": str(self.home / host["provider_path"]), "kind": "json", "pointer": ["env", "ANTHROPIC_AUTH_TOKEN"], "value": None, "owner": "activation:" + host_id, "operation": "uninstall", "takeover_selected_field": True, "activation_field": True})
                    provider_specs.append({"path": str(self.home / host["provider_path"]), "kind": "json", "pointer": ["env", "ANTHROPIC_MODEL"], "value": native_model, "owner": "activation:" + host_id, "operation": "install", "takeover_selected_field": True, "activation_field": True})
                elif host_id in ("codex", "codex-desktop"):
                    name = "senrelay_" + provider_id.replace("provider_", "")
                    for key, value in (("model_provider", name), ("model", native_model)):
                        provider_specs.append({"path": str(self.home / ".codex/config.toml"), "kind": "toml_selection", "pointer": [key], "value": value, "owner": "activation:" + host_id, "operation": "install", "takeover_selected_field": True, "activation_field": True})
                elif host_id == "opencode":
                    provider_specs.append({"path": str(self.home / host["provider_path"]), "kind": "json", "pointer": ["model"], "value": provider_id + "/" + native_model, "owner": "activation:" + host_id, "operation": "install", "takeover_selected_field": True, "activation_field": True})
                elif host_id == "omp":
                    provider_specs.extend(omp_activation_specs(host, provider, native_model, home=self.home))
                else:
                    provider_specs.extend(render_activation_specs(host_id, provider, native_model, owner="activation:" + host_id, home=self.home))
            if request.get("replace_provider_id"):
                if host_id != "claude-code" or operation != "install" or request["replace_provider_id"] == provider_id:
                    raise ConfigurationError("Explicit replacement currently supports a different owned Claude Code provider only; other renderers append named providers", 422)
                previous = self.get("provider", request["replace_provider_id"])
                versions[previous["id"]] = previous["version"]
                for spec in provider_specs:
                    spec["replaces_owner"] = previous["id"]
                # A new endpoint must never inherit the previous provider's
                # secret when the new provider has no credential configured.
                if not provider.get("credential_ref"):
                    path = str(self.home / host["provider_path"])
                    key = "json:" + canonical(["env", "ANTHROPIC_AUTH_TOKEN"])
                    with self.connect() as conn:
                        old_secret = conn.execute("SELECT 1 FROM management_owned WHERE path=? AND item_key=? AND owner=?", (path, key, previous["id"])).fetchone()
                    if old_secret:
                        provider_specs.append({"path": path, "kind": "json", "pointer": ["env", "ANTHROPIC_AUTH_TOKEN"], "owner": previous["id"], "operation": "uninstall", "value": None})
            specs += provider_specs
        if not specs:
            raise ConfigurationError("Select at least one supported asset or provider", 422)
        if request.get("bundle_id"):
            for spec in specs:
                spec["bundle_field"] = True
        return specs, {"host_id": host_id, "operation": operation, "entity_versions": versions, 'provider_dependencies': provider_dependencies,
            'configuration_changes': configuration_changes, **({'model_selection': model_selection} if model_selection else {}),
            "configuration_notice": host.get("configuration_notice"),
            "shared_configuration": host_id in ("codex", "codex-desktop"),
            "selection_config_only": host_id in ("omp", "codex-desktop"),
            **({"native_paths": {key: host[key] for key in ("provider_path", "selection_path")}} if host_id == "omp" and provider_id else {}),
            "prompt_notice": host.get("prompt_notice") if any(change["kind"] == "prompt" for change in configuration_changes) else None,
            "bundle_id": request.get("bundle_id"), "scope": "selected_host_global", "activate_provider": request.get("activate_provider") is True, "provider_id": provider_id, "model_id": native_model if request.get("activate_provider") is True else None}

    def _selected_model(self, provider: dict, model_id: str | None) -> str:
        if not isinstance(model_id, str) or not model_id:
            raise ConfigurationError("Select an explicit model_id for native activation", 422)
        try:
            model = self.get("model", model_id)
        except ConfigurationError:
            model = None
        if model:
            if model["provider_id"] != provider["id"]:
                raise ConfigurationError("Selected model belongs to another provider", 422)
            return model["model_id"]
        mappings = provider.get("model_map", {})
        if model_id in mappings:
            return mappings[model_id]
        if model_id in mappings.values():
            return model_id
        raise ConfigurationError("Selected model must exist in this provider's model catalog/map", 422)

    def _restore_activation(self, host_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM management_activations WHERE host_id=?", (host_id,)).fetchone()
        if not row:
            if host_id in HOSTS and HOSTS[host_id].get("provider_unsupported_reason") and not HOSTS[host_id].get("provider_path"):
                raise ConfigurationError(HOSTS[host_id]["provider_unsupported_reason"], 422)
            raise ConfigurationError("This host has no Senrelay activation baseline", 404)
        activation = json.loads(row[0])
        host = effective_host(HOSTS[host_id], self.home)
        if host_id == "omp":
            for key, native_owner in (("provider_path", activation.get("provider_id")), ("selection_path", "activation:omp")):
                selected = self.home / host[key]
                alternatives = [str(selected.with_suffix(suffix)) for suffix in (".yml", ".yaml")]
                # Named provider installation is independent of activation, so
                # its models file need not appear in the activation snapshot.
                # Restore still depends on that provider's effective native path.
                with self.connect() as conn:
                    owned = conn.execute("SELECT path FROM management_owned WHERE owner=? AND path IN (?,?)", (native_owner, *alternatives)).fetchall()
                baseline_shadowed = any(field["path"] in alternatives and field["path"] != str(selected) for field in activation["fields"])
                if baseline_shadowed or any(row["path"] != str(selected) for row in owned):
                    raise ConfigurationError("Native configuration filename precedence changed since activation; original files were preserved")
        specs = []
        for field in activation["fields"]:
            baseline = activation["files"][field["path"]]
            format_name = "toml" if field["kind"] == "toml_selection" else field["kind"] if field["kind"] in ("yaml", "json5", "file", "block") else "json"
            source = {"snapshot": baseline["snapshot"], "hash": baseline["hash"], "pointer": field.get("pointer", []), "format": format_name}
            if field.get("marker"):
                source["marker"] = field["marker"]
            if field.get("native_identity"):
                source["native_identity"] = field["native_identity"]
            prior = field.get("original_ownership")
            spec = {"path": field["path"], "kind": field["kind"], "value": "[Previous selected value]", "owner": field.get("owner", "activation:" + host_id), "operation": "install", "relinquish": not bool(prior), "restore_from": source}
            if field.get("managed_skill") or (field["kind"] == "file" and field.get("asset_kind") != "prompt" and re.fullmatch(r"asset_[a-f0-9]{16}", field.get("owner", ""))):
                # Copy-only receipts predate typed link state. Their snapshots
                # can only describe regular files, never an arbitrary link.
                state = baseline.get("skill_state")
                if state is None and baseline["hash"] is not None:
                    state = {"kind": "copy", "bytes_hash": baseline["hash"]}
                spec["managed_skill"] = {"mode": state["kind"] if state else "copy", "source_version": state.get("source_version") if state else None}
                spec["restore_skill_state"] = state
            for key in ("pointer", "marker", "format", "native_identity"):
                if key in field:
                    spec[key] = field[key]
            if prior:
                spec["restore_owner"] = prior["owner"]
            specs.append(spec)
        return self.transactions.create(specs, {"host_id": host_id, "operation": "restore", "activation_restore": True, "entity_versions": {}, "activation_hash": hashlib.sha256(canonical(activation).encode()).hexdigest(),
            **({"native_paths": {key: host[key] for key in ("provider_path", "selection_path")}} if host_id == "omp" and any(field["path"] in {str(self.home / host[key]) for key in ("provider_path", "selection_path")} for field in activation["fields"]) else {}),
            "shared_configuration": host_id in ("codex", "codex-desktop"), "configuration_notice": host.get("configuration_notice"), "prompt_notice": host.get("prompt_notice")})

    async def probe_asset(self, asset_id: str, confirmed: bool, transport=None, *, expected_version=None) -> dict:
        if confirmed is not True:
            raise ConfigurationError("Confirm the selected MCP network connection before probing", 422)
        asset = self.get("asset", asset_id)
        if expected_version is not None and (type(expected_version) is not int or expected_version != asset['version']):
            raise ConfigurationError("MCP definition changed after review; reload before probing", 409)
        if asset["kind"] != "mcp":
            raise ConfigurationError("Only MCP assets support tools discovery", 422)
        config = asset["config"]
        if config.get("transport", "stdio") in ("stdio", "sse"):
            return await self._probe_sdk(asset_id, config)
        import httpx
        headers = {}
        for key, value in config.get("headers", {}).items():
            if not isinstance(value, str):
                raise ConfigurationError("MCP headers must be strings", 422)
            headers[key] = SECRET_REF.sub(lambda match: self.vault.get(match[1]), value)
        headers.update({"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": "2025-03-26"})
        session_id = None
        try:
            async with httpx.AsyncClient(timeout=8, follow_redirects=False, trust_env=False, transport=transport) as client:
                async def rpc(method, params, request_id):
                    request = {"jsonrpc": "2.0", "method": method, "params": params}
                    if request_id is not None:
                        request["id"] = request_id
                    async with client.stream("POST", config["url"], headers=headers, json=request) as response:
                        if response.is_redirect:
                            raise ConfigurationError("MCP redirects are refused; credentials stay on the selected endpoint", 422)
                        response.raise_for_status()
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(body) + len(chunk) > 1024 * 1024:
                                raise ConfigurationError("MCP response exceeds 1 MiB", 422)
                            body.extend(chunk)
                        response = httpx.Response(response.status_code, headers=response.headers, content=bytes(body), request=response.request)
                    if request_id is None:
                        return {}, response
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        candidates = [line[5:].strip() for line in response.text.splitlines() if line.startswith("data:")]
                        replies = [json.loads(line) for line in candidates if line and line != "[DONE]"]
                        data = next((item for item in replies if item.get("id") == request_id), {})
                    else:
                        data = response.json()
                    if not isinstance(data, dict) or data.get("error") or data.get("id") != request_id or not isinstance(data.get("result"), dict):
                        raise ConfigurationError("MCP returned an invalid JSON-RPC response", 422)
                    return data["result"], response
                initialized, response = await rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "Senrelay", "version": "1"}}, 1)
                session_id = response.headers.get("mcp-session-id")
                if session_id:
                    headers["Mcp-Session-Id"] = session_id
                await rpc("notifications/initialized", {}, None)
                result, _ = await rpc("tools/list", {}, 2)
                tools = result.get("tools")
                if not isinstance(tools, list) or len(tools) > 200:
                    raise ConfigurationError("MCP tool list exceeds limits or is invalid", 422)
                names = []
                for tool in tools:
                    name = tool.get("name") if isinstance(tool, dict) else None
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", name):
                        raise ConfigurationError("MCP tool names are invalid", 422)
                    names.append(name)
                status = "partial" if result.get("nextCursor") else "verified_tools_list"
                return {"asset_id": asset_id, "status": status, "tools": names, "tool_count": len(names), "protocol_version": initialized.get("protocolVersion"), "host_loaded": "not_verified", "checked_at": datetime.now(timezone.utc).isoformat()}
        except (httpx.HTTPError, json.JSONDecodeError, UnicodeDecodeError):
            raise ConfigurationError("MCP connection or protocol check failed; no tools were executed", 502) from None

    async def _probe_sdk(self, asset_id: str, config: dict) -> dict:
        import asyncio
        from datetime import timedelta
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.sse import sse_client
        import httpx
        def resolve(value):
            return SECRET_REF.sub(lambda match: self.vault.get(match[1]), value)
        async def query(read, write):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=8)) as session:
                await session.initialize()
                result = await session.list_tools()
                names = [tool.name for tool in result.tools]
                if len(names) > 200 or any(not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,128}", name) for name in names):
                    raise ConfigurationError("MCP tool list exceeded limits", 422)
                return {"asset_id": asset_id, "status": "partial" if result.nextCursor else "verified_tools_list", "tools": names, "tool_count": len(names), "host_loaded": "not_verified", "transport": config.get("transport", "stdio"), "command": config.get("command") if config.get("transport", "stdio") == "stdio" else None, "checked_at": datetime.now(timezone.utc).isoformat()}
        async def run():
            if config.get("transport", "stdio") == "stdio":
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self.home), "TMPDIR": str(self.db_path.parent)}
                env.update({key: resolve(value) for key, value in config.get("env", {}).items()})
                params = StdioServerParameters(command=resolve(config["command"]), args=[resolve(arg) for arg in config.get("args", [])], env=env)
                # Third-party stderr can contain credentials; do not persist or
                # mirror it into the API/service log. SDK owns child cleanup.
                with open(os.devnull, "w") as errlog:
                    async with stdio_client(params, errlog=errlog) as streams:
                        return await query(*streams)
            headers = {key: resolve(value) for key, value in config.get("headers", {}).items()}
            def client_factory(headers=None, timeout=None, auth=None):
                return httpx.AsyncClient(headers=headers, timeout=timeout or 8, auth=auth, follow_redirects=False, trust_env=False)
            async with sse_client(config["url"], headers=headers, timeout=8, sse_read_timeout=8, httpx_client_factory=client_factory) as streams:
                return await query(*streams)
        try:
            return await asyncio.wait_for(run(), timeout=12)
        except asyncio.CancelledError:
            raise
        except SecretUnavailable:
            raise
        except Exception:
            raise ConfigurationError("MCP initialization/tools discovery failed or timed out; no tools were executed", 502) from None

    def asset_versions(self, asset_id: str) -> list[dict]:
        self.get("asset", asset_id)
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM management_asset_versions WHERE asset_id=? ORDER BY version DESC", (asset_id,)).fetchall()
        return [{"version": row["version"], "created_at": row["created_at"], "data": json.loads(row["data"])} for row in rows]

    def asset_version_page(self, asset_id: str, limit=20, offset=0) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
            raise ConfigurationError("Use a page size of 1–100 and a nonnegative offset", 422)
        with self.connect() as conn:
            conn.execute('BEGIN')
            if not conn.execute("SELECT 1 FROM management_entities WHERE kind='asset' AND id=?", (asset_id,)).fetchone():
                raise ConfigurationError("Asset was not found", 404)
            total = conn.execute("SELECT count(*) FROM management_asset_versions WHERE asset_id=?", (asset_id,)).fetchone()[0]
            rows = conn.execute("SELECT version,created_at FROM management_asset_versions WHERE asset_id=? ORDER BY version DESC LIMIT ? OFFSET ?", (asset_id, limit, offset)).fetchall()
        items = [{'version': row['version'], 'created_at': row['created_at']} for row in rows]
        return {'items': items, 'total': total, 'limit': limit, 'offset': offset,
                'has_more': offset + len(items) < total, 'summary': True}

    def asset_revision(self, asset_id: str, version: int) -> dict:
        if type(version) is not int or version < 1:
            raise ConfigurationError("Select a positive revision number", 422)
        with self.connect() as conn:
            if not conn.execute("SELECT 1 FROM management_entities WHERE kind='asset' AND id=?", (asset_id,)).fetchone():
                raise ConfigurationError("Asset was not found", 404)
            row = conn.execute("SELECT version,created_at,data FROM management_asset_versions WHERE asset_id=? AND version=?", (asset_id, version)).fetchone()
        if not row:
            raise ConfigurationError("Asset revision was not found", 404)
        return {'version': row['version'], 'created_at': row['created_at'], 'data': json.loads(row['data'])}

    def restore_asset(self, asset_id: str, version: int, expected_version: int) -> dict:
        self.get("asset", asset_id)
        with self.connect() as conn:
            row = conn.execute("SELECT data FROM management_asset_versions WHERE asset_id=? AND version=?", (asset_id, version)).fetchone()
        if not row:
            raise ConfigurationError("Asset revision was not found", 404)
        return self.save("asset", json.loads(row["data"]), asset_id, expected_version)

    def discover_assets(self, host_id: str | None = None) -> dict:
        if host_id is None:
            results = []
            for hid in HOSTS:
                try:
                    results.append(self.discover_assets(hid))
                except Exception:
                    pass
            return {"results": results}
        if host_id not in HOSTS:
            raise ConfigurationError("Unknown host", 422)
        host = HOSTS[host_id]
        if not host.get("mcp_path"):
            return {"host_id": host_id, "status": "unsupported", "candidates": [], "reason": host.get("unsupported_reason")}
        path = self.home / host["mcp_path"]
        raw = self.transactions._read(path)
        if raw is None:
            return {"host_id": host_id, "status": "not_found", "candidates": []}
        try:
            if host["format"] in ("yaml", "json5"):
                from app.integrations.structured_config import parse_document
                data = parse_document(host["format"], raw.decode())
            else:
                data = tomllib.loads(raw.decode()) if host["format"] == "toml" else json.loads(raw)
            servers = data.get("mcp_servers" if host["format"] == "toml" else host["container"], {})
            if not isinstance(servers, dict):
                raise ValueError()
        except (ValueError, UnicodeDecodeError):
            raise ConfigurationError("Native config is invalid or requires an unsupported lossless parser", 422) from None
        candidates = []
        for name, config in servers.items():
            if not isinstance(config, dict):
                continue
            # Discovery never copies native opaque values into the asset DB.
            # Remote URLs and executable names are metadata; args/env/header
            # values require explicit manual review before an import.
            safe = {}
            omitted = []
            for key, value in config.items():
                if key in ("command", "url", "serverUrl", "type", "enabled"):
                    try:
                        reject_secrets(value)
                        if key in ("url", "serverUrl"):
                            self._endpoint(value)
                        elif key == "command" and (not isinstance(value, str) or any(char.isspace() for char in value)):
                            raise ValueError()
                        safe[key] = value
                    except (ValueError, TypeError):
                        omitted.append(key)
                else:
                    omitted.append(key)
            candidates.append({"name": name, "config_preview": safe, "omitted_fields": omitted,
                               "import_status": "manual_review_required", "source": {"type": "host_config", "host_id": host_id}})
        return {"host_id": host_id, "status": "parsed", "source_path": str(path), "candidates": candidates, "host_loaded": "not_verified"}

    def _asset_specs(self, host_id: str, host: dict, asset: dict, operation: str) -> list[dict]:
        owner = asset["id"]
        if asset["kind"] == "mcp":
            if not host.get("mcp_path"):
                raise ConfigurationError(host.get("unsupported_reason", "MCP format is unsupported"), 422)
            config = copy.deepcopy(asset["config"])
            name = "senrelay_" + owner.replace("asset_", "")
            path = str(self.home / host["mcp_path"])
            self.transactions.safe_path(path)
            if host_id in HOST_FORMATS:
                return render_mcp_specs(host_id, host, config, name=name, owner=owner, operation=operation, home=self.home)
            transport = config.pop("transport", "stdio")
            if host_id in ("codex", "codex-desktop"):
                if transport == "sse":
                    raise ConfigurationError("Codex SSE configuration is not a verified supported format", 422)
                if transport == "http":
                    config["http_headers"] = config.pop("headers", {})
                body = f"[mcp_servers.{name}]\n" + self._toml_fields(config)
                return [{"path": path, "kind": "block", "marker": name, "format": "toml", "owner": owner, "operation": operation, "value": body.rstrip()}]
            if host_id == "opencode":
                if transport == "stdio":
                    config = {"type": "local", "command": [config["command"], *config.get("args", [])], "environment": config.get("env", {}), "enabled": True}
                else:
                    config = {"type": "remote", "url": config["url"], "headers": config.get("headers", {}), "enabled": True}
            elif transport != "stdio":
                if host_id == "claude-desktop":
                    raise ConfigurationError("Claude Desktop direct remote MCP format is not verified", 422)
                config["type"] = transport
                if host_id == "antigravity":
                    config["serverUrl"] = config.pop("url")
                    config.pop("type", None)
            return [{"path": path, "kind": "json", "pointer": [host["container"], name], "owner": owner, "operation": operation, "value": config}]
        if asset["kind"] == "prompt":
            if host.get("prompt_mode") == "manual_skill":
                return [cursor_prompt_spec(host, asset, home=self.home, operation=operation)]
            if host.get("prompt_mode") == "global_rule":
                return [antigravity_prompt_spec(host, asset, home=self.home, operation=operation)]
            if not host.get("prompt_path"):
                raise ConfigurationError(host.get("prompt_unsupported_reason", "Prompt file format/path is not verified for this host"), 422)
            validate_prompt(host_id, asset["content"])
            return [{"path": str(self.home / host["prompt_path"]), "kind": "block", "marker": owner, "owner": owner, "operation": operation, "value": asset["content"]}]
        if not host.get("skill_dir"):
            raise ConfigurationError("Skill installation is unsupported for this host", 422)
        folder, files = native_skill(asset, host_id) if operation == "install" else (owner, asset["files"])
        skill_root = self.home / host["skill_dir"]
        target_root = skill_root / folder
        with self.connect() as conn:
            owned = conn.execute("SELECT path FROM management_owned WHERE owner=? AND item_key LIKE 'file:%'", (owner,)).fetchall()
        owned_paths = {item["path"] for item in owned if Path(item["path"]).is_relative_to(skill_root)}
        descriptor = {"mode": asset.get("install_mode", "copy"), "source_version": asset["version"]}
        if operation == "uninstall":
            if not owned_paths:
                raise ConfigurationError("Only installed Senrelay-owned Skill files may be uninstalled")
            return [{"path": path, "kind": "file", "managed_skill": descriptor, "owner": owner, "operation": "uninstall", "value": ""} for path in sorted(owned_paths)]
        specs = [{"path": str(target_root / name), "kind": "file", "managed_skill": descriptor, "owner": owner, "operation": "install", "value": text} for name, text in files.items()]
        current_paths = {spec["path"] for spec in specs}
        specs += [{"path": path, "kind": "file", "managed_skill": descriptor, "owner": owner, "operation": "uninstall", "value": ""} for path in sorted(owned_paths - current_paths)]
        return specs

    def _provider_specs(self, host_id: str, host: dict, provider: dict, operation: str, *, replacing_selected=False) -> list[dict]:
        from app.services.provider_endpoints import require_endpoint, inference_endpoint
        owner = provider["id"]
        if host_id == "omp":
            for key, native_owner in (("provider_path", owner), ("selection_path", "activation:omp")):
                selected = self.home / host[key]
                alternatives = [str(selected.with_suffix(suffix)) for suffix in (".yml", ".yaml")]
                with self.connect() as conn:
                    previous = conn.execute("SELECT path FROM management_owned WHERE owner=? AND path IN (?,?)", (native_owner, *alternatives)).fetchall()
                if any(row["path"] != str(selected) for row in previous):
                    raise ConfigurationError("Native configuration filename precedence changed since installation; original owned files were preserved")
        if operation == 'uninstall':
            from app.services.provider_uninstall import owned_specs
            return owned_specs(self, host_id, host, owner, replacing_selected=replacing_selected)
        if host_id == "omp":
            return omp_provider_specs(host, provider, home=self.home, operation=operation)
        if host.get("provider_unsupported_reason") and not host.get("provider_path"):
            raise ConfigurationError(host["provider_unsupported_reason"], 422)
        ref = provider.get("credential_ref")
        secret = "{{credential:" + ref + "}}" if ref else None
        if host_id == "claude-code":
            if provider["protocol"] != "anthropic":
                raise ConfigurationError("Claude Code requires an Anthropic protocol endpoint", 422)
            if operation == 'install':
                require_endpoint(provider, 'messages')
            values = {"ANTHROPIC_BASE_URL": provider["base_url"]}
            if secret:
                values["ANTHROPIC_AUTH_TOKEN"] = secret
            return [{"path": str(self.home / host["provider_path"]), "kind": "json", "pointer": ["env", key], "value": value, "owner": owner, "operation": operation} for key, value in values.items()]
        if host_id == "opencode":
            if provider["protocol"] not in ("openai", "anthropic"):
                raise ConfigurationError("OpenCode provider protocol is not supported by this renderer", 422)
            endpoint = inference_endpoint(provider)
            package = '@ai-sdk/openai' if endpoint == 'responses' else '@ai-sdk/openai-compatible' if endpoint == 'chat/completions' else '@ai-sdk/anthropic'
            config = {"npm": package, "name": provider["name"], "options": {"baseURL": provider["base_url"]}, "models": {model: {"name": model} for model in provider.get("model_map", {}).values()}}
            if secret:
                config["options"]["apiKey"] = secret
            return [{"path": str(self.home / host["provider_path"]), "kind": "json", "pointer": ["provider", owner], "value": config, "owner": owner, "operation": operation}]
        if host_id in ("codex", "codex-desktop"):
            if provider["protocol"] != "openai":
                raise ConfigurationError("Codex custom providers require the OpenAI protocol", 422)
            if operation == 'install':
                require_endpoint(provider, 'responses')
            # Official custom-provider http_headers are native static headers:
            # https://developers.openai.com/codex/config-reference/
            # Credentials resolve only at explicitly confirmed 0600 file apply;
            # no dependency on inherited CLI env or another app's auth cache.
            name = "senrelay_" + owner.replace("provider_", "")
            fields = {"name": provider["name"], "base_url": provider["base_url"], "wire_api": "responses", "requires_openai_auth": False}
            if secret:
                fields["http_headers"] = {"Authorization": "Bearer " + secret}
            body = f"[model_providers.{name}]\n" + self._toml_fields(fields)
            return [{"path": str(self.home / ".codex/config.toml"), "kind": "block", "marker": owner, "format": "toml", "value": body.rstrip(), "owner": owner, "operation": operation}]
        if host_id in HOST_FORMATS:
            return render_provider_specs(host_id, host, provider, operation, home=self.home, replacing_selected=replacing_selected)
        raise ConfigurationError("Provider configuration format is not verified for this host", 422)

    @staticmethod
    def _toml_fields(values: dict) -> str:
        def val(value):
            if isinstance(value, str):
                return json.dumps(value, ensure_ascii=False)
            if isinstance(value, bool):
                return str(value).lower()
            if isinstance(value, (int, float)):
                return str(value)
            if isinstance(value, list):
                return "[" + ", ".join(val(item) for item in value) + "]"
            if isinstance(value, dict):
                return "{ " + ", ".join(json.dumps(key) + " = " + val(item) for key, item in value.items()) + " }"
            raise ConfigurationError("Unsupported TOML field value", 422)
        return "".join(json.dumps(key) + " = " + val(value) + "\n" for key, value in values.items())

    @configuration_locked
    @audited_plan_operation
    def apply(self, plan_id: str, expected_version: int, confirmed: bool) -> dict:
        plan = self.transactions.get(plan_id)
        self.transactions.require_fresh(plan)
        metadata = plan["payload"]["metadata"]
        if metadata.get("native_paths") and plan["status"] != "applied":
            host = effective_host(HOSTS[metadata["host_id"]], self.home)
            if any(host.get(key) != value for key, value in metadata["native_paths"].items()):
                raise ConfigurationError("Native configuration filename precedence changed since preview; create a new plan")
            if metadata["host_id"] == "omp":
                require_omp_models_migration(host, self.home)
                if metadata.get("activate_provider"):
                    require_omp_settings_migration(host, self.home)
        if plan["payload"]["metadata"].get("activation_restore") and plan["status"] != "applied":
            with self.connect() as conn:
                active = conn.execute("SELECT data FROM management_activations WHERE host_id=?", (plan["payload"]["metadata"].get("host_id"),)).fetchone()
            if not active or hashlib.sha256(canonical(json.loads(active[0])).encode()).hexdigest() != plan["payload"]["metadata"].get("activation_hash"):
                raise ConfigurationError("Host activation changed since restore preview")
        for entity_id, version in plan["payload"]["metadata"].get("entity_versions", {}).items():
            with self.connect() as conn:
                row = conn.execute("SELECT version FROM management_entities WHERE id=?", (entity_id,)).fetchone()
            if not row or row[0] != version:
                raise ConfigurationError("Managed asset/provider changed since preview; create a new plan")

        from app.services.host_routing import HostRouting
        routing = HostRouting(self)
        pending = routing.prepare(plan, expected_version, confirmed)

        def finalize_activation(conn, applying_plan):
            metadata = applying_plan["payload"]["metadata"]
            host_id = metadata.get("host_id")
            if metadata.get("activation_restore"):
                conn.execute("DELETE FROM management_activations WHERE host_id=?", (host_id,))
            elif metadata.get("activate_provider") or metadata.get("bundle_id"):
                row = conn.execute("SELECT data FROM management_activations WHERE host_id=?", (host_id,)).fetchone()
                active = json.loads(row[0]) if row else {"files": {}, "fields": []}
                from app.services.selection_receipts import selected_state
                active = selected_state(conn, host_id, active)
                for item in applying_plan["payload"]["files"]:
                    for spec in item["specs"]:
                        if not (spec.get("activation_field") or spec.get("bundle_field")):
                            continue
                        active["files"].setdefault(item["path"], {"snapshot": item.get("snapshot"), "hash": item["before_hash"], "skill_state": item.get("before_state")})
                        field = {"path": item["path"], "kind": spec["kind"], "owner": spec["owner"], "original_ownership": spec.get("original_ownership")}
                        for key in ("pointer", "marker", "format", "native_identity", "managed_skill", "asset_kind"):
                            if key in spec:
                                field[key] = spec[key]
                        identity = lambda value: (value["path"], value["kind"], canonical({"container": value["pointer"][:-1], "identity": value["native_identity"]}) if value.get("native_identity") else canonical(value.get("pointer", value.get("marker", "file"))))
                        if not any(identity(old) == identity(field) for old in active["fields"]):
                            active["fields"].append(field)
                # A bundle can install tools or a named provider without
                # changing the host's selected model. Keep that selection's
                # receipt until an explicit activation actually replaces it.
                if metadata.get('activate_provider'):
                    active["provider_id"] = metadata.get("provider_id")
                    active["model_id"] = metadata.get("model_id")
                    active.pop('selection_receipt_unknown', None)
                active['selection_receipt_version'] = 1
                active["bundle_id"] = metadata.get("bundle_id")
                active["last_plan_id"] = applying_plan["id"]
                conn.execute("INSERT OR REPLACE INTO management_activations VALUES(?,?,?)", (host_id, canonical(active), datetime.now(timezone.utc).isoformat()))
            routing.finalize(conn, applying_plan)

        # Activation baselines are part of the configuration transaction's
        # final SQLite commit. If this write fails, ownership and `applied`
        # status roll back too, and the transaction retains a recoverable plan.
        original_vault = self.transactions.vault
        try:
            if pending:
                self.transactions.vault = routing.apply_vault(plan, pending)
            result = self.transactions.apply(plan_id, expected_version, confirmed, finalize=finalize_activation)
        finally:
            self.transactions.vault = original_vault
        if pending:
            self.retry_credential_cleanup()
        return result

    @configuration_locked
    @audited_plan_operation
    def recover_plan(self, plan_id, confirmed):
        from app.services.host_routing import HostRouting
        return HostRouting(self).recover(plan_id, confirmed)

    @configuration_locked
    def preview_connection(self, host_id, body, origin):
        from app.services.host_routing import HostRouting
        return HostRouting(self).preview(host_id, body, origin)

    def connection_mode(self, host_id):
        from app.services.host_routing import HostRouting
        return HostRouting(self).state(host_id)

    def sync_local_environment(self) -> dict:
        """Import compatible local configuration and host definitions. ok is true only when every attempted write and readable source succeeded."""
        failures: list[dict[str, str]] = []
        failure_count = 0

        def record_failure(stage: str, name: str) -> None:
            nonlocal failure_count
            failure_count += 1
            if len(failures) < 30:
                failures.append({"stage": str(stage)[:40], "name": str(name)[:120]})

        previous_save = self.save

        def recording_save(kind, payload, *args, **kwargs):
            try:
                return previous_save(kind, payload, *args, **kwargs)
            except Exception:
                label = kind
                if isinstance(payload, dict):
                    label = payload.get("name") or payload.get("host_id") or kind
                record_failure(str(kind), "" if label is None else str(label))
                raise

        self._sync_record_failure = record_failure
        self.save = recording_save
        try:
            result = self._sync_local_environment_body()
        finally:
            self.save = previous_save
            self._sync_record_failure = None
        result["ok"] = failure_count == 0
        result["failures"] = failures
        result["failure_count"] = failure_count
        return result

    def _note_sync_failure(self, stage: str, name: str) -> None:
        record = getattr(self, "_sync_record_failure", None)
        if callable(record):
            record(stage, name)

    def _sync_local_environment_body(self) -> dict:
        """One-click idempotent sync of compatible local configuration, host MCP configs, and local skills."""
        existing_providers = {
            (p["name"].strip().lower(), p["protocol"]): p
            for p in self.list("provider")
        }
        existing_models = {
            (m.get("provider_id"), m.get("model_id")): m
            for m in self.list("model")
        }
        existing_assets = {
            (a["kind"], a["name"].strip().lower()): a
            for a in self.list("asset")
        }
        existing_bindings = {
            (b.get("host_id"), b.get("provider_id") or "", b.get("project_id") or ""): b
            for b in self.list("binding")
        }
        created_provider_ids: set[str] = set()
        created_model_ids: set[str] = set()
        created_asset_ids: set[str] = set()

        cc_app_to_host = {
            "claude": "claude-code",
            "claude-desktop": "claude-desktop",
            "claude_desktop": "claude-desktop",
            "codex": "codex",
            "gemini": "gemini-cli",
            "opencode": "opencode",
            "mcode": "minimax-code",
            "agy": "antigravity-cli",
            "grokbuild": "grok-build",
            "openclaw": "openclaw",
        }
        default_endpoints = {
            "anthropic": "https://api.anthropic.com",
            "openai": "https://api.openai.com/v1",
            "gemini": "https://generativelanguage.googleapis.com",
        }
        stats = {
            "providers_synced": 0,
            "models_synced": 0,
            "mcp_synced": 0,
            "prompts_synced": 0,
            "skills_synced": 0,
            "bindings_synced": 0,
            "sources": [],
        }

        def safe_endpoint(url_val: str | None, proto: str) -> str:
            fallback = default_endpoints.get(proto, "https://api.openai.com/v1")
            if not isinstance(url_val, str) or not url_val.strip():
                return fallback
            try:
                return self._endpoint(url_val.strip())
            except Exception:
                return fallback

        cc_current_keys_in_run: set[tuple[str, str, str]] = set()
        uuid_pattern = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

        def friendly_provider_name(raw_name: Any, raw_id: Any, endpoint_url: str, models_map: dict[str, str], proto: str) -> str:
            candidate = str(raw_name or "").strip()
            if candidate and not uuid_pattern.match(candidate):
                return candidate
            parsed_host = urlsplit(safe_endpoint(endpoint_url, proto)).hostname or proto
            default_m = str((models_map or {}).get("default") or next(iter((models_map or {}).values()), "")).strip()
            if default_m:
                return f"{parsed_host} ({default_m})"
            return parsed_host or str(raw_id or "Provider")

        def upsert_provider_record(
            name: str,
            protocol: str,
            endpoint: str,
            models: dict[str, str],
            host_id: str | None,
            sort_index: int | None = None,
            has_key: bool = False,
            raw_secret: str = "",
            is_current: bool = False,
            wire_api: str | None = None,
            supported_endpoints: list[str] | None = None,
            model_labels: dict[str, str] | None = None,
            from_cc_switch: bool = False,
            legacy_id: str | None = None,
        ):
            clean_name = name.strip()[:180]
            if not clean_name:
                return None
            def safe_model_key(raw_key: str) -> str:
                cleaned = str(raw_key).strip()[:120]
                if SENSITIVE.search(cleaned):
                    cleaned = SENSITIVE.sub("model", cleaned)
                return cleaned or "default"

            clean_models = {
                safe_model_key(k): str(v).strip()[:180]
                for k, v in (models or {}).items()
                if isinstance(k, str)
                and isinstance(v, str)
                and k.strip()
                and v.strip()
                and not any(ch.isspace() for ch in v.strip())
                and not SECRET_REF.search(v)
            }
            has_explicit_models = bool(clean_models)

            default_supported = (
                ["messages"]
                if protocol == "anthropic"
                else (["generateContent"] if protocol == "gemini" else ["chat/completions", "responses"])
            )
            declared_eps = list(dict.fromkeys((supported_endpoints or []) + default_supported))
            effective_wire = (
                wire_api
                if protocol == "openai" and wire_api in ("responses", "chat")
                else (
                    "responses"
                    if protocol == "openai" and host_id in ("codex", "codex-desktop", "grok-build")
                    else ("chat" if protocol == "openai" else None)
                )
            )

            key = (clean_name.lower(), protocol)
            existing = existing_providers.get(key)
            if not existing and legacy_id:
                legacy_key = (legacy_id.strip().lower(), protocol)
                if legacy_key in existing_providers:
                    existing = existing_providers.pop(legacy_key)
                    existing_providers[key] = existing
            if from_cc_switch and is_current and host_id and host_id in HOSTS:
                cc_current_keys_in_run.add((clean_name.lower(), protocol, host_id))
            agent_list = [host_id] if host_id and host_id in HOSTS else []
            if existing:
                is_created_in_run = existing["id"] in created_provider_ids
                existing_agents = existing.get("agent_ids")
                if is_created_in_run or existing_agents is None:
                    merged_agents = list(dict.fromkeys((existing_agents or []) + agent_list))
                else:
                    merged_agents = list(existing_agents)

                existing_raw_map = existing.get("model_map")
                existing_map = {
                    safe_model_key(k): v
                    for k, v in (existing_raw_map or {}).items()
                    if isinstance(k, str) and isinstance(v, str) and v.strip() and not any(ch.isspace() for ch in v.strip())
                }
                if is_created_in_run:
                    merged_models = {**existing_map, **clean_models} if has_explicit_models else (existing_map or clean_models)
                elif existing_raw_map is None:
                    merged_models = clean_models
                else:
                    merged_models = dict(existing_raw_map)

                existing_eps = existing.get("supported_endpoints")
                if is_created_in_run or existing_eps is None:
                    merged_eps = list(dict.fromkeys((existing_eps or []) + declared_eps))
                else:
                    merged_eps = list(existing_eps)
                merged_wire = existing.get("wire_api") or effective_wire
                prior_cc_hosts = [
                    h
                    for h in (existing.get("cc_switch_current_hosts") or [])
                    if h in HOSTS
                    and not (
                        from_cc_switch
                        and h == host_id
                        and not is_current
                        and (clean_name.lower(), protocol, h) not in cc_current_keys_in_run
                    )
                ]
                curr_hosts = list(
                    dict.fromkeys(
                        prior_cc_hosts + ([host_id] if is_current and host_id in HOSTS else [])
                    )
                )
                target_url = existing.get("base_url") or safe_endpoint(endpoint, protocol)
                if is_created_in_run and endpoint and endpoint.strip() and target_url in default_endpoints.values():
                    candidate_url = safe_endpoint(endpoint, protocol)
                    if candidate_url not in default_endpoints.values():
                        target_url = candidate_url

                patch_data: dict[str, Any] = {}
                if is_created_in_run and clean_name != existing.get("name"):
                    patch_data["name"] = clean_name
                if merged_agents != (existing.get("agent_ids") or []):
                    patch_data["agent_ids"] = merged_agents
                if merged_models != (existing.get("model_map") or {}):
                    patch_data["model_map"] = merged_models
                if merged_eps != (existing.get("supported_endpoints") or []):
                    patch_data["supported_endpoints"] = merged_eps
                if protocol == "openai" and merged_wire and merged_wire != existing.get("wire_api"):
                    patch_data["wire_api"] = merged_wire
                if curr_hosts != (existing.get("cc_switch_current_hosts") or []):
                    patch_data["cc_switch_current_hosts"] = curr_hosts
                if target_url != existing.get("base_url"):
                    patch_data["base_url"] = target_url

                if patch_data:
                    try:
                        updated = self.save(
                            "provider",
                            patch_data,
                            existing["id"],
                            existing["version"],
                        )
                        existing_providers[key] = updated
                        existing = updated
                        stats["providers_synced"] += 1
                    except Exception:
                        pass
                prov_obj = existing
            else:
                payload: dict[str, Any] = {
                    "name": clean_name,
                    "protocol": protocol,
                    "base_url": safe_endpoint(endpoint, protocol),
                    "enabled": True,
                    "model_map": clean_models,
                    "agent_ids": agent_list,
                    "supported_endpoints": declared_eps,
                    "credential_reattach_required": bool(has_key),
                    "cc_switch_current_hosts": [host_id] if is_current and host_id in HOSTS else [],
                }
                if protocol == "openai" and effective_wire:
                    payload["wire_api"] = effective_wire
                if isinstance(sort_index, int) and -1000000 <= sort_index <= 1000000:
                    payload["sort_index"] = sort_index
                try:
                    prov_obj = self.save("provider", payload)
                    created_provider_ids.add(prov_obj["id"])
                    existing_providers[key] = prov_obj
                    stats["providers_synced"] += 1
                except Exception:
                    return None

            # Attach secret into Keychain / Vault when available so 1-click switch & auth checks work
            secret_clean = str(raw_secret or "").strip()
            if (
                secret_clean
                and not SECRET_REF.search(secret_clean)
                and (not prov_obj.get("credential_ref") or prov_obj.get("credential_reattach_required"))
            ):
                if not ("PYTEST_CURRENT_TEST" in os.environ and isinstance(self.vault, KeychainSecretStore)):
                    try:
                        prov_obj = self.credential("provider", prov_obj["id"], secret_clean)
                        existing_providers[key] = prov_obj
                    except Exception:
                        self._note_sync_failure("credential", clean_name)

            # Remove any previously corrupted model entities whose model_id contains whitespace
            for (pid, mid_key), m_ent in list(existing_models.items()):
                if pid == prov_obj["id"] and isinstance(mid_key, str) and any(ch.isspace() for ch in mid_key.strip()):
                    try:
                        self.delete("model", m_ent["id"], m_ent["version"])
                        existing_models.pop((pid, mid_key), None)
                    except Exception:
                        self._note_sync_failure("model", str(mid_key))

            # Ensure model entities exist for this provider
            seen_mids = set()
            all_mids = list((prov_obj.get("model_map") or clean_models).values())
            for mid in all_mids:
                mid_clean = str(mid).strip()
                if not mid_clean or any(ch.isspace() for ch in mid_clean) or mid_clean in seen_mids:
                    continue
                seen_mids.add(mid_clean)
                display_label = str((model_labels or {}).get(mid_clean) or mid_clean).strip()[:180]
                if (prov_obj["id"], mid_clean) not in existing_models:
                    try:
                        m_obj = self.save(
                            "model",
                            {
                                "name": display_label,
                                "provider_id": prov_obj["id"],
                                "model_id": mid_clean,
                                "agent_ids": prov_obj.get("agent_ids") or agent_list,
                            },
                        )
                        created_model_ids.add(m_obj["id"])
                        existing_models[(prov_obj["id"], mid_clean)] = m_obj
                        stats["models_synced"] += 1
                    except Exception:
                        pass
                else:
                    ex_m = existing_models[(prov_obj["id"], mid_clean)]
                    if ex_m["id"] in created_model_ids or ex_m.get("agent_ids") is None:
                        target_m_agents = list(dict.fromkeys((ex_m.get("agent_ids") or []) + (prov_obj.get("agent_ids") or agent_list)))
                        if target_m_agents != (ex_m.get("agent_ids") or []):
                            try:
                                existing_models[(prov_obj["id"], mid_clean)] = self.save(
                                    "model",
                                    {"agent_ids": target_m_agents},
                                    ex_m["id"],
                                    ex_m["version"],
                                )
                            except Exception:
                                pass

            if is_current and host_id and host_id in HOSTS:
                bkey = (host_id, prov_obj["id"], "")
                if bkey not in existing_bindings:
                    first_mid = (prov_obj.get("model_map") or {}).get("default") or next(iter(seen_mids), None)
                    model_entity = existing_models.get((prov_obj["id"], first_mid)) if first_mid else None
                    try:
                        b_obj = self.save(
                            "binding",
                            {
                                "host_id": host_id,
                                "provider_id": prov_obj["id"],
                                "model_id": model_entity["id"] if model_entity else None,
                            },
                        )
                        existing_bindings[bkey] = b_obj
                        stats["bindings_synced"] += 1
                    except Exception:
                        pass
            return prov_obj

        # 1. Sync from ~/.cc-switch/cc-switch.db if available
        cc_db_path = self.home / ".cc-switch/cc-switch.db"
        if not cc_db_path.parent.is_symlink() and not cc_db_path.is_symlink() and cc_db_path.is_file():
            try:
                conn = sqlite3.connect(cc_db_path.as_uri() + "?mode=ro", uri=True, timeout=3)
                conn.row_factory = sqlite3.Row
                try:
                    tables = {
                        r["name"]
                        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
                    }
                    if "providers" in tables:
                        p_cols = {r[1] for r in conn.execute('PRAGMA table_info("providers")').fetchall()}
                        sel_cols = [c for c in ("id", "app_type", "name", "settings_config", "is_current", "sort_index", "meta") if c in p_cols]
                        rows = conn.execute(f'SELECT {",".join(sel_cols)} FROM providers LIMIT 200').fetchall()
                        for r in rows:
                            row = dict(r)
                            app_type = str(row.get("app_type") or "claude").strip()
                            host_id = cc_app_to_host.get(app_type, "claude-code")
                            try:
                                cfg = json.loads(row.get("settings_config") or "{}")
                                if not isinstance(cfg, dict):
                                    cfg = {}
                            except Exception:
                                cfg = {}
                            protocol = "anthropic"
                            endpoint = ""
                            wire_api: str | None = None
                            models: dict[str, str] = {}
                            model_labels: dict[str, str] = {}
                            raw_secret = ""
                            if app_type == "gemini" or (isinstance(cfg.get("env"), dict) and "GOOGLE_GEMINI_BASE_URL" in cfg.get("env", {})):
                                env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
                                protocol = "gemini"
                                endpoint = str(env.get("GOOGLE_GEMINI_BASE_URL") or "")
                                raw_secret = str(env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or "").strip()
                                if isinstance(env.get("GEMINI_MODEL"), str) and env["GEMINI_MODEL"].strip():
                                    gm = env["GEMINI_MODEL"].strip()
                                    models["default"] = gm
                                    model_labels[gm] = gm
                            elif app_type in ("codex", "grokbuild") or isinstance(cfg.get("config"), str):
                                protocol = "openai"
                                wire_api = "responses"
                                toml_str = cfg.get("config") if isinstance(cfg.get("config"), str) else ""
                                native = {}
                                if toml_str.strip():
                                    try:
                                        native = tomllib.loads(toml_str)
                                    except Exception:
                                        native = {}
                                mp = native.get("model_providers") if isinstance(native.get("model_providers"), dict) else {}
                                sel = native.get("model_provider")
                                p_entry = mp.get(sel) if isinstance(sel, str) and isinstance(mp.get(sel), dict) else None
                                if not p_entry and mp:
                                    first_v = next(iter(mp.values()), None)
                                    if isinstance(first_v, dict):
                                        p_entry = first_v
                                if isinstance(p_entry, dict):
                                    endpoint = str(p_entry.get("base_url") or "")
                                    if p_entry.get("wire_api") in ("responses", "chat"):
                                        wire_api = str(p_entry["wire_api"])
                                if isinstance(native.get("model"), str) and native["model"].strip():
                                    cm = native["model"].strip()
                                    models["default"] = cm
                                    model_labels[cm] = cm
                                auth = cfg.get("auth") if isinstance(cfg.get("auth"), dict) else {}
                                raw_secret = str(auth.get("OPENAI_API_KEY") or auth.get("API_KEY") or "").strip()
                            elif "options" in cfg or app_type in ("opencode", "mcode"):
                                npm_pkg = str(cfg.get("npm") or "").lower()
                                protocol = "anthropic" if "anthropic" in npm_pkg else "openai"
                                if protocol == "openai":
                                    wire_api = "responses" if npm_pkg == "@ai-sdk/openai" else "chat"
                                opts = cfg.get("options") if isinstance(cfg.get("options"), dict) else {}
                                endpoint = str(opts.get("baseURL") or opts.get("baseUrl") or "")
                                raw_secret = str(opts.get("apiKey") or opts.get("api_key") or "").strip()
                                if isinstance(cfg.get("models"), dict):
                                    for mk, mv in cfg["models"].items():
                                        mk_str = str(mk).strip()
                                        if not mk_str:
                                            continue
                                        mid = (
                                            mv.get("id").strip()
                                            if isinstance(mv, dict) and isinstance(mv.get("id"), str) and mv["id"].strip()
                                            else mk_str
                                        )
                                        mlabel = (
                                            mv.get("name").strip()
                                            if isinstance(mv, dict) and isinstance(mv.get("name"), str) and mv["name"].strip()
                                            else mid
                                        )
                                        if not any(ch.isspace() for ch in mid):
                                            models[mk_str] = mid
                                            model_labels[mid] = mlabel
                                    if models and "default" not in models:
                                        models["default"] = next(iter(models.values()))
                            else:
                                env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
                                protocol = "anthropic"
                                endpoint = str(env.get("ANTHROPIC_BASE_URL") or cfg.get("base_url") or "")
                                raw_secret = str(env.get("ANTHROPIC_AUTH_TOKEN") or env.get("ANTHROPIC_API_KEY") or cfg.get("api_key") or "").strip()
                                for k in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL"):
                                    if isinstance(env.get(k), str) and env[k].strip():
                                        am = env[k].strip()
                                        models[k] = am
                                        model_labels[am] = am
                                        if "default" not in models:
                                            models["default"] = am

                            upsert_provider_record(
                                name=friendly_provider_name(row.get("name"), row.get("id"), endpoint, models, protocol),
                                protocol=protocol,
                                endpoint=endpoint,
                                models=models,
                                host_id=host_id,
                                sort_index=row.get("sort_index") if isinstance(row.get("sort_index"), int) else None,
                                has_key=bool(raw_secret),
                                raw_secret=raw_secret,
                                is_current=bool(row.get("is_current")),
                                wire_api=wire_api,
                                model_labels=model_labels,
                                from_cc_switch=True,
                                legacy_id=str(row.get("id") or ""),
                            )
                        active_cc_hosts = {h for (_, _, h) in cc_current_keys_in_run}
                        for key, prov in list(existing_providers.items()):
                            old_cc = prov.get("cc_switch_current_hosts") or []
                            if not old_cc:
                                continue
                            new_cc = [
                                h
                                for h in old_cc
                                if h in HOSTS and (h not in active_cc_hosts or (key[0], key[1], h) in cc_current_keys_in_run)
                            ]
                            if new_cc != old_cc:
                                try:
                                    existing_providers[key] = self.save(
                                        "provider",
                                        {"cc_switch_current_hosts": new_cc},
                                        prov["id"],
                                        prov["version"],
                                    )
                                except Exception:
                                    pass

                    if "mcp_servers" in tables:
                        m_cols = {r[1] for r in conn.execute('PRAGMA table_info("mcp_servers")').fetchall()}
                        sel_m = [c for c in ("id", "name", "server_config", "enabled_claude", "enabled_codex", "enabled_gemini", "enabled_opencode") if c in m_cols]
                        for r in conn.execute(f'SELECT {",".join(sel_m)} FROM mcp_servers LIMIT 200').fetchall():
                            row = dict(r)
                            m_name = str(row.get("name") or row.get("id") or "").strip()
                            if not m_name or m_name.lower() == "contextcore" or m_name.startswith("senrelay_"):
                                continue
                            try:
                                scfg = json.loads(row.get("server_config") or "{}")
                                if not isinstance(scfg, dict):
                                    continue
                            except Exception:
                                continue
                            agent_ids = []
                            if row.get("enabled_claude"):
                                agent_ids.append("claude-code")
                            if row.get("enabled_codex"):
                                agent_ids.append("codex")
                            if row.get("enabled_gemini"):
                                agent_ids.append("gemini-cli")
                            if row.get("enabled_opencode"):
                                agent_ids.append("opencode")
                            if not agent_ids:
                                agent_ids = ["claude-code", "codex", "cursor", "opencode"]
                            transport = "http" if scfg.get("url") else "stdio"
                            mcp_cfg: dict = {"transport": transport}
                            if transport == "stdio":
                                cmd = str(scfg.get("command") or "").strip()
                                if not cmd:
                                    continue
                                mcp_cfg["command"] = cmd
                                if isinstance(scfg.get("args"), list):
                                    mcp_cfg["args"] = [
                                        str(a)
                                        for a in scfg["args"]
                                        if not SECRET_REF.search(str(a))
                                        and not re.search(r"(?i)sk-[A-Za-z0-9_-]{8,}|--(?:api[-_]key|token|secret|password|auth)(?:[=\s]|$)", str(a))
                                    ]
                                if isinstance(scfg.get("env"), dict):
                                    mcp_cfg["env"] = {
                                        str(k): str(v)
                                        for k, v in scfg["env"].items()
                                        if not SECRET_REF.search(str(v))
                                        and not re.search(r"(?i)(key|secret|password|auth|token|credential|cookie)", str(k))
                                    }
                            else:
                                try:
                                    mcp_cfg["url"] = self._endpoint(str(scfg.get("url") or ""))
                                except Exception:
                                    continue
                            akey = ("mcp", m_name.lower())
                            if akey in existing_assets:
                                ex = existing_assets[akey]
                                if ex["id"] in created_asset_ids or ex.get("agent_ids") is None:
                                    merged_a = list(dict.fromkeys((ex.get("agent_ids") or []) + agent_ids))
                                    if merged_a != (ex.get("agent_ids") or []):
                                        try:
                                            existing_assets[akey] = self.save("asset", {"agent_ids": merged_a}, ex["id"], ex["version"])
                                            stats["mcp_synced"] += 1
                                        except Exception:
                                            pass
                            else:
                                try:
                                    created = self.save(
                                        "asset",
                                        {
                                            "kind": "mcp",
                                            "name": m_name[:180],
                                            "config": mcp_cfg,
                                            "agent_ids": agent_ids,
                                            "source": {"type": "cc-switch", "id": row.get("id")},
                                        },
                                    )
                                    created_asset_ids.add(created["id"])
                                    existing_assets[akey] = created
                                    stats["mcp_synced"] += 1
                                except Exception:
                                    pass

                    doc_secret_re = re.compile(
                        r"(?i)sk-[A-Za-z0-9_-]{8,}|bearer\s+[^\s\"']+|(?:authorization|x-api-key)\s*[:=]|--(?:api[-_]key|token|secret|password|auth)(?:[=\s]|$)"
                    )
                    if "prompts" in tables:
                        pr_cols = {r[1] for r in conn.execute('PRAGMA table_info("prompts")').fetchall()}
                        sel_pr = [c for c in ("id", "app_type", "name", "content", "enabled") if c in pr_cols]
                        for r in conn.execute(f'SELECT {",".join(sel_pr)} FROM prompts LIMIT 200').fetchall():
                            row = dict(r)
                            pr_name = str(row.get("name") or row.get("id") or "").strip()
                            pr_content = doc_secret_re.sub("[REDACTED]", SECRET_REF.sub("", str(row.get("content") or "").strip()))
                            if not pr_name or not pr_content:
                                continue
                            host_id = cc_app_to_host.get(str(row.get("app_type") or "claude"), "claude-code")
                            agent_ids = [host_id] if host_id in HOSTS else ["claude-code"]
                            akey = ("prompt", pr_name.lower())
                            if akey in existing_assets:
                                ex = existing_assets[akey]
                                if ex["id"] in created_asset_ids or ex.get("agent_ids") is None:
                                    merged_a = list(dict.fromkeys((ex.get("agent_ids") or []) + agent_ids))
                                    if merged_a != (ex.get("agent_ids") or []):
                                        try:
                                            existing_assets[akey] = self.save("asset", {"agent_ids": merged_a}, ex["id"], ex["version"])
                                            stats["prompts_synced"] += 1
                                        except Exception:
                                            pass
                            else:
                                try:
                                    created = self.save(
                                        "asset",
                                        {
                                            "kind": "prompt",
                                            "name": pr_name[:180],
                                            "content": pr_content[:190000],
                                            "agent_ids": agent_ids,
                                            "source": {"type": "cc-switch", "id": row.get("id"), "app_type": row.get("app_type")},
                                        },
                                    )
                                    created_asset_ids.add(created["id"])
                                    existing_assets[akey] = created
                                    stats["prompts_synced"] += 1
                                except Exception:
                                    pass
                    stats["sources"].append("cc-switch.db")
                finally:
                    conn.close()
            except Exception:
                self._note_sync_failure("cc-switch", "cc-switch.db")

        # 2. Sync MCP servers from local host config files
        for h_id, host in HOSTS.items():
            if not host.get("mcp_path"):
                continue
            mcp_file = self.home / host["mcp_path"]
            try:
                raw_bytes = self.transactions._read(mcp_file)
                if raw_bytes is None:
                    continue
                if host.get("format") in ("yaml", "json5"):
                    from app.integrations.structured_config import parse_document
                    doc_data = parse_document(host["format"], raw_bytes.decode())
                elif host.get("format") == "toml":
                    doc_data = tomllib.loads(raw_bytes.decode())
                else:
                    doc_data = json.loads(raw_bytes)
                servers = doc_data.get("mcp_servers" if host.get("format") == "toml" else host.get("container", "mcpServers"), {})
                if not isinstance(servers, dict):
                    continue
            except Exception:
                self._note_sync_failure("host-mcp", h_id)
                continue
            for c_name_raw, scfg in servers.items():
                c_name = str(c_name_raw or "").strip()
                if not c_name or c_name.lower() == "contextcore" or c_name.startswith("senrelay_") or not isinstance(scfg, dict):
                    continue
                mcp_cfg = None
                raw_url = scfg.get("url") or scfg.get("serverUrl")
                if isinstance(raw_url, str) and raw_url.strip():
                    try:
                        mcp_cfg = {"transport": "http", "url": self._endpoint(raw_url.strip())}
                    except Exception:
                        mcp_cfg = None
                else:
                    cmd_val = scfg.get("command")
                    args_val = scfg.get("args")
                    if isinstance(cmd_val, list) and cmd_val:
                        cmd_str = str(cmd_val[0] or "").strip()
                        args_val = [str(x) for x in cmd_val[1:]]
                    elif isinstance(cmd_val, str):
                        cmd_str = cmd_val.strip()
                    else:
                        cmd_str = ""
                    if cmd_str and not SECRET_REF.search(cmd_str):
                        safe_args = []
                        if isinstance(args_val, list):
                            safe_args = [
                                str(a)
                                for a in args_val
                                if not SECRET_REF.search(str(a))
                                and not re.search(r"(?i)sk-[A-Za-z0-9_-]{8,}|--(?:api[-_]key|token|secret|password|auth)(?:[=\s]|$)", str(a))
                            ]
                        mcp_cfg = {"transport": "stdio", "command": cmd_str, "args": safe_args}
                        if isinstance(scfg.get("cwd"), str) and scfg["cwd"].strip() and not SECRET_REF.search(scfg["cwd"]):
                            mcp_cfg["cwd"] = scfg["cwd"].strip()
                        env_dict = scfg.get("env") or scfg.get("environment")
                        if isinstance(env_dict, dict):
                            safe_env = {
                                str(k): str(v)
                                for k, v in env_dict.items()
                                if not SECRET_REF.search(str(v))
                                and not re.search(r"(?i)(key|secret|password|auth|token|credential|cookie)", str(k))
                            }
                            if safe_env:
                                mcp_cfg["env"] = safe_env
                if not mcp_cfg:
                    continue
                akey = ("mcp", c_name.lower())
                if akey in existing_assets:
                    ex = existing_assets[akey]
                    patch_asset: dict[str, Any] = {}
                    if ex["id"] in created_asset_ids or ex.get("agent_ids") is None:
                        merged_a = list(dict.fromkeys((ex.get("agent_ids") or []) + [h_id]))
                        if merged_a != (ex.get("agent_ids") or []):
                            patch_asset["agent_ids"] = merged_a
                    ex_cfg = ex.get("config") if isinstance(ex.get("config"), dict) else {}
                    if (
                        ex_cfg.get("transport") == "stdio"
                        and mcp_cfg.get("transport") == "stdio"
                        and ex_cfg.get("command") == mcp_cfg.get("command")
                        and not ex_cfg.get("args")
                        and mcp_cfg.get("args")
                    ):
                        merged_cfg = copy.deepcopy(ex_cfg)
                        merged_cfg["args"] = mcp_cfg["args"]
                        if not merged_cfg.get("cwd") and mcp_cfg.get("cwd"):
                            merged_cfg["cwd"] = mcp_cfg["cwd"]
                        existing_env = dict(merged_cfg.get("env") or {})
                        for env_k, env_v in (mcp_cfg.get("env") or {}).items():
                            existing_env.setdefault(env_k, env_v)
                        if existing_env:
                            merged_cfg["env"] = existing_env
                        if merged_cfg != ex_cfg:
                            patch_asset["config"] = merged_cfg
                    if patch_asset:
                        try:
                            existing_assets[akey] = self.save("asset", patch_asset, ex["id"], ex["version"])
                            stats["mcp_synced"] += 1
                        except Exception:
                            pass
                else:
                    try:
                        created = self.save(
                            "asset",
                            {
                                "kind": "mcp",
                                "name": c_name[:180],
                                "config": mcp_cfg,
                                "agent_ids": [h_id],
                                "source": {"type": "host_config", "host_id": h_id},
                            },
                        )
                        created_asset_ids.add(created["id"])
                        existing_assets[akey] = created
                        stats["mcp_synced"] += 1
                    except Exception:
                        pass

        # 3. Sync local Skills directories (~/.claude/skills, ~/.codex/skills, ~/.agents/skills, ~/.cc-switch/skills)
        try:
            from app.integrations.installer import refresh_owned_skill_files

            refresh_owned_skill_files(home=self.home)
        except Exception:
            pass
        doc_secret_re = re.compile(
            r"(?i)sk-[A-Za-z0-9_-]{8,}|bearer\s+[^\s\"']+|(?:authorization|x-api-key)\s*[:=]|--(?:api[-_]key|token|secret|password|auth)(?:[=\s]|$)"
        )

        def collect_local_skill_files(skill_dir: Path) -> dict[str, str] | None:
            collected: dict[str, str] = {}
            total_bytes = 0
            open_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            for directory, subdirs, names in os.walk(skill_dir, followlinks=False):
                dir_path = Path(directory)
                if any((dir_path / entry_name).is_symlink() for entry_name in subdirs + names):
                    raise ValueError("Skill directory contains symlink")
                subdirs[:] = sorted(d for d in subdirs if d not in (".git", "__pycache__"))
                for fname in sorted(names):
                    if fname == ".DS_Store" or fname.endswith(".pyc"):
                        continue
                    fpath = dir_path / fname
                    rel_posix = fpath.relative_to(skill_dir).as_posix()
                    self._validate_skill_path(rel_posix)
                    if rel_posix != "SKILL.md" and SENSITIVE.search(rel_posix):
                        continue
                    if len(collected) >= 100:
                        if rel_posix == "SKILL.md":
                            raise ValueError("Skill exceeds file count limit")
                        continue
                    fd = os.open(fpath, open_flags)
                    try:
                        with os.fdopen(fd, "rb") as stream:
                            info = os.fstat(stream.fileno())
                            if not stat.S_ISREG(info.st_mode):
                                if rel_posix == "SKILL.md":
                                    raise ValueError("SKILL.md must be a regular file")
                                continue
                            if info.st_size > 400000:
                                if rel_posix == "SKILL.md":
                                    return None
                                continue
                            raw_data = stream.read(400001)
                            if len(raw_data) > 400000:
                                if rel_posix == "SKILL.md":
                                    return None
                                continue
                    except Exception:
                        if rel_posix == "SKILL.md":
                            raise
                        continue
                    try:
                        decoded_text = raw_data.decode("utf-8")
                    except UnicodeDecodeError:
                        if rel_posix == "SKILL.md":
                            raise
                        continue
                    sanitized_text = doc_secret_re.sub("[REDACTED]", SECRET_REF.sub("", decoded_text))
                    encoded_len = len(sanitized_text.encode("utf-8"))
                    if total_bytes + encoded_len > 1800000:
                        if rel_posix == "SKILL.md":
                            return None
                        continue
                    total_bytes += encoded_len
                    collected[rel_posix] = sanitized_text
            if "SKILL.md" not in collected:
                return None
            return collected

        skill_roots = [
            (self.home / ".claude/skills", ["claude-code"]),
            (self.home / ".codex/skills", ["codex"]),
            (self.home / ".agents/skills", [h for h in ("claude-code", "codex", "cursor", "opencode", "antigravity-cli") if h in HOSTS]),
            (self.home / ".cc-switch/skills", ["claude-code", "codex"]),
        ]
        total_skills = sum(1 for k in existing_assets if k[0] == "skill")
        for s_root, default_hosts in skill_roots:
            if not s_root.is_dir() or s_root.is_symlink():
                continue
            try:
                entries = sorted(s_root.iterdir(), key=lambda p: p.name.lower())
            except Exception:
                self._note_sync_failure("skills", s_root.name)
                continue
            for item in entries:
                if item.name.startswith(".") or item.name.startswith("asset_"):
                    continue
                s_name = item.name.strip()[:180]
                if item.is_symlink():
                    self._note_sync_failure("skill", s_name)
                    continue
                if not item.is_dir():
                    continue
                skill_md = item / "SKILL.md"
                if skill_md.is_symlink():
                    self._note_sync_failure("skill", s_name)
                    continue
                if not skill_md.is_file():
                    continue
                akey = ("skill", s_name.lower())
                if akey in existing_assets:
                    ex = existing_assets[akey]
                    patch_skill: dict[str, Any] = {}
                    if ex["id"] in created_asset_ids or ex.get("agent_ids") is None:
                        merged_a = list(dict.fromkeys([a for a in (ex.get("agent_ids") or []) + default_hosts if a in HOSTS]))
                        if merged_a != (ex.get("agent_ids") or []):
                            patch_skill["agent_ids"] = merged_a
                    ex_source = ex.get("source") if isinstance(ex.get("source"), dict) else {}
                    if s_name.lower() == "contextcore-handoff" and ex_source.get("type") == "local":
                        try:
                            raw_md = skill_md.read_bytes()
                            if len(raw_md) <= 400000:
                                md_text = doc_secret_re.sub("[REDACTED]", SECRET_REF.sub("", raw_md.decode("utf-8")))
                                cur_files = ex.get("files") if isinstance(ex.get("files"), dict) else {}
                                if cur_files.get("SKILL.md") != md_text:
                                    patch_skill["files"] = {**cur_files, "SKILL.md": md_text}
                        except Exception:
                            pass
                    if patch_skill:
                        try:
                            existing_assets[akey] = self.save("asset", patch_skill, ex["id"], ex["version"])
                            stats["skills_synced"] += 1
                        except Exception:
                            pass
                    continue
                if total_skills >= 60:
                    continue
                try:
                    collected_files = collect_local_skill_files(item)
                except Exception:
                    self._note_sync_failure("skill", s_name)
                    continue
                if not collected_files:
                    continue
                try:
                    created = self.save(
                        "asset",
                        {
                            "kind": "skill",
                            "name": s_name,
                            "install_mode": "copy",
                            "files": collected_files,
                            "agent_ids": [a for a in default_hosts if a in HOSTS],
                            "source": {"type": "local", "path": str(item)},
                        },
                    )
                    created_asset_ids.add(created["id"])
                    existing_assets[akey] = created
                    stats["skills_synced"] += 1
                    total_skills += 1
                except Exception:
                    pass

        return {
            "ok": True,
            **stats,
            "total_providers": len(existing_providers),
            "total_models": len(existing_models),
            "total_assets": len(existing_assets),
        }


def service() -> ManagementService:
    return ManagementService()


def resolve_provider(provider_id: str) -> dict:
    manager = service()
    value = manager.get("provider", provider_id)
    if not value.get("enabled", True):
        raise ConfigurationError("Provider is disabled")
    from app.services.provider_credentials import binding, resolved_key
    key = resolved_key(manager, binding(manager, value))
    return {**value, "api_key": key}
