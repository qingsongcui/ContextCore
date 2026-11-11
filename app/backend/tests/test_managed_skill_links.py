"""Real filesystem/API Skill transactions in a synthetic HOME; no host execution."""
from __future__ import annotations

import base64
import io
import json
import os
from pathlib import Path
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.integrations.config_transactions import ConfigurationError, canonical, digest
from app.routers import manage
from app.services import management_service
from app.services.management_service import ManagementService
from tests.test_management import FakeVault


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    manager = ManagementService(tmp_path / "data" / "service.db", home, FakeVault())
    monkeypatch.setattr(management_service, "service", lambda: manager)
    app = FastAPI()
    app.include_router(manage.router)
    with TestClient(app) as client:
        yield manager, client


def skill(manager, mode="copy", files=None):
    return manager.save("asset", {"name": "Synthetic Skill", "kind": "skill", "install_mode": mode,
                                  "files": files or {"SKILL.md": "# Version one", "scripts/helper.sh": "echo not-executed\n"}})


def apply(manager, asset, operation="install"):
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]], "operation": operation})
    return manager.apply(plan["id"], plan["version"], True)


def root(manager, asset):
    return manager.home / ".codex" / "skills" / asset["id"]


def archive(files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, text in files.items():
            bundle.writestr(name, text)
    return base64.b64encode(output.getvalue()).decode()


def test_skill_api_lifecycle_smoke(env, tmp_path):
    """Parent smoke: preview/confirm/readback/update/modes/version/uninstall."""
    manager, client = env
    marker = tmp_path / "must-not-execute"
    files = {"SKILL.md": "# Imported linked version", "scripts/run.sh": f"touch {marker}\n"}
    response = client.post("/api/manage/assets/import", json={"format": "skill-zip", "install_mode": "symlink", "name": "Linked", "data": archive(files)})
    assert response.status_code == 200, response.text
    asset = response.json()["imported"][0]
    target_root = root(manager, asset)
    response = client.post("/api/manage/plans", json={"host_id": "codex", "asset_ids": [asset["id"]]})
    assert response.status_code == 200, response.text
    plan = response.json()
    assert not target_root.exists()
    assert not (manager.db_path.parent / "skill-versions").exists()
    for entry in plan["payload"]["files"]:
        assert entry["before_state"] is None
        assert entry["install_mode"] == "symlink"
        assert entry["source_version"] == asset["version"]
        expected = files[Path(entry["path"]).relative_to(target_root).as_posix()].encode()
        assert entry["after_hash"] == digest(expected)
        assert entry["after_state"]["bytes_hash"] == digest(expected)
        assert entry["after_state"]["target_hash"] == digest(entry["after_state"]["target"].encode())
        assert "symlink" in entry["diff"]
        assert "specs" not in entry
    endpoint = f"/api/manage/plans/{plan['id']}/apply"
    assert client.post(endpoint, json={"confirmed": False, "expected_version": 1}).status_code == 422
    assert not target_root.exists()
    response = client.post(endpoint, json={"confirmed": True, "expected_version": 1})
    assert response.status_code == 200, response.text
    receipt = client.get(f"/api/manage/plans/{plan['id']}").json()
    assert receipt["status"] == "applied"
    assert receipt["payload"]["readback"] == "verified"
    assert receipt["payload"]["host_reload"] == "not_run"
    targets = {}
    for name, text in files.items():
        path = target_root / name
        assert path.is_symlink()
        assert path.read_text() == text
        targets[name] = Path(os.readlink(path))
        assert targets[name].is_relative_to(manager.db_path.parent / "skill-versions")
        assert targets[name].stat().st_mode & 0o777 == 0o400
    assert not marker.exists()

    changed = {"SKILL.md": "# Updated linked version", "scripts/new.sh": "echo still-not-executed\n"}
    response = client.post("/api/manage/assets/import", json={"format": "skill-zip", "data": archive(changed), "target_id": asset["id"], "expected_version": asset["version"], "name": asset["name"]})
    assert response.status_code == 200, response.text
    updated = response.json()["imported"][0]
    assert updated["install_mode"] == "symlink"
    apply(manager, updated)
    assert not (target_root / "scripts/run.sh").is_symlink()
    for name, text in changed.items():
        assert (target_root / name).is_symlink()
        assert (target_root / name).read_text() == text
        assert str(updated["version"]) == Path(os.readlink(target_root / name)).parent.name
    assert targets["SKILL.md"].read_text() == files["SKILL.md"]

    response = client.patch(f"/api/manage/assets/{asset['id']}", json={"install_mode": "copy", "expected_version": updated["version"]})
    assert response.status_code == 200
    copied = response.json()
    apply(manager, copied)
    for name, text in changed.items():
        assert not (target_root / name).is_symlink()
        assert (target_root / name).read_text() == text
    response = client.post(f"/api/manage/assets/{asset['id']}/restore", json={"version": asset["version"], "expected_version": copied["version"]})
    assert response.status_code == 200, response.text
    restored = response.json()
    assert restored["install_mode"] == "symlink"
    apply(manager, restored)
    for name, text in files.items():
        assert (target_root / name).is_symlink()
        assert (target_root / name).read_text() == text
    foreign = target_root / "user-added.txt"
    foreign.write_text("keep outside managed file set")
    receipt = apply(manager, restored, "uninstall")
    assert receipt["status"] == "applied"
    assert not (target_root / "SKILL.md").is_symlink()
    assert foreign.read_text() == "keep outside managed file set"
    assert targets["SKILL.md"].read_text() == files["SKILL.md"]
    assert not marker.exists()


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_export_import_and_local_source_updates_preserve_mode(env, tmp_path, mode):
    manager, client = env
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("source one")
    result = manager.import_objects({"format": "skill-local", "data": str(source), "install_mode": mode})
    asset = result["imported"][0]
    apply(manager, asset)
    (source / "SKILL.md").write_text("source two")
    assert (root(manager, asset) / "SKILL.md").read_text() == "source one"
    exported = client.get("/api/manage/assets/export?kind=skill").json()
    assert exported["objects"][0]["data"]["install_mode"] == mode
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    other = ManagementService(tmp_path / "other-data" / "service.db", other_home, FakeVault())
    imported = other.import_objects({"format": "senrelay", "data": exported})
    assert imported["unsupported"] == []
    copied_asset = other.list("asset")[0]
    assert copied_asset["install_mode"] == mode
    apply(other, copied_asset)
    assert (root(other, copied_asset) / "SKILL.md").read_text() == "source one"
    updated = manager.import_objects({"format": "skill-local", "data": str(source), "target_id": asset["id"], "expected_version": asset["version"]})["imported"][0]
    assert updated["install_mode"] == mode
    apply(manager, updated)
    assert (root(manager, asset) / "SKILL.md").read_text() == "source two"


@pytest.mark.parametrize("initial,updated", [("copy", "symlink"), ("symlink", "copy"), ("symlink", "symlink")])
def test_bundle_restore_recovers_exact_original_mode_and_target(env, initial, updated):
    manager, _ = env
    asset = skill(manager, initial)
    apply(manager, asset)
    path = root(manager, asset) / "SKILL.md"
    original_target = os.readlink(path) if path.is_symlink() else None
    asset = manager.save("asset", {"install_mode": updated, "files": {"SKILL.md": "bundle version", "new.md": "new"}}, asset["id"], asset["version"])
    bundle = manager.save("bundle", {"name": "Synthetic bundle", "asset_ids": [asset["id"]], "host_ids": ["codex"]})
    plan = manager.plan({"host_id": "codex", "bundle_id": bundle["id"]})
    manager.apply(plan["id"], 1, True)
    assert path.read_text() == "bundle version"
    restore = manager.plan({"host_id": "codex", "operation": "restore"})
    receipt = manager.apply(restore["id"], 1, True)
    assert receipt["payload"]["readback"] == "verified"
    assert path.read_text() == "# Version one"
    assert path.is_symlink() == (initial == "symlink")
    if original_target:
        assert os.readlink(path) == original_target
    assert (root(manager, asset) / "scripts/helper.sh").read_text() == "echo not-executed\n"
    assert not (root(manager, asset) / "new.md").exists()
    apply(manager, asset, "uninstall")
    assert not path.is_symlink() and not path.exists()


def test_legacy_owned_copy_can_switch_to_link_without_weakening_ownership(env):
    manager, _ = env
    asset = skill(manager)
    path = root(manager, asset) / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("legacy")
    with manager.connect() as conn:
        conn.execute("INSERT INTO management_owned VALUES(?,?,?,?)", (str(path), 'file:"file"', digest(canonical("legacy").encode()), asset["id"]))
    asset = manager.save("asset", {"install_mode": "symlink"}, asset["id"], asset["version"])
    apply(manager, asset)
    assert path.is_symlink()
    assert path.read_text() == "# Version one"


def test_stale_plan_detects_asset_and_same_bytes_mode_changes(env):
    manager, _ = env
    asset = skill(manager)
    apply(manager, asset)
    stale = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    asset = manager.save("asset", {"install_mode": "symlink"}, asset["id"], asset["version"])
    with pytest.raises(ConfigurationError, match="changed since preview"):
        manager.apply(stale["id"], 1, True)
    first = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    second = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    manager.apply(first["id"], 1, True)
    with pytest.raises(ConfigurationError, match="ownership changed since preview"):
        manager.apply(second["id"], 1, True)
    assert (root(manager, asset) / "SKILL.md").is_symlink()


@pytest.mark.parametrize("mutation", ["repoint", "canonical_edit", "replace_same_bytes", "replace_changed_bytes"])
def test_external_link_or_content_changes_never_overwrite_or_uninstall(env, tmp_path, mutation):
    manager, _ = env
    asset = skill(manager, "symlink")
    apply(manager, asset)
    path = root(manager, asset) / "SKILL.md"
    target = Path(os.readlink(path))
    outside = tmp_path / "external-owned"
    outside.write_text("external private bytes")
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]], "operation": "uninstall"})
    if mutation == "repoint":
        path.unlink()
        path.symlink_to(outside)
    elif mutation == "canonical_edit":
        target.chmod(0o600)
        target.write_text("external change")
    else:
        path.unlink()
        path.write_text("# Version one" if mutation == "replace_same_bytes" else "external change")
    with pytest.raises(ConfigurationError):
        manager.apply(plan["id"], 1, True)
    for operation in ("install", "uninstall"):
        with pytest.raises(ConfigurationError):
            manager.plan({"host_id": "codex", "asset_ids": [asset["id"]], "operation": operation})
    assert outside.read_text() == "external private bytes"
    if mutation == "repoint":
        assert os.readlink(path) == str(outside)
    else:
        assert path.read_text() == ("# Version one" if mutation == "replace_same_bytes" else "external change")


