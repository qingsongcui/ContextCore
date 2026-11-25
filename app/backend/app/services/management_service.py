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
from contextlib import ExitStack, contextmanager
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
    "codex": {"name": "Codex", "mcp_path": ".codex/config.toml", "provider_path": ".codex/config.toml", "format": "toml", "skill_dir": ".codex/skills", "prompt_path": ".codex/AGENTS.md"},
    "claude-code": {"name": "Claude Code", "mcp_path": ".claude.json", "format": "json", "container": "mcpServers", "provider_path": ".claude/settings.json", "skill_dir": ".claude/skills", "prompt_path": ".claude/CLAUDE.md"},
    "opencode": {"name": "OpenCode", "mcp_path": ".config/opencode/opencode.json", "format": "json", "container": "mcp", "provider_path": ".config/opencode/opencode.json", "skill_dir": ".config/opencode/skills"},
}
from app.integrations.host_management_native import (
    NATIVE_HOSTS, effective_host, omp_provider_specs, omp_activation_specs,
    cursor_prompt_spec, antigravity_prompt_spec, native_skill,
    require_omp_models_migration, require_omp_settings_migration,
)
HOSTS.update(NATIVE_HOSTS)
HOSTS["codex"]["configuration_notice"] = "Writes shared Codex native files also used by Codex Desktop. Running sessions may need to reload the new settings."

ENTITY_KINDS = {"provider", "model", "binding", "account", "asset", "bundle"}


