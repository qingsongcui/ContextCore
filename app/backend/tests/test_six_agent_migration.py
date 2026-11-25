import json

from app.database import create_schema, db, get_app_setting, now_iso
from app.services.six_agent_migration import (
    MIGRATION_KEY,
    migrate_six_agent_variant_ids,
)


def test_six_agent_variant_migration_is_idempotent_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "six-agent.sqlite"))
    create_schema()

    with db() as conn:
        stamp = now_iso()
        conn.execute(
            "INSERT OR REPLACE INTO connections (id,label,discovered,enabled,paused,paths,capabilities,created_at,updated_at) "
            "VALUES ('codex-desktop','Legacy Desktop',0,1,0,'[]','{}',?,?)",
            (stamp, stamp),
        )
        conn.execute(
            "INSERT INTO management_activations (host_id,data,updated_at) VALUES ('codex-desktop','{}',?)",
            (stamp,),
        )
        conn.execute(
            "INSERT INTO management_entities (id,kind,data,version,created_at,updated_at) VALUES ('asset_x','asset',?,1,?,?)",
            (
                json.dumps(
                    {
                        "agent_ids": ["codex", "codex-desktop", "claude_code"],
                        "host_ids": ["antigravity-cli", "codex-desktop"],
                        "source": {"host_id": "codex-desktop"},
                    }
                ),
                stamp,
                stamp,
            ),
        )
        conn.execute(
            "INSERT INTO management_plans (id,version,status,payload,created_at,updated_at) VALUES ('plan_x',1,'planned',?,?,?)",
            (json.dumps({"metadata": {"host_id": "codex-desktop"}}), stamp, stamp),
        )
        conn.execute(
            "INSERT INTO management_owned (path,item_key,value_hash,owner) VALUES (?,?,?,?)",
            ("/tmp/config.toml", "toml_selection:model", "hash", "activation:codex-desktop"),
        )
        conn.execute(
            "INSERT INTO management_preferences (name,value) VALUES ('hosts',?)",
            (json.dumps(["codex-desktop", "codex", "antigravity-cli"]),),
        )
        conn.execute("INSERT INTO usage_v2_requests (id,created_at) VALUES ('logical_x',?)", (stamp,))
        conn.execute(
            "INSERT INTO usage_v2_attempts (id,logical_request_id,host_id,started_at,status,data) "
            "VALUES ('attempt_x','logical_x','claude_code',?,'ok','{}')",
            (stamp,),
        )

        stats = migrate_six_agent_variant_ids(conn, force=True)
        assert stats["connections:codex-desktop"] == 1
        assert get_app_setting(MIGRATION_KEY, default=False, conn=conn) is True
        assert [row[0] for row in conn.execute("SELECT id FROM connections WHERE id LIKE 'codex%'")] == ["codex"]
        assert [row[0] for row in conn.execute("SELECT host_id FROM management_activations")] == ["codex"]
        entity = json.loads(conn.execute("SELECT data FROM management_entities WHERE id='asset_x'").fetchone()[0])
        assert entity["agent_ids"] == ["codex", "claude-code"]
        assert entity["host_ids"] == ["antigravity", "codex"]
        assert entity["source"]["host_id"] == "codex"

        plan = json.loads(conn.execute("SELECT payload FROM management_plans WHERE id='plan_x'").fetchone()[0])
        assert plan["metadata"]["host_id"] == "codex"

        owner = conn.execute("SELECT owner FROM management_owned").fetchone()[0]
        assert owner == "activation:codex"

        preferences = json.loads(conn.execute("SELECT value FROM management_preferences WHERE name='hosts'").fetchone()[0])
        assert preferences == ["codex", "antigravity"]

        assert conn.execute("SELECT host_id FROM usage_v2_attempts WHERE id='attempt_x'").fetchone()[0] == "claude-code"

        second = migrate_six_agent_variant_ids(conn, force=True)
        assert second == {}


def test_removed_host_ids_are_pruned_so_old_records_stay_editable(tmp_path, monkeypatch):
    from app.services.six_agent_migration import PRUNE_KEY, prune_removed_host_ids

    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "prune.sqlite"))
    create_schema()
    stamp = now_iso()
    with db() as conn:
        rows = [
            ("asset_old", "asset", {"name": "Old MCP", "kind": "mcp", "agent_ids": ["cursor", "claude-desktop", "minimax-code"]}),
            ("binding_gone", "binding", {"host_id": "claude-desktop", "provider_id": "p", "model_id": "m"}),
            ("binding_owner", "binding", {"host_id": "gemini-cli", "provider_id": "p", "model_id": "m"}),
            ("binding_live", "binding", {"host_id": "claude-code", "provider_id": "p", "model_id": "m"}),
        ]
        for entity_id, kind, data in rows:
            conn.execute(
                "INSERT INTO management_entities (id,kind,data,version,created_at,updated_at) VALUES (?,?,?,1,?,?)",
                (entity_id, kind, json.dumps(data), stamp, stamp),
            )
        # A binding that still owns a native file entry must be kept for uninstall.
        conn.execute("INSERT INTO management_owned (path,item_key,value_hash,owner) VALUES ('/x','k','h','binding_owner')")
        conn.execute("INSERT INTO management_activations (host_id,data,updated_at) VALUES ('pi','{}',?)", (stamp,))
        conn.execute("DELETE FROM app_settings WHERE key=?", (PRUNE_KEY,))
        stats = prune_removed_host_ids(conn)

        asset = json.loads(conn.execute("SELECT data FROM management_entities WHERE id='asset_old'").fetchone()[0])
        assert asset["agent_ids"] == ["cursor"]
        ids = {row[0] for row in conn.execute("SELECT id FROM management_entities WHERE kind='binding'")}
        assert ids == {"binding_owner", "binding_live"}
        assert conn.execute("SELECT COUNT(*) FROM management_activations WHERE host_id='pi'").fetchone()[0] == 0
        assert get_app_setting(PRUNE_KEY, default=False, conn=conn)
        assert prune_removed_host_ids(conn) == {}
    assert stats["management_entities:binding_deleted"] == 1


def test_pruned_asset_can_be_renamed(tmp_path):
    import sqlite3
    from tests.test_management import FakeVault
    from app.services.management_service import ManagementService
    from app.services.six_agent_migration import prune_removed_host_ids

    home = tmp_path / "home"
    home.mkdir()
    manager = ManagementService(tmp_path / "m.db", home, FakeVault())
    asset = manager.save("asset", {"name": "Legacy", "kind": "mcp", "config": {"transport": "stdio", "command": "x"}, "agent_ids": ["codex"]})
    with sqlite3.connect(tmp_path / "m.db") as conn:
        data = json.loads(conn.execute("SELECT data FROM management_entities WHERE id=?", (asset["id"],)).fetchone()[0])
        data["agent_ids"].append("minimax-code")
        conn.execute("UPDATE management_entities SET data=? WHERE id=?", (json.dumps(data), asset["id"]))
        conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        prune_removed_host_ids(conn, force=True)
    current = manager.get("asset", asset["id"])
    renamed = manager.save("asset", {"name": "Renamed"}, asset["id"], current["version"])
    assert renamed["name"] == "Renamed" and renamed["agent_ids"] == ["codex"]
