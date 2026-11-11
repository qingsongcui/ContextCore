"""A12-A14 regressions. All runtime state and vault data are synthetic and isolated."""
from __future__ import annotations

import json
import sqlite3
from threading import Event, Thread

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.database import create_schema, seed_data
from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretUnavailable
from app.services import content_service, handoff_service, project_service
from app.services.management_service import ManagementService


@pytest.fixture
def handoff_api(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "handoff.sqlite"))
    create_schema()
    seed_data()
    from app.main import app

    client = TestClient(
        app,
        base_url="http://127.0.0.1:8420",
        client=("127.0.0.1", 50000),
    )
    session = client.get("/api/session")
    assert session.status_code == 200
    return client, session.json()["csrf_token"]


def test_stale_handoff_get_is_conflict_and_list_redacts_source_body(handoff_api):
    client, csrf = handoff_api
    project = project_service.create_project(name="Stale handoff fixture")
    task = content_service.create_content(
        project_id=project["id"], category="task", title="Synthetic task",
        content="Continue the synthetic task", agent="cursor",
    )
    decision = content_service.create_content(
        project_id=project["id"], category="decision", title="Synthetic decision",
        content="STALE_SECRET_DECISION_TEXT", agent="cursor",
    )
    handoff = handoff_service.create_handoff(
        project_id=project["id"], task_id=task["id"], target_agent="codex", source_agent="cursor",
    )
    handoff_service.confirm_handoff(handoff["id"], confirmed=True, actor="codex")
    content_service.update_content(
        decision["id"], expected_version=1, content="Updated synthetic decision",
    )

    single = client.get(f"/api/handoffs/{handoff['id']}", headers={"X-CSRF-Token": csrf})
    assert single.status_code == 409
    assert "regenerate" in single.json()["detail"].lower() or "重新生成" in single.json()["detail"]

    listing = client.get("/api/handoffs", headers={"X-CSRF-Token": csrf})
    assert listing.status_code == 200
    stale = next(item for item in listing.json() if item["id"] == handoff["id"])
    assert stale["status"] == "stale"
    assert stale["stale_reason"]
    assert "STALE_SECRET_DECISION_TEXT" not in json.dumps(stale, ensure_ascii=False)
    assert all("重新生成" in stale["card"][field] for field in (
        "goal", "progress", "decisions", "evidence", "blockers", "next_steps",
    ))
    assert stale["source_ids"] == []
    assert stale["source_revisions"] == {}


def test_task_id_must_be_an_active_task(monkeypatch, tmp_path):
    monkeypatch.setenv("CONTEXTCORE_DB", str(tmp_path / "strict-task.sqlite"))
    create_schema()
    seed_data()
    project = project_service.create_project(name="Strict task fixture")
    decision = content_service.create_content(
        project_id=project["id"], category="decision", title="Not a task", content="synthetic",
    )
    with pytest.raises(HTTPException) as not_task:
        handoff_service.create_handoff(
            project_id=project["id"], task_id=decision["id"], target_agent="codex", source_agent="cursor",
        )
    assert not_task.value.status_code == 422
    assert "active task" in str(not_task.value.detail).lower() or "任务" in str(not_task.value.detail)

    task = content_service.create_content(
        project_id=project["id"], category="task", title="Historical task", content="synthetic",
    )
    content_service.update_content(task["id"], status="historical", expected_version=1)
    with pytest.raises(HTTPException) as inactive:
        handoff_service.create_handoff(
            project_id=project["id"], task_id=task["id"], target_agent="codex", source_agent="cursor",
        )
    assert inactive.value.status_code == 422

    active_task = content_service.create_content(
        project_id=project["id"], category="task", title="Active task", content="synthetic",
    )
    handoff = handoff_service.create_handoff(
        project_id=project["id"], task_id=active_task["id"], target_agent="codex", source_agent="cursor",
    )
    assert active_task["id"] in handoff["source_ids"]