def _has_entity_reference(conn, kind, entity_id):
    if kind in ('provider', 'account'):
        field = 'provider_id' if kind == 'provider' else 'account_id'
        predicate = f"json_extract(e.data,'$.{field}')=?"
        params = [entity_id]
        if kind == 'account':
            predicate += """ OR (e.kind='asset' AND EXISTS(SELECT 1 FROM json_each(e.data,'$.mcp_account_bindings') b
                WHERE json_extract(b.value,'$.account_id')=?))"""
            params.append(entity_id)
    elif kind == 'model':
        predicate = "e.kind IN ('binding','bundle') AND json_extract(e.data,'$.model_id')=?"
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
        from app.database import db

        with db() as local:
            ensure_schema(local)
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS management_undo_states (
        plan_id TEXT PRIMARY KEY, selection TEXT, dependencies TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_native_imports (
        source_key TEXT PRIMARY KEY, entity_id TEXT NOT NULL, definition_hash TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_native_origins (
        entity_id TEXT PRIMARY KEY, relative_path TEXT NOT NULL, entry_name TEXT NOT NULL, value_hash TEXT NOT NULL,
        portable_entry INTEGER NOT NULL, was_managed INTEGER NOT NULL DEFAULT 0)""")
    conn.execute('CREATE INDEX IF NOT EXISTS idx_management_native_origins_path ON management_native_origins(relative_path)')
    from app.services import agent_preferences, shared_batches
    agent_preferences.ensure_schema(conn)
    shared_batches.ensure_schema(conn)
    conn.execute("""CREATE TABLE IF NOT EXISTS management_entities (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, data TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_personal_methods (
        method_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL UNIQUE)""")
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


def store_definition(conn, kind, entity_id, value, prior_version, timestamp):
    """Write a validated definition and its history in the caller's transaction."""
    data = canonical(value)
    if prior_version:
        changed = conn.execute(
            "UPDATE management_entities SET data=?,version=version+1,updated_at=? WHERE id=? AND kind=? AND version=?",
            (data, timestamp, entity_id, kind, prior_version),
        )
        if changed.rowcount != 1:
            raise ConfigurationError("Object changed concurrently")
    else:
        conn.execute("INSERT INTO management_entities VALUES(?,?,?,1,?,?)", (entity_id, kind, data, timestamp, timestamp))
    if kind == "asset":
        conn.execute("INSERT INTO management_asset_versions VALUES(?,?,?,?)", (entity_id, prior_version + 1, data, timestamp))


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
        conn.execute("PRAGMA mmap_size = 0")
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
                "capabilities": {"independent_workbench": True, "plan_apply_readback": True, "secret_free_export": True, "automatic_host_reload": False, "mcp_tools_probe": True}}

    def host_config(self, host_id, *, display=False):
        from app.services.host_config_roots import resolve
        return resolve(self, host_id, HOSTS[host_id], {} if self.isolated_home else os.environ, display=display)

    def hosts(self) -> list[dict]:
        from app.services import host_registry
        registry = {entry["id"]: entry for entry in host_registry.discover_hosts(home=self.home, env={} if self.isolated_home else dict(os.environ))}
        result = []
        for host_id, host in HOSTS.items():
            host = self.host_config(host_id, display=True)
            result.append({"id": host_id, "name": host["name"], "surface": "cli" if host_id in ("codex", "claude-code", "opencode", "omp") else "desktop_or_service",
                           "shared_configuration": host['shared_configuration'],
                           **({"config_root": host["config_root"]} if "config_root" in host else {}),
                           "configuration_status": "supported_format" if any(host.get(key) for key in ("mcp_path", "provider_path", "skill_dir", "prompt_path")) else "unsupported",
                           "verification_status": "not_verified", "unsupported_reason": host.get("unsupported_reason"), "provider_unsupported_reason": host.get("provider_unsupported_reason"), "provider_mode": host.get("provider_mode", "coexist_named_entries"),
                           "configuration_notice": host.get("configuration_notice"), "prompt_notice": host.get("prompt_notice"), "format_sources": host.get("sources", []),
                           "selection_config_only": host_id == "omp",
                           "operation_limits": {category: {"status": "external_host_ui_required", "operations": ["install", "update", "switch", "restore", "uninstall"], "reason": host["provider_unsupported_reason"], "sources": host.get("sources", [])} for category in ("providers",) if host.get("provider_unsupported_reason")},
                           "capabilities": {"mcp": bool(host.get("mcp_path")), "providers": bool(host.get("provider_path")) or host_id == "codex",
                                            "skills": bool(host.get("skill_dir")), "prompts": bool(host.get("prompt_path") or host.get("prompt_dir")), "oauth": False},
                           "paths": {key: str(self.home / value) for key, value in host.items() if key.endswith("_path") or key in ("skill_dir", "prompt_dir")}})
        for item in result:
            item["capabilities"]["native_activation"] = item["id"] in ("codex", "omp", "claude-code", "opencode")
            item["provider_protocols"] = ["openai"] if item["id"] in ("codex",) else ["openai", "anthropic", "gemini"] if item["id"] == "omp" else ["anthropic"] if item["id"] == "claude-code" else ["openai", "anthropic"] if item["capabilities"]["providers"] else []
            dependency = HOSTS[item["id"]].get("runtime_dependency")
            if dependency:
                from app.integrations.structured_config import runtime_status
                format_name = "yaml" if dependency == "ruamel.yaml" else "json5"
                item["format_runtime"] = runtime_status(format_name)
            identity = item["id"]
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
        codex = next(item for item in result if item["id"] == "codex")
        if codex["shared_configuration"]:
            # Legacy Desktop activations were migrated to the canonical codex owner.
            codex["shared_provider_target"] = "codex"
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
        from app.services.host_config_roots import shared_membership

        removed = shared_membership(self, host_id, kind, old.get("kind")) if not selected else ()
        agents = list(dict.fromkeys(agent for agent in agents if agent in HOSTS and agent not in removed))
        if selected and host_id not in agents:
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
        state = self.host_state(host_id)
        selection = state.get("selection") or {}
        target_state = state.get("resources", {}).get(provider_id, {}).get("status")
        if (selection.get("provider_id") and selection.get("status") in ('missing', 'externally_changed')) or target_state in ('missing', 'externally_changed'):
            raise ConfigurationError('Managed configuration was externally edited or removed; review the native file before switching', 409)
        resolved_model = model_id or (selection.get("model_id") if selection.get("provider_id") == provider_id else None)
        if not resolved_model and auto_apply:
            summary = self.catalog_page('provider', entity_ids=[provider_id])
            resolved_model = summary['items'][0].get('default_model')
        if not resolved_model:
            raise ConfigurationError("Select an explicit model before previewing a provider switch", 422)
        request = {"host_id": host_id, "provider_id": provider_id,
                   "activate_provider": True, "model_id": resolved_model}
        if host_id == "claude-code" and selection.get("provider_id") and selection["provider_id"] != provider_id:
            request["replace_provider_id"] = selection["provider_id"]
        plan = self.plan(request)
        metadata = plan["payload"]["metadata"]
        choice = metadata.get('model_selection', {})
        account_changed = selection.get('provider_id') == provider_id and any(
            selection.get(field) != choice.get(field) for field in ('account_id', 'credential_source')
        )
        review_needed = account_changed or target_state == 'update_available' or bool(state.get("env_conflicts")) or selection.get("connection_mode", "direct") != "direct" or bool(
            metadata.get("shared_configuration") and selection.get("status") != "verified"
        ) or any(
            item.get("before_hash") is not None and any(
                spec.get("takeover_selected_field") and not spec.get("original_ownership")
                for spec in item.get("specs", [])
            )
            for item in plan["payload"]["files"]
        )
        if auto_apply and review_needed:
            return {"ok": True, "status": "review_required", "host_id": host_id, "provider_id": provider_id,
                    "model_id": resolved_model, "plan_id": plan["id"], "plan": plan}
        if auto_apply:
            try:
                applied = self.apply(plan["id"], plan["version"], True)
                return {"ok": True, "status": "applied", "host_id": host_id, "provider_id": provider_id,
                        "model_id": resolved_model, "plan_id": applied["id"], "plan": applied}
            except ConfigurationError as exc:
                latest = self.transactions.get(plan["id"])
                if latest["status"] != "planned" or exc.status_code != 409:
                    raise
                return {"ok": False, "status": "conflict_preview", "conflict_error": str(exc), "host_id": host_id,
                        "provider_id": provider_id, "model_id": resolved_model, "plan_id": plan["id"], "plan": latest}
        return {"ok": True, "status": "planned", "host_id": host_id, "provider_id": provider_id,
                "model_id": resolved_model, "plan_id": plan["id"], "plan": plan}

    @configuration_locked
    def quick_toggle_asset(self, host_id: str, asset_id: str, enabled: bool = True, *, auto_apply: bool = False) -> dict:
        """Prepare an owned-resource install/removal; when auto_apply=True, apply immediately unless a conflict occurs."""
        if type(enabled) is not bool:
            raise ConfigurationError("enabled must be a boolean", 422)
        operation = "install" if enabled else "uninstall"
        plan = self.plan({"host_id": host_id, "asset_ids": [asset_id], "operation": operation})
        if auto_apply and plan['payload']['metadata'].get('native_takeover'):
            return {"ok": True, "status": "review_required", "host_id": host_id, "asset_id": asset_id,
                    "enabled": enabled, "operation": operation, "plan_id": plan["id"], "plan": plan}
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
                if result.get("status") in ("conflict_preview", "review_required"):
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
        value.pop("cc_switch_current_hosts", None)
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
                from app.services.mcp_bindings import settings as mcp_settings
                mcp_settings(self, value)
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
            for field in ('host_ids', 'asset_ids'):
                identifiers = value.setdefault(field, [])
                if not isinstance(identifiers, list) or any(not isinstance(key, str) for key in identifiers):
                    raise ConfigurationError("Profile references must be lists of identifiers", 422)
                value[field] = list(dict.fromkeys(identifiers))
            if type(value.get("replace_previous_assets", False)) is not bool:
                raise ConfigurationError("Invalid profile switching policy", 422)
            if value.get("model_id"):
                model = self.get("model", value["model_id"])
                if model["provider_id"] != value.get("provider_id"):
                    raise ConfigurationError("Profile model belongs to a different provider", 422)
            for asset_id in value["asset_ids"]:
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
        if kind == 'asset' and old and old.get('kind') == 'mcp':
            from app.services.mcp_bindings import requires_review
            if requires_review(old):
                if data.get('mcp_reviewed') is True and 'config' in data:
                    for key in ('native_review_required', 'credential_reattach_required', 'mcp_fields_to_review'):
                        merged.pop(key, None)
                else:
                    merged['native_review_required'] = True
        if kind == 'asset':
            merged = {k: v for k, v in merged.items() if k != 'mcp_reviewed'}
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
            store_definition(conn, kind, entity_id, value, expected_version if old else 0, now)
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
                        bound = value.pop('mcp_account_bindings', [])
                        if bound or had_creds:
                            fields = [{key: record[key] for key in ('location', 'name', 'bearer')} for record in bound]
                            fields += [{'location': location, 'name': name, 'bearer': raw.startswith('Bearer ')}
                                       for location in ('headers', 'env') for name, raw in obj['config'].get(location, {}).items()
                                       if isinstance(raw, str) and SECRET_REF.search(raw)]
                            value['mcp_fields_to_review'] = fields
                            value['credential_reattach_required'] = True
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
            if entry['data'].get('mcp_account_bindings'):
                raise ConfigurationError('Reattach MCP accounts locally after import', 422)
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
        if request.get('bundle_id') and request.get('operation', 'install') == 'install':
            bundle = self.get('bundle', request['bundle_id'])
            if bundle.get('model_id') and 'activate_provider' not in request:
                request = {**request, 'activate_provider': True, 'model_id': bundle['model_id']}
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
        host = self.host_config(host_id)
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
        profile_options = {}
        if request.get('bundle_id'):
            profile_options['bundle_assets'] = asset_ids if operation == 'install' else []
        if request.get('bundle_id') and operation == 'install' and bundle.get('replace_previous_assets'):
            with self.connect() as conn:
                prior = conn.execute("""SELECT json_extract(data,'$.bundle_plan_id'),json_extract(data,'$.bundle_exclusive_assets')
                    FROM management_activations WHERE host_id=?""", (host_id,)).fetchone()
            profile_options['previous_bundle_plan'] = prior[0] if prior else None
            previous = json.loads(prior[1]) if prior and prior[1] else []
            profile_options['previous_exclusive_assets'] = previous
            removed = [key for key in previous if key not in asset_ids]
            profile_options['removed_assets'] = removed
            for key in removed:
                try:
                    asset = self.get('asset', key)
                except ConfigurationError as exc:
                    if exc.status_code == 404:
                        continue  # An already uninstalled and deleted resource has no remaining managed entry.
                    raise
                versions[key] = asset['version']
                configuration_changes.append({'name': asset['name'], 'kind': asset['kind'], 'operation': 'uninstall'})
                specs.extend(self._asset_specs(host_id, host, asset, 'uninstall'))
        for asset_id in asset_ids:
            asset = self.get("asset", asset_id)
            versions[asset_id] = asset["version"]
            configuration_changes.append({'name': asset['name'], 'kind': asset['kind'], 'operation': operation})
            if asset.get('mcp_account_bindings') and operation == 'install':
                from app.services.mcp_bindings import settings as mcp_settings
                _, bound = mcp_settings(self, asset)
                versions.update(bound)
                provider_dependencies[asset_id] = list(bound)
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
                if host_id == 'omp' and model.get('context_window') is not None:
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
            if host_id == 'claude-code' and operation == 'install':
                path = str(self.home / host['provider_path'])
                raw = self.transactions._read(Path(path))
                try:
                    existing = (json.loads(raw).get('env') or {}) if raw else {}
                    if not isinstance(existing, dict):
                        raise ValueError
                except (ValueError, AttributeError):
                    raise ConfigurationError('Native environment is not a valid object', 422) from None
                for key in ('ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_CUSTOM_HEADERS'):
                    if not existing.get(key) or (key == 'ANTHROPIC_AUTH_TOKEN' and provider.get('credential_ref')):
                        continue
                    pointer = ['env', key]
                    prior = self.transactions._owned(path, 'json:' + canonical(pointer))
                    if key == 'ANTHROPIC_AUTH_TOKEN' and prior and (
                        prior['owner'] == 'activation:' + host_id or prior['owner'] == request.get('replace_provider_id')
                    ):
                        # The existing owned-token removal path handles this field.
                        continue
                    if request.get('activate_provider') is not True:
                        raise ConfigurationError('Existing native authentication needs an explicit model activation and reviewed snapshot', 409)
                    # Shadow an unbound credential with an empty native env
                    # value. The original is kept only in the private snapshot.
                    provider_specs.append({'path': path, 'kind': 'json', 'pointer': pointer, 'value': '',
                                           'owner': 'activation:' + host_id, 'operation': 'install'})
            if choice and choice['projection']:
                for spec in provider_specs:
                    spec['credential_projection'] = {choice['reference']: choice['projection']}
            if request.get("activate_provider") is True:
                if operation != "install":
                    raise ConfigurationError("Activation is an explicit install operation", 422)
                native_model = self._selected_model(provider, request.get("model_id"))
                model_selection = {'provider_name': provider['name'], 'native_model': native_model, 'protocol': provider['protocol'],
                                   'credential_source': choice['source'] if choice else None,
                                   'account_id': (choice.get('account') or {}).get('id') if choice else None,
                                   'account_name': (choice.get('account') or {}).get('name') if choice else None}
                if host_id == "claude-code":
                    # The reviewed activation owns exactly these native fields,
                    # retaining their original values only in private snapshots.
                    for spec in provider_specs:
                        spec.update(owner="activation:" + host_id, takeover_selected_field=True, activation_field=True)
                    if not provider.get("credential_ref"):
                        token_path = str(self.home / host["provider_path"])
                        token_key = "json:" + canonical(["env", "ANTHROPIC_AUTH_TOKEN"])
                        with self.connect() as conn:
                            act_secret = conn.execute(
                                "SELECT 1 FROM management_owned WHERE path=? AND item_key=? AND owner=?",
                                (token_path, token_key, "activation:" + host_id),
                            ).fetchone()
                        if act_secret:
                            provider_specs.append({"path": token_path, "kind": "json", "pointer": ["env", "ANTHROPIC_AUTH_TOKEN"], "value": None, "owner": "activation:" + host_id, "operation": "uninstall", "activation_field": True})
                    provider_specs.append({"path": str(self.home / host["provider_path"]), "kind": "json", "pointer": ["env", "ANTHROPIC_MODEL"], "value": native_model, "owner": "activation:" + host_id, "operation": "install", "takeover_selected_field": True, "activation_field": True})
                elif host_id in ("codex",):
                    name = "senrelay_" + provider_id.replace("provider_", "")
                    for key, value in (("model_provider", name), ("model", native_model)):
                        provider_specs.append({"path": str(self.home / host["mcp_path"]), "kind": "toml_selection", "pointer": [key], "value": value, "owner": "activation:" + host_id, "operation": "install", "takeover_selected_field": True, "activation_field": True})
                elif host_id == "opencode":
                    provider_specs.append({"path": str(self.home / host["provider_path"]), "kind": "json", "pointer": ["model"], "value": provider_id + "/" + native_model, "owner": "activation:" + host_id, "operation": "install", "takeover_selected_field": True, "activation_field": True})
                elif host_id == "omp":
                    provider_specs.extend(omp_activation_specs(host, provider, native_model, home=self.home))
                else:
                    raise ConfigurationError("Native provider activation has not been verified for this host surface", 422)
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
        return specs, {"host_id": host_id, "operation": operation,
            "native_takeover": any(spec.get('native_origin') for spec in specs), "entity_versions": versions, 'provider_dependencies': provider_dependencies,
            'configuration_changes': configuration_changes, **({'model_selection': model_selection} if model_selection else {}),
            "configuration_notice": host.get("configuration_notice"),
            "shared_configuration": host['shared_configuration'], **({'root_scope': host['root_scope']} if 'root_scope' in host else {}),
            "selection_config_only": host_id == "omp",
            **({"native_paths": {key: host[key] for key in ("provider_path", "selection_path")}} if host_id == "omp" and provider_id else {}),
            "prompt_notice": host.get("prompt_notice") if any(change["kind"] == "prompt" for change in configuration_changes) else None,
            "bundle_id": request.get("bundle_id"), **profile_options, "scope": "selected_host_global", "activate_provider": request.get("activate_provider") is True, "provider_id": provider_id, "model_id": native_model if request.get("activate_provider") is True else None}

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
        with self.connect() as conn:
            saved = conn.execute("""SELECT 1 FROM management_entities WHERE kind='model'
                AND json_extract(data,'$.provider_id')=? AND json_extract(data,'$.model_id')=? LIMIT 1""",
                (provider["id"], model_id)).fetchone()
        if saved:
            return model_id
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
        host = self.host_config(host_id)
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
            format_name = "toml" if field["kind"] in ("toml_selection", "toml_table") else field["kind"] if field["kind"] in ("yaml", "json5", "file", "block") else "json"
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
            "shared_configuration": host['shared_configuration'], **({'root_scope': host['root_scope']} if 'root_scope' in host else {}), "configuration_notice": host.get("configuration_notice"), "prompt_notice": host.get("prompt_notice")})

    async def probe_asset(self, asset_id: str, confirmed: bool, transport=None, *, expected_version=None) -> dict:
        if confirmed is not True:
            raise ConfigurationError("Confirm the selected MCP network connection before probing", 422)
        asset = self.get("asset", asset_id)
        if expected_version is not None and (type(expected_version) is not int or expected_version != asset['version']):
            raise ConfigurationError("MCP definition changed after review; reload before probing", 409)
        if asset["kind"] != "mcp":
            raise ConfigurationError("Only MCP assets support tools discovery", 422)
        from app.services.mcp_bindings import requires_review, settings as mcp_settings
        if requires_review(asset):
            raise ConfigurationError('Review and save the MCP configuration before probing', 409)
        config, _ = mcp_settings(self, asset, resolve=True)
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
        if host_id not in HOSTS:
            raise ConfigurationError("Unknown host", 422)
        host = self.host_config(host_id)
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
        from app.services import native_asset_origin
        owner = asset["id"]
        native = native_asset_origin.plan_target(self, host_id, asset, operation)
        if native and asset['kind'] == 'prompt':
            return [{**native, 'value': asset['content'], 'asset_kind': 'prompt'}]
        if asset["kind"] == "mcp":
            from app.services.mcp_bindings import requires_review, settings as mcp_settings
            if operation == 'install' and requires_review(asset):
                raise ConfigurationError('Review and save the MCP configuration to supply omitted native environment or headers', 409)
            if not host.get("mcp_path"):
                raise ConfigurationError(host.get("unsupported_reason", "MCP format is unsupported"), 422)
            config = mcp_settings(self, asset, resolve=True)[0] if operation == 'install' else copy.deepcopy(asset["config"])
            name = "senrelay_" + owner.replace("asset_", "")
            path = str(self.home / host["mcp_path"])
            self.transactions.safe_path(path)
            if native and native['kind'] == 'toml_table':
                transport = config.pop('transport', 'stdio')
                if host_id in ('codex',) and transport == 'sse':
                    raise ConfigurationError('Codex SSE configuration is unsupported', 422)
                if transport == 'http' and host_id in ('codex',) and 'headers' in config:
                    config['http_headers'] = config.pop('headers')
                return [{**native, 'value': config}]
            transport = config.pop("transport", "stdio")
            if host_id in ("codex",):
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
                config["type"] = transport
                if host_id == "antigravity":
                    config["serverUrl"] = config.pop("url")
                    config.pop("type", None)
            return [{"path": path, "kind": "json", "pointer": [host["container"], name], "owner": owner,
                     "operation": operation, "value": config, **(native or {})}]
        if asset["kind"] == "prompt":
            if host.get("prompt_mode") == "manual_skill":
                return [cursor_prompt_spec(host, asset, home=self.home, operation=operation)]
            if host.get("prompt_mode") == "global_rule":
                return [antigravity_prompt_spec(host, asset, home=self.home, operation=operation)]
            if not host.get("prompt_path"):
                raise ConfigurationError(host.get("prompt_unsupported_reason", "Prompt file format/path is not verified for this host"), 422)
            return [{"path": str(self.home / host["prompt_path"]), "kind": "block", "marker": owner, "owner": owner, "operation": operation, "value": asset["content"]}]
        if not host.get("skill_dir"):
            raise ConfigurationError("Skill installation is unsupported for this host", 422)
        folder, files = native_skill(asset, host_id) if operation == "install" else (owner, asset["files"])
        skill_root = self.home / host["skill_dir"]
        with self.connect() as conn:
            owned = conn.execute("SELECT path FROM management_owned WHERE owner=? AND item_key LIKE 'file:%'", (owner,)).fetchall()
        owned_paths = {item["path"] for item in owned if Path(item["path"]).is_relative_to(skill_root)}
        if host_id in ("codex",) and owned_paths:
            # Older CLI and Desktop renderers chose different folder names for
            # the same physical library. Keep the installed location on upgrade.
            installed = sorted({path.parent.name for raw in owned_paths if (path := Path(raw)).name == "SKILL.md"
                                and path.parent.parent == skill_root})
            if installed and folder not in installed:
                folder = installed[0]
        target_root = skill_root / folder
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
            # Claude Code appends /v1/messages to its SDK root. Senrelay
            # presets and provider checks use the versioned API base.
            values = {"ANTHROPIC_BASE_URL": provider["base_url"].removesuffix("/v1")}
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
        if host_id in ("codex",):
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
            return [{"path": str(self.home / host["mcp_path"]), "kind": "block", "marker": owner, "format": "toml", "value": body.rstrip(), "owner": owner, "operation": operation}]
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
        from app.services.host_config_roots import check_plan
        check_plan(self, plan)
        self.transactions.require_fresh(plan)
        metadata = plan["payload"]["metadata"]
        if 'previous_bundle_plan' in metadata and plan['status'] != 'applied':
            with self.connect() as conn:
                prior = conn.execute("""SELECT json_extract(data,'$.bundle_plan_id'),json_extract(data,'$.bundle_exclusive_assets')
                    FROM management_activations WHERE host_id=?""", (metadata['host_id'],)).fetchone()
            exclusive = json.loads(prior[1]) if prior and prior[1] else []
            if (prior[0] if prior else None) != metadata['previous_bundle_plan'] or exclusive != metadata.get('previous_exclusive_assets', []):
                raise ConfigurationError('Configuration profile changed since preview')
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
            from app.services import management_undo
            if metadata.get('undo_plan_id'):
                management_undo.restore_receipt(conn, applying_plan)
                return
            management_undo.remember_before(conn, applying_plan)
            for item in applying_plan['payload']['files']:
                for spec in item['specs']:
                    if spec.get('native_origin'):
                        # Only an intentional removal grants permission to reinstall
                        # an absent original. An external deletion must stay visible.
                        conn.execute('UPDATE management_native_origins SET was_managed=? WHERE entity_id=?',
                                     (int(spec['operation'] == 'uninstall'), spec['native_origin']))
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
                    active["model_selection"] = metadata.get("model_selection", {})
                    active.pop('selection_receipt_unknown', None)
                if 'root_scope' in metadata:
                    active['root_scope'] = metadata['root_scope']
                active['selection_receipt_version'] = 1
                if metadata.get('bundle_id'):
                    active["bundle_id"] = metadata['bundle_id'] if metadata.get('operation') == 'install' else None
                    active["bundle_plan_id"] = applying_plan['id']
                    from app.services.configuration_profiles import exclusive_resources
                    active["bundle_exclusive_assets"] = exclusive_resources(active, applying_plan)
                active["last_plan_id"] = applying_plan["id"]
                conn.execute("INSERT OR REPLACE INTO management_activations VALUES(?,?,?)", (host_id, canonical(active), datetime.now(timezone.utc).isoformat()))
            from app.services.configuration_profiles import release_independently_managed
            release_independently_managed(conn, applying_plan)
            routing.finalize(conn, applying_plan)

        # Activation baselines are part of the configuration transaction's
        # final SQLite commit. If this write fails, ownership and `applied`
        # status roll back too, and the transaction retains a recoverable plan.
        original_vault = self.transactions.vault
        try:
            if pending:
                self.transactions.vault = routing.apply_vault(plan, pending)
            from app.services import management_undo
            renderer = (lambda undo: management_undo.render(self, undo)) if metadata.get('undo_plan_id') else None
            result = self.transactions.apply(plan_id, expected_version, confirmed, finalize=finalize_activation, renderer=renderer)
        finally:
            self.transactions.vault = original_vault
        if pending:
            self.retry_credential_cleanup()
        return result

    @configuration_locked
    @audited_plan_operation
    def recover_plan(self, plan_id, confirmed):
        from app.services.host_routing import HostRouting
        from app.services.host_config_roots import check_plan
        check_plan(self, self.transactions.get(plan_id))
        return HostRouting(self).recover(plan_id, confirmed)

    @configuration_locked
    def preview_connection(self, host_id, body, origin):
        from app.services.host_routing import HostRouting
        return HostRouting(self).preview(host_id, body, origin)

    def connection_mode(self, host_id):
        if host_id in HOSTS:
            self.host_config(host_id)
        from app.services.host_routing import HostRouting
        return HostRouting(self).state(host_id)



def service() -> ManagementService:
    return ManagementService()


def resolve_provider(provider_id: str) -> dict:
    manager = service()
    value = manager.get("provider", provider_id)
    if not value.get("enabled", True):
        raise ConfigurationError("Provider is disabled")
    from app.services.provider_credentials import binding, resolved_key
    choice = binding(manager, value)
    key = resolved_key(manager, choice)
    account = choice.get("account") or {}
    return {**value, "api_key": key, "credential_source": choice["source"],
            "resolved_account_id": account.get("id"), "resolved_account_name": account.get("name"),
            "resolved_account_version": account.get("version")}