@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_collisions_and_symlink_ancestors_remain_rejected(env, tmp_path, mode):
    manager, _ = env
    asset = skill(manager, mode)
    path = root(manager, asset) / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text("foreign skill")
    with pytest.raises(ConfigurationError, match="already exists"):
        apply(manager, asset)
    assert path.read_text() == "foreign skill"
    path.unlink()
    external = tmp_path / "external-directory"
    external.mkdir()
    path.parent.rmdir()
    path.parent.symlink_to(external, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="Symlink"):
        apply(manager, asset)
    assert list(external.iterdir()) == []
    with pytest.raises(ConfigurationError, match="Symlink"):
        manager.transactions.safe_path(str(path))


@pytest.mark.parametrize("mode", ["copy", "symlink"])
@pytest.mark.parametrize("attack", ["directory", "file", "ancestor", "traversal", "fifo"])
def test_local_source_links_traversal_and_special_files_are_refused(env, tmp_path, mode, attack):
    manager, _ = env
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("safe")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("do not import")
    selected = str(source)
    if attack == "directory":
        (source / "linked").symlink_to(outside, target_is_directory=True)
    elif attack == "file":
        (source / "linked.md").symlink_to(outside / "secret.md")
    elif attack == "ancestor":
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        selected = str(tmp_path / "alias" / "source")
    elif attack == "traversal":
        selected = str(source / ".." / "source")
    else:
        os.mkfifo(source / "pipe")
    with pytest.raises(ConfigurationError):
        manager.import_objects({"format": "skill-local", "data": selected, "install_mode": mode})
    assert manager.list("asset") == []
    assert (outside / "secret.md").read_text() == "do not import"


