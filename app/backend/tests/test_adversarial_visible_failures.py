"""User-visible failures must not be reported as success.

Covers the 2026-11-01 adversarial review: a local sync that cannot write or
read a present source, and a conversation that stays unlinked.
"""
import os
from pathlib import Path

from app.database import create_schema
from app.integrations.config_transactions import ConfigurationError
from app.services.management_service import ManagementService


def test_empty_local_sync_is_success_only_without_failures():
    create_schema()
    result = ManagementService().sync_local_environment()
    assert result["ok"] is True
    assert result["failure_count"] == 0
    assert result["failures"] == []


def test_unreadable_cc_switch_source_is_not_a_successful_sync():
    create_schema()
    cc_dir = Path(os.environ["HOME"]) / ".cc-switch"
    cc_dir.mkdir()
    (cc_dir / "cc-switch.db").write_bytes(b"not a database")
    result = ManagementService().sync_local_environment()
    assert result["ok"] is False
    assert result["failure_count"] >= 1
    assert any(item["stage"] == "cc-switch" for item in result["failures"])
    assert "not a database" not in str(result["failures"])


def test_failed_skill_write_is_not_counted_as_synced(monkeypatch):
    create_schema()
    skill = Path(os.environ["HOME"]) / ".claude" / "skills" / "synthetic-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# synthetic\n", encoding="utf-8")
    manager = ManagementService()
    original = manager.save

    def fail_skill(kind, data, entity_id=None, expected_version=None):
        if kind == "asset" and isinstance(data, dict) and data.get("kind") == "skill":
            raise ConfigurationError("synthetic skill write failed")
        return original(kind, data, entity_id, expected_version)

    monkeypatch.setattr(manager, "save", fail_skill)
    result = manager.sync_local_environment()
    assert result["ok"] is False
    assert result["skills_synced"] == 0
    assert any(item["stage"] == "asset" and item["name"] == "synthetic-skill" for item in result["failures"])
    assert "synthetic skill write failed" not in str(result["failures"])


def test_unlinked_conversation_failure_is_visible(monkeypatch):
    from app.services import handoff_service

    class Result:
        def fetchall(self):
            return [{"id": "c1", "cwd": "/tmp/synthetic-unlinked", "title": "synthetic"}]

    class Conn:
        def execute(self, *_args, **_kwargs):
            return Result()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(handoff_service, "db", lambda: Conn())

    def boom(_path):
        raise RuntimeError("synthetic match failure")

    monkeypatch.setattr(handoff_service.project_service, "resolve_project_by_path", boom)
    assert handoff_service._link_unlinked_conversations_to_projects() == 1

    monkeypatch.setattr("app.services.connection_service.list_connections", lambda: [])
    synced = handoff_service.sync_recent_agent_chats_for_relay()
    assert any(item.get("status") == "error" for item in synced)
    assert all("synthetic match failure" not in str(item) for item in synced)
