"""Quota receipts are independent of usage estimates and native configuration state."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretUnavailable
from app.services import quota_readers as readers
from app.services.provider_credentials import account_status


def _now():
    return datetime.now(timezone.utc).isoformat()


def _source(source_id, name, adapter, hosts, *, reference=None, owner=None, provider_name=None, account=None, status=None):
    native = source_id == "native-codex"
    refreshable = native or (adapter in readers.ENDPOINTS and reference is not None and status is None)
    revision = hashlib.sha256(json.dumps([source_id, adapter, owner, reference, status]).encode()).hexdigest()
    return {
        "id": source_id, "name": name, "source": adapter, "host_ids": sorted(hosts),
        "account_id": account, "provider_id": owner, "provider_name": provider_name, "status": status or ("not_checked" if refreshable else "unsupported"),
        "revision": revision, "refreshable": refreshable, "dashboard": readers.DASHBOARDS.get(adapter),
        "_reference": reference,
    }


def _catalog(conn):
    # Project only metadata. Listing quotas never fetches OAuth token bundles,
    # native account files, vault contents, model maps or raw provider settings.
    rows = [dict(row) for row in conn.execute("""
        SELECT id, kind AS entity_kind, json_extract(data,'$.kind') AS kind,
               substr(json_extract(data,'$.name'),1,200) AS name,
               json_extract(data,'$.base_url') AS base_url,
               json_extract(data,'$.provider_id') AS provider_id,
               json_extract(data,'$.account_id') AS account_id,
               json_extract(data,'$.credential_source') AS credential_source,
               json_extract(data,'$.credential_ref') AS credential_ref,
               json_extract(data,'$.status') AS status, json_extract(data,'$.expires_at') AS expires_at,
               json_extract(data,'$.enabled') AS enabled,
               CASE WHEN json_type(data,'$.agent_ids')='array' THEN json_extract(data,'$.agent_ids')
                    WHEN json_type(data,'$.host_ids')='array' THEN json_extract(data,'$.host_ids')
                    ELSE '[]' END AS hosts
        FROM management_entities WHERE kind IN ('account','provider') ORDER BY created_at,id
    """)]
    assigned = {}
    for row in conn.execute("""SELECT json_extract(data,'$.provider_id'), host_id FROM management_activations
            UNION SELECT json_extract(data,'$.provider_id'), json_extract(data,'$.host_id')
              FROM management_entities WHERE kind='binding'"""):
        if row[0] and row[1]:
            assigned.setdefault(row[0], set()).add(row[1])
    accounts = {r["id"]: r for r in rows if r["entity_kind"] == "account"}
    providers = {r["id"]: r for r in rows if r["entity_kind"] == "provider"}
    account_providers = {}
    for provider in providers.values():
        if provider["account_id"] and (provider["credential_source"] == "account" or
                (not provider["credential_source"] and not provider["credential_ref"])):
            account_providers.setdefault(provider["account_id"], []).append(provider)
    items = [
        _source("native-codex", "Codex", "codex", ["codex"]),
        _source("native-claude", "Claude Code", "claude", ["claude-code"], status="native_only"),
    ]

    def add(owner, provider, id_):
        is_account = owner["entity_kind"] == "account"
        adapter = readers.BASES.get((provider.get("base_url") or "").rstrip("/"), "custom")
        ref = owner.get("credential_ref")
        hosts = {host for host in json.loads(owner["hosts"]) + json.loads(provider.get("hosts", "[]"))
                 if isinstance(host, str)}
        hosts.update(assigned.get(provider.get("id"), ()))
        status = None
        if provider.get("enabled") == 0:
            status = "disabled"
        elif is_account and owner["kind"] == "oauth" and account_status(owner) == "expired":
            status = "expired"
        elif adapter not in readers.ENDPOINTS or (is_account and owner["kind"] != "api_key"):
            status = "unsupported"
        elif not ref:
            status = "unauthorized"
        items.append(_source(id_, owner["name"], adapter, hosts, reference=ref, owner=provider.get("id"),
                             provider_name=provider.get("name"), account=owner["id"] if is_account else None, status=status))

    for acct in accounts.values():
        if acct["provider_id"] in providers:
            add(acct, providers[acct["provider_id"]], acct["id"])
            continue
        # An unbound account can be explicitly selected by several providers.
        # Each selection has its own origin and confirmation, even with one key.
        matches = account_providers.get(acct["id"])
        if matches:
            for p in matches:
                add(acct, p, f'{acct["id"]}.{p["id"]}')
        else:
            add(acct, {}, acct["id"])
    for provider in providers.values():
        if provider["credential_source"] == "account" or (not provider["credential_ref"] and provider["account_id"]):
            continue
        if provider["credential_ref"] or readers.BASES.get((provider["base_url"] or "").rstrip("/")) in readers.ENDPOINTS:
            add(provider, provider, provider["id"])
    return items


def _attach_receipt(item, receipt):
    public = {k: v for k, v in item.items() if not k.startswith("_")}
    public.update(last_success_at=None, last_attempt_at=None, snapshot=None)
    if receipt and receipt["revision"] == item["revision"]:
        public.update(status=receipt["status"], last_success_at=receipt["success_at"],
                      last_attempt_at=receipt["attempt_at"], snapshot=json.loads(receipt["snapshot"]) if receipt["snapshot"] else None)
    return public


def list_quotas(manager, host_id=None, account_id=None):
    with manager.connect() as conn:
        conn.execute("BEGIN")
        items = _catalog(conn)
        has_cache = conn.execute("SELECT 1 FROM sqlite_master WHERE name='account_quota_receipts'").fetchone()
        cached = {r["id"]: r for r in conn.execute("SELECT * FROM account_quota_receipts")} if has_cache else {}
    return {"items": [_attach_receipt(i, cached.get(i["id"])) for i in items
                      if (not host_id or host_id in i["host_ids"]) and (not account_id or account_id == i["account_id"])]}


def _selected(conn, source_id, revision):
    item = next((item for item in _catalog(conn) if item["id"] == source_id), None)
    if not item:
        raise ConfigurationError("Quota source does not exist", 404)
    if revision != item["revision"]:
        raise ConfigurationError("Quota source has changed; review the selected account again", 409)
    return item


def refresh_quota(manager, source_id, revision, confirmed, *, transport=None):
    if confirmed is not True:
        raise ConfigurationError("Explicit quota read confirmation required", 422)
    with manager.connect() as conn:
        conn.execute("BEGIN")
        item = _selected(conn, source_id, revision)
    if not item["refreshable"]:
        raise ConfigurationError("This source cannot be refreshed with the selected account", 422)
    started = _now()
    try:
        if item["source"] == "codex":
            snapshot = asyncio.run(readers.read_codex(manager.home, isolated=manager.isolated_home))
        else:
            key = manager.vault.get(item["_reference"])
            snapshot = asyncio.run(readers.read_balance(item["source"], key, transport))
        status = "available"
    except readers.QuotaUnavailable as error:
        snapshot, status = None, error.status
    except SecretUnavailable:
        snapshot, status = None, "keychain_locked"
    with manager.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        _selected(conn, source_id, revision)
        conn.execute("""CREATE TABLE IF NOT EXISTS account_quota_receipts
            (id TEXT PRIMARY KEY, revision TEXT NOT NULL, started_at TEXT NOT NULL,
             status TEXT NOT NULL, attempt_at TEXT NOT NULL, success_at TEXT, snapshot TEXT)""")
        conn.execute("""CREATE TRIGGER IF NOT EXISTS quota_remove_owner AFTER DELETE ON management_entities BEGIN
            DELETE FROM account_quota_receipts WHERE id=OLD.id OR
              substr(id,1,length(OLD.id)+1)=OLD.id||'.' OR substr(id,-length(OLD.id)-1)='.'||OLD.id;
            END""")
        previous = conn.execute("SELECT * FROM account_quota_receipts WHERE id=?", (source_id,)).fetchone()
        if not previous or previous["revision"] != revision or previous["started_at"] <= started:
            finished = _now()
            same = previous and previous["revision"] == revision
            # If Codex signed out or switched identity, remove the old subscription
            # from its current native slot. Never attribute one login to the next.
            retain = same and not (item["source"] == "codex" and status != "available")
            payload = json.dumps(snapshot) if snapshot is not None else previous["snapshot"] if retain else None
            success = finished if snapshot is not None else previous["success_at"] if retain else None
            conn.execute("INSERT OR REPLACE INTO account_quota_receipts VALUES(?,?,?,?,?,?,?)",
                         (source_id, revision, started, status, finished, success, payload))
        row = conn.execute("SELECT * FROM account_quota_receipts WHERE id=?", (source_id,)).fetchone()
    return _attach_receipt(item, row)
