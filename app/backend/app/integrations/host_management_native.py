"""Native management for OMP and desktop surfaces, without live-host claims.

Format evidence (official sources, read 2026-11-04):
https://github.com/can1357/oh-my-pi/blob/main/docs/{models,settings,skills,context-files}.md
https://cursor.com/docs/skills and https://cursor.com/help/customization/skills
https://cursor.com/help/models-and-usage/api-keys
https://antigravity.google/docs/{skills,rules,models,customization-discovery}
https://developers.openai.com/codex/config-basic/

Only file operations are rendered. No host process, account or model is invoked.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from app.integrations.config_transactions import ConfigurationError, SECRET_REF


NATIVE_HOSTS = {
    "omp": {"name": "Oh My Pi", "mcp_path": ".omp/agent/mcp.json", "format": "json", "container": "mcpServers",
            "provider_path": ".omp/agent/models.yml", "provider_format": "yaml", "selection_path": ".omp/agent/config.yml",
            "runtime_dependency": "ruamel.yaml", "skill_dir": ".omp/agent/skills", "prompt_path": ".omp/agent/AGENTS.md",
            "configuration_notice": "Default OMP profile only. Named profiles and directory/environment overrides are separate. Saved modelRoles.default affects subsequent resolution, not a running session's model; project/CLI/runtime settings can override it.",
            "prompt_notice": "User AGENTS.md is startup context for OMP main sessions/advisors; task subagents skip AGENTS.md. It is not a per-turn sticky RULES.md or an automatically invoked command.",
            "sources": ["https://github.com/can1357/oh-my-pi/blob/main/docs/models.md", "https://github.com/can1357/oh-my-pi/blob/main/docs/settings.md", "https://github.com/can1357/oh-my-pi/blob/main/docs/skills.md", "https://github.com/can1357/oh-my-pi/blob/main/docs/context-files.md"]},
    "cursor": {"name": "Cursor", "mcp_path": ".cursor/mcp.json", "format": "json", "container": "mcpServers", "skill_dir": ".cursor/skills",
               "prompt_dir": ".cursor/skills", "prompt_mode": "manual_skill",
               "prompt_notice": "Prompts are explicit-invocation instruction Skills (/asset-...). They do not replace global User Rules, which Cursor stores outside the documented filesystem interface. Project .mdc rules require a project scope.",
               "provider_unsupported_reason": "Cursor documents API-key setup in Cursor Settings > Models and model choice in its own picker, not a supported provider/default-model configuration file API. Save/export definitions here; add, update, switch or remove keys/models in Cursor Settings > Models. Private storage databases are not rewritten.",
               "sources": ["https://cursor.com/docs/skills", "https://cursor.com/help/customization/skills", "https://cursor.com/docs/rules", "https://cursor.com/help/models-and-usage/api-keys"]},
    "antigravity": {"name": "Antigravity Desktop", "mcp_path": ".gemini/config/mcp_config.json", "format": "json", "container": "mcpServers",
                    "skill_dir": ".gemini/config/skills", "prompt_dir": ".gemini/config/rules", "prompt_mode": "global_rule",
                    "provider_unsupported_reason": "Antigravity Desktop documents plan-dependent models selected in the conversation model menu, not a custom API-provider/key or default-model configuration file API. Select models in Antigravity; its private account/profile caches are not rewritten or reused as provider credentials.",
                    "sources": ["https://antigravity.google/docs/skills", "https://antigravity.google/docs/rules", "https://antigravity.google/docs/models", "https://antigravity.google/docs/customization-discovery"]},
    "codex-desktop": {"name": "Codex Desktop", "mcp_path": ".codex/config.toml", "provider_path": ".codex/config.toml", "format": "toml",
                      "skill_dir": ".codex/skills", "prompt_path": ".codex/AGENTS.md",
                      "configuration_notice": "Writes shared Codex native files also used by Codex CLI. Desktop consumption, reload and model availability have not been verified. Review the shared-file impact; writing defaults is not switching a running Desktop conversation.",
                      "prompt_notice": "Writes user AGENTS.md for subsequent instruction discovery. AGENTS.override.md and non-default CODEX_HOME can supersede this file; Desktop consumption remains unverified.",
                      "sources": ["https://developers.openai.com/codex/config-basic/", "https://developers.openai.com/codex/config-reference/", "https://learn.chatgpt.com/docs/build-skills", "https://learn.chatgpt.com/docs/agent-configuration/agents-md"]},
}


def effective_host(host: dict, home: Path) -> dict:
    """Honor OMP's existing accepted YAML filename without migrating legacy files."""
    result = copy.deepcopy(host)
    if result.get("selection_path"):
        for key in ("provider_path", "selection_path"):
            path = home / result[key]
            alternate = path.with_suffix(".yaml")
            if not path.exists() and not path.is_symlink() and (alternate.exists() or alternate.is_symlink()):
                result[key] = alternate.relative_to(home).as_posix()
    return result