@pytest.mark.parametrize("name", ["../bad", "/bad", "a/../../bad", "a//bad", "./bad", "a\\bad", "a/./bad", "nul\x00name"])
def test_skill_paths_are_canonical_relative_files(env, name):
    manager, _ = env
    with pytest.raises(ConfigurationError):
        skill(manager, "symlink", {"SKILL.md": "safe", name: "bad"})
    assert manager.list("asset") == []


def test_canonical_store_symlink_refused_before_target_read(env, tmp_path):
    manager, _ = env
    asset = skill(manager, "symlink")
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    outside = tmp_path / "external-store"
    outside.mkdir()
    (manager.db_path.parent / "skill-versions").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigurationError, match="symlink"):
        manager.apply(plan["id"], 1, True)
    assert list(outside.iterdir()) == []
    assert not root(manager, asset).exists()


@pytest.mark.parametrize("initial", ["copy", "symlink"])
def test_mixed_partial_failure_recovery_restores_mode_ownership_and_bytes(env, monkeypatch, initial):
    manager, _ = env
    asset = skill(manager, initial)
    apply(manager, asset)
    path = root(manager, asset) / "SKILL.md"
    target = os.readlink(path) if path.is_symlink() else None
    with manager.connect() as conn:
        owned_before = [tuple(row) for row in conn.execute("SELECT * FROM management_owned ORDER BY path,item_key")]
        links_before = [tuple(row) for row in conn.execute("SELECT * FROM management_skill_files ORDER BY path")]
    asset = manager.save("asset", {"install_mode": "copy" if initial == "symlink" else "symlink", "files": {"SKILL.md": "changed", "scripts/helper.sh": "changed script"}}, asset["id"], asset["version"])
    mcp = manager.save("asset", {"name": "synthetic MCP", "kind": "mcp", "config": {"command": "not-executed"}})
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"], mcp["id"]]})
    atomic = manager.transactions._atomic
    def fail_mcp(path, data):
        if path.name == "config.toml":
            raise OSError("synthetic disk failure")
        return atomic(path, data)
    monkeypatch.setattr(manager.transactions, "_atomic", fail_mcp)
    with pytest.raises(ConfigurationError, match="recovery is required"):
        manager.apply(plan["id"], 1, True)
    assert path.read_text() == "changed"
    assert path.is_symlink() == (initial == "copy")
    persisted = manager.transactions.get(plan["id"])
    assert persisted["status"] == "recovery_required"
    assert persisted["payload"]["readback"] == "not_run"
    with manager.connect() as conn:
        assert [tuple(row) for row in conn.execute("SELECT * FROM management_owned ORDER BY path,item_key")] == owned_before
        assert [tuple(row) for row in conn.execute("SELECT * FROM management_skill_files ORDER BY path")] == links_before
    monkeypatch.setattr(manager.transactions, "_atomic", atomic)
    # A restarted service must use persisted link states, not process memory.
    restarted = ManagementService(manager.db_path, manager.home, manager.vault)
    assert restarted.transactions.recover(plan["id"], True)["status"] == "recovered"
    assert path.read_text() == "# Version one"
    assert path.is_symlink() == (initial == "symlink")
    if target:
        assert os.readlink(path) == target
    assert not (manager.home / ".codex/config.toml").exists()
    assert restarted.transactions.recover(plan["id"], True)["status"] == "recovered"
    apply(restarted, asset)
    assert path.read_text() == "changed"


