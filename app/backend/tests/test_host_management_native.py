"""Native-file lifecycle through real management HTTP/SQLite/filesystem consumers.

All paths and credentials are synthetic. No host process is started, no network
request is made, and a verified file readback is never called host acceptance.
"""
from __future__ import annotations

import json
from pathlib import Path
import tomllib

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.structured_config import parse_document
from app.routers import manage
from app.services import management_service
from app.services.management_service import ManagementService
from tests.test_management import FakeVault


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    manager = ManagementService(tmp_path / "data" / "service.db", home, FakeVault())
    monkeypatch.setattr(management_service, "service", lambda: manager)
    app = FastAPI()
    app.include_router(manage.router)
    with TestClient(app) as client:
        yield manager, client


def post(client, path, body):
    response = client.post("/api/manage/" + path, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def preview(client, host, **body):
    return post(client, "plans", {"host_id": host, **body})


def apply(client, plan):
    receipt = post(client, f"plans/{plan['id']}/apply", {"confirmed": True, "expected_version": plan["version"]})
    assert receipt["status"] == "applied"
    assert receipt["payload"]["readback"] == "verified"
    assert receipt["payload"]["host_reload"] == "not_run"
    assert receipt["payload"]["real_request"] == "not_run"
    return receipt


def provider(client, name="Synthetic", protocol="openai", wire="responses"):
    definition = {"name": name, "protocol": protocol, "base_url": "https://synthetic.invalid/v1", "model_map": {"friendly": "native-model"}}
    if protocol == "openai":
        definition.update(wire_api=wire, supported_endpoints=["responses" if wire == "responses" else "chat/completions"])
    value = post(client, "providers", definition)
    post(client, f"providers/{value['id']}/credential", {"secret": "synthetic-native-secret-\"-literal"})
    return value


def write(home, relative, content):
    path = home / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def yaml(path):
    return parse_document("yaml", path.read_text())


def test_omp_provider_install_update_switch_restore_uninstall_preserves_native_fields(env):
    manager, client = env
    models = write(manager.home, ".omp/agent/models.yml", "# keep models comment\nforeign_root: keep\nproviders:\n  foreign:\n    apiKey: opaque-original-native-secret\n    baseUrl: https://foreign.invalid\n    custom: [preserve]\n")
    config = write(manager.home, ".omp/agent/config.yml", "# keep settings comment\nmodelRoles:\n  default: foreign/original:high\n  smol: foreign/small\nretry:\n  fallbackChains: {unrelated: []}\ntheme: {dark: titanium}\n")
    original_models, original_config = yaml(models), yaml(config)
    first = provider(client)
    plan = preview(client, "omp", provider_id=first["id"])
    assert "opaque-original-native-secret" not in json.dumps(plan)
    assert "synthetic-native-secret" not in json.dumps(plan)
    assert yaml(models) == original_models
    refused = client.post(f"/api/manage/plans/{plan['id']}/apply", json={"confirmed": False, "expected_version": 1})
    assert refused.status_code == 422
    assert yaml(models) == original_models
    apply(client, plan)
    native = yaml(models)["providers"][first["id"]]
    assert native["api"] == "openai-responses"
    assert native["apiKey"] == 'synthetic-native-secret-"-literal'
    assert native["models"] == [{"id": "native-model", "name": "friendly"}]
    assert yaml(config) == original_config  # coexist installation is not selection
    assert yaml(models)["providers"]["foreign"] == original_models["providers"]["foreign"]
    assert "# keep models comment" in models.read_text()
    assert models.stat().st_mode & 0o777 == 0o600

    model = post(client, "models", {"name": "Additional", "provider_id": first["id"], "model_id": "additional-native"})
    changed = client.patch(f"/api/manage/providers/{first['id']}", json={"expected_version": first["version"] + 1, "base_url": "https://updated.invalid/v1"})
    assert changed.status_code == 200, changed.text
    apply(client, preview(client, "omp", provider_id=first["id"], activate_provider=True, model_id=model["id"]))
    assert yaml(config)["modelRoles"] == {"default": first["id"] + "/additional-native", "smol": "foreign/small"}
    assert yaml(models)["providers"][first["id"]]["baseUrl"] == "https://updated.invalid/v1"
    assert {row["id"] for row in yaml(models)["providers"][first["id"]]["models"]} == {"native-model", "additional-native"}
    assert client.get("/api/manage/hosts/omp/state").json()["selection"]["status"] == "verified"
    assert client.get("/api/manage/hosts/omp/state").json()["effects"]["host_loaded"] == "not_verified"
    refused = client.post("/api/manage/plans", json={"host_id": "omp", "provider_id": first["id"], "operation": "uninstall"})
    assert refused.status_code == 409 and "model roles" in refused.json()["detail"]

    second = provider(client, "Second", protocol="anthropic")
    apply(client, preview(client, "omp", provider_id=second["id"], activate_provider=True, model_id="native-model"))
    assert yaml(config)["modelRoles"]["default"] == second["id"] + "/native-model"
    apply(client, preview(client, "omp", operation="restore"))
    assert yaml(config) == original_config
    assert "# keep settings comment" in config.read_text()
    for value in (first, second):
        apply(client, preview(client, "omp", provider_id=value["id"], operation="uninstall"))
    assert yaml(models) == original_models
    assert yaml(config) == original_config
    assert "synthetic-native-secret" not in manager.db_path.read_bytes().decode(errors="ignore")
    assert "synthetic-native-secret" not in client.get("/api/manage/export").text


@pytest.mark.parametrize("protocol,wire,api", [("openai", "chat", "openai-completions"), ("anthropic", None, "anthropic-messages"), ("gemini", None, "google-generative-ai")])
def test_omp_native_api_mapping_never_substitutes_another_protocol(env, protocol, wire, api):
    manager, client = env
    value = provider(client, protocol=protocol, wire=wire)
    apply(client, preview(client, "omp", provider_id=value["id"]))
    assert yaml(manager.home / ".omp/agent/models.yml")["providers"][value["id"]]["api"] == api


def test_omp_existing_native_filenames_are_updated_in_place(env):
    manager, client = env
    model_path = write(manager.home, ".omp/agent/models.yaml", '# models alternate\nproviders: {}\nunknown: preserved\n')
    config = write(manager.home, ".omp/agent/config.yaml", "# alternate\nmodelRoles: {default: old/model}\nother: keep\n")
    value = provider(client)
    plan = preview(client, "omp", provider_id=value["id"], activate_provider=True, model_id="native-model")
    assert {item["path"] for item in plan["payload"]["files"]} == {str(model_path), str(config)}
    apply(client, plan)
    document = yaml(model_path)
    assert document["unknown"] == "preserved" and value["id"] in document["providers"]
    assert not (manager.home / ".omp/agent/models.yml").exists()
    assert not (manager.home / ".omp/agent/config.yml").exists()
    apply(client, preview(client, "omp", operation="restore"))
    assert yaml(config)["modelRoles"]["default"] == "old/model"
    apply(client, preview(client, "omp", provider_id=value["id"], operation="uninstall"))


def test_omp_legacy_models_require_native_migration_without_shadowing(env):
    manager, client = env
    legacy = write(manager.home, ".omp/agent/models.json", '{"providers":{"original":{"apiKey":"keep-private"}},"unknown":true}')
    original = legacy.read_bytes()
    value = provider(client)
    response = client.post("/api/manage/plans", json={"host_id": "omp", "provider_id": value["id"]})
    assert response.status_code == 422 and "official models migration" in response.json()["detail"]
    assert legacy.read_bytes() == original
    assert not (manager.home / ".omp/agent/models.yml").exists()


def test_omp_legacy_settings_need_explicit_native_migration(env):
    manager, client = env
    legacy = write(manager.home, ".omp/agent/settings.json", '{"modelRoles":{"default":"old/model"},"unknown":true}')
    original = legacy.read_bytes()
    value = provider(client)
    response = client.post("/api/manage/plans", json={"host_id": "omp", "provider_id": value["id"], "activate_provider": True, "model_id": "native-model"})
    assert response.status_code == 422 and "official settings migration" in response.json()["detail"]
    assert legacy.read_bytes() == original
    assert not (manager.home / ".omp/agent/config.yml").exists()
    assert not (manager.home / ".omp/agent/models.yml").exists()


def test_omp_stale_preview_and_external_owned_edits_keep_files_and_ownership(env):
    manager, client = env
    value = provider(client)
    apply(client, preview(client, "omp", provider_id=value["id"]))
    path = manager.home / ".omp/agent/models.yml"
    plan = preview(client, "omp", provider_id=value["id"])
    path.write_text(path.read_text() + "external: keep-after-preview\n")
    changed = path.read_bytes()
    response = client.post(f"/api/manage/plans/{plan['id']}/apply", json={"confirmed": True, "expected_version": 1})
    assert response.status_code == 409
    assert path.read_bytes() == changed
    assert client.get("/api/manage/hosts/omp/state").json()["resources"][value["id"]]["status"] == "verified"
    path.write_text(path.read_text().replace("https://synthetic.invalid/v1", "https://external.invalid/v1"))
    changed = path.read_bytes()
    state = client.get("/api/manage/hosts/omp/state").json()
    assert state["resources"][value["id"]]["status"] == "externally_changed"
    for operation in ("install", "uninstall"):
        response = client.post("/api/manage/plans", json={"host_id": "omp", "provider_id": value["id"], "operation": operation})
        assert response.status_code == 409
        assert path.read_bytes() == changed
    with manager.connect() as conn:
        assert conn.execute("SELECT count(*) FROM management_owned WHERE owner=?", (value["id"],)).fetchone()[0] == 1


def test_omp_locked_keychain_failure_never_writes_plaintext_fallback(env):
    manager, client = env
    path = write(manager.home, ".omp/agent/models.yml", "# original\nproviders: {}\nother: keep\n")
    original = path.read_bytes()
    value = provider(client)
    plan = preview(client, "omp", provider_id=value["id"])
    manager.vault.locked = True
    response = client.post(f"/api/manage/plans/{plan['id']}/apply", json={"confirmed": True, "expected_version": 1})
    assert response.status_code in (409, 503)
    assert path.read_bytes() == original
    receipt = client.get(f"/api/manage/plans/{plan['id']}").json()
    assert receipt["status"] != "applied"
    assert "synthetic-native-secret" not in json.dumps(receipt)


def test_codex_desktop_shared_native_files_not_live_desktop_acceptance(env):
    manager, client = env
    path = write(manager.home, ".codex/config.toml", '# keep comment\nmodel = "original-model"\nmodel_provider = "original-provider"\n[features]\nkeep = true\n')
    original = tomllib.loads(path.read_text())
    value = provider(client)
    plan = preview(client, "codex-desktop", provider_id=value["id"], activate_provider=True, model_id="native-model")
    assert plan["payload"]["metadata"]["shared_configuration"] is True
    assert "Desktop consumption" in plan["payload"]["metadata"]["configuration_notice"]
    assert tomllib.loads(path.read_text()) == original
    apply(client, plan)
    native = tomllib.loads(path.read_text())
    assert native["model"] == "native-model" and native["features"] == original["features"]
    assert native["model_providers"][native["model_provider"]]["http_headers"]["Authorization"] == 'Bearer synthetic-native-secret-"-literal'
    state = client.get("/api/manage/hosts/codex-desktop/state").json()
    assert state["selection"]["provider_id"] == value["id"]
    assert state["effects"]["host_loaded"] == "not_verified"
    # CLI and Desktop share the selected native field; an independent owner may
    # not take it over and silently destroy the Desktop restoration baseline.
    response = client.post("/api/manage/plans", json={"host_id": "codex", "provider_id": value["id"], "activate_provider": True, "model_id": "native-model"})
    assert response.status_code == 409
    response = client.post("/api/manage/plans", json={"host_id": "codex-desktop", "provider_id": value["id"], "operation": "uninstall"})
    assert response.status_code == 409
    apply(client, preview(client, "codex-desktop", operation="restore"))
    apply(client, preview(client, "codex-desktop", provider_id=value["id"], operation="uninstall"))
    assert tomllib.loads(path.read_text()) == original
    assert "# keep comment" in path.read_text()


@pytest.mark.parametrize("host,actionable", [("cursor", "Cursor Settings > Models"), ("antigravity", "conversation model menu")])
def test_external_provider_operations_fail_precisely_without_writing(env, host, actionable):
    manager, client = env
    value = provider(client)
    capability = next(row for row in client.get("/api/manage/hosts").json() if row["id"] == host)
    assert capability["operation_limits"]["providers"]["operations"] == ["install", "update", "switch", "restore", "uninstall"]
    assert capability["operation_limits"]["providers"]["sources"]
    for request in ({"provider_id": value["id"]}, {"provider_id": value["id"], "activate_provider": True, "model_id": "native-model"}, {"operation": "uninstall", "provider_id": value["id"]}, {"operation": "restore"}):
        response = client.post("/api/manage/plans", json={"host_id": host, **request})
        assert response.status_code == 422 and actionable in response.json()["detail"]
    assert not list(manager.home.iterdir())


@pytest.mark.parametrize("host", ["omp", "cursor", "antigravity", "codex-desktop"])
@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_native_skills_full_lifecycle_preserves_foreign_files_and_never_executes(env, host, mode):
    manager, client = env
    marker = manager.home / "never-executed"
    asset = post(client, "assets", {"name": "Synthetic workflow", "kind": "skill", "install_mode": mode,
        "files": {"SKILL.md": "# Reviewed workflow", "scripts/never.sh": f"touch {marker}\n", "obsolete.txt": "old"}})
    plan = preview(client, host, asset_ids=[asset["id"]])
    path = next(Path(item["path"]) for item in plan["payload"]["files"] if item["path"].endswith("/SKILL.md"))
    assert not path.exists()
    apply(client, plan)
    assert path.is_symlink() == (mode == "symlink")
    assert "# Reviewed workflow" in path.read_text()
    assert path.read_text().startswith("---\n")
    assert "description: \"Synthetic workflow\"" in path.read_text()
    assert client.get(f"/api/manage/hosts/{host}/state").json()["resources"][asset["id"]]["status"] == "verified"
    foreign = path.parent / "foreign.txt"
    foreign.write_text("keep foreign file")
    changed = client.patch(f"/api/manage/assets/{asset['id']}", json={"expected_version": asset["version"], "files": {"SKILL.md": "# Updated", "new.txt": "new"}, "install_mode": "copy" if mode == "symlink" else "symlink"})
    assert changed.status_code == 200, changed.text
    updated = changed.json()
    apply(client, preview(client, host, asset_ids=[asset["id"]]))
    assert "# Updated" in path.read_text() and path.is_symlink() == (mode != "symlink")
    assert not (path.parent / "obsolete.txt").exists()
    assert not (path.parent / "scripts/never.sh").exists()
    restored = post(client, f"assets/{asset['id']}/restore", {"version": asset["version"], "expected_version": updated["version"]})
    assert restored["install_mode"] == mode
    apply(client, preview(client, host, asset_ids=[asset["id"]]))
    assert "# Reviewed workflow" in path.read_text() and path.is_symlink() == (mode == "symlink")
    assert (path.parent / "obsolete.txt").read_text() == "old"
    assert not (path.parent / "new.txt").exists()
    apply(client, preview(client, host, asset_ids=[asset["id"]], operation="uninstall"))
    assert not path.exists() and not path.is_symlink()
    assert foreign.read_text() == "keep foreign file"
    assert not marker.exists()


@pytest.mark.parametrize("host", ["omp", "cursor", "antigravity", "codex-desktop"])
def test_native_prompts_update_restore_conflict_and_uninstall(env, host):
    manager, client = env
    original_file = None
    if host in ("omp", "codex-desktop"):
        original_file = write(manager.home, ".omp/agent/AGENTS.md" if host == "omp" else ".codex/AGENTS.md", "User-owned original instructions\n")
    else:
        foreign = write(manager.home, ".cursor/skills/foreign/SKILL.md" if host == "cursor" else ".gemini/config/rules/foreign.md", "Unrelated user rule\n")
    asset = post(client, "assets", {"name": "Reviewed prompt", "kind": "prompt", "content": "Wait for user confirmation before executing."})
    plan = preview(client, host, asset_ids=[asset["id"]])
    path = Path(plan["payload"]["files"][0]["path"])
    apply(client, plan)
    assert asset["content"] in path.read_text()
    if host == "cursor":
        assert "disable-model-invocation: true" in path.read_text()
        assert "explicit-invocation" in plan["payload"]["metadata"]["prompt_notice"]
    elif host == "antigravity":
        assert path.parent.name == "rules"
        assert path.read_text().startswith("---\ntrigger: always_on\n")
    baseline = path.read_bytes()
    changed = client.patch(f"/api/manage/assets/{asset['id']}", json={"expected_version": asset["version"], "content": "Updated prompt, still wait for confirmation."})
    assert changed.status_code == 200, changed.text
    bundle = post(client, "bundles", {"name": "Prompt change", "asset_ids": [asset["id"]], "host_ids": [host]})
    apply(client, preview(client, host, bundle_id=bundle["id"]))
    assert "Updated prompt" in path.read_text()
    apply(client, preview(client, host, operation="restore"))
    assert path.read_bytes() == baseline
    state = client.get(f"/api/manage/hosts/{host}/state").json()
    assert state["resources"][asset["id"]]["status"] in ("update_available", "verified")
    assert state["effects"]["host_loaded"] == "not_verified"
    stale = preview(client, host, asset_ids=[asset["id"]], operation="uninstall")
    path.write_text(path.read_text() + "User change after preview\n")
    external = path.read_bytes()
    response = client.post(f"/api/manage/plans/{stale['id']}/apply", json={"confirmed": True, "expected_version": 1})
    assert response.status_code == 409 and path.read_bytes() == external
    if host in ("cursor", "antigravity"):
        assert foreign.read_text() == "Unrelated user rule\n"
    path.write_bytes(baseline)  # undo this test's external edit, not a product recovery
    apply(client, preview(client, host, asset_ids=[asset["id"]], operation="uninstall"))
    if original_file:
        assert path.read_text().strip() == "User-owned original instructions"
    else:
        assert not path.exists()


@pytest.mark.parametrize("content", ["---\nname: valid\n", "---\nname: valid\n---\nMissing description", "---\nname: bad_name\ndescription: usable\n---\nBody"])
def test_cursor_invalid_skill_metadata_is_a_visible_refusal_not_a_success(env, content):
    manager, client = env
    asset = post(client, "assets", {"name": "Invalid", "kind": "skill", "files": {"SKILL.md": content}})
    response = client.post("/api/manage/plans", json={"host_id": "cursor", "asset_ids": [asset["id"]]})
    assert response.status_code == 422
    assert any(part in response.json()["detail"] for part in ("frontmatter", "Cursor Skill name"))
    assert not (manager.home / ".cursor").exists()


def test_antigravity_rule_limit_refuses_truncation_without_touching_files(env):
    manager, client = env
    asset = post(client, "assets", {"name": "Too large", "kind": "prompt", "content": "中" * 8000})
    response = client.post("/api/manage/plans", json={"host_id": "antigravity", "asset_ids": [asset["id"]]})
    assert response.status_code == 422 and "24000" in response.json()["detail"]
    assert not (manager.home / ".gemini").exists()


@pytest.mark.parametrize("host,relative", [("omp", ".omp"), ("cursor", ".cursor"), ("antigravity", ".gemini"), ("codex-desktop", ".agents")])
def test_new_native_categories_never_relax_directory_symlink_safety(env, tmp_path, host, relative):
    manager, client = env
    outside = tmp_path / "outside"
    outside.mkdir()
    (manager.home / relative).symlink_to(outside, target_is_directory=True)
    asset = post(client, "assets", {"name": "Safe", "kind": "skill", "install_mode": "symlink", "files": {"SKILL.md": "Reviewed"}})
    response = client.post("/api/manage/plans", json={"host_id": host, "asset_ids": [asset["id"]]})
    assert response.status_code == 409
    assert not list(outside.iterdir())


@pytest.mark.parametrize("host", ["omp", "cursor", "antigravity", "codex-desktop"])
def test_native_managed_skill_external_replacement_is_visible_and_not_overwritten(env, host):
    manager, client = env
    asset = post(client, "assets", {"name": "Managed", "kind": "skill", "install_mode": "symlink", "files": {"SKILL.md": "Reviewed"}})
    plan = preview(client, host, asset_ids=[asset["id"]])
    apply(client, plan)
    path = Path(plan["payload"]["files"][0]["path"])
    original_link = path.readlink()
    path.unlink()
    path.write_text("User replacement, not a managed link")
    external = path.read_bytes()
    state = client.get(f"/api/manage/hosts/{host}/state").json()
    assert state["resources"][asset["id"]]["status"] == "externally_changed"
    for operation in ("install", "uninstall"):
        response = client.post("/api/manage/plans", json={"host_id": host, "asset_ids": [asset["id"]], "operation": operation})
        assert response.status_code == 409 and path.read_bytes() == external
    assert original_link.read_text().endswith("Reviewed")


def test_cursor_authored_skill_names_update_owned_paths_without_deleting_foreign_files(env):
    manager, client = env
    asset = post(client, "assets", {"name": "Authored", "kind": "skill", "files": {"SKILL.md": "---\nname: initial-workflow\ndescription: Use for reviewed workflows\ncustom: preserved\n---\nBody"}})
    plan = preview(client, "cursor", asset_ids=[asset["id"]])
    apply(client, plan)
    original = Path(plan["payload"]["files"][0]["path"])
    assert original.parent.name == "initial-workflow"
    assert original.read_text() == asset["files"]["SKILL.md"]
    foreign = original.parent / "foreign.txt"
    foreign.write_text("keep")
    response = client.patch(f"/api/manage/assets/{asset['id']}", json={"expected_version": asset["version"], "files": {"SKILL.md": "---\nname: renamed-workflow\ndescription: Use for reviewed workflows\ncustom: preserved\n---\nUpdated"}})
    assert response.status_code == 200
    apply(client, preview(client, "cursor", asset_ids=[asset["id"]]))
    renamed = original.parent.parent / "renamed-workflow" / "SKILL.md"
    assert renamed.read_text().endswith("Updated")
    assert not original.exists() and foreign.read_text() == "keep"
    apply(client, preview(client, "cursor", asset_ids=[asset["id"]], operation="uninstall"))
    assert not renamed.exists() and foreign.read_text() == "keep"


def test_omp_preserves_other_roles_and_refuses_uninstalling_their_provider(env):
    manager, client = env
    value = provider(client)
    model = post(client, "models", {"name": "Sized", "provider_id": value["id"], "model_id": "sized-model", "context_window": 64000})
    apply(client, preview(client, "omp", provider_id=value["id"]))
    rows = yaml(manager.home / ".omp/agent/models.yml")["providers"][value["id"]]["models"]
    assert next(row for row in rows if row["id"] == model["model_id"])["contextWindow"] == 64000
    config = write(manager.home, ".omp/agent/config.yml", f"modelRoles:\n  default: foreign/default\n  smol: {value['id']}/sized-model\nother: keep\n")
    original = config.read_bytes()
    response = client.post("/api/manage/plans", json={"host_id": "omp", "provider_id": value["id"], "operation": "uninstall"})
    assert response.status_code == 409 and "model roles" in response.json()["detail"]
    assert config.read_bytes() == original


@pytest.mark.parametrize("host", ["omp", "cursor", "antigravity", "codex-desktop"])
def test_installed_native_resources_stay_discoverable_in_scoped_catalog_without_membership(env, host):
    manager, client = env
    skill = post(client, "assets", {"name": "Unassigned skill", "kind": "skill", "agent_ids": [], "files": {"SKILL.md": "Reviewed skill"}})
    prompt = post(client, "assets", {"name": "Unassigned prompt", "kind": "prompt", "agent_ids": [], "content": "Wait for confirmation"})
    for asset in (skill, prompt):
        apply(client, preview(client, host, asset_ids=[asset["id"]]))
        page = client.get(f"/api/manage/assets?summary=true&agent_id={host}&kind={asset['kind']}").json()
        assert [row["id"] for row in page["items"]] == [asset["id"]]
        state = client.get(f"/api/manage/hosts/{host}/state").json()
        assert state["resources"][asset["id"]]["status"] == "verified"
    apply(client, preview(client, host, asset_ids=[prompt["id"]], operation="uninstall"))
    assert client.get(f"/api/manage/assets?summary=true&agent_id={host}&kind=prompt").json()["items"] == []
    assert client.get(f"/api/manage/assets?summary=true&agent_id={host}&kind=skill").json()["items"][0]["id"] == skill["id"]


def test_omp_alternate_provider_file_is_in_the_consumer_catalog(env):
    manager, client = env
    write(manager.home, ".omp/agent/models.yaml", "providers: {}\n")
    value = provider(client)
    apply(client, preview(client, "omp", provider_id=value["id"]))
    page = client.get("/api/manage/providers?summary=true&agent_id=omp").json()
    assert [row["id"] for row in page["items"]] == [value["id"]]
    assert client.get("/api/manage/hosts/omp/state").json()["resources"][value["id"]]["status"] == "verified"


def test_codex_desktop_mcp_install_update_remove_keeps_foreign_toml(env):
    manager, client = env
    path = write(manager.home, ".codex/config.toml", '# private original\nmodel = "keep"\n[mcp_servers.foreign]\ncommand = "foreign-server"\nenv = {FOREIGN_SECRET = "opaque-preserved"}\n')
    original = tomllib.loads(path.read_text())
    asset = post(client, "assets", {"name": "Desktop tool", "kind": "mcp", "config": {"transport": "http", "url": "https://tool.invalid/mcp", "headers": {"X-Trace": "reviewed"}}})
    plan = preview(client, "codex-desktop", asset_ids=[asset["id"]])
    assert "opaque-preserved" not in json.dumps(plan)
    apply(client, plan)
    key = "senrelay_" + asset["id"].replace("asset_", "")
    document = tomllib.loads(path.read_text())
    assert document["mcp_servers"][key] == {"url": "https://tool.invalid/mcp", "http_headers": {"X-Trace": "reviewed"}}
    assert document["mcp_servers"]["foreign"] == original["mcp_servers"]["foreign"]
    changed = client.patch(f"/api/manage/assets/{asset['id']}", json={"expected_version": asset["version"], "config": {"transport": "stdio", "command": "synthetic-never-executed", "args": ["--stdio"]}})
    assert changed.status_code == 200
    apply(client, preview(client, "codex-desktop", asset_ids=[asset["id"]]))
    assert tomllib.loads(path.read_text())["mcp_servers"][key] == {"command": "synthetic-never-executed", "args": ["--stdio"]}
    apply(client, preview(client, "codex-desktop", asset_ids=[asset["id"]], operation="uninstall"))
    assert tomllib.loads(path.read_text()) == original


def test_omp_new_canonical_filename_invalidates_alternate_file_preview(env):
    manager, client = env
    models = write(manager.home, ".omp/agent/models.yaml", "providers: {}\nforeign: keep\n")
    alternate = write(manager.home, ".omp/agent/config.yaml", "modelRoles: {default: original/model}\n")
    original_models, original_config = models.read_bytes(), alternate.read_bytes()
    value = provider(client)
    plan = preview(client, "omp", provider_id=value["id"], activate_provider=True, model_id="native-model")
    canonical = write(manager.home, ".omp/agent/config.yml", "modelRoles: {default: external/model}\n")
    external = canonical.read_bytes()
    response = client.post(f"/api/manage/plans/{plan['id']}/apply", json={"confirmed": True, "expected_version": 1})
    assert response.status_code == 409 and "filename precedence changed" in response.json()["detail"]
    assert models.read_bytes() == original_models
    assert alternate.read_bytes() == original_config and canonical.read_bytes() == external


@pytest.mark.parametrize("relative,activating", [(".omp/agent/models.json", False), (".omp/agent/settings.json", True)])
def test_omp_new_legacy_file_after_preview_is_not_shadowed(env, relative, activating):
    manager, client = env
    value = provider(client)
    plan = preview(client, "omp", provider_id=value["id"], **({"activate_provider": True, "model_id": "native-model"} if activating else {}))
    legacy = write(manager.home, relative, '{"original":"keep-private"}')
    original = legacy.read_bytes()
    response = client.post(f"/api/manage/plans/{plan['id']}/apply", json={"confirmed": True, "expected_version": 1})
    assert response.status_code == 422 and "official" in response.json()["detail"]
    assert legacy.read_bytes() == original
    assert not (manager.home / ".omp/agent/models.yml").exists()
    assert not (manager.home / ".omp/agent/config.yml").exists()


@pytest.mark.parametrize("shadow", ["models", "config"])
def test_shadowed_omp_owned_files_remain_visible_as_conflicts_not_success(env, shadow):
    manager, client = env
    write(manager.home, ".omp/agent/models.yaml", "providers: {}\n")
    write(manager.home, ".omp/agent/config.yaml", "modelRoles: {default: original/model}\n")
    value = provider(client)
    apply(client, preview(client, "omp", provider_id=value["id"], activate_provider=True, model_id="native-model"))
    original_models = (manager.home / ".omp/agent/models.yaml").read_bytes()
    original_config = (manager.home / ".omp/agent/config.yaml").read_bytes()
    external = write(manager.home, f".omp/agent/{shadow}.yml", "providers: {}\n" if shadow == "models" else "modelRoles: {default: external/model}\n")
    external_bytes = external.read_bytes()
    state = client.get("/api/manage/hosts/omp/state").json()
    assert state["selection"]["status"] == "externally_changed"
    assert state["resources"][value["id"]]["status"] == "externally_changed"
    page = client.get("/api/manage/providers?summary=true&agent_id=omp").json()
    assert value["id"] in [row["id"] for row in page["items"]]
    for request in ({"provider_id": value["id"]}, {"provider_id": value["id"], "operation": "uninstall"}, {"operation": "restore"}):
        response = client.post("/api/manage/plans", json={"host_id": "omp", **request})
        assert response.status_code == 409 and "filename precedence changed" in response.json()["detail"]
    assert external.read_bytes() == external_bytes
    assert (manager.home / ".omp/agent/models.yaml").read_bytes() == original_models
    assert (manager.home / ".omp/agent/config.yaml").read_bytes() == original_config