def configuration_paths(host: dict, home: Path) -> list[str]:
    """Retain ownership visibility for OMP files shadowed by a new YAML sibling."""
    paths = [str(home / value) for key, value in host.items() if key in ("mcp_path", "provider_path", "prompt_path", "selection_path")]
    if host.get("selection_path"):
        for key in ("provider_path", "selection_path"):
            paths.extend(str((home / host[key]).with_suffix(suffix)) for suffix in (".yml", ".yaml"))
    return list(dict.fromkeys(paths))


def shadowed_configuration_paths(host: dict, home: Path) -> set[str]:
    if not host.get("selection_path"):
        return set()
    result = set()
    for key in ("provider_path", "selection_path"):
        selected = home / host[key]
        result.update(str(selected.with_suffix(suffix)) for suffix in (".yml", ".yaml") if selected.with_suffix(suffix) != selected)
    return result


def require_omp_settings_migration(host: dict, home: Path) -> None:
    target = home / host["selection_path"]
    legacy = home / ".omp/agent/settings.json"
    if not target.exists() and not target.is_symlink() and (legacy.exists() or legacy.is_symlink()):
        raise ConfigurationError("OMP legacy settings.json requires OMP's official settings migration before changing model selection; existing settings were preserved", 422)


def require_omp_models_migration(host: dict, home: Path) -> None:
    path = home / host["provider_path"]
    legacy = path.with_suffix(".json")
    if not path.exists() and not path.is_symlink() and (legacy.exists() or legacy.is_symlink()):
        raise ConfigurationError("OMP legacy models.json requires OMP's official models migration before management; creating a parallel models.yml would shadow existing providers", 422)


def omp_provider_specs(host: dict, provider: dict, *, home: Path, operation: str) -> list[dict]:
    from app.services.provider_endpoints import inference_endpoint
    require_omp_models_migration(host, home)
    protocol = provider.get("protocol")
    if protocol not in ("openai", "anthropic", "gemini"):
        raise ConfigurationError("OMP custom providers require a declared OpenAI, Anthropic or Gemini API", 422)
    endpoint = "generateContent" if protocol == "gemini" else inference_endpoint(provider)
    api = {"responses": "openai-responses", "chat/completions": "openai-completions", "messages": "anthropic-messages", "generateContent": "google-generative-ai"}.get(endpoint)
    if not api:
        raise ConfigurationError("Select the OMP provider's actual supported inference endpoint", 422)
    models = provider.get("model_map")
    if not isinstance(models, dict) or not models:
        raise ConfigurationError("OMP requires at least one explicit model mapping", 422)
    reference = provider.get("credential_ref")
    secret = "{{credential:" + reference + "}}" if isinstance(reference, str) else None
    if not secret or not SECRET_REF.fullmatch(secret):
        raise ConfigurationError("OMP custom provider requires a configured Keychain credential; native OAuth/account caches are never borrowed", 422)
    windows = provider.get("model_context_windows", {})
    if not isinstance(windows, dict) or any(type(capacity) is not int or not 0 < capacity <= 9007199254740991 for capacity in windows.values()):
        raise ConfigurationError("OMP model_context_windows must contain positive safe integer capacities", 422)
    native = {"baseUrl": provider["base_url"], "api": api, "apiKey": secret,
              "models": [{"id": value, "name": alias, **({"contextWindow": windows[value]} if value in windows else {})} for alias, value in models.items()]}
    return [{"path": str(home / host["provider_path"]), "kind": host["provider_format"], "pointer": ["providers", provider["id"]],
             "value": native, "owner": provider["id"], "operation": operation}]