def test_handoff_card_and_source_revision_share_one_write_snapshot(handoff_api, monkeypatch):
    project = project_service.create_project(name="Concurrent handoff fixture")
    task = content_service.create_content(
        project_id=project["id"], category="task", title="Original title",
        content="Original body", agent="cursor",
    )
    writer_attempting = Event()
    writer_finished = Event()
    writer_thread = None
    original_quote = handoff_service._raw_source_quotes

    def update_task():
        writer_attempting.set()
        content_service.update_content(
            task["id"], expected_version=1, title="Updated title", content="Updated body",
        )
        writer_finished.set()

    def concurrent_quote_hook(conversation, pattern):
        nonlocal writer_thread
        if writer_thread is None:
            writer_thread = Thread(target=update_task, daemon=True)
            writer_thread.start()
            assert writer_attempting.wait(timeout=2)
            # The writer has started while task text is already in the card but
            # source_revisions have not yet been captured. A transaction-wide
            # write claim must keep it from finishing inside that window.
            assert not writer_finished.wait(timeout=0.2)
        return original_quote(conversation, pattern)

    monkeypatch.setattr(handoff_service, "_raw_source_quotes", concurrent_quote_hook)
    card = handoff_service.create_handoff(
        project_id=project["id"], task_id=task["id"], target_agent="codex", source_agent="cursor",
    )
    assert writer_thread is not None
    writer_thread.join(timeout=6)
    assert not writer_thread.is_alive()
    assert writer_finished.is_set()
    assert card["card"]["goal"] == "Original title: Original body"
    assert card["source_revisions"][task["id"]]["version"] == 1
    assert content_service.get_content(task["id"])["version"] == 2
    with pytest.raises(HTTPException) as stale:
        handoff_service.confirm_handoff(card["id"], confirmed=True, actor="codex")
    assert stale.value.status_code == 409


class LockedFakeVault:
    def __init__(self):
        self.values = {}
        self.locked = False
        self.counter = 0

    def put(self, secret):
        if self.locked:
            raise SecretUnavailable("locked")
        self.counter += 1
        ref = "keychain:senrelay:" + f"{self.counter:032x}"
        self.values[ref] = secret
        return ref

    def get(self, ref):
        if self.locked or ref not in self.values:
            raise SecretUnavailable("locked")
        return self.values[ref]

    def delete(self, ref):
        if self.locked:
            raise SecretUnavailable("locked")
        self.values.pop(ref, None)


@pytest.fixture
def managed_home(tmp_path):
    home = tmp_path / "managed-home"
    home.mkdir()
    vault = LockedFakeVault()
    manager = ManagementService(tmp_path / "management.sqlite", home, vault)
    return manager, vault


def test_locked_vault_does_not_block_owned_provider_uninstall_or_fake_secret_deletion(managed_home):
    manager, vault = managed_home
    provider = manager.save("provider", {
        "name": "Synthetic Anthropic", "protocol": "anthropic", "base_url": "https://api.example.test",
    })
    provider = manager.credential("provider", provider["id"], "synthetic-credential-value")
    ref = provider["credential_ref"]

    install = manager.plan({"host_id": "claude-code", "provider_id": provider["id"]})
    manager.apply(install["id"], install["version"], True)
    config = manager.home / ".claude/settings.json"
    installed = json.loads(config.read_text())
    assert installed["env"]["ANTHROPIC_AUTH_TOKEN"] == "synthetic-credential-value"
    installed_bytes = config.read_bytes()

    # An external rewrite still blocks ownership-checked removal, even while locked.
    modified = json.loads(config.read_text())
    modified["env"]["ANTHROPIC_AUTH_TOKEN"] = "external-value"
    config.write_text(json.dumps(modified))
    vault.locked = True
    with pytest.raises(ConfigurationError, match="externally edited"):
        manager.plan({"host_id": "claude-code", "provider_id": provider["id"], "operation": "uninstall"})

    config.write_bytes(installed_bytes)
    uninstall = manager.plan({"host_id": "claude-code", "provider_id": provider["id"], "operation": "uninstall"})
    result = manager.apply(uninstall["id"], uninstall["version"], True)
    assert result["status"] == "applied"
    assert "ANTHROPIC_AUTH_TOKEN" not in json.loads(config.read_text()).get("env", {})
    assert vault.values[ref] == "synthetic-credential-value"

    # Host-field removal and vault cleanup are separate outcomes; a locked vault
    # must leave a durable pending cleanup receipt instead of claiming deletion.
    deletion = manager.delete("provider", provider["id"], provider["version"])
    assert deletion["deleted"] is True
    assert deletion["credential_cleanup"][0]["status"] == "pending"
    assert ref in vault.values


