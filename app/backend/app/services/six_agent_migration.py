"""One-time canonicalization of legacy assistant ids.

The six supported host ids are the only ids that may remain in API-facing data.
This module is intentionally idempotent and uses only the connection handed to
it by `create_schema`, so callers never touch a database by accident.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

HOST_ALIASES = {
    "codex-desktop": "codex",
    "antigravity-cli": "antigravity",
    "claude_code": "claude-code",
}
MIGRATION_KEY = "six_agent_variant_ids_migrated_v1"


def canonical_host(host: Any) -> Any:
    if isinstance(host, str):
        return HOST_ALIASES.get(host, host)
    return host


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _rewrite_json_host_ids(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key in {"host_id", "source_host", "host"} and isinstance(item, str):
                result[key] = canonical_host(item)
            elif key in {"agent_ids", "host_ids"} and isinstance(item, list):
                result[key] = list(dict.fromkeys(canonical_host(x) for x in item))
            elif key == "activation" and isinstance(item, str) and item.startswith("activation:"):
                result[key] = "activation:" + canonical_host(item.split(":", 1)[1])
            else:
                result[key] = _rewrite_json_host_ids(item)
        return result
    if isinstance(value, list):
        return [_rewrite_json_host_ids(item) for item in value]
    return value


def _rewrite_json_text(raw: Any) -> Any:
    if not isinstance(raw, str) or not raw.strip():
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return raw
    rewritten = _rewrite_json_host_ids(parsed)
    if rewritten == parsed:
        return raw
    return json.dumps(rewritten, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _update_simple_host_columns(conn: sqlite3.Connection, stats: dict[str, int]) -> None:
    for table in ("usage_meters", "usage_v2_attempts", "host_capability_observations"):
        if not _table_exists(conn, table) or "host_id" not in _columns(conn, table):
            continue
        for old, new in HOST_ALIASES.items():
            count = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE host_id=?", (old,)
            ).fetchone()[0]
            if not count:
                continue
            if table == "host_capability_observations":
                # Composite key (host_id, surface, capability, evidence_kind): merge counts
                # into the canonical row before removing the legacy rows.
                rows = conn.execute(
                    f"SELECT surface,capability,evidence_kind,metadata,first_seen_at,last_seen_at,count "
                    f"FROM {table} WHERE host_id=?", (old,)
                ).fetchall()
                for row in rows:
                    canonical = conn.execute(
                        f"SELECT first_seen_at,last_seen_at,count FROM {table} "
                        f"WHERE host_id=? AND surface=? AND capability=? AND evidence_kind=?",
                        (new, row["surface"], row["capability"], row["evidence_kind"]),
                    ).fetchone()
                    if canonical is None:
                        conn.execute(
                            f"UPDATE {table} SET host_id=? WHERE host_id=? AND surface=? AND capability=? AND evidence_kind=?",
                            (new, old, row["surface"], row["capability"], row["evidence_kind"]),
                        )
                    else:
                        conn.execute(
                            f"UPDATE {table} SET count=?, first_seen_at=?, last_seen_at=? "
                            f"WHERE host_id=? AND surface=? AND capability=? AND evidence_kind=?",
                            (
                                int(canonical["count"] or 0) + int(row["count"] or 0),
                                min(canonical["first_seen_at"] or row["first_seen_at"] or "", row["first_seen_at"] or ""),
                                max(canonical["last_seen_at"] or "", row["last_seen_at"] or ""),
                                new, row["surface"], row["capability"], row["evidence_kind"],
                            ),
                        )
                        conn.execute(
                            f"DELETE FROM {table} WHERE host_id=? AND surface=? AND capability=? AND evidence_kind=?",
                            (old, row["surface"], row["capability"], row["evidence_kind"]),
                        )
            else:
                conn.execute(f"UPDATE {table} SET host_id=? WHERE host_id=?", (new, old))
            stats[f"{table}:{old}"] = count


def _update_json_tables(conn: sqlite3.Connection, stats: dict[str, int]) -> None:
    for table, column in (("management_entities", "data"), ("management_plans", "payload")):
        if not _table_exists(conn, table) or column not in _columns(conn, table):
            continue
        rows = conn.execute(f"SELECT id, {column} FROM {table}").fetchall()
        for row in rows:
            original = row[column]
            rewritten = _rewrite_json_text(original)
            if rewritten == original:
                continue
            version = "version=version+1," if table == "management_entities" and "version" in _columns(conn, table) else ""
            conn.execute(
                f"UPDATE {table} SET {version}{column}=? WHERE id=?",
                (rewritten, row["id"]),
            )
            stats[f"{table}:{column}"] = stats.get(f"{table}:{column}", 0) + 1


def _primary_key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [row[1] for row in sorted((r for r in rows if r[5]), key=lambda r: r[5])]


def _update_primary_key_tables(conn: sqlite3.Connection, stats: dict[str, int]) -> None:
    for table, timestamp in (
        ("connections", "updated_at"),
        ("management_activations", "updated_at"),
        ("management_routing_profiles", "updated_at"),
        ("management_shared_targets", None),
    ):
        if not _table_exists(conn, table):
            continue
        cols = _columns(conn, table)
        key_cols = _primary_key_columns(conn, table)
        if table == "connections":
            id_col = "id"
            if id_col not in cols or "updated_at" not in cols:
                continue
            for old, new in HOST_ALIASES.items():
                legacy = conn.execute(
                    f"SELECT * FROM {table} WHERE {id_col}=?", (old,)
                ).fetchone()
                if legacy is None:
                    continue
                stats[f"{table}:{old}"] = 1
                canonical = conn.execute(
                    f"SELECT * FROM {table} WHERE {id_col}=?", (new,)
                ).fetchone()
                if canonical is None:
                    conn.execute(
                        f"UPDATE {table} SET {id_col}=? WHERE {id_col}=?", (new, old)
                    )
                    continue
                # Keep the canonical seeded row and fold in any real legacy settings.
                updates, params = [], []
                for field in ("enabled", "last_sync"):
                    if field in cols and (not canonical[field] or canonical[field] < legacy[field]):
                        updates.append(f"{field}=?"); params.append(legacy[field])
                if "paused" in cols and legacy["paused"] and not canonical["paused"]:
                    updates.append("paused=?"); params.append(legacy["paused"])
                for field in ("paths", "capabilities", "error", "config_preview"):
                    if field in cols and (not canonical[field] or canonical[field] in ("[]", "{}", "")) and legacy[field]:
                        updates.append(f"{field}=?"); params.append(legacy[field])
                if updates:
                    params.append(new)
                    conn.execute(f"UPDATE {table} SET {','.join(updates)} WHERE {id_col}=?", tuple(params))
                conn.execute(f"DELETE FROM {table} WHERE {id_col}=?", (old,))
            continue

        host_col = "host_id"
        if host_col not in cols or host_col not in key_cols:
            continue
        for old, new in HOST_ALIASES.items():
            legacy_rows = conn.execute(
                f"SELECT * FROM {table} WHERE {host_col}=?", (old,)
            ).fetchall()
            if not legacy_rows:
                continue
            stats[f"{table}:{old}"] = len(legacy_rows)
            for legacy in legacy_rows:
                other_keys = [c for c in key_cols if c != host_col]
                where_other = " AND ".join(f"{c}=?" for c in other_keys)
                where_suffix = (" AND " + where_other) if where_other else ""
                params_other = tuple(legacy[c] for c in other_keys)
                canonical = conn.execute(
                    f"SELECT * FROM {table} WHERE {host_col}=?{where_suffix}",
                    (new, *params_other),
                ).fetchone()
                if canonical is None:
                    conn.execute(
                        f"UPDATE {table} SET {host_col}=? WHERE {host_col}=?{where_suffix}",
                        (new, old, *params_other),
                    )
                    continue
                winner = canonical
                if timestamp in cols and (legacy[timestamp] or "") > (canonical[timestamp] or ""):
                    winner = legacy
                conn.execute(
                    f"DELETE FROM {table} WHERE {host_col}=?{where_suffix}",
                    (new, *params_other),
                )
                conn.execute(
                    f"DELETE FROM {table} WHERE {host_col}=?{where_suffix}",
                    (old, *params_other),
                )
                values = dict(winner)
                values[host_col] = new
                insert_cols = list(values)
                conn.execute(
                    f"INSERT INTO {table} ({','.join(insert_cols)}) VALUES ({','.join('?' for _ in insert_cols)})",
                    tuple(values[c] for c in insert_cols),
                )


def _update_owned_owners(conn: sqlite3.Connection, stats: dict[str, int]) -> None:
    if not _table_exists(conn, "management_owned"):
        return
    rows = conn.execute(
        "SELECT path,item_key,owner FROM management_owned WHERE owner LIKE 'activation:%'"
    ).fetchall()
    for row in rows:
        owner = row["owner"]
        host = owner.split(":", 1)[1] if ":" in owner else ""
        canonical = canonical_host(host)
        if canonical == host:
            continue
        target = "activation:" + canonical
        # Primary key is (path,item_key); if the canonical row exists, prefer it.
        exists = conn.execute(
            "SELECT 1 FROM management_owned WHERE path=? AND item_key=? AND owner=?",
            (row["path"], row["item_key"], target),
        ).fetchone()
        if exists:
            conn.execute(
                "DELETE FROM management_owned WHERE path=? AND item_key=? AND owner=?",
                (row["path"], row["item_key"], owner),
            )
        else:
            conn.execute(
                "UPDATE management_owned SET owner=? WHERE path=? AND item_key=? AND owner=?",
                (target, row["path"], row["item_key"], owner),
            )
        stats[f"management_owned.owner:{host}"] = stats.get(f"management_owned.owner:{host}", 0) + 1


def _update_preferences(conn: sqlite3.Connection, stats: dict[str, int]) -> None:
    if not _table_exists(conn, "management_preferences"):
        return
    rows = conn.execute(
        "SELECT name,value FROM management_preferences WHERE name IN ('hosts','recent-hosts') OR name LIKE 'providers:%'"
    ).fetchall()
    for row in rows:
        try:
            values = json.loads(row["value"])
        except (TypeError, ValueError):
            continue
        if not isinstance(values, list):
            continue
        rewritten = list(dict.fromkeys(canonical_host(v) for v in values))
        if rewritten == values:
            continue
        conn.execute(
            "UPDATE management_preferences SET value=? WHERE name=?",
            (json.dumps(rewritten, ensure_ascii=False), row["name"]),
        )
        stats["management_preferences"] = stats.get("management_preferences", 0) + 1


def migrate_six_agent_variant_ids(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    """Rewrite legacy host ids to the six canonical ids. Safe on empty databases."""
    if not _table_exists(conn, "app_settings"):
        return {}
    from app.database import get_app_setting, set_app_setting

    stats: dict[str, int] = {}
    if not force and get_app_setting(MIGRATION_KEY, default=False, conn=conn):
        return stats

    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        _update_primary_key_tables(conn, stats)
        _update_simple_host_columns(conn, stats)
        _update_json_tables(conn, stats)
        _update_owned_owners(conn, stats)
        _update_preferences(conn, stats)
        set_app_setting(MIGRATION_KEY, True, conn=conn)
    finally:
        conn.row_factory = previous_factory
    return stats


# Hosts the product no longer supports. Their ids may survive in data saved by
# earlier builds; they are pruned so validation of the six supported ids never
# rejects an unrelated edit of an old record.
REMOVED_HOSTS = frozenset({
    "pi", "grok-build", "grok", "kimi-code", "minimax-code", "hermes", "openclaw",
    "claude-desktop", "gemini-cli", "agy",
})
PRUNE_KEY = "six_agent_removed_hosts_pruned_v1"


def _prune_lists(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ([x for x in item if x not in REMOVED_HOSTS] if key in {"agent_ids", "host_ids"} and isinstance(item, list)
                  else _prune_lists(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_prune_lists(item) for item in value]
    return value


def _owns_files(conn: sqlite3.Connection, owner: str) -> bool:
    return _table_exists(conn, "management_owned") and conn.execute(
        "SELECT 1 FROM management_owned WHERE owner=? LIMIT 1", (owner,)
    ).fetchone() is not None


def prune_removed_host_ids(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    """Drop references to removed hosts. Rows that still own native files are kept."""
    if not _table_exists(conn, "app_settings"):
        return {}
    from app.database import get_app_setting, set_app_setting

    stats: dict[str, int] = {}
    if not force and get_app_setting(PRUNE_KEY, default=False, conn=conn):
        return stats
    previous_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    marks = ",".join("?" for _ in REMOVED_HOSTS)
    removed = tuple(sorted(REMOVED_HOSTS))
    try:
        if _table_exists(conn, "management_entities"):
            for row in conn.execute("SELECT id, kind, data FROM management_entities").fetchall():
                try:
                    data = json.loads(row["data"])
                except (TypeError, ValueError):
                    continue
                if row["kind"] == "binding" and data.get("host_id") in REMOVED_HOSTS:
                    if not _owns_files(conn, row["id"]):
                        conn.execute("DELETE FROM management_entities WHERE id=? AND kind='binding'", (row["id"],))
                        stats["management_entities:binding_deleted"] = stats.get("management_entities:binding_deleted", 0) + 1
                    continue
                pruned = _prune_lists(data)
                if pruned != data:
                    conn.execute(
                        "UPDATE management_entities SET version=version+1, data=? WHERE id=?",
                        (json.dumps(pruned, ensure_ascii=False, sort_keys=True, separators=(",", ":")), row["id"]),
                    )
                    stats["management_entities:pruned"] = stats.get("management_entities:pruned", 0) + 1
        for table in ("management_activations", "management_routing_profiles", "management_shared_targets"):
            if not _table_exists(conn, table) or "host_id" not in _columns(conn, table):
                continue
            for (host,) in conn.execute(f"SELECT DISTINCT host_id FROM {table} WHERE host_id IN ({marks})", removed).fetchall():
                if _owns_files(conn, "activation:" + host):
                    continue  # keep the restore snapshot for files Senrelay wrote
                count = conn.execute(f"DELETE FROM {table} WHERE host_id=?", (host,)).rowcount
                stats[f"{table}:{host}"] = count
        if _table_exists(conn, "management_preferences"):
            for row in conn.execute(
                "SELECT name,value FROM management_preferences WHERE name IN ('hosts','recent-hosts') OR name LIKE 'providers:%'"
            ).fetchall():
                try:
                    values = json.loads(row["value"])
                except (TypeError, ValueError):
                    continue
                if isinstance(values, list):
                    kept = [v for v in values if v not in REMOVED_HOSTS]
                    if kept != values:
                        conn.execute("UPDATE management_preferences SET value=? WHERE name=?",
                                     (json.dumps(kept, ensure_ascii=False), row["name"]))
                        stats["management_preferences"] = stats.get("management_preferences", 0) + 1
        set_app_setting(PRUNE_KEY, True, conn=conn)
    finally:
        conn.row_factory = previous_factory
    return stats
