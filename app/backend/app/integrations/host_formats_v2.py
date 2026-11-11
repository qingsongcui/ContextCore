"""Native format extensions, independently authored from pinned format evidence.

Format support never proves host loading.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app.integrations.config_transactions import ConfigurationError, SECRET_REF

UPSTREAM_COMMIT = "d8983bb10b82a1ccc15ae5f44ab070902623e990"
UPSTREAM_LICENSE = "MIT"
UPSTREAM_REPOSITORY = "https://github.com/qingsongcui/ContextCore"

HOST_FORMATS = {
    "grok-build": {"name": "Grok Build CLI", "mcp_path": ".grok/config.toml", "format": "toml", "container": "mcp_servers",
                   "skill_dir": ".grok/skills", "prompt_path": ".grok/AGENTS.md",
                   "provider_source_path": ".grok/config.toml", "provider_format": "toml",
                   "provider_unsupported_reason": "Native custom model configuration requires models.default selection; additive provider metadata must not silently change the global default"},
    "kimi-code": {"name": "Kimi Code CLI", "mcp_path": ".kimi-code/mcp.json", "format": "json", "container": "mcpServers",
                  "provider_path": ".kimi-code/config.toml", "provider_format": "toml",
                  "skill_dir": ".kimi-code/skills", "prompt_path": ".kimi-code/AGENTS.md",
                  "provider_mode": "append_providers_and_models_preserve_default"},
    "minimax-code": {"name": "MiniMax Code", "mcp_path": ".minimax/mcp.json", "format": "json", "container": "mcpServers",
                     "provider_source_path": ".minimax/config.yaml", "provider_path": ".minimax/config.yaml", "provider_format": "yaml", "runtime_dependency": "ruamel.yaml",
                     "skill_dir": ".minimax/skills", "prompt_path": ".minimax/AGENTS.md",
                     "provider_mode": "append_custom_provider_preserve_native_defaults"},
    "pi": {"name": "Pi CLI", "mcp_path": ".pi/agent/mcp.json", "format": "json", "container": "mcpServers",
           "provider_path": ".pi/agent/models.json", "provider_format": "json",
           "skill_dir": ".pi/agent/skills", "prompt_path": ".pi/agent/AGENTS.md",
           "provider_mode": "append_provider_preserve_settings_defaults", "provider_format_detail": "Strict JSON subset; existing native JSON5 is preserved and refused until a lossless renderer is available"},
    "antigravity-cli": {"name": "Antigravity CLI (agy)", "mcp_path": ".gemini/config/mcp_config.json", "format": "json", "container": "mcpServers",
                        "skill_dir": ".gemini/config/skills", "prompt_path": ".gemini/config/AGENTS.md",
                        "provider_source_path": ".gemini/antigravity-cli/settings.json", "provider_format": "environment_and_private_profile",
                        "provider_unsupported_reason": "agy credentials belong to process env/private gemini_dir profiles; native settings.json alone does not configure a provider"},
    "hermes": {"name": "Hermes Agent", "mcp_path": ".hermes/config.yaml", "provider_path": ".hermes/config.yaml", "format": "yaml", "container": "mcp_servers", "provider_format": "yaml", "runtime_dependency": "ruamel.yaml", "skill_dir": ".hermes/skills", "prompt_path": ".hermes/SOUL.md",
               "mcp_source_path": ".hermes/config.yaml", "provider_source_path": ".hermes/config.yaml", "source_format": "yaml",
               "provider_mode": "append_or_merge_named_custom_providers_preserve_model_defaults"},
    "openclaw": {"name": "OpenClaw", "skill_dir": ".openclaw/skills", "prompt_path": ".openclaw/AGENTS.md",
                 "provider_source_path": ".openclaw/openclaw.json", "provider_path": ".openclaw/openclaw.json", "provider_format": "json5", "source_format": "json5", "runtime_dependency": "json-five",
                 "unsupported_reason": "This pinned upstream has no OpenClaw native MCP synchronization module; provider JSON5 and Skills/prompts are independently supported"},
}

# Evidence retained separately from paths advertised as writable provider support.
PROVIDER_FORMAT_EVIDENCE = {
    "grok-build": {"container": "model", "selection": "models.default", "required": ["model", "name", "base_url", "api_key_or_env_key", "api_backend", "context_window"]},
    "kimi-code": {"containers": ["providers", "models"], "selection": "default_model", "required_model": ["model", "provider", "max_context_size"], "types": ["kimi", "openai", "openai_responses", "anthropic", "google-genai", "vertexai"]},
    "minimax-code": {"container": "custom_provider", "selection": "defaultModel", "api_field": "api", "endpoint": "options.baseURL", "credential": "options.apiKey"},
    "pi": {"container": "providers", "selection_file": ".pi/agent/settings.json", "selection": ["defaultProvider", "defaultModel"], "endpoint": "baseUrl", "credential": "apiKey"},
    "openclaw": {"container": "models.providers", "source_format": "json5"},
    "hermes": {"container": "custom_providers", "selection": "model", "source_format": "yaml", "mcp_container": "mcp_servers"},
    "antigravity-cli": {"credential_env": ["GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL"], "selection": ["modelProvider", "model"], "scope": "private gemini_dir profile; global settings not overwritten"},
}


def _target(home: Path, relative: str) -> Path:
    base = Path(home).resolve()
    path = Path(home) / relative
    if not path.resolve().is_relative_to(base):
        raise ConfigurationError("Native configuration target escapes the selected home", 422)
    return path


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ConfigurationError("Native managed keys require letters, digits, _ or -", 422)
    return value


def _toml(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml(v) for v in value) + "]"
    if isinstance(value, dict) and all(isinstance(k, str) for k in value):
        return "{ " + ", ".join(f"{_toml(k)} = {_toml(v)}" for k, v in value.items()) + " }"
    raise ConfigurationError("Native TOML values must be bounded strings, integers, booleans, lists or maps", 422)


def _table(parts: list[str], fields: dict) -> str:
    return "[" + ".".join(_toml(p) for p in parts) + "]\n" + "\n".join(f"{_toml(key)} = {_toml(value)}" for key, value in fields.items())


def _native_mcp(config: dict) -> tuple[str, dict]:
    if not isinstance(config, dict):
        raise ConfigurationError("MCP configuration must be an object", 422)
    transport = config.get("transport", config.get("type", "stdio"))
    if transport == "streamable-http":
        transport = "http"
    if transport not in {"stdio", "http", "sse"}:
        raise ConfigurationError("Unsupported native MCP transport", 422)
    fields = ("command", "args", "env", "cwd") if transport == "stdio" else ("url", "headers")
    native = {k: copy.deepcopy(config[k]) for k in fields if k in config}
    if transport == "stdio":
        if not isinstance(native.get("command"), str) or not native["command"].strip():
            raise ConfigurationError("stdio MCP requires a command", 422)
        if not isinstance(native.get("args", []), list) or not all(isinstance(v, str) for v in native.get("args", [])):
            raise ConfigurationError("MCP args must be a string array", 422)
        if not isinstance(native.get("env", {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in native.get("env", {}).items()):
            raise ConfigurationError("MCP env must map strings", 422)
    else:
        url = urlsplit(native.get("url", ""))
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            raise ConfigurationError("Remote MCP requires a credential-free HTTP(S) URL", 422)
        if not isinstance(native.get("headers", {}), dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in native.get("headers", {}).items()):
            raise ConfigurationError("MCP headers must map strings", 422)
    return transport, native


def _pi_collision(path: Path, name: str) -> None:
    if not path.exists():
        return
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ConfigurationError("Pi MCP configuration exceeds the read limit", 422)
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise ConfigurationError("Pi MCP JSON is unreadable; native configuration is preserved", 422) from exc
    entries = root.get("mcpServers", {}) if isinstance(root, dict) else None
    if not isinstance(entries, dict):
        raise ConfigurationError("Pi mcpServers must be an object", 422)
    if any(key != name and key.replace("-", "_") == name.replace("-", "_") for key in entries):
        raise ConfigurationError("Pi treats MCP names differing only in '-' and '_' as identical", 409)


def render_mcp_specs(host_id: str, host: dict, config: dict, *, name: str, owner: str,
                     operation: str, home: Path) -> list[dict]:
    """Build reviewed transaction specs. Never starts a host or executes a tool."""
    _identifier(name)
    if not host.get("mcp_path") or host_id not in HOST_FORMATS:
        raise ConfigurationError(host.get("unsupported_reason", "Native MCP renderer is unsupported"), 422)
    path = _target(home, host["mcp_path"])
    transport, native = _native_mcp(config)
    if host_id in {"pi", "antigravity-cli"} and transport == "sse":
        raise ConfigurationError(f"{host_id} supports stdio/streamable HTTP, not SSE", 422)
    if host_id == "hermes":
        from app.integrations.structured_config import runtime_status, StructuredFormatUnavailable
        if runtime_status("yaml")["status"] != "available":
            raise StructuredFormatUnavailable("yaml")
        native.pop("cwd", None)  # not part of this pinned Hermes MCP transport contract
        native["enabled"] = True
        stale = [key for key in ("command", "args", "env", "cwd", "url", "headers", "type", "transport") if key not in native]
        return [{"path": str(path), "kind": "yaml", "pointer": ["mcp_servers", name], "owner": owner,
                 "operation": operation, "value": native, "merge": True, "remove_fields": stale}]
    if host_id == "grok-build":
        # Grok infers command/url; its native header field is headers.
        body = _table(["mcp_servers", name], native)
        return [{"path": str(path), "kind": "block", "marker": name, "format": "toml", "owner": owner, "operation": operation, "value": body}]
    if host_id == "kimi-code":
        if transport == "sse":
            native["transport"] = "sse"
        native["enabled"] = True
    elif host_id == "minimax-code":
        native["type"] = transport
        native["enabled"] = True
    elif host_id == "pi":
        _pi_collision(path, name)
        native["type"] = transport
        native["enabled"] = True
    elif host_id == "antigravity-cli":
        native["disabled"] = False
    else:
        raise ConfigurationError("Native MCP renderer is unsupported", 422)
    return [{"path": str(path), "kind": "json", "pointer": [host["container"], name], "owner": owner, "operation": operation, "value": native}]


def render_provider_specs(host_id: str, host: dict, provider: dict, operation: str, *, home: Path, replacing_selected=False) -> list[dict]:
    """Append named native entries; never writes native active-provider defaults."""
    from app.services.provider_endpoints import require_endpoint, inference_endpoint
    if host_id in {"hermes", "minimax-code", "openclaw"}:
        return _render_structured_provider(host_id, host, provider, operation, home=home, replacing_selected=replacing_selected)
    if host_id not in {"pi", "kimi-code"} or not host.get("provider_path"):
        raise ConfigurationError(host.get("provider_unsupported_reason", host.get("unsupported_reason", "Native provider renderer is unsupported")), 422)
    owner = _identifier(provider["id"])
    protocol = provider.get("protocol")
    if protocol not in {"openai", "anthropic"}:
        raise ConfigurationError("This native provider renderer supports OpenAI or Anthropic formats", 422)
    models = provider.get("model_map", {})
    if not isinstance(models, dict) or not models or not all(isinstance(k, str) and k and isinstance(v, str) and v for k, v in models.items()):
        raise ConfigurationError("At least one explicit model mapping is required", 422)
    ref = provider.get("credential_ref")
    secret = "{{credential:" + ref + "}}" if isinstance(ref, str) else None
    if not secret or not SECRET_REF.fullmatch(secret):
        raise ConfigurationError("A configured credential reference is required; native OAuth caches are never borrowed", 422)
    path = _target(home, host["provider_path"])
    if host_id == "pi":
        if operation == 'install':
            require_endpoint(provider, 'chat/completions' if protocol == 'openai' else 'messages')
        native = {"baseUrl": provider["base_url"], "api": "openai-completions" if protocol == "openai" else "anthropic-messages",
                  "apiKey": secret, "models": [{"id": native, "name": alias} for alias, native in models.items()]}
        return [{"path": str(path), "kind": "json", "pointer": ["providers", owner], "value": native, "owner": owner, "operation": operation}]
    windows = provider.get("model_context_windows", {})
    if not isinstance(windows, dict) or any(isinstance(windows.get(native), bool) or not isinstance(windows.get(native), int) or windows[native] <= 0 or windows[native] > 9223372036854775807 for native in models.values()):
        raise ConfigurationError("Kimi requires an explicit positive model_context_windows value for every native model", 422)
    endpoint = inference_endpoint(provider) if operation == 'install' else None
    native_provider = {"type": ("openai_responses" if endpoint == 'responses' else "openai") if protocol == "openai" else "anthropic", "base_url": provider["base_url"], "api_key": secret}
    sections = [_table(["providers", owner], native_provider)]
    for alias, native in models.items():
        sections.append(_table(["models", f"{owner}:{alias}"], {"model": native, "provider": owner, "max_context_size": windows[native]}))
    return [{"path": str(path), "kind": "block", "marker": owner, "format": "toml", "value": "\n\n".join(sections), "owner": owner, "operation": operation}]


def validate_prompt(host_id: str, content: str) -> None:
    if host_id == "minimax-code" and len(content.encode("utf-8")) > 32768:
        raise ConfigurationError("MiniMax native global instructions must not exceed 32 KiB of UTF-8", 422)



def _read_structured(path: Path, format: str) -> dict:
    from app.integrations.structured_config import parse_document
    if path.exists():
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ConfigurationError("Native document exceeds read limit", 422)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ConfigurationError("Native document is not readable UTF-8; original file is preserved", 422) from exc
    else:
        text = "{}"
    return parse_document(format, text)


def _render_structured_provider(host_id: str, host: dict, provider: dict, operation: str, *, home: Path, replacing_selected=False) -> list[dict]:
    """Known native fields only; no default activation and no host-token reuse.

    Hermes API modes and OpenClaw APIs additionally verified in the same pinned
    src/config/{hermes,openclaw}ProviderPresets.ts and deeplink/tests.rs.
    """
    owner = _identifier(provider["id"])
    path = _target(home, host["provider_path"])
    format = host["provider_format"]
    document = _read_structured(path, format)
    removing = operation in {"uninstall", "remove", "delete"}
    models = provider.get("model_map", {})
    if not isinstance(models, dict) or (not removing and not models) or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in models.items()):
        raise ConfigurationError("An explicit native model mapping is required", 422)
    protocol = provider.get("protocol")
    if protocol not in {"openai", "anthropic"}:
        raise ConfigurationError("Native custom providers require an evidenced OpenAI or Anthropic format", 422)
    ref = provider.get("credential_ref")
    secret = "{{credential:" + ref + "}}" if isinstance(ref, str) else None
    if not removing and (not secret or not SECRET_REF.fullmatch(secret)):
        raise ConfigurationError("A configured credential reference is required; host login caches are never borrowed", 422)
    from app.services.provider_endpoints import inference_endpoint
    responses = not removing and inference_endpoint(provider) == 'responses'
    enabled = provider.get("enabled", True)
    incoming_models = set(models.values())
    if host_id == "hermes":
        entries = document.get("custom_providers", [])
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ConfigurationError("Hermes custom_providers must be a sequence of objects", 422)
        matches = [index for index, entry in enumerate(entries) if entry.get("name") == owner]
        if len(matches) > 1:
            raise ConfigurationError("Hermes provider name is ambiguous", 409)
        overlays = document.get("providers", {})
        if not matches and isinstance(overlays, dict) and owner in overlays:
            raise ConfigurationError("Hermes providers dictionary overlays are host-owned; manage only named custom_providers", 409)
        active = document.get("model", {})
        if isinstance(active, dict) and active.get("provider") == owner and not replacing_selected:
            if removing or not enabled or active.get("default") and active["default"] not in incoming_models:
                raise ConfigurationError("Select another native Hermes model before removing the active provider/model", 409)
        index = matches[0] if matches else len(entries)
        if removing and not matches:
            return []
        native = {"name": owner, "base_url": provider["base_url"], "api_key": secret,
                  "api_mode": "anthropic_messages" if protocol == "anthropic" else ("codex_responses" if responses else "chat_completions"),
                  "model": next(iter(models.values()), ""), "models": {model: {} for model in models.values()}}
        pointer = ["custom_providers", index]
    elif host_id == "minimax-code":
        prefix = f"custom_provider:{owner}/"
        for field in ("defaultModel", "defaultLightModel"):
            selected = document.get(field)
            if isinstance(selected, str) and selected.startswith(prefix) and not (replacing_selected and field == "defaultModel"):
                if removing or not enabled or selected[len(prefix):] not in incoming_models:
                    raise ConfigurationError("Select another native MiniMax model before removing its default/light provider/model", 409)
        native = {"name": provider.get("name", owner), "kind": "custom", "enabled": enabled,
                  "api": "anthropic-messages" if protocol == "anthropic" else ("openai-responses" if responses else "openai-completions"),
                  "options": {"baseURL": provider["base_url"], "apiKey": secret},
                  "models": {model: {} for model in models.values()}}
        pointer = ["custom_provider", owner]
    else:
        agents = document.get("agents", {})
        defaults = agents.get("defaults", {}) if isinstance(agents, dict) else {}
        selection = defaults.get("model", {}) if isinstance(defaults, dict) else {}
        references = ([selection] if isinstance(selection, str) else
                      [selection.get("primary"), *selection.get("fallbacks", [])] if isinstance(selection, dict) and isinstance(selection.get("fallbacks", []), list) else [])
        prefix = owner + "/"
        if any(isinstance(selected, str) and selected.startswith(prefix) and
               (removing or not enabled or selected[len(prefix):] not in incoming_models) for index, selected in enumerate(references) if not (replacing_selected and index == 0)):
            raise ConfigurationError("Select another native OpenClaw primary/fallback before removing its provider/model", 409)
        native = {"baseUrl": provider["base_url"], "apiKey": secret,
                  "api": "anthropic-messages" if protocol == "anthropic" else ("openai-responses" if responses else "openai-completions"),
                  "models": [{"id": model, "name": alias} for alias, model in models.items()]}
        pointer = ["models", "providers", owner]
    return [{"path": str(path), "kind": format, "pointer": pointer, "owner": owner, "operation": operation,
             "value": native, "merge": True, "remove_fields": ["models"], "native_identity": {"name": owner} if host_id == "hermes" else None}]


ACTIVATION_FORMATS = {
    # Source: pi_config/mod.rs::read_pi_native_defaults (settings supports JSON5).
    "pi": {"path": ".pi/agent/settings.json", "kind": "json5", "format": "json5"},
    # Source: kimi_config.rs::select_provider_model; points to a models table KEY.
    "kimi-code": {"path": ".kimi-code/config.toml", "kind": "toml_selection", "format": "toml"},
    # Source: hermes_config.rs::apply_switch_defaults; only these two fields change.
    "hermes": {"path": ".hermes/config.yaml", "kind": "yaml", "format": "yaml"},
    # Source: mcode_config.rs native-default guard + validate_additive_flow_with_real_api_key
    # fixture: defaultModel is custom_provider:<id>/<model>; light default is independent.
    "minimax-code": {"path": ".minimax/config.yaml", "kind": "yaml", "format": "yaml"},
    # Source: openclaw_config.rs::{OpenClawDefaultModel,set_default_model}.
    "openclaw": {"path": ".openclaw/openclaw.json", "kind": "json5", "format": "json5"},
}


def render_activation_specs(host_id: str, provider: dict, model_id: str, *, owner: str,
                            home: Path, original_text: str | None = None) -> list[dict]:
    """Render an explicitly selected native default, separate from coexist install.

    Caller must invoke only for activate_provider=True after resolving catalog
    identity. No file is read here; optional original_text is caller-authorized
    text for shape validation. ConfigurationTransactions keeps the FIRST private
    activation snapshot and restores selected field scopes, including absence.
    OpenClaw owns the model object as a stable restore scope while only merging
    primary during activation, preserving fallbacks and native extras.
    """
    if host_id not in ACTIVATION_FORMATS:
        raise ConfigurationError("Native provider activation has not been verified for this host surface", 422)
    if owner != "activation:" + host_id:
        raise ConfigurationError("Native activation fields require the host activation owner", 422)
    provider_id = _identifier(provider.get("id"))
    if provider.get("enabled", True) is not True:
        raise ConfigurationError("A disabled provider cannot become the native default", 422)
    mappings = provider.get("model_map", {})
    if not isinstance(model_id, str) or not model_id or not isinstance(mappings, dict):
        raise ConfigurationError("Select one explicit native model from the provider map", 422)
    if model_id in mappings:
        alias, native_model = model_id, mappings[model_id]
    else:
        alias = next((key for key, value in mappings.items() if value == model_id), None)
        native_model = model_id
    if not alias or not isinstance(alias, str) or not isinstance(native_model, str) or not native_model:
        raise ConfigurationError("Selected native model does not belong to this provider's declared map", 422)
    definition = ACTIVATION_FORMATS[host_id]
    kind, format = definition["kind"], definition["format"]
    path = _target(home, definition["path"])
    document = {}
    if format in {"yaml", "json5"}:
        from app.integrations.structured_config import parse_document, runtime_status, StructuredFormatUnavailable
        if runtime_status(format)["status"] != "available":
            raise StructuredFormatUnavailable(format)
        if original_text is not None:
            document = parse_document(format, original_text)
    elif original_text is not None:
        import tomllib
        try:
            document = tomllib.loads(original_text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigurationError("Existing native TOML is invalid", 422) from exc
    if host_id == "pi":
        fields = [(["defaultProvider"], provider_id), (["defaultModel"], native_model)]
    elif host_id == "kimi-code":
        windows = provider.get("model_context_windows", {})
        capacity = windows.get(native_model) if isinstance(windows, dict) else None
        if isinstance(capacity, bool) or not isinstance(capacity, int) or not 0 < capacity <= 9223372036854775807:
            raise ConfigurationError("Kimi activation requires an explicit positive context window for the selected native model", 422)
        # Matches render_provider_specs [models.<provider id>:<map alias>].
        fields = [(["default_model"], f"{provider_id}:{alias}")]
    elif host_id == "hermes":
        fields = [(["model", "provider"], provider_id), (["model", "default"], native_model)]
    elif host_id == "minimax-code":
        # Do not switch native defaultLightModel as a side effect.
        fields = [(["defaultModel"], f"custom_provider:{provider_id}/{native_model}")]
    else:
        # Own the model object consistently across A -> B -> restore. Updating
        # only a newly-created primary leaf would restore to model:{}, which is
        # invalid for native OpenClawDefaultModel (required primary). Merge changes
        # only primary during activation, retaining fallbacks and native extras.
        fields = [(["agents", "defaults", "model"], {"primary": f"{provider_id}/{native_model}"})]
    result = []
    for pointer, value in fields:
        node = document
        for component in pointer[:-1]:
            if not isinstance(node, dict):
                raise ConfigurationError("Native selection ancestor is not an object; explicit format migration is required", 422)
            node = node.get(component, {})
        if not isinstance(node, dict):
            raise ConfigurationError("Native selection parent is not an object", 422)
        expected_type = dict if host_id == "openclaw" else str
        if pointer[-1] in node and node[pointer[-1]] is not None and not isinstance(node[pointer[-1]], expected_type):
            raise ConfigurationError("Native selected provider/model field has an unsupported shape", 422)
        result.append({"path": str(path), "kind": kind, "pointer": pointer, "value": value,
                       "owner": owner, "operation": "install", "takeover_selected_field": True,
                       "activation_field": True, "merge": host_id == "openclaw",
                       "restore_metadata": {"mode": "first_activation_snapshot", "format": format,
                                            "pointer": pointer, "preserve_unselected": True}})
    return result
