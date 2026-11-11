"""Management acceptance uses temporary homes and an injected fake vault only."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sqlite3
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.config_transactions import ConfigurationError, ConfigurationTransactions, digest
from app.integrations.secret_store import SecretUnavailable
from app.services.management_service import ManagementService, ensure_schema


class FakeVault:
    def __init__(self):
        self.values = {}
        self.locked = False
        self.next_id = 0

    def put(self, value):
        if self.locked:
            raise SecretUnavailable("locked")
        if not isinstance(value, str) or not value:
            raise ValueError("empty")
        self.next_id += 1
        ref = "keychain:senrelay:" + f"{self.next_id:032x}"
        self.values[ref] = value
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
def manager(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return ManagementService(tmp_path / "service" / "test.db", home, FakeVault())


def mcp(manager, name="Test MCP"):
    return manager.save("asset", {"name": name, "kind": "mcp", "config": {"transport": "stdio", "command": "test-server", "args": ["--stdio"]}})


def test_schema_does_not_commit_callers_transaction(tmp_path):
    conn = sqlite3.connect(tmp_path / "test.db")
    conn.execute("CREATE TABLE baseline (id INTEGER)")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO baseline VALUES(1)")
    ensure_schema(conn)
    assert conn.in_transaction
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM baseline").fetchone()[0] == 0
    conn.close()


def test_provider_credential_not_in_database_exports_or_api(manager):
    provider = manager.save("provider", {"name": "Local", "base_url": "http://127.0.0.1:9001/v1", "protocol": "openai", "model_map": {"requested": "actual"}})
    response = manager.credential("provider", provider["id"], "sk-test-secret-never-persist")
    assert response["has_credential"]
    assert manager.vault.get(response["credential_ref"]) == "sk-test-secret-never-persist"
    assert "sk-test-secret" not in manager.db_path.read_bytes().decode("utf-8", errors="ignore")
    exported = json.dumps(manager.export())
    assert "keychain:senrelay:" not in exported
    assert "sk-test-secret" not in exported
    model = manager.save("model", {"name": "Model", "provider_id": provider["id"], "model_id": "actual"})
    binding = manager.save("binding", {"provider_id": provider["id"], "model_id": model["id"], "host_id": "codex"})
    assert binding["model_id"] == model["id"]
    with pytest.raises(ConfigurationError):
        manager.delete("provider", provider["id"])
    manager.vault.locked = True
    with pytest.raises(SecretUnavailable):
        manager.credential("provider", provider["id"], "replacement")
    assert manager.get("provider", provider["id"])["credential_ref"] == response["credential_ref"]


def test_native_json_install_preserves_foreign_settings_and_owned_uninstall(manager):
    path = manager.home / ".claude.json"
    path.write_text(json.dumps({"userSetting": 7, "mcpServers": {"foreign": {"command": "keep"}}}))
    asset = mcp(manager)
    plan = manager.plan({"host_id": "claude-code", "asset_ids": [asset["id"]]})
    assert not (manager.home / ".claude" / "skills").exists()
    applied = manager.apply(plan["id"], 1, True)
    assert applied["status"] == "applied"
    assert applied["payload"]["readback"] == "verified"
    assert applied["payload"]["host_reload"] == "not_run"
    data = json.loads(path.read_text())
    assert data["userSetting"] == 7 and data["mcpServers"]["foreign"]["command"] == "keep"
    uninstall = manager.plan({"host_id": "claude-code", "asset_ids": [asset["id"]], "operation": "uninstall"})
    manager.apply(uninstall["id"], 1, True)
    assert json.loads(path.read_text()) == {"userSetting": 7, "mcpServers": {"foreign": {"command": "keep"}}}


def test_plan_rejects_external_edits_and_stale_assets(manager):
    asset = mcp(manager)
    plan = manager.plan({"host_id": "cursor", "asset_ids": [asset["id"]]})
    path = manager.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text('{"external":true}')
    with pytest.raises(ConfigurationError, match="changed since preview"):
        manager.apply(plan["id"], 1, True)
    assert path.read_text() == '{"external":true}'
    fresh = manager.plan({"host_id": "cursor", "asset_ids": [asset["id"]]})
    manager.save("asset", {"name": "Updated"}, asset["id"], 1)
    with pytest.raises(ConfigurationError, match="changed since preview"):
        manager.apply(fresh["id"], 1, True)


def test_plan_uninstall_rejects_unowned_or_modified(manager):
    asset = mcp(manager)
    with pytest.raises(ConfigurationError, match="owned"):
        manager.plan({"host_id": "cursor", "asset_ids": [asset["id"]], "operation": "uninstall"})
    plan = manager.plan({"host_id": "cursor", "asset_ids": [asset["id"]]})
    manager.apply(plan["id"], 1, True)
    path = manager.home / ".cursor/mcp.json"
    data = json.loads(path.read_text())
    key = next(iter(data["mcpServers"]))
    data["mcpServers"][key]["command"] = "user-edited"
    path.write_text(json.dumps(data))
    with pytest.raises(ConfigurationError, match="externally edited"):
        manager.plan({"host_id": "cursor", "asset_ids": [asset["id"]], "operation": "uninstall"})
    with pytest.raises(ConfigurationError, match="Uninstall"):
        manager.delete("asset", asset["id"])


def test_codex_toml_preserves_comments_and_other_servers(manager):
    path = manager.home / ".codex/config.toml"
    path.parent.mkdir()
    original = '# user comment\nmodel = "existing"\n[mcp_servers.foreign]\ncommand="keep"\n'
    path.write_text(original)
    asset = mcp(manager)
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    manager.apply(plan["id"], 1, True)
    assert path.read_text().startswith(original)
    import tomllib
    parsed = tomllib.loads(path.read_text())
    assert parsed["model"] == "existing"
    assert len(parsed["mcp_servers"]) == 2
    # Reapplying the same plan is idempotent and does not duplicate entries.
    manager.apply(plan["id"], 1, True)
    assert len(tomllib.loads(path.read_text())["mcp_servers"]) == 2


def test_skill_zip_rejects_paths_symlinks_and_does_not_execute(manager):
    def archive(files):
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as z:
            for name, content in files.items():
                z.writestr(name, content)
        return base64.b64encode(out.getvalue()).decode()
    with pytest.raises(ConfigurationError):
        manager.import_objects({"format": "skill-zip", "data": archive({"SKILL.md": "safe", "../escape": "bad"})})
    result = manager.import_objects({"format": "skill-zip", "data": archive({"SKILL.md": "# Skill", "scripts/never.sh": "touch /definitely-not-executed"})})
    skill = result["imported"][0]
    plan = manager.plan({"host_id": "codex", "asset_ids": [skill["id"]]})
    manager.apply(plan["id"], 1, True)
    target = manager.home / ".codex/skills" / skill["id"] / "scripts/never.sh"
    assert target.read_text().startswith("touch ")
    assert target.stat().st_mode & 0o777 == 0o600
    evil = io.BytesIO()
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("SKILL.md", "# Safe")
        entry = zipfile.ZipInfo("link")
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        z.writestr(entry, "/tmp/elsewhere")
    with pytest.raises(ConfigurationError):
        manager.import_objects({"format": "skill-zip", "data": base64.b64encode(evil.getvalue()).decode()})


def test_symlink_target_and_jsonc_are_rejected(manager, tmp_path):
    external = tmp_path / "outside"
    external.mkdir()
    (manager.home / ".cursor").symlink_to(external, target_is_directory=True)
    asset = mcp(manager)
    with pytest.raises(ConfigurationError, match="Symlink"):
        manager.plan({"host_id": "cursor", "asset_ids": [asset["id"]]})
    (manager.home / ".cursor").unlink()
    (manager.home / ".cursor").mkdir()
    (manager.home / ".cursor/mcp.json").write_text('{// comment\n"mcpServers":{}}')
    with pytest.raises(ConfigurationError, match="JSONC"):
        manager.plan({"host_id": "cursor", "asset_ids": [asset["id"]]})


def test_multi_file_failure_has_durable_recovery_and_preserves_external_edit(manager, monkeypatch):
    skill = manager.save("asset", {"name": "Two files", "kind": "skill", "files": {"SKILL.md": "# Skill", "detail.md": "detail"}})
    plan = manager.plan({"host_id": "codex", "asset_ids": [skill["id"]]})
    real_atomic = manager.transactions._atomic
    calls = []
    def failing(path, data):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("simulated disk full")
        real_atomic(path, data)
    monkeypatch.setattr(manager.transactions, "_atomic", failing)
    with pytest.raises(ConfigurationError, match="recovery"):
        manager.apply(plan["id"], 1, True)
    persisted = manager.transactions.get(plan["id"])
    assert persisted["status"] == "recovery_required"
    first = manager.home / ".codex/skills" / skill["id"] / "SKILL.md"
    first.write_text("external edit after failure")
    monkeypatch.setattr(manager.transactions, "_atomic", real_atomic)
    with pytest.raises(ConfigurationError, match="externally edited"):
        manager.transactions.recover(plan["id"], True)
    assert first.read_text() == "external edit after failure"
    first.write_text("# Skill")
    recovered = manager.transactions.recover(plan["id"], True)
    assert recovered["status"] == "recovered"
    assert not first.exists()
    assert manager.transactions.recover(plan["id"], True)["status"] == "recovered"


def test_provider_apply_resolves_secret_only_in_native_target(manager):
    provider = manager.save("provider", {"name": "Anthropic", "base_url": "https://example.test", "protocol": "anthropic"})
    provider = manager.credential("provider", provider["id"], "sk-native-secret-value")
    plan = manager.plan({"host_id": "claude-code", "provider_id": provider["id"]})
    assert "sk-native-secret-value" not in json.dumps(plan)
    manager.apply(plan["id"], 1, True)
    path = manager.home / ".claude/settings.json"
    assert json.loads(path.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-native-secret-value"
    assert "sk-native-secret-value" not in manager.db_path.read_bytes().decode("utf-8", errors="ignore")
    assert path.stat().st_mode & 0o777 == 0o600
    uninstall = manager.plan({"host_id": "claude-code", "provider_id": provider["id"], "operation": "uninstall"})
    manager.apply(uninstall["id"], 1, True)
    assert json.loads(path.read_text())["env"] == {}


def test_accounts_cannot_fake_oauth_and_explicit_local_api_key(manager):
    account = manager.save("account", {"name": "Official login", "kind": "oauth"})
    assert account["status"] == "unsupported"
    assert manager.account_action(account["id"], "authorize")["authorization_url"] is None
    with pytest.raises(ConfigurationError):
        manager.credential("account", account["id"], "secret")
    api = manager.save("account", {"name": "API key", "kind": "api_key"})
    configured = manager.credential("account", api["id"], "private-key")
    assert configured["status"] == "configured"
    logged_out = manager.account_action(api["id"], "logout")
    assert not logged_out["credential_ref"] and logged_out["status"] == "logged_out"
    assert not manager.vault.values


def test_export_import_relinks_entities_and_reports_unknown_migration_fields(manager, tmp_path):
    provider = manager.save("provider", {"name": "P", "base_url": "https://example.test/v1", "protocol": "openai"})
    asset = mcp(manager)
    manager.save("bundle", {"name": "B", "provider_id": provider["id"], "asset_ids": [asset["id"]], "host_ids": ["codex"]})
    other_home = tmp_path / "other"
    other_home.mkdir()
    other = ManagementService(tmp_path / "other.db", other_home, FakeVault())
    result = other.import_objects({"format": "senrelay", "data": manager.export()})
    assert not result["unsupported"]
    assert other.list("bundle")[0]["provider_id"] == other.list("provider")[0]["id"]
    assert other.list("bundle")[0]["asset_ids"] == [other.list("asset")[0]["id"]]
    result = other.import_objects({"format": "cc-switch", "data": {"unknownScript": "do not run", "mcpServers": {"Test": {"command": "server"}}}})
    assert len(result["imported"]) == 1 and result["unsupported"][0]["field"] == "unknownScript"


def test_management_router_contract(manager, monkeypatch):
    from app.routers import manage
    from app.services import management_service
    monkeypatch.setattr(management_service, "service", lambda: manager)
    app = FastAPI()
    app.include_router(manage.router)
    client = TestClient(app)
    result = client.post("/api/manage/providers", json={"name": "P", "protocol": "openai", "base_url": "https://example.test/v1"})
    assert result.status_code == 200
    provider = result.json()
    assert client.post(f"/api/manage/providers/{provider['id']}/credential", json={"secret": "sk-no-response-leak"}).status_code == 200
    exported = client.get("/api/manage/providers/export")
    assert exported.status_code == 200
    assert "keychain:senrelay:" not in exported.text
    assert "sk-no-response-leak" not in client.get("/api/manage/providers").text
    asset = client.post("/api/manage/assets", json={"kind": "mcp", "name": "M", "config": {"command": "server"}}).json()
    plan = client.post("/api/manage/plans", json={"host_id": "cursor", "asset_ids": [asset["id"]]}).json()
    assert "specs" not in plan["payload"]["files"][0]
    assert client.post(f"/api/manage/plans/{plan['id']}/apply", json={"confirmed": False, "expected_version": 1}).status_code == 422
    applied = client.post(f"/api/manage/plans/{plan['id']}/apply", json={"confirmed": True, "expected_version": 1})
    assert applied.status_code == 200 and applied.json()["status"] == "applied"
    desktop = next(host for host in client.get("/api/manage/hosts").json() if host["id"] == "codex-desktop")
    assert desktop["configuration_status"] == "supported_format"
    assert desktop["verification_status"] == "not_verified"
    assert "Desktop consumption" in desktop["configuration_notice"]


def test_opaque_native_credentials_never_enter_plan_database_diff_or_exports(manager):
    path = manager.home / ".claude.json"
    path.write_text(json.dumps({"env": {"ANTHROPIC_AUTH_TOKEN": "syntheticvalue", "MY_KEY": "another-opaque-value"}, "mcpServers": {"foreign": {"headers": {"X-Custom": "opaque-unknown-value"}, "args": ["header-other-secret"]}}}))
    asset = mcp(manager)
    plan = manager.plan({"host_id": "claude-code", "asset_ids": [asset["id"]]})
    for original in ("syntheticvalue", "another-opaque-value", "opaque-unknown-value", "header-other-secret"):
        assert original not in json.dumps(plan)
        assert original not in manager.db_path.read_bytes().decode("utf-8", errors="ignore")
        assert original not in json.dumps(manager.export())
    manager.apply(plan["id"], 1, True)
    assert json.loads(path.read_text())["env"]["ANTHROPIC_AUTH_TOKEN"] == "syntheticvalue"
    assert "syntheticvalue" not in json.dumps(manager.transactions.get(plan["id"]))
    with pytest.raises(ConfigurationError, match="secret|credential"):
        manager.save("asset", {"kind": "mcp", "name": "Bad", "config": {"command": "server", "env": {"CUSTOM_AUTH": "opaque-secret"}}})
    with pytest.raises(ConfigurationError, match="secret|credential"):
        manager.save("asset", {"kind": "mcp", "name": "Bad", "config": {"command": "server", "args": ["--token", "opaque-secret"]}})


def test_local_skill_source_update_removes_only_owned_obsolete_files(manager, tmp_path):
    source = tmp_path / "selected-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# First")
    (source / "obsolete.md").write_text("old")
    asset = manager.import_objects({"format": "skill-local", "data": str(source)})["imported"][0]
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    manager.apply(plan["id"], 1, True)
    target = manager.home / ".codex/skills" / asset["id"]
    (target / "user-added.md").write_text("not ours")
    (source / "obsolete.md").unlink()
    (source / "SKILL.md").write_text("# Second")
    update = manager.import_objects({"format": "skill-local", "data": str(source), "target_id": asset["id"], "expected_version": asset["version"]})["imported"][0]
    plan = manager.plan({"host_id": "codex", "asset_ids": [update["id"]]})
    manager.apply(plan["id"], 1, True)
    assert (target / "SKILL.md").read_text() == "# Second"
    assert not (target / "obsolete.md").exists()
    uninstall = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]], "operation": "uninstall"})
    manager.apply(uninstall["id"], 1, True)
    assert (target / "user-added.md").read_text() == "not ours"
    assert not (target / "SKILL.md").exists()


def test_github_import_rejects_unpinned_or_credential_urls_without_network(manager):
    for url, ref in (("https://github.com/owner/repo", "main"), ("https://user:secret@github.com/owner/repo", "a" * 40), ("http://evil.test/owner/repo", "a" * 40)):
        with pytest.raises(ConfigurationError):
            manager.import_objects({"format": "skill-github", "data": {"url": url, "ref": ref}})


def test_mcp_probe_is_explicit_no_tools_call_and_keeps_host_unknown(manager):
    import asyncio
    import httpx
    asset = manager.save("asset", {"kind": "mcp", "name": "HTTP", "config": {"transport": "http", "url": "http://127.0.0.1:9876/mcp"}})
    calls = []
    def mock(request):
        data = json.loads(request.content)
        calls.append(data["method"])
        if data["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "2025-03-26", "capabilities": {}}}, headers={"Mcp-Session-Id": "session"})
        if data["method"] == "notifications/initialized":
            return httpx.Response(202)
        assert request.headers["Mcp-Session-Id"] == "session"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "read_file"}]}})
    with pytest.raises(ConfigurationError, match="Confirm"):
        asyncio.run(manager.probe_asset(asset["id"], False, httpx.MockTransport(mock)))
    assert not calls
    result = asyncio.run(manager.probe_asset(asset["id"], True, httpx.MockTransport(mock)))
    assert result["status"] == "verified_tools_list"
    assert result["host_loaded"] == "not_verified"
    assert calls == ["initialize", "notifications/initialized", "tools/list"]


def test_asset_history_restore_and_host_discovery_never_copy_opaque_values(manager):
    asset = mcp(manager)
    manager.save("asset", {"name": "Second"}, asset["id"], 1)
    assert [entry["version"] for entry in manager.asset_versions(asset["id"])] == [2, 1]
    restored = manager.restore_asset(asset["id"], 1, 2)
    assert restored["version"] == 3 and restored["name"] == asset["name"]
    path = manager.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"Existing": {"command": "node", "args": ["opaque-argument"], "env": {"CUSTOM": "opaque-env"}, "headers": {"X-Custom": "opaque-header"}}}}))
    discovery = manager.discover_assets("cursor")
    assert discovery["status"] == "parsed"
    assert discovery["candidates"][0]["config_preview"] == {"command": "node"}
    assert discovery["candidates"][0]["import_status"] == "manual_review_required"
    assert not any(value in json.dumps(discovery) for value in ("opaque-argument", "opaque-env", "opaque-header"))
    manager.delete("asset", asset["id"])
    with manager.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_asset_versions WHERE asset_id=?", (asset["id"],)).fetchone()[0] == 0


def test_delete_claim_prevents_new_credential_orphan_and_cleanup_is_recoverable(manager, monkeypatch):
    provider = manager.save("provider", {"name": "P", "protocol": "openai", "base_url": "https://example.test"})
    provider = manager.credential("provider", provider["id"], "old-secret")
    original_delete = manager.vault.delete
    attempted = []
    def concurrent_delete(reference):
        try:
            manager.credential("provider", provider["id"], "new-secret")
        except ConfigurationError as exc:
            attempted.append(exc.status_code)
        original_delete(reference)
    monkeypatch.setattr(manager.vault, "delete", concurrent_delete)
    result = manager.delete("provider", provider["id"], provider["version"])
    assert result["deleted"] and attempted == [404]
    assert not manager.vault.values
    assert not manager.list("provider")
    other = manager.save("provider", {"name": "Q", "protocol": "openai", "base_url": "https://example.test"})
    other = manager.credential("provider", other["id"], "recover-later")
    manager.vault.locked = True
    result = manager.delete("provider", other["id"], other["version"])
    assert result["credential_cleanup"][0]["status"] == "pending"
    with manager.connect() as conn:
        receipt = conn.execute("SELECT * FROM management_secret_cleanup WHERE status='pending'").fetchone()
        assert receipt["credential_ref"] == other["credential_ref"]
    manager.vault.locked = False
    monkeypatch.setattr(manager.vault, "delete", original_delete)
    assert manager.retry_credential_cleanup(receipt["id"])[0]["status"] == "completed"
    assert not manager.vault.values


def test_toml_credential_value_cannot_inject_unreviewed_server(manager):
    import tomllib
    injected = 'opaque"}\n[mcp_servers.unreviewed]\ncommand="synthetic-command"\n#'
    reference = manager.vault.put(injected)
    asset = manager.save("asset", {"kind": "mcp", "name": "Safe", "config": {"command": "safe-server", "env": {"API_KEY": "{{credential:" + reference + "}}"}}})
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    assert injected not in json.dumps(plan)
    result = manager.apply(plan["id"], 1, True)
    assert result["status"] == "applied"
    parsed = tomllib.loads((manager.home / ".codex/config.toml").read_text())
    assert "unreviewed" not in parsed["mcp_servers"]
    assert len(parsed["mcp_servers"]) == 1
    server = next(iter(parsed["mcp_servers"].values()))
    assert server["command"] == "safe-server"
    assert server["env"]["API_KEY"] == injected


def test_explicit_owned_provider_switch_never_reuses_previous_secret(manager):
    first = manager.save("provider", {"name": "First", "protocol": "anthropic", "base_url": "https://first.example.test"})
    first = manager.credential("provider", first["id"], "first-secret")
    plan = manager.plan({"host_id": "claude-code", "provider_id": first["id"]})
    manager.apply(plan["id"], 1, True)
    second = manager.save("provider", {"name": "Second", "protocol": "anthropic", "base_url": "https://second.example.test"})
    with pytest.raises(ConfigurationError):
        manager.plan({"host_id": "claude-code", "provider_id": second["id"]})
    switch = manager.plan({"host_id": "claude-code", "provider_id": second["id"], "replace_provider_id": first["id"]})
    manager.apply(switch["id"], 1, True)
    config = json.loads((manager.home / ".claude/settings.json").read_text())
    assert config["env"]["ANTHROPIC_BASE_URL"] == second["base_url"]
    assert "ANTHROPIC_AUTH_TOKEN" not in config["env"]
    assert "first-secret" not in json.dumps(switch)


def test_native_codex_activation_switch_and_restore_keep_original_defaults_and_foreign_fields(manager):
    import tomllib
    path = manager.home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('# user comment\nmodel = "original-model"\nmodel_provider = "original-provider"\n[features]\nkeep = true\n')
    first = manager.save("provider", {"name": "First", "protocol": "openai", "base_url": "https://first.example.test/v1", "model_map": {"chosen": "first-model"}})
    first = manager.credential("provider", first["id"], "first-native-key")
    plan = manager.plan({"host_id": "codex", "provider_id": first["id"], "activate_provider": True, "model_id": "chosen"})
    manager.apply(plan["id"], 1, True)
    parsed = tomllib.loads(path.read_text())
    assert parsed["model"] == "first-model"
    assert parsed["model_provider"] == "senrelay_" + first["id"].replace("provider_", "")
    assert parsed["model_providers"][parsed["model_provider"]]["http_headers"]["Authorization"] == "Bearer first-native-key"
    second = manager.save("provider", {"name": "Second", "protocol": "openai", "base_url": "https://second.example.test/v1", "model_map": {"chosen": "second-model"}})
    switch = manager.plan({"host_id": "codex", "provider_id": second["id"], "activate_provider": True, "model_id": "chosen"})
    manager.apply(switch["id"], 1, True)
    assert tomllib.loads(path.read_text())["model"] == "second-model"
    with pytest.raises(ConfigurationError):
        manager.delete("provider", second["id"])
    restore = manager.plan({"host_id": "codex", "operation": "restore"})
    manager.apply(restore["id"], 1, True)
    parsed = tomllib.loads(path.read_text())
    assert parsed["model"] == "original-model" and parsed["model_provider"] == "original-provider"
    assert parsed["features"]["keep"] and "# user comment" in path.read_text()
    assert "first-native-key" not in json.dumps(manager.transactions.list())


def test_explicit_opencode_bundle_activation_and_restore_are_separate_plans(manager):
    path = manager.home / ".config/opencode/opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "original/native", "permission": {"keep": "ask"}}))
    provider = manager.save("provider", {"name": "P", "protocol": "openai", "base_url": "https://example.test/v1", "model_map": {"selected": "native"}})
    asset = mcp(manager)
    bundle = manager.save("bundle", {"name": "Project setup", "asset_ids": [asset["id"]], "provider_id": provider["id"], "host_ids": ["opencode"]})
    plan = manager.plan({"host_id": "opencode", "bundle_id": bundle["id"], "activate_provider": True, "model_id": "selected"})
    assert json.loads(path.read_text())["model"] == "original/native"
    manager.apply(plan["id"], 1, True)
    applied = json.loads(path.read_text())
    assert applied["model"] == provider["id"] + "/native" and applied["permission"]["keep"] == "ask"
    restore = manager.plan({"host_id": "opencode", "operation": "restore"})
    manager.apply(restore["id"], 1, True)
    assert json.loads(path.read_text())["model"] == "original/native"


def test_stdio_mcp_probe_initializes_synthetic_server_only_after_confirmation(manager, tmp_path):
    import asyncio
    import sys
    server = tmp_path / "synthetic_mcp.py"
    server.write_text('''import sys,json
for line in sys.stdin:
    request=json.loads(line)
    if "id" not in request: continue
    method=request.get("method")
    if method=="initialize": result={"protocolVersion":"2025-03-26","capabilities":{"tools":{}},"serverInfo":{"name":"synthetic","version":"1"}}
    elif method=="tools/list": result={"tools":[{"name":"synthetic_read","inputSchema":{"type":"object"}}]}
    else: result={}
    print(json.dumps({"jsonrpc":"2.0","id":request["id"],"result":result}),flush=True)
''')
    asset = manager.save("asset", {"kind": "mcp", "name": "Synthetic", "config": {"transport": "stdio", "command": sys.executable, "args": [str(server)]}})
    with pytest.raises(ConfigurationError, match="Confirm"):
        asyncio.run(manager.probe_asset(asset["id"], False))
    result = asyncio.run(manager.probe_asset(asset["id"], True))
    assert result["status"] == "verified_tools_list" and result["tools"] == ["synthetic_read"]
    assert result["command"] == sys.executable and result["host_loaded"] == "not_verified"


def test_registry_skill_requires_pinned_archive_and_reuses_safe_installer(manager, monkeypatch):
    import hashlib
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("SKILL.md", "# Registry Skill")
    raw_zip = archive.getvalue()
    manifest = {"name": "Registry fixture", "version": "1", "source": {"type": "zip", "url": "https://registry.example.test/skill.zip", "sha256": hashlib.sha256(raw_zip).hexdigest()}}
    def download(url, limit):
        return json.dumps(manifest).encode() if url.endswith("manifest.json") else raw_zip
    monkeypatch.setattr(manager, "_download_source", download)
    imported = manager.import_objects({"format": "skill-registry", "data": {"manifest_url": "https://registry.example.test/manifest.json"}})["imported"][0]
    assert imported["source"]["type"] == "registry" and imported["files"]["SKILL.md"] == "# Registry Skill"
    manifest["source"]["sha256"] = "0" * 64
    with pytest.raises(ConfigurationError, match="hash"):
        manager.import_objects({"format": "skill-registry", "data": {"manifest_url": "https://registry.example.test/manifest.json"}})
    manifest["install_script"] = "never execute"
    with pytest.raises(ConfigurationError, match="executable"):
        manager.import_objects({"format": "skill-registry", "data": {"manifest_url": "https://registry.example.test/manifest.json"}})


def test_structured_native_transactions_hash_actual_nodes_and_hide_foreign_secrets(manager):
    from app.integrations.structured_config import parse_document
    path = manager.home / ".synthetic/config.yaml"
    path.parent.mkdir()
    path.write_text('# keep comment\nforeign:\n  value: opaque-native-secret\n')
    reference = manager.vault.put('opaque"\nvalue')
    spec = {"path": str(path), "kind": "yaml", "pointer": ["providers", "owned"], "owner": "provider_test", "value": {"api_key": "{{credential:" + reference + "}}"}}
    plan = manager.transactions.create([spec])
    assert "opaque-native-secret" not in json.dumps(plan)
    manager.transactions.apply(plan["id"], 1, True)
    assert parse_document("yaml", path.read_text())["providers"]["owned"]["api_key"] == 'opaque"\nvalue'
    assert "# keep comment" in path.read_text()
    removal = manager.transactions.create([{**spec, "operation": "uninstall"}])
    manager.transactions.apply(removal["id"], 1, True)
    assert parse_document("yaml", path.read_text())["foreign"]["value"] == "opaque-native-secret"


def test_bundle_restore_removes_its_new_assets_and_preserves_foreign_content(manager):
    path = manager.home / ".config/opencode/opencode.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"model": "original/native", "mcp": {"foreign": {"type": "local", "command": ["keep"]}}}))
    provider = manager.save("provider", {"name": "P", "protocol": "openai", "base_url": "https://example.test/v1", "model_map": {"selected": "native"}})
    asset = mcp(manager)
    skill = manager.save("asset", {"kind": "skill", "name": "Bundle skill", "files": {"SKILL.md": "# Bundle"}})
    bundle = manager.save("bundle", {"name": "Complete bundle", "asset_ids": [asset["id"], skill["id"]], "provider_id": provider["id"], "host_ids": ["opencode"]})
    plan = manager.plan({"host_id": "opencode", "bundle_id": bundle["id"], "activate_provider": True, "model_id": "selected"})
    manager.apply(plan["id"], 1, True)
    target = manager.home / ".config/opencode/skills" / skill["id"] / "SKILL.md"
    assert target.exists()
    restore = manager.plan({"host_id": "opencode", "operation": "restore"})
    manager.apply(restore["id"], 1, True)
    data = json.loads(path.read_text())
    assert data["model"] == "original/native"
    assert set(data["mcp"]) == {"foreign"}
    assert not data["provider"]
    assert not target.exists()


def test_hermes_provider_identity_survives_other_owned_list_entry_deletion(manager):
    from app.integrations.structured_config import parse_document
    providers = []
    for number in (1, 2):
        provider = manager.save("provider", {"name": f"P{number}", "protocol": "openai", "base_url": f"https://p{number}.example.test/v1", "model_map": {"m": f"native-{number}"}})
        provider = manager.credential("provider", provider["id"], f"private-{number}")
        plan = manager.plan({"host_id": "hermes", "provider_id": provider["id"]})
        manager.apply(plan["id"], 1, True)
        providers.append(provider)
    path = manager.home / ".hermes/config.yaml"
    assert len(parse_document("yaml", path.read_text())["custom_providers"]) == 2
    for provider in providers:
        removal = manager.plan({"host_id": "hermes", "provider_id": provider["id"], "operation": "uninstall"})
        manager.apply(removal["id"], 1, True)
    assert parse_document("yaml", path.read_text())["custom_providers"] == []


def test_toml_model_activation_preserves_real_inline_comment_and_hash_in_string(manager):
    import tomllib
    path = manager.home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('model = "old#model" # keep actual comment\nmodel_provider="old" # provider note\n')
    provider = manager.save("provider", {"name": "P", "protocol": "openai", "base_url": "https://example.test/v1", "model_map": {"selected": "new"}})
    plan = manager.plan({"host_id": "codex", "provider_id": provider["id"], "activate_provider": True, "model_id": "selected"})
    manager.apply(plan["id"], 1, True)
    assert '# keep actual comment' in path.read_text() and '# provider note' in path.read_text()
    restore = manager.plan({"host_id": "codex", "operation": "restore"})
    manager.apply(restore["id"], 1, True)
    assert tomllib.loads(path.read_text())["model"] == "old#model"
    assert '# keep actual comment' in path.read_text()


def test_export_mcp_with_credentials_reimport_roundtrip(manager, tmp_path):
    ref1 = manager.vault.put("secret-mcp-key")
    ref2 = manager.vault.put("secret-auth-bearer")
    asset = manager.save("asset", {
        "name": "CredMCP",
        "kind": "mcp",
        "config": {
            "transport": "http",
            "url": "https://api.example.test",
            "headers": {"Authorization": "Bearer {{credential:" + ref2 + "}}", "X-Normal": "ok"},
            "env": {"API_KEY": "{{credential:" + ref1 + "}}", "ENV_NORMAL": "value"},
        }
    })
    exported = manager.export()
    assert exported["format"] == "senrelay-management"
    assert exported["version"] == 1
    assert exported["managed_credentials_excluded"] is True
    assert exported["embedded_text_review_required"] is True
    assert bool(exported.get("review_notice")) is True
    assert "contains_secrets" not in exported
    assert "keychain:senrelay:" not in json.dumps(exported)
    assert "[CREDENTIAL REQUIRED]" not in json.dumps(exported)

    mcp_obj = next(o["data"] for o in exported["objects"] if o["entity_kind"] == "asset")
    assert mcp_obj.get("credential_reattach_required") is True
    assert "API_KEY" not in mcp_obj["config"].get("env", {})
    assert mcp_obj["config"].get("env", {}).get("ENV_NORMAL") == "value"
    assert "Authorization" not in mcp_obj["config"].get("headers", {})
    assert mcp_obj["config"].get("headers", {}).get("X-Normal") == "ok"

    other_home = tmp_path / "other_home"
    other_home.mkdir()
    other = ManagementService(tmp_path / "other.db", other_home, FakeVault())
    import_result = other.import_objects({"format": "senrelay", "data": exported})
    assert import_result["unsupported"] == []
    assert len(import_result["imported"]) == 1
    assert import_result["managed_credentials_excluded"] is True
    assert import_result["embedded_text_review_required"] is True
    assert "contains_secrets" not in import_result

    imported_asset = other.list("asset")[0]
    assert imported_asset["name"] == "CredMCP"
    assert imported_asset.get("credential_reattach_required") is True
    assert imported_asset["config"]["env"] == {"ENV_NORMAL": "value"}
    assert imported_asset["config"]["headers"] == {"X-Normal": "ok"}


def test_export_import_provider_account_cycle_and_dependency_remapping(manager, tmp_path):
    provider = manager.save("provider", {"name": "LocalP", "protocol": "openai", "base_url": "https://local.example.test/v1"})
    manager.credential("provider", provider["id"], "sk-local-secret")
    account = manager.save("account", {"name": "LocalA", "kind": "api_key", "provider_id": provider["id"]})
    manager.credential("account", account["id"], "secret-account-key")
    provider = manager.get("provider", provider["id"])
    provider = manager.save("provider", {"account_id": account["id"]}, entity_id=provider["id"], expected_version=provider["version"])

    model = manager.save("model", {"name": "CustomModel", "provider_id": provider["id"], "model_id": "gpt-custom"})
    binding = manager.save("binding", {"host_id": "codex", "provider_id": provider["id"], "model_id": model["id"]})
    asset = mcp(manager, name="Mcp1")
    bundle = manager.save("bundle", {"name": "Bundle1", "provider_id": provider["id"], "asset_ids": [asset["id"]], "host_ids": ["codex"]})

    exported = manager.export()
    assert "keychain:senrelay:" not in json.dumps(exported)
    assert "sk-local-secret" not in json.dumps(exported)
    assert "secret-account-key" not in json.dumps(exported)
    account_exported = next(o["data"] for o in exported["objects"] if o["entity_kind"] == "account")
    assert account_exported["status"] == "unconfigured"

    other_home = tmp_path / "other_cycle_home"
    other_home.mkdir()
    other = ManagementService(tmp_path / "other_cycle.db", other_home, FakeVault())
    result = other.import_objects({"format": "senrelay", "data": exported})
    assert result["unsupported"] == []
    assert len(result["imported"]) == 6

    other_p = other.list("provider")[0]
    other_a = other.list("account")[0]
    other_m = other.list("model")[0]
    other_b = other.list("binding")[0]
    other_ast = other.list("asset")[0]
    other_bun = other.list("bundle")[0]

    assert other_p["account_id"] == other_a["id"]
    assert other_a["provider_id"] == other_p["id"]
    assert other_m["provider_id"] == other_p["id"]
    assert other_b["provider_id"] == other_p["id"]
    assert other_b["model_id"] == other_m["id"]
    assert other_bun["provider_id"] == other_p["id"]
    assert other_bun["asset_ids"] == [other_ast["id"]]
    assert other_a["status"] == "unconfigured"
    assert not other.vault.values


def test_staged_import_resolves_reverse_order_and_multi_pass(manager, tmp_path):
    provider = manager.save("provider", {"name": "PRev", "protocol": "openai", "base_url": "https://prev.example.test/v1"})
    account = manager.save("account", {"name": "ARev", "kind": "api_key", "provider_id": provider["id"]})
    provider = manager.save("provider", {"account_id": account["id"]}, entity_id=provider["id"], expected_version=provider["version"])
    model = manager.save("model", {"name": "MRev", "provider_id": provider["id"], "model_id": "model-rev"})
    binding = manager.save("binding", {"host_id": "cursor", "provider_id": provider["id"], "model_id": model["id"]})
    asset = mcp(manager, name="McpRev")
    bundle = manager.save("bundle", {"name": "BunRev", "provider_id": provider["id"], "asset_ids": [asset["id"]], "host_ids": ["cursor"]})

    exported = manager.export()
    # Reverse the order of objects completely
    reversed_objects = list(reversed(exported["objects"]))
    reverse_payload = {**exported, "objects": reversed_objects}

    other_home = tmp_path / "other_rev_home"
    other_home.mkdir()
    other = ManagementService(tmp_path / "other_rev.db", other_home, FakeVault())
    result = other.import_objects({"format": "senrelay", "data": reverse_payload})
    assert result["unsupported"] == []
    assert len(result["imported"]) == 6

    other_p = other.list("provider")[0]
    other_a = other.list("account")[0]
    assert other_p["account_id"] == other_a["id"]
    assert other_a["provider_id"] == other_p["id"]


def test_export_preserves_prompt_and_skill_text_verbatim(manager, tmp_path):
    prompt_text = "Document authentication separately.\nexport AWS_SECRET_ACCESS_KEY=example_key"
    prompt = manager.save("asset", {"name": "DocPrompt", "kind": "prompt", "content": prompt_text})
    skill_files = {"SKILL.md": "# Guide\nRun `export GITHUB_TOKEN=abc`\n", "script.sh": "echo done\n"}
    skill = manager.save("asset", {"name": "DocSkill", "kind": "skill", "files": skill_files, "install_mode": "copy"})

    exported = manager.export()
    assert exported["embedded_text_review_required"] is True
    assert "contains_secrets" not in exported

    exp_prompt = next(o["data"] for o in exported["objects"] if o["data"]["name"] == "DocPrompt")
    assert exp_prompt["content"] == prompt_text
    exp_skill = next(o["data"] for o in exported["objects"] if o["data"]["name"] == "DocSkill")
    assert exp_skill["files"] == skill_files

    other_home = tmp_path / "other_text_home"
    other_home.mkdir()
    other = ManagementService(tmp_path / "other_text.db", other_home, FakeVault())
    result = other.import_objects({"format": "senrelay", "data": exported})
    assert result["unsupported"] == []
    imported_prompt = next(a for a in other.list("asset") if a["name"] == "DocPrompt")
    assert imported_prompt["content"] == prompt_text
    imported_skill = next(a for a in other.list("asset") if a["name"] == "DocSkill")
    assert imported_skill["files"] == skill_files


def test_import_rejects_raw_keychain_refs_and_plaintext_credentials(manager):
    with pytest.raises(ConfigurationError) as rejected_reference:
        manager.import_objects({"format": "senrelay", "data": {"format": "senrelay-management", "version": 1, "objects": [{
            "entity_kind": "provider",
            "data": {"name": "Bad", "protocol": "openai", "base_url": "https://bad.example.test", "credential_ref": "keychain:senrelay:0123456789abcdef0123456789abcdef"}
        }]}})
    assert rejected_reference.value.status_code == 422

    with pytest.raises(ConfigurationError) as rejected_secret:
        manager.import_objects({"format": "senrelay", "data": {"format": "senrelay-management", "version": 1, "objects": [{
            "entity_kind": "asset",
            "data": {"name": "Bad", "kind": "mcp", "config": {"command": "server", "env": {"AUTH": "Bearer sk-synthetic12345678"}}}
        }]}})
    assert rejected_secret.value.status_code == 422


def test_config_transactions_f6_f7_f10_regressions(manager, tmp_path):
    # F6: uninstall on json when parent does not exist does not materialize empty container
    json_path = manager.home / ".claude.json"
    json_path.write_text(json.dumps({"unrelated": "ok"}))
    spec_f6 = {"path": str(json_path), "kind": "json", "pointer": ["missing_parent", "target_key"], "owner": "test_owner", "operation": "uninstall", "takeover_selected_field": True}
    plan_f6 = manager.transactions.create([spec_f6])
    manager.transactions.apply(plan_f6["id"], 1, True)
    after_json = json.loads(json_path.read_text())
    assert "missing_parent" not in after_json
    assert after_json == {"unrelated": "ok"}

    # F10: block uninstall when block is absent does not touch or rewrite file
    toml_path = manager.home / ".codex/config.toml"
    original_text = 'model = "m"\n\n\n'
    toml_path.parent.mkdir(parents=True, exist_ok=True)
    toml_path.write_text(original_text)
    spec_f10 = {"path": str(toml_path), "kind": "block", "marker": "absent_marker", "format": "toml", "owner": "test_owner", "operation": "uninstall", "takeover_selected_field": True}
    plan_f10 = manager.transactions.create([spec_f10])
    manager.transactions.apply(plan_f10["id"], 1, True)
    assert toml_path.read_text() == original_text

    # F7: recover validates snapshot containment within state_dir
    outside_snapshot = tmp_path / "outside_snapshot"
    outside_snapshot.write_bytes(b"malicious")
    plan = manager.transactions.create([{"path": str(json_path), "kind": "file", "owner": "test_owner", "operation": "install", "value": "new", "takeover_selected_field": True}])
    # Tamper plan with outside snapshot
    with manager.transactions.connect() as conn:
        plan_row = conn.execute("SELECT * FROM management_plans WHERE id=?", (plan["id"],)).fetchone()
        payload = json.loads(plan_row["payload"])
        payload["files"][0]["snapshot"] = str(outside_snapshot)
        payload["files"][0]["stage"] = "intent"
        conn.execute("UPDATE management_plans SET payload=?, status='recovery_required' WHERE id=?", (json.dumps(payload), plan["id"]))
    with pytest.raises(ConfigurationError) as unsafe_recovery:
        manager.transactions.recover(plan["id"], True)
    assert unsafe_recovery.value.status_code >= 400
    assert json.loads(json_path.read_text()) == {"unrelated": "ok"}


def test_assistant_resource_scope_survives_edits_and_rejects_unknown_hosts(manager):
    item = manager.save('asset', {'name': 'Shared rule', 'kind': 'prompt', 'content': 'Be clear', 'agent_ids': ['codex', 'omp', 'codex']})
    assert item['agent_ids'] == ['codex', 'omp']
    edited = manager.save('asset', {'content': 'Be concise'}, item['id'], item['version'])
    assert edited['agent_ids'] == ['codex', 'omp']
    removed = manager.save('asset', {'agent_ids': []}, item['id'], edited['version'])
    assert removed['agent_ids'] == []
    assert not list(manager.home.rglob('AGENTS.md'))
    for invalid in ('codex', ['unknown'], [123], None):
        with pytest.raises(ConfigurationError):
            manager.save('asset', {'agent_ids': invalid}, item['id'], removed['version'])


def test_import_associates_only_selected_assistant_and_update_preserves_other_references(manager, tmp_path):
    source = tmp_path / 'source'; source.mkdir(); (source / 'SKILL.md').write_text('# Be clear')
    imported = manager.import_objects({'format': 'skill-local', 'data': str(source), 'agent_id': 'codex'})['imported'][0]
    assert imported['agent_ids'] == ['codex']
    shared = manager.save('asset', {'agent_ids': ['codex', 'omp']}, imported['id'], imported['version'])
    updated = manager.import_objects({'format': 'skill-local', 'data': str(source), 'agent_id': 'codex', 'target_id': shared['id'], 'expected_version': shared['version']})['imported'][0]
    assert updated['agent_ids'] == ['codex', 'omp']
    records = manager.import_objects({'format': 'senrelay', 'agent_id': 'omp', 'data': {'objects': [{'entity_kind': 'asset', 'data': {'name': 'Imported rule', 'kind': 'prompt', 'content': 'Clear', 'agent_ids': ['codex']}}]}})
    assert records['imported'][0]['agent_ids'] == ['omp']
    with pytest.raises(ConfigurationError):
        manager.import_objects({'format': 'skill-local', 'data': str(source), 'agent_id': 'unknown'})


def test_agent_import_keeps_bundle_and_binding_usable_for_selected_host(manager):
    result = manager.import_objects({'format': 'senrelay', 'agent_id': 'codex', 'data': {'objects': [
        {'entity_kind': 'provider', 'data': {'id': 'old-provider', 'name': 'Service', 'protocol': 'openai', 'base_url': 'https://example.com/v1'}},
        {'entity_kind': 'bundle', 'data': {'name': 'Setup', 'host_ids': ['omp'], 'provider_id': 'old-provider', 'asset_ids': []}},
        {'entity_kind': 'binding', 'data': {'host_id': 'omp', 'provider_id': 'old-provider'}},
    ]}})
    assert not result['unsupported']
    bundle = next(item for item in result['imported'] if item['entity_kind'] == 'bundle')
    binding = next(item for item in result['imported'] if item['entity_kind'] == 'binding')
    assert bundle['host_ids'] == ['codex']
    assert binding['host_id'] == 'codex'
    assert bundle['provider_id'] == binding['provider_id']