def test_recovery_rejects_repointed_link_without_deleting_external_file(env, tmp_path, monkeypatch):
    manager, _ = env
    asset = skill(manager, "symlink")
    mcp = manager.save("asset", {"name": "MCP", "kind": "mcp", "config": {"command": "not-executed"}})
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"], mcp["id"]]})
    atomic = manager.transactions._atomic
    def fail(path, data):
        if path.name == "config.toml":
            raise OSError("synthetic failure")
        return atomic(path, data)
    monkeypatch.setattr(manager.transactions, "_atomic", fail)
    with pytest.raises(ConfigurationError):
        manager.apply(plan["id"], 1, True)
    path = root(manager, asset) / "SKILL.md"
    original = os.readlink(path)
    external = tmp_path / "external"
    external.write_text("keep")
    path.unlink()
    path.symlink_to(external)
    monkeypatch.setattr(manager.transactions, "_atomic", atomic)
    with pytest.raises(ConfigurationError, match="repointed"):
        manager.transactions.recover(plan["id"], True)
    assert os.readlink(path) == str(external)
    assert external.read_text() == "keep"
    assert (root(manager, asset) / "scripts/helper.sh").is_symlink()
    path.unlink()
    path.symlink_to(original)
    assert manager.transactions.recover(plan["id"], True)["status"] == "recovered"
    assert not path.is_symlink()
    assert external.read_text() == "keep"


