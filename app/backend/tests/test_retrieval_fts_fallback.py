import sqlite3

from app.database import create_schema, db, seed_data
from app.services import content_service, project_service, retrieval_service


class WithoutFTS:
    """Model a SQLite runtime where creating the optional FTS module fails."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, parameters=()):
        if sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
            raise sqlite3.OperationalError("no such module: fts5")
        return self.connection.execute(sql, parameters)


def test_existing_fts_triggers_adapt_to_unavailable_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "contextcore.db"))
    monkeypatch.setenv("CONTEXTCORE_DATA_DIR", str(tmp_path))
    create_schema()
    seed_data()
    project = project_service.create_project(name="Optional FTS transition")
    item = content_service.create_content(
        title="Retrieval mutation", content="An indexed canonical memory.", project_id=project["id"]
    )
    with db() as conn:
        retrieval_service.refresh_index(_connection=conn)
        assert conn.execute("SELECT entity_id FROM retrieval_index WHERE entity_id=?", (item["id"],)).fetchone()
        conn.execute("DROP TABLE retrieval_fts")
        unavailable = WithoutFTS(conn)
        retrieval_service.ensure_schema(unavailable)

        # Actual canonical mutations must clear derived data, not fail in an old FTS trigger.
        conn.execute("UPDATE contents SET title='Changed after FTS became unavailable' WHERE id=?", (item["id"],))
        assert conn.execute("SELECT entity_id FROM retrieval_index WHERE entity_id=?", (item["id"],)).fetchone() is None
        retrieval_service.refresh_index(_connection=unavailable)
        assert conn.execute("SELECT entity_id FROM retrieval_index WHERE entity_id=?", (item["id"],)).fetchone()
        conn.execute("DELETE FROM contents WHERE id=?", (item["id"],))
        assert conn.execute("SELECT id FROM contents WHERE id=?", (item["id"],)).fetchone() is None
        assert conn.execute("SELECT entity_id FROM retrieval_index WHERE entity_id=?", (item["id"],)).fetchone() is None

        # Repeated reads in the same availability state must not rebuild schema.
        version = conn.execute("PRAGMA schema_version").fetchone()[0]
        retrieval_service.ensure_schema(unavailable)
        assert conn.execute("PRAGMA schema_version").fetchone()[0] == version
