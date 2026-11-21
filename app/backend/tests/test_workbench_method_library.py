import re

import pytest
from fastapi import HTTPException

from app.database import create_schema
from app.routers.workbench import Profile
from app.routers.workbench_methods import CatalogWrite, MethodSelection, preview, save_to_catalog
from app.services import management_service as management
from app.services.workbench_service import save_profile


def test_promoted_method_is_a_versioned_skill_without_private_fields():
    create_schema()
    asset = {"id": "周报", "title": "Audit report", "inputs": "Table", "steps": "Compare rows",
             "outputs": "Summary", "verification": "Sample two", "notes": "PRIVATE",
             "contribution": "PRIVATE", "excerpt": "PRIVATE", "selected": False}
    save_profile(Profile(revision=0, assets=[asset]))
    req = MethodSelection(asset_id=asset["id"], profile_revision=1)
    result = preview(req)
    assert result["state"] == "new"
    assert result["catalog_id"] is None
    assert "PRIVATE" not in result["content"] and "Compare rows" in result["content"]
    write = CatalogWrite(**req.model_dump(), review_token=result["review_token"])
    saved = save_to_catalog(write)
    assert re.fullmatch(r"asset_[a-f0-9]{16}", saved["id"])
    assert save_to_catalog(write) == saved
    assert preview(req)["state"] == "current"
    svc = management.service()
    definition = svc.get("asset", saved["id"])
    assert definition["files"] == {"SKILL.md": result["content"]}
    assert not definition["agent_ids"]
    save_profile(Profile(revision=1, assets=[{**asset, "steps": "New method"}]))
    with pytest.raises(HTTPException) as stale:
        save_to_catalog(write)
    assert stale.value.status_code == 409
    new = MethodSelection(asset_id=asset["id"], profile_revision=2)
    update = preview(new)
    assert update["previous"] == result["content"] and update["state"] == "update"
    svc.save("asset", {"files": {"SKILL.md": "Edited separately", "example.md": "Keep me"}}, saved["id"], 1)
    with pytest.raises(HTTPException) as changed:
        save_to_catalog(CatalogWrite(**new.model_dump(), review_token=update["review_token"]))
    assert changed.value.status_code == 409
    refreshed = preview(new)
    assert refreshed["previous"] == "Edited separately"
    assert save_to_catalog(CatalogWrite(**new.model_dump(), review_token=refreshed["review_token"]))["version"] == 3
    assert svc.get("asset", saved["id"])["files"]["example.md"] == "Keep me"


def test_incomplete_method_cannot_be_promoted():
    create_schema()
    save_profile(Profile(revision=0, assets=[{"id": "a", "title": "Not ready"}]))
    with pytest.raises(HTTPException) as err:
        preview(MethodSelection(asset_id="a", profile_revision=1))
    assert err.value.status_code == 422

def test_deleted_and_republished_method_cannot_revive_reviewed_host_plan():
    create_schema()
    method = {"id": "m", "title": "Method", "inputs": "Input", "steps": "Old steps", "outputs": "Summary"}
    save_profile(Profile(revision=0, assets=[method]))
    first = MethodSelection(asset_id="m", profile_revision=1)
    original = save_to_catalog(CatalogWrite(**first.model_dump(), review_token=preview(first)["review_token"]))
    svc = management.service()
    plan = svc.plan({"host_id": "codex", "asset_ids": [original["id"]], "operation": "install"})
    svc.delete("asset", original["id"], original["version"])
    save_profile(Profile(revision=1, assets=[{**method, "steps": "New steps"}]))
    second = MethodSelection(asset_id="m", profile_revision=2)
    replacement = save_to_catalog(CatalogWrite(**second.model_dump(), review_token=preview(second)["review_token"]))
    assert replacement["id"] != original["id"] and replacement["version"] == 1
    with pytest.raises(management.ConfigurationError):
        svc.apply(plan["id"], plan["version"], True)
