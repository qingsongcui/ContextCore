"""User-facing setup and installed-state paths use only synthetic homes/vaults."""
import json
import sqlite3
import base64
import io
import threading
import zipfile

import pytest

from app.integrations.config_transactions import ConfigurationError
from app.integrations.secret_store import SecretUnavailable
from app.services.management_service import ManagementService


class Vault:
    def __init__(self):
        self.values = {}
        self.locked = False

    def put(self, value):
        if self.locked:
            raise SecretUnavailable("locked")
        ref = "keychain:senrelay:" + f"{len(self.values) + 1:032x}"
        self.values[ref] = value
        return ref

    def get(self, ref):
        if self.locked:
            raise SecretUnavailable("locked")
        if ref not in self.values:
            raise SecretUnavailable("missing")
        return self.values[ref]

    def delete(self, ref):
        self.values.pop(ref, None)


@pytest.fixture
def manager(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return ManagementService(tmp_path / "data/test.db", home, Vault())


def setup(host="codex", token="setup-request-0001"):
    return {"request_id": token, "host_id": host,
            "provider": {"name": "Fixture provider", "base_url": "https://fixture.example/v1", "protocol": "openai"},
            "model_id": "fixture-model", "secret": "synthetic-only-value-7z9"}


UNCHECKED_EFFECTS = {
    "host_loaded": "not_verified",
    "reachability": "not_checked",
    "authentication": "not_checked",
    "invocation": "not_checked",
}


def assert_write_is_not_loaded_or_callable(state):
    for key, value in UNCHECKED_EFFECTS.items():
        assert state["effects"][key] == value
    assert state["credentials_read"] is False and state["host_executed"] is False


def test_provider_key_model_saved_together_and_retry_does_not_duplicate(manager):
    first = manager.setup_provider(setup())
    assert first == manager.setup_provider(setup())
    assert len(manager.list("provider")) == len(manager.list("model")) == len(manager.vault.values) == 1
    assert first["model"]["provider_id"] == first["provider"]["id"]
    assert first["provider"]["agent_ids"] == ["codex"]
    assert first["provider"]["has_credential"]
    assert first["host_configuration"] == "unchanged"
    assert not list(manager.home.iterdir())
    assert setup()["secret"].encode() not in manager.db_path.read_bytes()
    assert setup()["secret"] not in json.dumps(first)
    assert setup()["secret"] not in json.dumps(manager.export())
    with pytest.raises(ConfigurationError, match="request changed"):
        manager.setup_provider({**setup(), "model_id": "different-model"})


def test_same_display_name_keeps_independent_providers_and_models(manager):
    first = manager.setup_provider(setup())
    second = manager.setup_provider({**setup(token="setup-request-0002"), "provider": {**setup()["provider"], "base_url": "https://other.example/v1"}, "model_id": "other-model", "secret": "synthetic-only-value-other"})
    assert first["provider"]["name"] == second["provider"]["name"]
    assert first["provider"]["id"] != second["provider"]["id"]
    renamed = manager.save("provider", {"name": "Renamed fixture"}, first["provider"]["id"], first["provider"]["version"])
    assert renamed["name"] == "Renamed fixture"
    assert manager.get("provider", second["provider"]["id"])["name"] == "Fixture provider"
    assert {item["model_id"] for item in manager.list("model")} == {"fixture-model", "other-model"}
    assert len(manager.vault.values) == 2
    assert second["provider"]["base_url"] == "https://other.example/v1"


def test_concurrent_setup_requests_do_not_merge_or_leave_a_half_record(manager):
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run():
        barrier.wait()
        try:
            results.append(manager.setup_provider(setup()))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=run), threading.Thread(target=run)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert results[0]["provider"]["id"] == results[1]["provider"]["id"]
    other = manager.setup_provider({**setup(token="setup-request-0002"), "model_id": "other-model", "secret": "synthetic-only-value-other"})
    assert other["provider"]["id"] != results[0]["provider"]["id"]
    assert len(manager.list("provider")) == len(manager.list("model")) == len(manager.vault.values) == 2
    assert {item["model_id"] for item in manager.list("model")} == {"fixture-model", "other-model"}