def omp_activation_specs(host: dict, provider: dict, model: str, *, home: Path) -> list[dict]:
    require_omp_settings_migration(host, home)
    # A colon suffix is OMP syntax, not a literal model identifier. Do not write
    # an ambiguous selector that could resolve another model or thinking level.
    if any(char in model for char in (",", "\n", "\r")) or re.search(r":(?:off|minimal|low|medium|high|xhigh|max|auto|inherit)$", model):
        raise ConfigurationError("OMP model identifier conflicts with native selector syntax; select an unambiguous native model identifier", 422)
    return [{"path": str(home / host["selection_path"]), "kind": "yaml", "pointer": ["modelRoles", "default"],
             "value": provider["id"] + "/" + model, "owner": "activation:omp", "operation": "install",
             "takeover_selected_field": True, "activation_field": True}]


def cursor_prompt_spec(host: dict, asset: dict, *, home: Path, operation: str) -> dict:
    name = asset["id"].replace("_", "-")
    # Current Cursor replaces commands with Skills; manual invocation is explicit
    # rather than silently claiming filesystem access to global User Rules.
    content = "---\nname: " + name + "\ndescription: " + json.dumps(asset["name"], ensure_ascii=False) + "\ndisable-model-invocation: true\n---\n\n" + asset["content"]
    return {"path": str(home / host["prompt_dir"] / name / "SKILL.md"), "kind": "file", "value": content,
            "owner": asset["id"], "operation": operation, "asset_kind": "prompt"}


def antigravity_prompt_spec(host: dict, asset: dict, *, home: Path, operation: str) -> dict:
    content = "---\ntrigger: always_on\ndescription: " + json.dumps(asset["name"], ensure_ascii=False) + "\n---\n\n" + asset["content"]
    if operation == "install" and len(content.encode("utf-8")) > 24000:
        raise ConfigurationError("Antigravity truncates rules above 24000 UTF-8 bytes; shorten this prompt before applying", 422)
    return {"path": str(home / host["prompt_dir"] / (asset["id"] + ".md")), "kind": "file", "value": content,
            "owner": asset["id"], "operation": operation, "asset_kind": "prompt"}


def native_skill(asset: dict, host_id: str) -> tuple[str, dict]:
    """Make plain editor content discoverable; preserve authored metadata/body.

    Cursor's name must match the directory. OMP/Antigravity require a description.
    Existing frontmatter is never silently repaired or discarded.
    """
    files = dict(asset["files"])
    name = asset["id"].replace("_", "-") if host_id in ("omp", "cursor", "antigravity", "codex-desktop") else asset["id"]
    text = files["SKILL.md"]
    if host_id not in ("omp", "cursor", "antigravity", "codex-desktop"):
        return name, files
    if text.startswith("---\n") or text.startswith("---\r\n"):
        from app.integrations.structured_config import parse_document
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", text, re.S)
        if not match:
            raise ConfigurationError("Native SKILL.md frontmatter is not closed; edit the shared definition before applying", 422)
        try:
            metadata = parse_document("yaml", match[1])
        except ConfigurationError as exc:
            if exc.status_code == 503:
                raise
            raise ConfigurationError("Native SKILL.md frontmatter must be an unambiguous YAML mapping", 422) from exc
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            raise ConfigurationError("This host requires a non-empty Skill description in YAML frontmatter", 422)
        if host_id in ("omp", "antigravity", "codex-desktop") and "name" in metadata:
            authored_name = metadata["name"]
            if not isinstance(authored_name, str) or not authored_name.strip() or any(char in authored_name for char in ("/", "\\", "\n", "\r")):
                raise ConfigurationError("Native SKILL.md frontmatter name must be a non-empty name without path separators", 422)
        if host_id == "codex-desktop" and "name" not in metadata:
            raise ConfigurationError("Native SKILL.md frontmatter requires a name for Codex", 422)
        if host_id == "cursor":
            name = metadata.get("name")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                raise ConfigurationError("Cursor Skill name must contain lowercase letters, digits and single hyphens and match its folder", 422)
    else:
        files["SKILL.md"] = "---\nname: " + name + "\ndescription: " + json.dumps(asset["name"], ensure_ascii=False) + "\n---\n\n" + text
    return name, files