@pytest.mark.parametrize("name", ["link", "linked-directory/"])
@pytest.mark.parametrize("mode", ["copy", "symlink"])
def test_zip_source_links_are_rejected_even_with_directory_names(env, name, mode):
    manager, _ = env
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        bundle.writestr("SKILL.md", "safe")
        entry = zipfile.ZipInfo(name)
        entry.create_system = 3
        entry.external_attr = 0o120777 << 16
        bundle.writestr(entry, "/synthetic/external")
    with pytest.raises(ConfigurationError):
        manager.import_objects({"format": "skill-zip", "install_mode": mode, "data": base64.b64encode(output.getvalue()).decode()})
    assert manager.list("asset") == []


def test_copy_default_and_generic_link_rejection_remain_intact(env):
    manager, client = env
    created = client.post("/api/manage/assets", json={"name": "Default", "kind": "skill", "files": {"SKILL.md": "default"}})
    assert created.status_code == 200
    asset = created.json()
    assert asset["install_mode"] == "copy"
    apply(manager, asset)
    path = root(manager, asset) / "SKILL.md"
    assert path.read_text() == "default"
    assert not path.is_symlink()
    asset = manager.save("asset", {"install_mode": "symlink"}, asset["id"], asset["version"])
    apply(manager, asset)
    with pytest.raises(ConfigurationError, match="Symlink"):
        manager.transactions.safe_path(str(path))
    # A generic file operation must not gain the typed link capability.
    with pytest.raises(ConfigurationError, match="Symlink"):
        manager.transactions.create([{"path": str(path), "kind": "file", "owner": asset["id"], "value": "overwrite"}])
    assert path.is_symlink() and path.read_text() == "default"