def test_locked_keychain_or_invalid_model_creates_no_half_provider(manager):
    manager.vault.locked = True
    with pytest.raises(SecretUnavailable):
        manager.setup_provider(setup())
    assert not manager.list("provider") and not manager.list("model")
    manager.vault.locked = False
    with pytest.raises(ConfigurationError):
        manager.setup_provider({**setup(), "model_id": "invalid\nmodel"})
    assert not manager.vault.values
    assert not manager.list("provider")


def test_setup_rolls_back_both_entities_and_cleans_unadopted_key_on_database_failure(manager):
    with manager.connect() as conn:
        conn.execute("CREATE TRIGGER reject_setup BEFORE INSERT ON management_provider_setups BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        manager.setup_provider(setup())
    assert not manager.list("provider") and not manager.list("model") and not manager.vault.values
    with manager.connect() as conn:
        assert conn.execute("SELECT status FROM management_secret_cleanup").fetchone()[0] == "completed"


def activate(manager, host="codex", token="setup-request-0001"):
    result = manager.setup_provider(setup(host, token))
    plan = manager.plan({"host_id": host, "provider_id": result["provider"]["id"],
                         "activate_provider": True, "model_id": result["model"]["id"]})
    manager.apply(plan["id"], plan["version"], True)
    return result


def test_current_selection_reads_native_fields_without_keychain_or_whole_file_false_conflicts(manager):
    result = activate(manager)
    manager.vault.locked = True
    path = manager.home / ".codex/config.toml"
    path.write_text(path.read_text() + '\n[foreign]\nkeep = true\n')
    original = path.read_bytes()
    state = manager.host_state("codex")
    assert state["selection"]["provider_id"] == result["provider"]["id"]
    assert state["selection"]["model_id"] == "fixture-model"
    assert state["selection"]["status"] == "verified"
    assert state["selection"]["host_loaded"] == "not_verified"
    assert state["resources"][result["provider"]["id"]]["status"] == "verified"
    assert state["credentials_read"] is False and state["host_executed"] is False
    assert state["effects"] == {"selection": "recorded", "configuration": "verified", "host_loaded": "not_verified", "reachability": "not_checked", "authentication": "not_checked", "invocation": "not_checked"}
    assert path.read_bytes() == original
    assert setup()["secret"] not in json.dumps(state)
    assert "keychain:senrelay:" not in json.dumps(state)
    path.write_text(path.read_text().replace('model = "fixture-model"', 'model = "foreign-model"'))
    changed = manager.host_state("codex")
    assert changed["selection"]["status"] == "externally_changed"
    assert changed["effects"]["configuration"] == "externally_changed"
    assert changed["effects"]["invocation"] == "not_checked"


def test_saved_definition_update_and_native_removal_are_visible(manager):
    result = activate(manager)
    provider = result["provider"]
    manager.save("provider", {"name": "Changed fixture"}, provider["id"], provider["version"])
    assert manager.host_state("codex")["resources"][provider["id"]]["status"] == "update_available"
    (manager.home / ".codex/config.toml").unlink()
    assert manager.host_state("codex")["selection"]["status"] == "missing"


def test_switch_only_one_current_provider_and_restore_removes_current_marker(manager):
    first = activate(manager)
    second = activate(manager, token="setup-request-0002")
    state = manager.host_state("codex")
    assert state["selection"]["provider_id"] == second["provider"]["id"]
    assert state["resources"][first["provider"]["id"]]["status"] == "verified"
    restore = manager.plan({"host_id": "codex", "operation": "restore"})
    manager.apply(restore["id"], restore["version"], True)
    assert manager.host_state("codex")["selection"]["provider_id"] is None


def test_mcp_install_update_uninstall_and_unsafe_target_are_not_conflated(manager):
    asset = manager.save("asset", {"name": "Fixture tool", "kind": "mcp", "config": {"command": "fixture-server"}})
    plan = manager.plan({"host_id": "claude-code", "asset_ids": [asset["id"]]})
    manager.apply(plan["id"], plan["version"], True)
    assert manager.host_state("claude-code")["resources"][asset["id"]]["status"] == "verified"
    remove = manager.plan({"host_id": "claude-code", "asset_ids": [asset["id"]], "operation": "uninstall"})
    manager.apply(remove["id"], remove["version"], True)
    assert manager.host_state("claude-code")["resources"][asset["id"]]["status"] == "removed"
    reinstall = manager.plan({"host_id": "claude-code", "asset_ids": [asset["id"]]})
    manager.apply(reinstall["id"], reinstall["version"], True)
    path = manager.home / ".claude.json"
    target = manager.home / "foreign.json"
    target.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(target)
    assert manager.host_state("claude-code")["resources"][asset["id"]]["status"] == "unreadable"


def test_managed_skill_link_state_is_checked_without_modifying_it(manager):
    asset = manager.save("asset", {"name": "Fixture skill", "kind": "skill", "install_mode": "symlink", "files": {"SKILL.md": "# Fixture skill\n"}})
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    manager.apply(plan["id"], plan["version"], True)
    state = manager.host_state("codex")
    assert state["resources"][asset["id"]]["status"] == "verified"
    assert next((manager.home / ".codex/skills").rglob("SKILL.md")).is_symlink()


def test_host_state_and_setup_api_use_existing_manager_boundary(manager, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers.manage import router
    from app.services import management_service
    monkeypatch.setattr(management_service, "service", lambda: manager)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.post("/api/manage/providers/setup", json=setup())
    assert response.status_code == 200
    created = response.json()
    revised = client.post("/api/manage/providers/setup/revise", json=revision(created, model_id="api-revised-model"))
    assert revised.status_code == 200
    assert revised.json()["host_configuration"] == "unchanged"
    assert revised.json()["model"]["model_id"] == "api-revised-model"
    assert setup()["secret"] not in revised.text
    state = client.get("/api/manage/hosts/codex/state")
    assert state.status_code == 200
    assert client.get("/api/manage/hosts/unknown/state").status_code == 422
    assert setup()["secret"] not in state.text + response.text


def test_user_model_catalog_is_written_to_native_provider_and_changes_invalidate_preview(manager):
    result = manager.setup_provider(setup("opencode"))
    plan = manager.plan({"host_id": "opencode", "provider_id": result["provider"]["id"], "activate_provider": True, "model_id": result["model"]["id"]})
    manager.save("model", {"model_id": "updated-model"}, result["model"]["id"], result["model"]["version"])
    with pytest.raises(ConfigurationError, match="changed since preview"):
        manager.apply(plan["id"], plan["version"], True)
    assert not (manager.home / ".config/opencode/opencode.json").exists()
    next_plan = manager.plan({"host_id": "opencode", "provider_id": result["provider"]["id"], "activate_provider": True, "model_id": result["model"]["id"]})
    manager.apply(next_plan["id"], next_plan["version"], True)
    native = json.loads((manager.home / ".config/opencode/opencode.json").read_text())
    assert "updated-model" in native["provider"][result["provider"]["id"]]["models"]
    assert native["model"] == result["provider"]["id"] + "/updated-model"


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zipped:
        for name, content in files.items():
            zipped.writestr(name, content)
    return {"format": "skill-zip", "agent_id": "codex", "data": base64.b64encode(output.getvalue()).decode()}


def test_finder_zip_wrapper_and_metadata_import_as_one_named_skill_and_apply(manager):
    result = manager.import_objects(archive({
        "writing/SKILL.md": "---\nname: writing-fixture\n---\n# Writing\n",
        "writing/references/guide.md": "Keep references.",
        "writing/.DS_Store": b"\xff\x00finder metadata",
        "__MACOSX/writing/._SKILL.md": b"\xff\x00finder metadata",
    }))
    asset = result["imported"][0]
    assert asset["name"] == "writing-fixture" and asset["agent_ids"] == ["codex"]
    assert set(asset["files"]) == {"SKILL.md", "references/guide.md"}
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    manager.apply(plan["id"], plan["version"], True)
    assert (manager.home / ".codex/skills" / asset["id"] / "references/guide.md").read_text() == "Keep references."


@pytest.mark.parametrize("files", [
    {"one/SKILL.md": "one", "two/SKILL.md": "two"},
    {"SKILL.md": "skill", "Readme.md": "one", "readme.md": "two"},
    {"SKILL.md": "skill", "Folder": "file", "folder/child.md": "child"},
    {"SKILL.md": "skill", "café.md": "one", "cafe\u0301.md": "two"},
    {"writing/SKILL.md": "skill", "__MACOSX/../../escape": b"bad"},
])
def test_skill_zip_ambiguity_macos_path_collisions_and_metadata_traversal_fail_before_saving(manager, files):
    with pytest.raises(ConfigurationError):
        manager.import_objects(archive(files))
    assert manager.list("asset") == [] and not list(manager.home.iterdir())


def revision(created, **overrides):
    provider, model = created["provider"], created["model"]
    body = {"request_id": "revise-request-0001", "host_id": "codex", "provider_id": provider["id"], "model_entity_id": model["id"],
            "expected_provider_version": provider["version"], "expected_model_version": model["version"],
            "provider": {"name": provider["name"], "base_url": provider["base_url"], "protocol": provider["protocol"]},
            "model_id": model["model_id"]}
    body.update(overrides)
    return body


def test_revision_updates_connection_and_model_together_without_touching_the_host_file(manager):
    created = activate(manager)
    provider = manager.save("provider", {"model_map": {"keep": "mapped"}, "agent_ids": ["codex", "claude-code"]}, created["provider"]["id"], created["provider"]["version"])
    other = manager.save("model", {"name": "Other model", "provider_id": provider["id"], "model_id": "other-model"})
    original = (manager.home / ".codex/config.toml").read_bytes()
    body = revision({"provider": provider, "model": created["model"]}, request_id="revise-request-0001",
                    expected_provider_version=provider["version"],
                    provider={"name": "Revised name", "base_url": "https://revised.example/v1", "protocol": "openai", "wire_api": "responses", "supported_endpoints": ["responses"]},
                    model_id="revised-model", secret="synthetic-revised-value")
    first = manager.revise_provider_setup(body)
    assert first == manager.revise_provider_setup(body)
    assert first["host_configuration"] == "unchanged"
    assert first["provider"]["version"] == provider["version"] + 1
    assert first["model"]["version"] == created["model"]["version"] + 1
    assert first["provider"]["model_map"] == {"keep": "mapped"}
    assert first["provider"]["agent_ids"] == ["codex", "claude-code"]
    assert first["provider"]["base_url"] == "https://revised.example/v1"
    assert first["model"]["model_id"] == "revised-model"
    assert manager.get("model", other["id"])["model_id"] == "other-model"
    assert manager.get("model", other["id"])["version"] == other["version"]
    assert (manager.home / ".codex/config.toml").read_bytes() == original
    state = manager.host_state("codex")
    assert state["resources"][provider["id"]]["status"] == "update_available"
    assert state["effects"]["configuration"] == "update_available"
    assert state["effects"]["host_loaded"] == "not_verified"
    assert state["effects"]["invocation"] == "not_checked"
    assert "synthetic-revised-value" not in json.dumps(first) + json.dumps(state)
    assert b"synthetic-revised-value" not in manager.db_path.read_bytes()
    assert list(manager.vault.values.values()) == ["synthetic-revised-value"]
    with pytest.raises(ConfigurationError, match="request changed"):
        manager.revise_provider_setup({**body, "model_id": "another-model"})


def test_stale_revision_account_key_and_database_failure_leave_one_pair(manager):
    created = manager.setup_provider(setup())
    stale = revision(created, secret="synthetic-stale-value", model_id="should-not-apply")
    manager.save("provider", {"name": "Moved first"}, created["provider"]["id"], created["provider"]["version"])
    with pytest.raises(ConfigurationError, match="version changed"):
        manager.revise_provider_setup(stale)
    assert manager.get("model", created["model"]["id"])["model_id"] == "fixture-model"
    assert list(manager.vault.values.values()) == ["synthetic-only-value-7z9"]

    current = {"provider": manager.get("provider", created["provider"]["id"]), "model": manager.get("model", created["model"]["id"])}
    with manager.connect() as conn:
        conn.execute("CREATE TRIGGER reject_revision BEFORE INSERT ON management_provider_revisions BEGIN SELECT RAISE(ABORT,'fixture failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        manager.revise_provider_setup(revision(current, secret="synthetic-unadopted-value", model_id="rolled-back-model",
                                               provider={"name": "Rolled back", "base_url": "https://rolled.example/v1", "protocol": "openai"}))
    provider = manager.get("provider", created["provider"]["id"])
    assert provider["name"] == "Moved first"
    assert manager.get("model", created["model"]["id"])["model_id"] == "fixture-model"
    assert "synthetic-unadopted-value" not in manager.vault.values.values()
    with manager.connect() as conn:
        assert conn.execute("SELECT status FROM management_secret_cleanup WHERE reason='provider_revision_not_adopted'").fetchone()[0] == "completed"
    conn_provider = provider
    account = manager.save("account", {"name": "Bound account", "kind": "api_key", "provider_id": conn_provider["id"]})
    manager.credential("account", account["id"], "synthetic-account-value")
    conn_provider = manager.save("provider", {"account_id": account["id"], "credential_source": "account"}, conn_provider["id"], conn_provider["version"])
    with pytest.raises(ConfigurationError, match="account credential"):
        manager.revise_provider_setup(revision({"provider": conn_provider, "model": created["model"]}, secret="synthetic-should-not", model_id="still-fixture"))
    assert "synthetic-should-not" not in manager.vault.values.values()
    assert manager.get("provider", conn_provider["id"])["credential_source"] == "account"
    assert manager.get("model", created["model"]["id"])["model_id"] == "fixture-model"


def test_concurrent_revisions_of_one_provider_do_not_combine_two_requests(manager):
    created = manager.setup_provider(setup())
    barrier = threading.Barrier(2)
    results, errors = [], []

    def run(body):
        barrier.wait()
        try:
            results.append(manager.revise_provider_setup(body))
        except Exception as exc:
            errors.append(exc)

    first = revision(created, request_id="revise-request-0001", model_id="first-model", secret="synthetic-first-value")
    second = revision(created, request_id="revise-request-0002", model_id="second-model", secret="synthetic-second-value")
    threads = [threading.Thread(target=run, args=(first,)), threading.Thread(target=run, args=(second,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 1 and len(errors) == 1
    assert "version changed" in str(errors[0])
    saved = manager.get("model", created["model"]["id"])
    assert saved["model_id"] in {"first-model", "second-model"}
    assert saved["version"] == created["model"]["version"] + 1
    assert len(manager.list("provider")) == len(manager.list("model")) == 1
    assert manager.get("provider", created["provider"]["id"])["version"] == created["provider"]["version"] + 1
    assert list(manager.vault.values.values()) == ["synthetic-" + saved["model_id"].split("-")[0] + "-value"]


def test_switch_readback_keeps_unrelated_fields_and_removal_handles_selection_dependencies_and_restore(manager):
    import tomllib
    path = manager.home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('# user comment\nmodel = "original-model"\nmodel_provider = "original-provider"\n[features]\nkeep = true\n')

    def install(token, secret):
        result = manager.setup_provider({**setup(token=token), "secret": secret})
        plan = manager.plan({"host_id": "codex", "provider_id": result["provider"]["id"], "activate_provider": True, "model_id": result["model"]["id"]})
        manager.apply(plan["id"], plan["version"], True)
        return result

    def put_route(value):
        with manager.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")
            conn.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, updated_at) VALUES ('routing_v2_settings', ?, '2026-10-26T00:00:00+00:00')",
                (json.dumps(value),),
            )

    first = install("setup-request-0001", "synthetic-only-value-7z9")
    second = install("setup-request-0002", "synthetic-only-value-other")
    switched = path.read_text()
    assert tomllib.loads(switched)["features"]["keep"] is True and "# user comment" in switched
    state = manager.host_state("codex")
    assert state["selection"]["provider_id"] == second["provider"]["id"]
    assert state["selection"]["model_id"] == "fixture-model"
    assert state["effects"] == {
        "selection": "recorded", "configuration": "verified", "host_loaded": "not_verified",
        "reachability": "not_checked", "authentication": "not_checked", "invocation": "not_checked",
    }
    assert state["resources"][first["provider"]["id"]]["status"] == "verified"
    assert state["resources"][second["provider"]["id"]]["status"] == "verified"
    assert state["credentials_read"] is False and state["host_executed"] is False

    before = path.read_bytes()
    with pytest.raises(ConfigurationError, match="current model selection|active model selection") as rejected:
        manager.plan({"host_id": "codex", "provider_id": second["provider"]["id"], "operation": "uninstall"})
    assert rejected.value.status_code == 409 and path.read_bytes() == before
    with pytest.raises(ConfigurationError, match="Uninstall owned host entries") as owned:
        manager.delete("provider", first["provider"]["id"], first["provider"]["version"])
    assert owned.value.status_code == 409

    put_route({
        "enabled": True, "provider_id": first["provider"]["id"],
        "fallback_provider_ids": [first["provider"]["id"]],
        "model_routes": {"special-model": [first["provider"]["id"]]},
    })
    removal = manager.plan({"host_id": "codex", "provider_id": first["provider"]["id"], "operation": "uninstall"})
    manager.apply(removal["id"], removal["version"], True)
    removed = path.read_text()
    assert "synthetic-only-value-7z9" not in removed and "synthetic-only-value-other" in removed
    assert "# user comment" in removed and tomllib.loads(removed)["features"]["keep"] is True
    after_removal = manager.host_state("codex")
    assert after_removal["selection"]["provider_id"] == second["provider"]["id"]
    assert after_removal["resources"][first["provider"]["id"]]["status"] == "removed"
    current = manager.get("provider", first["provider"]["id"])
    with pytest.raises(ConfigurationError, match="independent client routing") as routed:
        manager.delete("provider", current["id"], current["version"])
    assert routed.value.status_code == 409

    put_route({"enabled": False, "provider_id": None, "fallback_provider_ids": [], "model_routes": {}})
    current = manager.get("provider", first["provider"]["id"])
    with pytest.raises(ConfigurationError, match="Object is referenced") as referenced:
        manager.delete("provider", current["id"], current["version"])
    assert referenced.value.status_code == 409

    restore = manager.plan({"host_id": "codex", "operation": "restore"})
    manager.apply(restore["id"], restore["version"], True)
    restored = path.read_text()
    parsed = tomllib.loads(restored)
    assert parsed["model"] == "original-model" and parsed["model_provider"] == "original-provider"
    assert parsed["features"]["keep"] is True and "# user comment" in restored
    assert "synthetic-only-value-7z9" not in restored
    assert manager.host_state("codex")["selection"]["provider_id"] is None

    uninstall_current = manager.plan({"host_id": "codex", "provider_id": second["provider"]["id"], "operation": "uninstall"})
    manager.apply(uninstall_current["id"], uninstall_current["version"], True)
    final_text = path.read_text()
    parsed = tomllib.loads(final_text)
    assert parsed["model"] == "original-model" and parsed["model_provider"] == "original-provider"
    assert parsed["features"]["keep"] is True and "# user comment" in final_text
    assert "synthetic-only-value-other" not in final_text and "synthetic-only-value-7z9" not in final_text
    final_state = manager.host_state("codex")
    assert final_state["resources"][first["provider"]["id"]]["status"] == "removed"
    assert final_state["resources"][second["provider"]["id"]]["status"] == "removed"
    assert final_state["effects"]["host_loaded"] == "not_verified"
    assert final_state["effects"]["invocation"] == "not_checked"
    assert final_state["credentials_read"] is False and final_state["host_executed"] is False

    current = manager.get("provider", second["provider"]["id"])
    with pytest.raises(ConfigurationError, match="Object is referenced"):
        manager.delete("provider", current["id"], current["version"])
    for item in (first, second):
        model = manager.get("model", item["model"]["id"])
        assert manager.delete("model", model["id"], model["version"])["deleted"] is True
        provider = manager.get("provider", item["provider"]["id"])
        assert manager.delete("provider", provider["id"], provider["version"])["deleted"] is True
    assert "synthetic-only-value-7z9" not in manager.vault.values.values()
    assert "synthetic-only-value-other" not in manager.vault.values.values()


def apply_asset(manager, host, asset, operation="install"):
    plan = manager.plan({"host_id": host, "asset_ids": [asset["id"]], "operation": operation})
    return manager.apply(plan["id"], plan["version"], True)


def installed_text(manager, kind, asset):
    if kind == "mcp":
        return (manager.home / ".claude.json").read_text()
    if kind == "skill":
        return next((manager.home / ".codex/skills").rglob("SKILL.md")).read_text()
    return (manager.home / ".codex/AGENTS.md").read_text()


@pytest.mark.parametrize("kind,host,create,update,first,second", [
    ("mcp", "claude-code", {"name": "Fixture MCP", "kind": "mcp", "config": {"command": "fixture-one"}},
     {"config": {"command": "fixture-two"}}, "fixture-one", "fixture-two"),
    ("skill", "codex", {"name": "Fixture skill", "kind": "skill", "files": {"SKILL.md": "# skill-one\n"}},
     {"files": {"SKILL.md": "# skill-two\n"}}, "skill-one", "skill-two"),
    ("prompt", "codex", {"name": "Fixture rule", "kind": "prompt", "content": "rule-one"},
     {"content": "rule-two"}, "rule-one", "rule-two"),
])
def test_mcp_skill_prompt_install_update_rollback_uninstall(manager, kind, host, create, update, first, second):
    asset = manager.save("asset", create)
    applied = apply_asset(manager, host, asset)
    assert applied["payload"]["host_reload"] == "not_run"
    assert applied["payload"]["real_request"] == "not_run"
    state = manager.host_state(host)
    assert state["resources"][asset["id"]]["status"] == "verified"
    assert_write_is_not_loaded_or_callable(state)
    assert first in installed_text(manager, kind, asset)

    asset = manager.save("asset", update, asset["id"], asset["version"])
    pending = manager.host_state(host)
    assert pending["resources"][asset["id"]]["status"] == "update_available"
    assert_write_is_not_loaded_or_callable(pending)
    apply_asset(manager, host, asset)
    text = installed_text(manager, kind, asset)
    assert second in text and first not in text
    assert_write_is_not_loaded_or_callable(manager.host_state(host))

    rolled = manager.restore_asset(asset["id"], 1, asset["version"])
    assert rolled["version"] == asset["version"] + 1
    apply_asset(manager, host, rolled)
    restored = installed_text(manager, kind, rolled)
    assert first in restored and second not in restored
    assert manager.host_state(host)["resources"][rolled["id"]]["status"] == "verified"

    apply_asset(manager, host, rolled, "uninstall")
    final = manager.host_state(host)
    assert final["resources"][rolled["id"]]["status"] == "removed"
    assert_write_is_not_loaded_or_callable(final)
    leftover = ""
    if kind == "mcp" and (manager.home / ".claude.json").exists():
        leftover = (manager.home / ".claude.json").read_text()
    elif kind == "prompt" and (manager.home / ".codex/AGENTS.md").exists():
        leftover = (manager.home / ".codex/AGENTS.md").read_text()
    elif kind == "skill":
        leftover = "".join(path.read_text() for path in (manager.home / ".codex/skills").rglob("*") if path.is_file())
    assert first not in leftover and second not in leftover


def test_missing_keychain_item_does_not_substitute_another_provider_secret(manager):
    def install(token, secret):
        result = manager.setup_provider({**setup(token=token), "secret": secret})
        plan = manager.plan({"host_id": "codex", "provider_id": result["provider"]["id"],
                             "activate_provider": True, "model_id": result["model"]["id"]})
        manager.apply(plan["id"], plan["version"], True)
        return result

    first = install("setup-request-0001", "synthetic-only-value-7z9")
    second = install("setup-request-0002", "synthetic-only-value-other")
    path = manager.home / ".codex/config.toml"
    before = path.read_bytes()
    first_ref = manager.get("provider", first["provider"]["id"])["credential_ref"]
    manager.vault.values.pop(first_ref)
    current = manager.get("provider", first["provider"]["id"])
    plan = manager.plan({"host_id": "codex", "provider_id": current["id"], "activate_provider": True, "model_id": first["model"]["id"]})
    with pytest.raises(SecretUnavailable):
        manager.apply(plan["id"], plan["version"], True)
    assert path.read_bytes() == before
    native = path.read_text()
    assert "synthetic-only-value-other" in native
    assert manager.host_state("codex")["selection"]["provider_id"] == second["provider"]["id"]
    assert_write_is_not_loaded_or_callable(manager.host_state("codex"))


def test_provider_key_rotate_drops_old_ref_and_locked_rotate_keeps_current(manager):
    created = manager.setup_provider(setup())
    provider = created["provider"]
    old_ref = manager.get("provider", provider["id"])["credential_ref"]
    rotated = manager.credential("provider", provider["id"], "synthetic-rotated-value")
    new_ref = manager.get("provider", provider["id"])["credential_ref"]
    assert new_ref != old_ref and old_ref not in manager.vault.values
    assert manager.vault.values[new_ref] == "synthetic-rotated-value"
    assert "synthetic-only-value-7z9" not in manager.vault.values.values()
    manager.vault.locked = True
    with pytest.raises(SecretUnavailable):
        manager.credential("provider", provider["id"], "synthetic-should-not-rotate")
    manager.vault.locked = False
    assert manager.get("provider", provider["id"])["credential_ref"] == new_ref
    assert manager.vault.values[new_ref] == "synthetic-rotated-value"
    assert rotated["has_credential"] is True
    assert "synthetic-should-not-rotate" not in manager.vault.values.values()
