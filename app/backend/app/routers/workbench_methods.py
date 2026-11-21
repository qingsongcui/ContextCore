"""Turn an explicitly saved personal method into an independently managed local Skill."""
import hashlib
import json
import re
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.database import db, log_audit, now_iso
from app.integrations.config_transactions import ConfigurationError
from app.services import management_service
from app.services.workbench_service import read_profile

router = APIRouter(prefix="/api/workbench/methods", tags=["workbench"])


class MethodSelection(BaseModel):
    asset_id: str = Field(min_length=1, max_length=100)
    profile_revision: int = Field(ge=0)


class CatalogWrite(MethodSelection):
    review_token: str = Field(min_length=64, max_length=64)


def render_method(asset):
    title = asset["title"].strip()
    fields = [("Inputs", "inputs"), ("Steps", "steps"), ("Outputs", "outputs")]
    if not title or any(not asset.get(key, "").strip() for _, key in fields):
        raise HTTPException(422, "Save the method title, inputs, steps and outputs first")
    suffix = asset["id"]
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", suffix) or len(suffix) > 50:
        suffix = hashlib.sha256(suffix.encode()).hexdigest()[:24]
    name = "method-" + suffix
    parts = ["---", "name: " + name, "description: " + json.dumps(title, ensure_ascii=False), "---", "", "# " + title]
    if asset.get("verification", "").strip():
        fields.append(("Verification", "verification"))
    for heading, key in fields:
        parts.extend(["", "## " + heading, "", asset[key].strip()])
    return "\n".join(parts) + "\n"


def method_state(conn, selection):
    profile = read_profile(conn)
    if profile["revision"] != selection.profile_revision:
        raise HTTPException(409, "Profile changed; reopen the saved method")
    asset = next((item for item in profile["assets"] if item["id"] == selection.asset_id), None)
    if not asset:
        raise HTTPException(404, "Method not found")
    content = render_method(asset)
    row = conn.execute(
        """SELECT e.id,e.data,e.version,e.created_at,e.updated_at
           FROM management_personal_methods m JOIN management_entities e ON e.id=m.asset_id
           WHERE m.method_id=? AND e.kind='asset'""", (asset["id"],)
    ).fetchone()
    catalog_id = row["id"] if row else None
    old = json.loads(row["data"]) if row else {}
    previous = old.get("files", {}).get("SKILL.md")
    token_fields = [asset["id"], catalog_id, profile["revision"], content, list(row) if row else None]
    token = hashlib.sha256(json.dumps(token_fields, ensure_ascii=False).encode()).hexdigest()
    return asset, old, {
        "catalog_id": catalog_id, "version": row["version"] if row else 0, "content": content,
        "previous": previous, "state": "current" if previous == content else "update" if row else "new",
        "review_token": token,
    }


@router.post("/preview")
def preview(selection: MethodSelection):
    service = management_service.service()
    with service.connect() as conn:
        conn.execute("BEGIN")
        return method_state(conn, selection)[2]


@router.post("/catalog")
def save_to_catalog(selection: CatalogWrite):
    service = management_service.service()
    with management_service.configuration_lock, db(service.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        asset, old, state = method_state(conn, selection)
        if state["state"] == "current":
            return {"id": state["catalog_id"], "version": state["version"]}
        if selection.review_token != state["review_token"]:
            raise HTTPException(409, "Catalog changed; review the current Skill before updating")
        data = old or {"name": asset["title"], "kind": "skill", "source": {"type": "personal_method", "method_id": asset["id"]}, "agent_ids": []}
        data = {**data, "files": {**data.get("files", {}), "SKILL.md": state["content"]}}
        # A deleted definition must receive a new identity when republished. Old host plans
        # reference the prior ID/version and must never become applicable to its replacement.
        catalog_id = state["catalog_id"] or "asset_" + uuid4().hex[:16]
        try:
            value = service._validate("asset", data, entity_id=catalog_id)
            management_service.store_definition(conn, "asset", catalog_id, value, state["version"], now_iso())
            if not state["catalog_id"]:
                conn.execute("""INSERT INTO management_personal_methods(method_id,asset_id) VALUES (?,?)
                    ON CONFLICT(method_id) DO UPDATE SET asset_id=excluded.asset_id""", (asset["id"], catalog_id))
        except ConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        log_audit("user", "profile.method.catalog", catalog_id, conn=conn)
        return {"id": catalog_id, "version": state["version"] + 1}