def test_activation_metadata_failure_recovers_host_file_and_keeps_ownership_consistent(managed_home):
    manager, _vault = managed_home
    config = manager.home / ".claude/settings.json"
    config.parent.mkdir(parents=True)
    original = b'{"env":{"ANTHROPIC_MODEL":"previous-model","EXTERNAL":"keep"}}\n'
    config.write_bytes(original)

    provider = manager.save("provider", {
        "name": "Synthetic activation provider", "protocol": "anthropic",
        "base_url": "https://api.example.test", "model_map": {"alias": "native-model"},
    })
    model = manager.save("model", {
        "name": "Synthetic model", "provider_id": provider["id"], "model_id": "native-model",
    })
    plan = manager.plan({
        "host_id": "claude-code", "provider_id": provider["id"],
        "model_id": model["id"], "activate_provider": True,
    })
    with manager.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_activation BEFORE INSERT ON management_activations "
            "BEGIN SELECT RAISE(ABORT, 'injected metadata failure'); END"
        )

    with pytest.raises(ConfigurationError, match="recovery is required"):
        manager.apply(plan["id"], plan["version"], True)

    with manager.connect() as conn:
        plan_status = conn.execute("SELECT status FROM management_plans WHERE id=?", (plan["id"],)).fetchone()[0]
        activation_count = conn.execute("SELECT COUNT(*) FROM management_activations").fetchone()[0]
        owned_count = conn.execute("SELECT COUNT(*) FROM management_owned").fetchone()[0]
    assert plan_status == "recovery_required"
    assert activation_count == 0
    assert owned_count == 0

    recovered = manager.transactions.recover(plan["id"], confirmed=True)
    assert recovered["status"] == "recovered"
    assert config.read_bytes() == original
    assert json.loads(config.read_text())["env"] == {
        "ANTHROPIC_MODEL": "previous-model", "EXTERNAL": "keep",
    }
    with manager.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_activations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM management_owned").fetchone()[0] == 0


def test_activation_restore_metadata_failure_recovers_active_file_and_owner_state(managed_home):
    manager, _vault = managed_home
    config = manager.home / ".claude/settings.json"
    config.parent.mkdir(parents=True)
    original = b'{"env":{"ANTHROPIC_MODEL":"previous-model","EXTERNAL":"keep"}}\n'
    config.write_bytes(original)
    provider = manager.save("provider", {
        "name": "Synthetic restore provider", "protocol": "anthropic",
        "base_url": "https://api.example.test", "model_map": {"alias": "native-model"},
    })
    provider = manager.credential("provider", provider["id"], "synthetic-restore-credential")
    model = manager.save("model", {
        "name": "Synthetic restore model", "provider_id": provider["id"], "model_id": "native-model",
    })
    activation = manager.plan({
        "host_id": "claude-code", "provider_id": provider["id"],
        "model_id": model["id"], "activate_provider": True,
    })
    manager.apply(activation["id"], activation["version"], True)
    active_bytes = config.read_bytes()
    active_data = json.loads(active_bytes)
    assert active_data["env"]["ANTHROPIC_MODEL"] == "native-model"
    with manager.connect() as conn:
        owned_before = conn.execute("SELECT COUNT(*) FROM management_owned").fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM management_activations").fetchone()[0] == 1

    restore = manager.plan({"host_id": "claude-code", "operation": "restore"})
    with manager.connect() as conn:
        conn.execute(
            "CREATE TRIGGER fail_activation_delete BEFORE DELETE ON management_activations "
            "BEGIN SELECT RAISE(ABORT, 'injected metadata failure'); END"
        )
    with pytest.raises(ConfigurationError, match="recovery is required"):
        manager.apply(restore["id"], restore["version"], True)

    with manager.connect() as conn:
        status = conn.execute("SELECT status FROM management_plans WHERE id=?", (restore["id"],)).fetchone()[0]
        assert conn.execute("SELECT COUNT(*) FROM management_activations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM management_owned").fetchone()[0] == owned_before
    assert status == "recovery_required"

    recovered = manager.transactions.recover(restore["id"], confirmed=True)
    assert recovered["status"] == "recovered"
    assert config.read_bytes() == active_bytes
    with manager.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_activations").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM management_owned").fetchone()[0] == owned_before

    with manager.connect() as conn:
        conn.execute("DROP TRIGGER fail_activation_delete")
    retry_restore = manager.plan({"host_id": "claude-code", "operation": "restore"})
    manager.apply(retry_restore["id"], retry_restore["version"], True)
    restored = json.loads(config.read_text())
    assert restored["env"]["ANTHROPIC_MODEL"] == "previous-model"
    assert restored["env"]["EXTERNAL"] == "keep"
    with manager.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_activations").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM management_owned").fetchone()[0] == 0
