"""Device-local assistant shortcuts and provider ordering. No host files are read."""
import json

from app.integrations.config_transactions import ConfigurationError
from app.services.host_registry import HOST_DEFINITIONS
from app.services.selection_receipts import EVENT_FILTER, HEADER_SQL, selected_state

DEFAULT_HOSTS = ["claude-code", "codex", "opencode"]


def _agents(ids):
    return list(dict.fromkeys(item for item in ids if item in HOST_DEFINITIONS))


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS management_preferences (
        name TEXT PRIMARY KEY, value TEXT NOT NULL)""")


def _items(conn, key, default=()):
    row = conn.execute("SELECT value FROM management_preferences WHERE name=?", (key,)).fetchone()
    return json.loads(row[0]) if row else list(default)


def _write(conn, key, values):
    conn.execute("INSERT OR REPLACE INTO management_preferences(name,value) VALUES(?,?)",
                 (key, json.dumps(values)))


def _state(conn):
    from app.services.management_service import HOSTS
    names = {host: HOSTS[host]["name"] for host in HOST_DEFINITIONS}
    return {"host_names": names,
            "favorite_hosts": _agents(_items(conn, "hosts", DEFAULT_HOSTS)),
            "recent_hosts": _agents(_items(conn, "recent-hosts"))}


def read(manager):
    with manager.connect() as conn:
        return _state(conn)


def update(manager, host, action, provider=None, neighbor=None):
    from app.services.management_service import HOSTS
    if not isinstance(host, str) or host not in HOSTS:
        raise ConfigurationError("Unknown assistant", 422)
    with manager.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if action == "visit":
            recent = _agents([host, *_items(conn, "recent-hosts")])
            _write(conn, "recent-hosts", recent[:8])
        elif action in ("pin", "unpin"):
            key = "hosts" if provider is None else "providers:" + host
            values = _items(conn, key, DEFAULT_HOSTS if provider is None else ())
            target = host if provider is None else provider
            if provider is None:
                values = _agents(values)
                if target not in HOST_DEFINITIONS:
                    return _state(conn)
            if provider is not None and (not isinstance(provider, str) or not conn.execute(
                "SELECT 1 FROM management_entities WHERE kind='provider' AND id=?", (provider,)
            ).fetchone()):
                raise ConfigurationError("Unknown provider", 422)
            if provider is not None and values:
                kept = {row[0] for row in conn.execute(
                    "SELECT id FROM management_entities WHERE kind='provider' AND id IN (SELECT value FROM json_each(?))",
                    (json.dumps(values),))}
                values = [value for value in values if value in kept]
            if action == "pin" and target not in values:
                if len(values) >= 100:
                    raise ConfigurationError("A shortcut list supports up to 100 entries", 422)
                values.append(target)
            if action == "unpin":
                values = [value for value in values if value != target]
            _write(conn, key, values)
        elif action in ("before", "after") and isinstance(provider, str) and isinstance(neighbor, str) and provider != neighbor:
            key = "providers:" + host
            values = _items(conn, key)
            if provider not in values or neighbor not in values:
                raise ConfigurationError("Only pinned providers can be reordered", 409)
            values.remove(provider)
            values.insert(values.index(neighbor) + int(action == "after"), provider)
            _write(conn, key, values)
        else:
            raise ConfigurationError("Invalid shortcut action", 422)
        return _state(conn)


def provider_order(conn, host):
    """Bound the shortcut list independently of the current catalog page."""
    pinned = _items(conn, "providers:" + host)
    row = conn.execute("SELECT " + HEADER_SQL + " FROM management_activations WHERE host_id=?", (host,)).fetchone()
    selected = selected_state(conn, host, json.loads(row[0]) if row else None) or {}
    recent = conn.execute("""SELECT json_extract(payload,'$.metadata.provider_id') AS provider,
        MAX(updated_at) AS last_used FROM management_plans WHERE """ + EVENT_FILTER + """
        AND json_extract(payload,'$.metadata.host_id')=? AND json_extract(payload,'$.metadata.activate_provider')=1
        GROUP BY provider ORDER BY last_used DESC, provider LIMIT 25""", (host,)).fetchall()
    sequence = dict.fromkeys([selected.get("provider_id"), *pinned, *(r[0] for r in recent)])
    sequence.pop(None, None)
    return {key: i for i, key in enumerate(sequence)}, pinned, {r[0]: r[1] for r in recent}