def test_failed_metadata_commit_recovers_link_install_without_false_ownership(env):
    manager, _ = env
    asset = skill(manager, "symlink")
    plan = manager.plan({"host_id": "codex", "asset_ids": [asset["id"]]})
    with manager.connect() as conn:
        conn.execute("CREATE TRIGGER fail_skill_receipt BEFORE INSERT ON management_skill_files BEGIN SELECT RAISE(ABORT, 'synthetic metadata failure'); END")
    with pytest.raises(ConfigurationError, match="recovery is required"):
        manager.apply(plan["id"], 1, True)
    assert (root(manager, asset) / "SKILL.md").is_symlink()
    with manager.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM management_skill_files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM management_owned").fetchone()[0] == 0
    assert manager.transactions.recover(plan["id"], True)["status"] == "recovered"
    assert not (root(manager, asset) / "SKILL.md").is_symlink()
    with manager.connect() as conn:
        conn.execute("DROP TRIGGER fail_skill_receipt")
    assert apply(manager, asset)["payload"]["readback"] == "verified"


def test_bundle_change_invalidates_skill_plan(env):
    manager, _ = env
    asset = skill(manager, "symlink")
    bundle = manager.save("bundle", {"name": "Bundle", "asset_ids": [asset["id"]], "host_ids": ["codex"]})
    plan = manager.plan({"host_id": "codex", "bundle_id": bundle["id"]})
    manager.save("bundle", {"asset_ids": []}, bundle["id"], bundle["version"])
    with pytest.raises(ConfigurationError, match="changed since preview"):
        manager.apply(plan["id"], 1, True)
    assert not root(manager, asset).exists()


def test_retained_copy_only_bundle_baseline_restores_after_link_cutover(env):
    manager, _ = env
    asset = skill(manager, "copy")
    apply(manager, asset)
    asset = manager.save("asset", {"files": {"SKILL.md": "bundle copy"}}, asset["id"], asset["version"])
    bundle = manager.save("bundle", {"name": "Retained baseline", "asset_ids": [asset["id"]]})
    plan = manager.plan({"host_id": "codex", "bundle_id": bundle["id"]})
    manager.apply(plan["id"], 1, True)
    # Reconstruct the precise persisted receipt shape written by copy-only
    # releases, not a source/wording assertion or a synthetic success result.
    with manager.connect() as conn:
        active = json.loads(conn.execute("SELECT data FROM management_activations WHERE host_id='codex'").fetchone()[0])
        for field in active["fields"]:
            field.pop("managed_skill", None)
        for entry in active["files"].values():
            entry.pop("skill_state", None)
        conn.execute("UPDATE management_activations SET data=? WHERE host_id='codex'", (canonical(active),))
    asset = manager.save("asset", {"install_mode": "symlink", "files": {"SKILL.md": "new linked version"}}, asset["id"], asset["version"])
    apply(manager, asset)
    path = root(manager, asset) / "SKILL.md"
    assert path.is_symlink()
    restore = manager.plan({"host_id": "codex", "operation": "restore"})
    assert all(item["source_version"] is None for item in restore["payload"]["files"])
    manager.apply(restore["id"], 1, True)
    assert not path.is_symlink()
    assert path.read_text() == "# Version one"
    assert (root(manager, asset) / "scripts/helper.sh").read_text() == "echo not-executed\n"
    apply(manager, asset, "uninstall")
    assert not path.exists()


def test_hidden_skill_file_does_not_block_sibling_install_or_uninstall(env):
    manager, _ = env
    files = {
        ".contextcore-path-check": "legitimate hidden Skill file",
        "SKILL.md": "# Skill with hidden metadata",
        "scripts/.contextcore-path-check": "nested metadata",
        "scripts/helper.sh": "echo not-executed\n",
    }
    asset = skill(manager, "symlink", files)
    installed = apply(manager, asset)
    assert installed["status"] == "applied"
    for name, text in files.items():
        path = root(manager, asset) / name
        assert path.is_symlink()
        assert path.read_text() == text
    removed = apply(manager, asset, "uninstall")
    assert removed["status"] == "applied"
    for name in files:
        path = root(manager, asset) / name
        assert not path.exists() and not path.is_symlink()
