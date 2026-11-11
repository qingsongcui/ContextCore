"""Complete user journeys around the existing configuration transaction engine.

Definitions and Keychain secrets are saved together; host mutation remains a
separate reviewed plan. Configured state is read from owned native entries and
never elevated to evidence that the host loaded or called a model.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from app.integrations.config_transactions import ConfigurationError, canonical, reject_secrets
from app.services.management_service import HOSTS
from app.services.selection_receipts import HEADER_SQL, selected_state


def setup_provider(manager, body: dict) -> dict:
    if not isinstance(body, dict) or set(body) - {"request_id", "host_id", "provider", "model_id", "context_window", "secret"}:
        raise ConfigurationError("Unsupported provider setup fields", 422)
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
        raise ConfigurationError("A unique request_id of 16–80 characters is required", 422)
    host_id = body.get("host_id")
    if host_id not in HOSTS:
        raise ConfigurationError("Unknown host", 422)
    definition = body.get("provider")
    if not isinstance(definition, dict) or set(definition) - {"name", "base_url", "protocol", "wire_api", "supported_endpoints"}:
        raise ConfigurationError("Provider setup accepts connection and declared API endpoint fields", 422)
    provider = manager._validate("provider", {**definition, "agent_ids": [host_id]})
    native_model = body.get("model_id")
    if not isinstance(native_model, str) or not native_model.strip() or len(native_model) > 200 or any(ord(char) < 32 for char in native_model):
        raise ConfigurationError("A model_id of 1–200 characters is required", 422)
    native_model = native_model.strip()
    reject_secrets(native_model)
    capacity = body.get('context_window')
    if capacity is not None and (type(capacity) is not int or not 0 < capacity <= 9223372036854775807):
        raise ConfigurationError('context_window must be a positive integer or unknown', 422)
    secret = body.get("secret")
    if secret is not None and (not isinstance(secret, str) or not secret or len(secret) > 16384 or any(char in secret for char in "\x00\r\n")):
        raise ConfigurationError("Credential must be a non-empty single-line value", 422)
    # Store a fingerprint only; no request body or credential enters SQLite.
    fingerprint = hashlib.sha256(canonical({"host_id": host_id, "provider": provider, "model": native_model,
        **({'context_window': capacity} if capacity is not None else {}),
        "secret": hashlib.sha256(secret.encode()).hexdigest() if secret is not None else None}).encode()).hexdigest()
    with manager.connect() as conn:
        prior = conn.execute("SELECT * FROM management_provider_setups WHERE request_id=?", (request_id,)).fetchone()
    if prior:
        if prior["request_hash"] != fingerprint:
            raise ConfigurationError("This setup request changed; start a new request", 409)
        return {"provider": manager._public(manager.get("provider", prior["provider_id"])),
                "model": manager._public(manager.get("model", prior["model_id"])),
                "host_configuration": "unchanged", "request_id": request_id}

    if host_id == 'kimi-code' and capacity is None:
        raise ConfigurationError('Kimi setup requires the model context_window before saving', 422)

    reference = manager._store_secret(secret) if secret is not None else None
    if reference:
        provider["credential_ref"] = reference
    provider_id, model_id = "provider_" + uuid4().hex[:16], "model_" + uuid4().hex[:16]
    model = {"name": native_model, "model_id": native_model, "provider_id": provider_id}
    if capacity is not None:
        model['context_window'] = capacity
    now = datetime.now(timezone.utc).isoformat()
    try:
        with manager.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO management_entities VALUES(?,?,?,1,?,?)", (provider_id, "provider", canonical(provider), now, now))
            conn.execute("INSERT INTO management_entities VALUES(?,?,?,1,?,?)", (model_id, "model", canonical(model), now, now))
            conn.execute("INSERT INTO management_provider_setups VALUES(?,?,?,?,?)", (request_id, fingerprint, provider_id, model_id, now))
    except Exception:
        if reference:
            with manager.connect() as conn:
                cleanup = manager._cleanup_receipt(conn, reference, "provider_setup_not_adopted")
            manager.retry_credential_cleanup(cleanup)
        raise
    return {"provider": manager._public(manager.get("provider", provider_id)), "model": manager._public(manager.get("model", model_id)),
            "host_configuration": "unchanged", "request_id": request_id}


def _revision_row(manager, request_id):
    with manager.connect() as conn:
        return conn.execute("SELECT * FROM management_provider_revisions WHERE request_id=?", (request_id,)).fetchone()


def _revision_result(manager, request_id):
    row = _revision_row(manager, request_id)
    return {"provider": manager._public(manager.get("provider", row["provider_id"])),
            "model": manager._public(manager.get("model", row["model_id"])),
            "host_configuration": "unchanged", "request_id": request_id}


def _without_identity(row):
    return {key: value for key, value in row.items() if key not in ("id", "version", "created_at", "updated_at", "has_credential")}


def revise_provider_setup(manager, body: dict) -> dict:
    """Update one provider and one of its models in a single write.

    Unrelated stored fields stay. The assistant file is not touched, and a
    failed or repeated request cannot leave a second provider or an adopted key.
    """
    allowed = {"request_id", "host_id", "provider_id", "model_entity_id", "expected_provider_version", "expected_model_version", "provider", "model_id", "context_window", "secret"}
    if not isinstance(body, dict) or set(body) - allowed:
        raise ConfigurationError("Unsupported provider revision fields", 422)
    request_id = body.get("request_id")
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,80}", request_id):
        raise ConfigurationError("A unique request_id of 16–80 characters is required", 422)
    host_id = body.get("host_id")
    if host_id not in HOSTS:
        raise ConfigurationError("Unknown host", 422)
    provider_id = body.get("provider_id")
    model_entity_id = body.get("model_entity_id")
    if not isinstance(provider_id, str) or not provider_id or not isinstance(model_entity_id, str) or not model_entity_id:
        raise ConfigurationError("Choose the provider and model to revise", 422)
    expected_provider = body.get("expected_provider_version")
    expected_model = body.get("expected_model_version")
    if type(expected_provider) is not int or type(expected_model) is not int or expected_provider < 1 or expected_model < 1:
        raise ConfigurationError("Reload the provider and model versions before editing", 422)
    definition = body.get("provider")
    if not isinstance(definition, dict) or set(definition) - {"name", "base_url", "protocol", "wire_api", "supported_endpoints"}:
        raise ConfigurationError("Provider revision accepts connection and declared API endpoint fields", 422)
    if any(key not in definition for key in ("name", "base_url", "protocol")):
        raise ConfigurationError("Name, endpoint and protocol are required", 422)
    native_model = body.get("model_id")
    if not isinstance(native_model, str) or not native_model.strip() or len(native_model) > 200 or any(ord(char) < 32 for char in native_model):
        raise ConfigurationError("A model_id of 1–200 characters is required", 422)
    native_model = native_model.strip()
    reject_secrets(native_model)
    capacity = body.get("context_window", "absent")
    if capacity != "absent" and capacity is not None and (type(capacity) is not int or not 0 < capacity <= 9223372036854775807):
        raise ConfigurationError("context_window must be a positive integer or unknown", 422)
    secret = body.get("secret", "absent")
    if secret != "absent" and (not isinstance(secret, str) or not secret or len(secret) > 16384 or any(char in secret for char in "\x00\r\n")):
        raise ConfigurationError("Credential must be a non-empty single-line value", 422)
    fingerprint = hashlib.sha256(canonical({
        "host_id": host_id, "provider_id": provider_id, "model_entity_id": model_entity_id,
        "expected_provider_version": expected_provider, "expected_model_version": expected_model,
        "provider": definition, "model": native_model,
        **({} if capacity == "absent" else {"context_window": capacity}),
        "secret": None if secret == "absent" else hashlib.sha256(secret.encode()).hexdigest(),
    }).encode()).hexdigest()
    prior = _revision_row(manager, request_id)
    if prior:
        if prior["request_hash"] != fingerprint:
            raise ConfigurationError("This setup request changed; start a new request", 409)
        return _revision_result(manager, request_id)

    stored_provider = manager.get("provider", provider_id)
    stored_model = manager.get("model", model_entity_id)
    if stored_model.get("provider_id") != provider_id:
        raise ConfigurationError("Selected model belongs to another provider", 422)
    if host_id not in (stored_provider.get("agent_ids") or []):
        raise ConfigurationError("This provider is not on this assistant's list", 422)
    if stored_provider["version"] != expected_provider or stored_model["version"] != expected_model:
        raise ConfigurationError("Object version changed; reload before editing", 409)
    merged_provider = _without_identity(stored_provider)
    for key in ("name", "base_url", "protocol"):
        merged_provider[key] = definition[key]
    for key in ("wire_api", "supported_endpoints"):
        if key in definition:
            if definition[key] is None:
                merged_provider.pop(key, None)
            else:
                merged_provider[key] = definition[key]
    merged_provider = manager._validate("provider", merged_provider, entity_id=provider_id)
    merged_model = _without_identity(stored_model)
    merged_model.update(name=native_model, model_id=native_model, provider_id=provider_id)
    if capacity != "absent":
        if capacity is None:
            merged_model.pop("context_window", None)
        else:
            merged_model["context_window"] = capacity
    merged_model = manager._validate("model", merged_model, entity_id=model_entity_id)
    if host_id == "kimi-code" and merged_model.get("context_window") is None:
        raise ConfigurationError("Kimi setup requires the model context_window before saving", 422)
    uses_account = stored_provider.get("credential_source") == "account" or (
        not stored_provider.get("credential_source") and stored_provider.get("account_id") and not stored_provider.get("credential_ref"))
    if secret != "absent" and uses_account:
        raise ConfigurationError("This provider uses an account credential; change that account instead of saving a provider key", 422)

    reference = None
    cleanup_id = None
    try:
        if secret != "absent":
            reference = manager._store_secret(secret)
            merged_provider["credential_ref"] = reference
            merged_provider["credential_source"] = "provider"
            merged_provider["credential_reattach_required"] = False
            merged_provider = manager._validate("provider", merged_provider, entity_id=provider_id)
        now = datetime.now(timezone.utc).isoformat()
        with manager.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_provider = conn.execute("SELECT version FROM management_entities WHERE id=? AND kind='provider'", (provider_id,)).fetchone()
            current_model = conn.execute("SELECT version FROM management_entities WHERE id=? AND kind='model'", (model_entity_id,)).fetchone()
            if not current_provider or not current_model or current_provider["version"] != expected_provider or current_model["version"] != expected_model:
                raise ConfigurationError("Object version changed; reload before editing", 409)
            changed = conn.execute("UPDATE management_entities SET data=?,version=version+1,updated_at=? WHERE id=? AND kind='provider' AND version=?", (canonical(merged_provider), now, provider_id, expected_provider))
            if changed.rowcount != 1:
                raise ConfigurationError("Object changed concurrently", 409)
            changed = conn.execute("UPDATE management_entities SET data=?,version=version+1,updated_at=? WHERE id=? AND kind='model' AND version=?", (canonical(merged_model), now, model_entity_id, expected_model))
            if changed.rowcount != 1:
                raise ConfigurationError("Object changed concurrently", 409)
            old_ref = stored_provider.get("credential_ref")
            if reference and old_ref and old_ref != reference:
                cleanup_id = manager._cleanup_receipt(conn, old_ref, "provider_revision_replaced")
            conn.execute("INSERT INTO management_provider_revisions VALUES(?,?,?,?,?)", (request_id, fingerprint, provider_id, model_entity_id, now))
    except Exception:
        if reference:
            with manager.connect() as conn:
                receipt = manager._cleanup_receipt(conn, reference, "provider_revision_not_adopted")
            manager.retry_credential_cleanup(receipt)
        adopted = _revision_row(manager, request_id)
        if adopted and adopted["request_hash"] == fingerprint:
            return _revision_result(manager, request_id)
        if adopted:
            raise ConfigurationError("This setup request changed; start a new request", 409)
        raise
    if cleanup_id:
        manager.retry_credential_cleanup(cleanup_id)
    return _revision_result(manager, request_id)


def _spec(row, skill_paths):
    kind, _, value = row["item_key"].partition(":")
    selected = json.loads(value)
    spec = {"path": row["path"], "owner": row["owner"], "kind": kind, "operation": "uninstall"}
    if isinstance(selected, list):
        spec["pointer"] = selected
    elif isinstance(selected, dict):
        spec.update(pointer=[*selected["container"], 0], native_identity=selected["identity"])
    elif kind == "block":
        spec["marker"] = selected
    if row["path"] in skill_paths:
        spec["managed_skill"] = True
    return spec


def _combined(entries):
    if not entries:
        return "not_managed"
    states = {entry["status"] for entry in entries}
    for status in ("externally_changed", "unreadable", "not_checked", "missing", "not_managed"):
        if status in states:
            return status
    return "verified"


def host_state(manager, host_id: str) -> dict:
    if host_id not in HOSTS:
        raise ConfigurationError("Unknown host", 422)
    from app.integrations.host_management_native import effective_host, configuration_paths, shadowed_configuration_paths
    host = effective_host(HOSTS[host_id], manager.home)
    paths = configuration_paths(host, manager.home)
    if host_id in ("codex", "codex-desktop"):
        paths.append(str(manager.home / ".codex/config.toml"))
    skill_prefix = str(manager.home / host["skill_dir"]) + "/" if host.get("skill_dir") else "\x00"
    prompt_prefix = str(manager.home / host["prompt_dir"]) + "/" if host.get("prompt_dir") else "\x00"
    with manager.connect() as conn:
        owned = conn.execute("SELECT * FROM management_owned WHERE path IN (" + ",".join("?" for _ in paths) + ") OR substr(path,1,?)=? OR substr(path,1,?)=? ORDER BY path,item_key LIMIT 501", (*paths, len(skill_prefix), skill_prefix, len(prompt_prefix), prompt_prefix)).fetchall() if paths or host.get("prompt_dir") else []
        skill_paths = {row[0] for row in conn.execute("SELECT path FROM management_skill_files WHERE substr(path,1,?)=? LIMIT 501", (len(skill_prefix), skill_prefix))}
        active_row = conn.execute(f"SELECT {HEADER_SQL} data,updated_at FROM management_activations WHERE host_id=?", (host_id,)).fetchone()
        active = selected_state(conn, host_id, json.loads(active_row['data'])) if active_row else {}
        recent = conn.execute("""WITH recent AS (
            SELECT id,status,updated_at,
                length(coalesce(json_extract(payload,'$.metadata'),'')) item_bytes
            FROM management_plans WHERE json_extract(payload,'$.metadata.host_id')=?
            AND status IN ('applied','applying','recovery_required') ORDER BY updated_at DESC,id DESC LIMIT 301
        ), budgeted AS (
            SELECT id,status,updated_at,
                sum(item_bytes) OVER (ORDER BY updated_at DESC,id DESC) metadata_bytes FROM recent
        ) SELECT b.id,b.status,b.updated_at,b.metadata_bytes,
            CASE WHEN b.metadata_bytes<=4194304 THEN json_extract(p.payload,'$.metadata') ELSE NULL END metadata
            FROM budgeted b
            LEFT JOIN management_plans p ON b.metadata_bytes<=4194304 AND p.id=b.id""", (host_id,)).fetchall()
    specs, invalid = [], []
    for row in owned[:500]:
        try:
            specs.append(_spec(row, skill_paths))
        except (ValueError, KeyError, TypeError):
            invalid.append({"owner": row["owner"], "path": row["path"], "status": "unreadable"})
    inspected = manager.transactions.inspect(specs) + invalid
    shadowed_paths = shadowed_configuration_paths(host, manager.home)
    for entry in inspected:
        if entry["path"] in shadowed_paths:
            entry.update(status="externally_changed", reason="native_filename_shadowed")
    grouped = {}
    for entry in inspected:
        owner = active.get("provider_id") if entry["owner"] == "activation:" + host_id else entry["owner"]
        if owner:
            grouped.setdefault(owner, []).append(entry)
    applied_versions, latest, recovering, dependencies = {}, {}, [], {}
    owned_ids = {active.get('provider_id'), *(entry['owner'] for entry in owned[:500])} - {None}
    metadata_limited = len(recent) > 300 or any(row['metadata_bytes'] > 4194304 for row in recent[:300])
    for row in recent[:300]:
        metadata = json.loads(row["metadata"] or "{}")
        if row["status"] in ("applying", "recovery_required"):
            recovering.append({"id": row["id"], "status": row["status"]})
        for entity_id, version in metadata.get("entity_versions", {}).items():
            if entity_id not in owned_ids and len(latest) >= 500:
                metadata_limited = True
                continue
            if row["status"] == "applied" and entity_id not in latest:
                latest[entity_id] = {"plan_id": row["id"], "operation": metadata.get("operation"), "updated_at": row["updated_at"]}
                applied_versions[entity_id] = version
                ids = metadata.get('provider_dependencies', {}).get(entity_id, [])
                dependencies[entity_id] = {key: metadata['entity_versions'].get(key) for key in ids}
    relevant_ids = list(owned_ids | set(latest))
    with manager.connect() as conn:
        definitions = conn.execute("SELECT id,kind,version,substr(json_extract(data,'$.name'),1,200) name FROM management_entities WHERE id IN (" + ','.join('?' for _ in relevant_ids) + ')', relevant_ids).fetchall() if relevant_ids else []
        dependency_ids = list({key for values in dependencies.values() for key in values})
        dependency_versions = {}
        for offset in range(0, len(dependency_ids), 500):
            batch = dependency_ids[offset:offset + 500]
            dependency_versions.update({row['id']: row['version'] for row in conn.execute('SELECT id,version FROM management_entities WHERE id IN (' + ','.join('?' for _ in batch) + ')', batch)})
    resources = {}
    for item in definitions:
        entry = grouped.get(item["id"], [])
        state = _combined(entry)
        receipt = latest.get(item["id"], {})
        if state == 'verified' and item['id'] not in applied_versions and metadata_limited:
            state = 'not_checked'
        if not entry and receipt.get("operation") == "uninstall":
            state = "removed"
        elif state == "verified" and item["version"] != applied_versions.get(item["id"], item["version"]):
            state = "update_available"
        elif state == 'verified' and any(dependency_versions.get(key) != version for key, version in dependencies.get(item['id'], {}).items()):
            state = 'update_available'
        resources[item["id"]] = {"kind": item['kind'], "status": state, "paths": len({value["path"] for value in entry}),
            "definition_version": item["version"], "applied_version": applied_versions.get(item["id"]), **receipt}
    names = {item["id"]: item["name"] for item in definitions}
    active_entries = [entry for entry in inspected if entry["owner"] in ("activation:" + host_id, active.get('provider_id'))]
    with manager.connect() as conn:
        gateway = conn.execute("SELECT json_extract(data,'$.gateway.id') id,json_extract(data,'$.mode') mode FROM management_routing_profiles WHERE host_id=?", (host_id,)).fetchone()
    gateway_selected = bool(gateway and gateway['id'] == active.get('provider_id') and gateway['mode'] != 'direct')
    activation_status = _combined(active_entries) if active_row else "not_managed"
    provider_status = resources.get(active.get('provider_id'), {}).get('status')
    if activation_status == 'verified' and provider_status in ('update_available', 'not_checked'):
        activation_status = provider_status
    if active.get('provider_id') and host_id == "claude-code" and not active_entries:
        activation_status = "missing"
    if active.get('selection_receipt_unknown'):
        activation_status = 'not_checked'
    return {"host_id": host_id, "provider_mode": "single_selection" if host_id in ("codex", "codex-desktop", "claude-code") else host.get("provider_mode", "coexist_named_entries"),
        "selection": {"provider_id": active.get("provider_id"), "provider_name": names.get(active.get("provider_id")), 'connection_mode': gateway['mode'] if gateway_selected else 'direct',
            "model_id": active.get("model_id"), "status": activation_status, 'receipt_unknown': bool(active.get('selection_receipt_unknown')), "updated_at": active_row["updated_at"] if active_row else None,
            "host_loaded": "not_verified", "activation_method": "restart_and_verify"},
        "effects": {"selection": "recorded" if active.get("provider_id") else "not_selected_by_senrelay", "configuration": activation_status,
            "host_loaded": "not_verified", "reachability": "not_checked", "authentication": "not_checked", "invocation": "not_checked"},
        "resources": resources, "recovery": recovering,
        "env_conflicts": check_env_conflicts(manager, host_id)["conflicts"],
        "coverage": "partial" if len(owned) > 500 or len(recent) > 300 or metadata_limited or any(entry["status"] == "not_checked" for entry in inspected) else "complete_owned_entries",
        "checked_at": datetime.now(timezone.utc).isoformat(), "credentials_read": False, "host_executed": False}


_HOST_ENV_VARS: dict[str, tuple[str, ...]] = {
    "claude-code": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
    ),
    "codex": (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_ORG_ID",
    ),
    "gemini-cli": (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ),
    "opencode": (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_BASE_URL",
        "ANTHROPIC_BASE_URL",
    ),
    "cursor": (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
    ),
    "omp": (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "DEEPSEEK_API_KEY",
    ),
}


def check_env_conflicts(manager, host_id: str | None = None) -> dict:
    """
    Compatible local configuration Environment Variable Conflict Detection.
    Detects process environment variables or shell startup script exports that may override
    host configuration files, without ever reading or echoing secret values.
    """
    import os

    if host_id is not None and host_id not in HOSTS:
        raise ConfigurationError("Unknown host", 422)

    target_hosts = [host_id] if host_id else list(_HOST_ENV_VARS.keys())
    var_to_hosts: dict[str, list[str]] = {}
    for hid in target_hosts:
        for var in _HOST_ENV_VARS.get(hid, ()):
            var_to_hosts.setdefault(var, []).append(hid)

    env_map = getattr(manager, "env_override", None)
    if env_map is None:
        env_map = {} if getattr(manager, "isolated_home", False) else dict(os.environ)

    conflicts: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for var_name, affected in var_to_hosts.items():
        val = env_map.get(var_name)
        if isinstance(val, str) and val.strip():
            key = (var_name, "process_env")
            if key not in seen:
                seen.add(key)
                conflicts.append({
                    "variable": var_name,
                    "env_var": var_name,
                    "source": "process_env",
                    "affected_hosts": affected,
                    "severity": "warning",
                    "message": f"进程环境变量 {var_name} 已设置，可能会覆盖 {', '.join(affected)} 配置文件中的模型服务商设置。",
                })

    shell_files = (".zshrc", ".bashrc", ".bash_profile", ".zshenv", ".profile")
    for rc_name in shell_files:
        rc_path = manager.home / rc_name
        try:
            if not rc_path.is_file() or rc_path.is_symlink() or rc_path.stat().st_size > 256 * 1024:
                continue
            text = rc_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            for var_name, affected in var_to_hosts.items():
                if re.match(rf"^(?:export\s+)?{re.escape(var_name)}\s*=", line):
                    src = f"~/{rc_name}"
                    key = (var_name, src)
                    if key not in seen:
                        seen.add(key)
                        conflicts.append({
                            "variable": var_name,
                            "env_var": var_name,
                            "source": src,
                            "affected_hosts": affected,
                            "severity": "warning",
                            "message": f"Shell 配置文件 {src} 中导出了 {var_name}，可能会覆盖 {', '.join(affected)} 的模型服务商配置。",
                        })

    return {
        "host_id": host_id,
        "has_conflicts": len(conflicts) > 0,
        "count": len(conflicts),
        "conflicts": conflicts,
        "credentials_read": False,
    }

