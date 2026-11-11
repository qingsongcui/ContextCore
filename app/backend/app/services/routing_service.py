"""Opt-in protocol-preserving local router, separate from browser/compute authority."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import json
import secrets
import time
from urllib.parse import urlsplit
from ipaddress import ip_address

import httpx
from fastapi import HTTPException, Request
from starlette.responses import Response, StreamingResponse

from app.database import db, get_app_setting, set_app_setting, log_audit, now_iso, new_id
from app.services import usage_service
from app.integrations.config_transactions import ConfigurationError, configuration_lock
from app.integrations.secret_store import SecretUnavailable

MAX_BODY = 4 * 1024 * 1024
MAX_RESPONSE = 16 * 1024 * 1024
MAX_EVENT = 1024 * 1024
DEFAULTS = {"enabled": False, "provider_id": None, "request_timeout_seconds": 120, "fallback_provider_ids": [], "model_routes": {}, "routing_strategy": "ordered", "circuit_threshold": 3, "circuit_cooldown_seconds": 60}


def settings(conn=None):
    value = get_app_setting("routing_v2_settings", {}, conn=conn) or {}
    return {**DEFAULTS, **{k: value[k] for k in DEFAULTS if k in value},
            "token_configured": bool(get_app_setting("routing_v2_token_hash", None, conn=conn))}


def configure(patch):
    if set(patch) - set(DEFAULTS):
        raise HTTPException(422, "Unsupported routing setting")
    if "enabled" in patch and not isinstance(patch["enabled"], bool):
        raise HTTPException(422, "enabled must be boolean")
    if "provider_id" in patch and patch["provider_id"] is not None and (not isinstance(patch["provider_id"], str) or not patch["provider_id"].strip()):
        raise HTTPException(422, "provider_id must be a nonempty identifier")
    if "request_timeout_seconds" in patch and (isinstance(patch["request_timeout_seconds"], bool) or not isinstance(patch["request_timeout_seconds"], (int, float)) or not 1 <= patch["request_timeout_seconds"] <= 600):
        raise HTTPException(422, "Request timeout must be between 1 and 600 seconds")
    if "fallback_provider_ids" in patch and (not isinstance(patch["fallback_provider_ids"], list) or len(patch["fallback_provider_ids"]) > 8 or any(not isinstance(v, str) or not v.strip() for v in patch["fallback_provider_ids"])):
        raise HTTPException(422, "Fallback providers must be up to eight provider identifiers")
    if "model_routes" in patch and (not isinstance(patch["model_routes"], dict) or len(patch["model_routes"]) > 200 or any(not isinstance(k, str) or not isinstance(v, list) or not v or len(v) > 8 or any(not isinstance(i, str) or not i.strip() for i in v) for k, v in patch["model_routes"].items())):
        raise HTTPException(422, "Model routes map model names to ordered provider identifier lists")
    if patch.get("routing_strategy", "ordered") not in {"ordered", "round_robin"}:
        raise HTTPException(422, "Routing strategy must be ordered or round_robin")
    for key, high in (("circuit_threshold", 100), ("circuit_cooldown_seconds", 3600)):
        if key in patch and (isinstance(patch[key], bool) or not isinstance(patch[key], int) or not 1 <= patch[key] <= high):
            raise HTTPException(422, f"Invalid {key}")
    with configuration_lock:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = settings(conn)
            value = {k: current[k] for k in DEFAULTS}
            value.update(patch)
            if value["enabled"] and not value["provider_id"]:
                raise HTTPException(422, "Select a provider before enabling routing")
            referenced_ids = list(dict.fromkeys([
                *([value["provider_id"]] if value["provider_id"] else []),
                *value["fallback_provider_ids"],
                *(pid for route in value["model_routes"].values() for pid in route),
            ]))
            if referenced_ids:
                placeholders = ",".join("?" for _ in referenced_ids)
                found = {
                    row["id"]
                    for row in conn.execute(
                        f"SELECT id FROM management_entities WHERE kind='provider' AND id IN ({placeholders})",
                        referenced_ids,
                    ).fetchall()
                }
                if any(pid not in found for pid in referenced_ids):
                    raise HTTPException(
                        409,
                        "Referenced provider does not exist in shared definitions; reload providers or remove stale route references before saving",
                    )
            set_app_setting("routing_v2_settings", value, conn=conn)
            log_audit(actor="user", action="routing.configure", target="routing", conn=conn)
    return settings()


def rotate_token(confirmed=False):
    if confirmed is not True:
        raise HTTPException(422, "Explicit confirmation is required to create or rotate a routing credential")
    token = secrets.token_urlsafe(40)
    with db() as conn:
        set_app_setting("routing_v2_token_hash", hashlib.sha256(token.encode()).hexdigest(), conn=conn)
        log_audit(actor="user", action="routing.token.rotate", target="routing", conn=conn)
    return {"token": token, "token_hint": token[-4:], "note": "Shown once; the prior token is immediately invalid"}


def authenticate(authorization):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Independent routing Bearer credential required")
    token = authorization[7:]
    if not token or token.strip() != token or len(token) > 256 or any(c.isspace() for c in token):
        raise HTTPException(401, "Invalid routing credential")
    expected = get_app_setting("routing_v2_token_hash", None)
    digest = hashlib.sha256(token.encode()).hexdigest()
    if not expected or not hmac.compare_digest(expected, digest):
        raise HTTPException(401, "Invalid or revoked routing credential")


def status():
    value = settings()
    return {**value, "state": "enabled" if value["enabled"] else "disabled", "protocol_conversion": "strict_text_and_tools",
            "automatic_retry": False, "safe_configured_failover": True, "compute_authorized": False,
            "active_requests": len(_active), "capabilities": ["openai_chat_passthrough", "openai_responses_passthrough", "anthropic_messages_passthrough"],
            "host_configuration_applied": False, "circuits": circuit_status(),
            "capability_matrix": {"native_openai_chat": "implemented", "native_openai_responses": "implemented", "native_anthropic_messages": "implemented", "cross_protocol_conversion": "text_system_tools_only", "openai_to_anthropic_stream": "buffered_until_final_usage", "complex_thinking_images": "unsupported_explicit_422", "cross_responses": "unsupported_explicit_422", "gemini_native": "unsupported_explicit_422", "configured_model_routing": "implemented", "ordered_or_round_robin": "implemented", "failover_connect_or_429_only": "implemented", "retry_after_stream_or_unknown": "forbidden", "automatic_host_endpoint_migration": "not_implemented"}}


def resolve_provider(provider_id):
    """Injection seam; secrets never leave this in-process resolver."""
    from app.services.management_service import resolve_provider as resolve
    return resolve(provider_id)


def client_factory(timeout):
    # Environment proxy variables must not silently receive credentials/bodies.
    return httpx.AsyncClient(timeout=httpx.Timeout(timeout), follow_redirects=False, trust_env=False)


_active = set()


def _reject_unknown(value, allowed, label):
    if not isinstance(value, dict) or any(k not in allowed and v is not None for k, v in value.items()):
        raise HTTPException(422, f"Unsupported {label} fields for protocol conversion")


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = []
        for block in value:
            _reject_unknown(block, {"type", "text"}, "text block")
            if block.get("type") != "text" or not isinstance(block.get("text"), str):
                raise HTTPException(422, "Only plain text blocks are supported in conversion")
            parts.append(block["text"])
        return "\n\n".join(parts)
    raise HTTPException(422, "Only plain text content is supported in conversion")


def _convert_request(payload, source, target):
    allowed = {"model", "messages", "max_tokens", "temperature", "top_p", "stream", "tools", "tool_choice"}
    _reject_unknown(payload, allowed | ({"system", "stop_sequences"} if source == "anthropic" else {"stop", "stream_options", "n"}), "request")
    if not isinstance(payload.get("max_tokens"), int) or isinstance(payload["max_tokens"], bool) or payload["max_tokens"] <= 0:
        raise HTTPException(422, "Conversion requires an explicit positive max_tokens")
    if not isinstance(payload.get("messages"), list):
        raise HTTPException(422, "Conversion requires messages")
    result = {k: payload[k] for k in ("model", "max_tokens", "temperature", "top_p", "stream") if k in payload}
    messages, seen = [], set()
    if source == "openai":
        if payload.get("n", 1) != 1:
            raise HTTPException(422, "Conversion supports exactly one response")
        if payload.get("stream_options"):
            _reject_unknown(payload["stream_options"], {"include_usage"}, "stream options")
        systems = []
        for msg in payload["messages"]:
            _reject_unknown(msg, {"role", "content", "tool_calls", "tool_call_id"}, "chat message")
            role = msg.get("role")
            if role == "system":
                if messages:
                    raise HTTPException(422, "Mid-conversation system messages cannot be converted")
                systems.append(_text(msg.get("content")))
                continue
            if role == "tool":
                tid = msg.get("tool_call_id")
                if not tid or tid not in seen:
                    raise HTTPException(422, "Tool result must reference a preceding tool call ID")
                messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": _text(msg.get("content"))}]})
                continue
            if role not in {"user", "assistant"}:
                raise HTTPException(422, "Unsupported chat role")
            blocks = []
            text = _text(msg.get("content"))
            if text:
                blocks.append({"type": "text", "text": text})
            for tool in msg.get("tool_calls") or []:
                _reject_unknown(tool, {"id", "type", "function"}, "tool call")
                fn = tool.get("function") or {}
                _reject_unknown(fn, {"name", "arguments"}, "tool function")
                if role != "assistant" or tool.get("type") != "function" or not tool.get("id") or not fn.get("name"):
                    raise HTTPException(422, "Malformed function tool call")
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (ValueError, TypeError):
                    raise HTTPException(422, "Tool arguments must be a JSON object") from None
                if not isinstance(args, dict):
                    raise HTTPException(422, "Tool arguments must be a JSON object")
                seen.add(tool["id"])
                blocks.append({"type": "tool_use", "id": tool["id"], "name": fn["name"], "input": args})
            messages.append({"role": role, "content": blocks or text})
        if systems:
            result["system"] = "\n\n".join(systems)
        if "stop" in payload:
            stop = payload["stop"]
            result["stop_sequences"] = [stop] if isinstance(stop, str) else stop
        if "tools" in payload:
            result["tools"] = []
            for tool in payload["tools"]:
                _reject_unknown(tool, {"type", "function"}, "tool definition")
                fn = tool.get("function") or {}
                _reject_unknown(fn, {"name", "description", "parameters"}, "tool schema")
                if tool.get("type") != "function" or not fn.get("name") or not isinstance(fn.get("parameters"), dict):
                    raise HTTPException(422, "Conversion requires function tools with JSON input schemas")
                result["tools"].append({"name": fn["name"], "input_schema": fn["parameters"], **({"description": fn["description"]} if "description" in fn else {})})
        choice = payload.get("tool_choice")
        if choice is not None:
            if isinstance(choice, str) and choice in {"auto", "required", "none"}:
                result["tool_choice"] = {"type": {"required": "any"}.get(choice, choice)}
            elif isinstance(choice, dict) and choice.get("type") == "function" and set(choice) <= {"type", "function"} and set(choice.get("function", {})) == {"name"}:
                result["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
            else:
                raise HTTPException(422, "Unsupported tool choice")
    else:
        system = _text(payload.get("system"))
        if system:
            messages.append({"role": "system", "content": system})
        for msg in payload["messages"]:
            _reject_unknown(msg, {"role", "content"}, "Anthropic message")
            role, content = msg.get("role"), msg.get("content")
            if role not in {"user", "assistant"}:
                raise HTTPException(422, "Unsupported Anthropic role")
            if isinstance(content, str):
                messages.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                raise HTTPException(422, "Message content must be text or blocks")
            text, calls = [], []
            def flush():
                if text or calls:
                    messages.append({"role": role, "content": "".join(text) or None, **({"tool_calls": calls.copy()} if calls else {})})
                    text.clear()
                    calls.clear()
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    _reject_unknown(block, {"type", "text"}, "text block")
                    text.append(_text(block.get("text")))
                elif kind == "tool_use":
                    _reject_unknown(block, {"type", "id", "name", "input"}, "tool use")
                    if role != "assistant" or not block.get("id") or not block.get("name") or not isinstance(block.get("input"), dict):
                        raise HTTPException(422, "Malformed tool use")
                    seen.add(block["id"])
                    calls.append({"id": block["id"], "type": "function", "function": {"name": block["name"], "arguments": json.dumps(block["input"], separators=(",", ":"), ensure_ascii=False)}})
                elif kind == "tool_result":
                    _reject_unknown(block, {"type", "tool_use_id", "content", "is_error"}, "tool result")
                    if role != "user" or block.get("is_error") or block.get("tool_use_id") not in seen:
                        raise HTTPException(422, "Unsupported or unpaired tool result")
                    flush()
                    messages.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": _text(block.get("content"))})
                else:
                    raise HTTPException(422, "Thinking, image, document and other block types are unsupported")
            flush()
        if "stop_sequences" in payload:
            result["stop"] = payload["stop_sequences"]
        if "tools" in payload:
            result["tools"] = []
            for tool in payload["tools"]:
                _reject_unknown(tool, {"name", "description", "input_schema"}, "tool definition")
                if not tool.get("name") or not isinstance(tool.get("input_schema"), dict):
                    raise HTTPException(422, "Malformed tool schema")
                result["tools"].append({"type": "function", "function": {"name": tool["name"], "parameters": tool["input_schema"], **({"description": tool["description"]} if "description" in tool else {})}})
        choice = payload.get("tool_choice")
        if choice is not None:
            _reject_unknown(choice, {"type", "name"}, "tool choice")
            kind = choice.get("type")
            if kind in {"auto", "any", "none"}:
                result["tool_choice"] = {"any": "required"}.get(kind, kind)
            elif kind == "tool" and choice.get("name"):
                result["tool_choice"] = {"type": "function", "function": {"name": choice["name"]}}
            else:
                raise HTTPException(422, "Unsupported tool choice")
    result["messages"] = messages
    return result


def _reply_usage(raw, source):
    usage = usage_service.normalize_usage(raw, source)
    if usage["input_tokens"] is None or usage["output_tokens"] is None:
        raise ValueError("missing_conversion_usage")
    if source == "anthropic":
        return {"prompt_tokens": usage["input_tokens"], "completion_tokens": usage["output_tokens"], "total_tokens": usage["input_tokens"] + usage["output_tokens"], "prompt_tokens_details": {"cached_tokens": usage["cache_read_tokens"]}}
    # Cache subset must be present to faithfully express uncached Anthropic input.
    if usage["cache_read_tokens"] is None:
        raise ValueError("missing_cache_semantics_for_conversion")
    return {"input_tokens": usage["uncached_input_tokens"], "output_tokens": usage["output_tokens"], "cache_read_input_tokens": usage["cache_read_tokens"]}


def _convert_reply(value, source):
    if source == "anthropic":
        text, calls = [], []
        for block in value.get("content") or []:
            if block.get("type") == "text":
                text.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append({"id": block["id"], "type": "function", "function": {"name": block["name"], "arguments": json.dumps(block["input"], ensure_ascii=False, separators=(",", ":"))}})
            else:
                raise ValueError("unsupported_response_block")
        reason = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(value.get("stop_reason"))
        if reason is None:
            raise ValueError("unsupported_stop_reason")
        return {"id": value.get("id"), "object": "chat.completion", "model": value.get("model"), "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(text) or None, **({"tool_calls": calls} if calls else {})}, "finish_reason": reason}], "usage": _reply_usage(value.get("usage"), source)}
    choices = value.get("choices") or []
    if len(choices) != 1:
        raise ValueError("unsupported_choices")
    choice = choices[0]
    msg = choice.get("message") or {}
    if any(v is not None and k not in {"role", "content", "tool_calls"} for k, v in msg.items()):
        raise ValueError("unsupported_response_message")
    content = []
    if msg.get("content"):
        if not isinstance(msg["content"], str):
            raise ValueError("unsupported_response_content")
        content.append({"type": "text", "text": msg["content"]})
    for call in msg.get("tool_calls") or []:
        args = json.loads(call["function"]["arguments"])
        if not isinstance(args, dict):
            raise ValueError("invalid_tool_arguments")
        content.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"], "input": args})
    reason = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}.get(choice.get("finish_reason"))
    if reason is None:
        raise ValueError("unsupported_finish_reason")
    return {"id": value.get("id"), "type": "message", "role": "assistant", "model": value.get("model"), "content": content, "stop_reason": reason, "stop_sequence": None, "usage": _reply_usage(value.get("usage"), source)}


class ConvertedStream:
    """Strict bounded conversion. OpenAI→Anthropic buffers until final usage.

    This avoids inventing zero input usage in the required message_start event.
    Anthropic→OpenAI can yield immediately. Neither emits a terminal on short EOF.
    """
    def __init__(self, source):
        self.source, self.buffer = source, b""
        self.usage, self.ident, self.model = {}, None, None
        self.blocks, self.finish, self.pending = {}, None, []
        self.size = 0
        self.chat_events = []
        self.done = False

    def event(self, value):
        return b"data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n\n"

    def anthropic_event(self, kind, value):
        return b"event: " + kind.encode() + b"\n" + self.event({"type": kind, **value})

    def chat(self, delta=None, finish=None, usage=None):
        return self.event({"id": self.ident, "object": "chat.completion.chunk", "model": self.model, "choices": [] if usage is not None else [{"index": 0, "delta": delta or {}, "finish_reason": finish}], **({"usage": usage} if usage is not None else {})})

    def feed(self, chunk):
        self.buffer = (self.buffer + chunk).replace(b"\r\n", b"\n")
        output = []
        while b"\n\n" in self.buffer:
            raw, self.buffer = self.buffer.split(b"\n\n", 1)
            rows = [r[5:].lstrip() for r in raw.split(b"\n") if r.startswith(b"data:")]
            if not rows:
                continue
            data = b"\n".join(rows)
            if self.source == "openai":
                self.size += len(data)
                if self.size > MAX_RESPONSE:
                    raise ValueError("conversion_buffer_limit")
                if data == b"[DONE]":
                    output.extend(self._flush_chat())
                    self.done = True
                else:
                    self.chat_events.append(json.loads(data))
            else:
                output.extend(self._anthropic(json.loads(data)))
        if len(self.buffer) > MAX_EVENT:
            raise ValueError("conversion_event_limit")
        return output

    def _anthropic(self, value):
        kind = value.get("type")
        if kind == "ping":
            return []
        if kind == "message_start":
            msg = value["message"]
            self.ident, self.model = msg.get("id"), msg.get("model")
            self.usage.update(msg.get("usage") or {})
            return [self.chat({"role": "assistant"})]
        if kind == "content_block_start":
            block, index = value["content_block"], value["index"]
            self.blocks[index] = block
            if block["type"] == "text":
                return [self.chat({"content": block["text"]})] if block.get("text") else []
            if block["type"] == "tool_use":
                tool_index = sum(v["type"] == "tool_use" for v in self.blocks.values()) - 1
                block["tool_index"] = tool_index
                return [self.chat({"tool_calls": [{"index": tool_index, "id": block["id"], "type": "function", "function": {"name": block["name"], "arguments": json.dumps(block["input"]) if block.get("input") else ""}}]})]
            raise ValueError("unsupported_stream_block")
        if kind == "content_block_delta":
            delta = value["delta"]
            if delta["type"] == "text_delta":
                return [self.chat({"content": delta["text"]})]
            if delta["type"] == "input_json_delta":
                block = self.blocks[value["index"]]
                return [self.chat({"tool_calls": [{"index": block["tool_index"], "function": {"arguments": delta["partial_json"]}}]})]
            raise ValueError("unsupported_stream_delta")
        if kind == "content_block_stop":
            return []
        if kind == "message_delta":
            self.usage.update(value.get("usage") or {})
            self.finish = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length", "tool_use": "tool_calls"}.get(value.get("delta", {}).get("stop_reason"))
            return []
        if kind == "message_stop":
            if self.finish is None:
                raise ValueError("missing_stream_finish")
            self.done = True
            return [self.chat(finish=self.finish), self.chat(usage=_reply_usage(self.usage, "anthropic")), b"data: [DONE]\n\n"]
        raise ValueError("unsupported_anthropic_event")

    def _flush_chat(self):
        usage, ident, model, reason = None, None, None, None
        blocks, sequence = {}, []
        for event in self.chat_events:
            ident, model = event.get("id") or ident, event.get("model") or model
            if event.get("usage"):
                usage = event["usage"]
            choices = event.get("choices") or []
            if len(choices) > 1 or choices and choices[0].get("index", 0) != 0:
                raise ValueError("multiple_conversion_choices")
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}
            if any(v is not None and k not in {"role", "content", "tool_calls"} for k, v in delta.items()):
                raise ValueError("unsupported_chat_delta")
            if delta.get("content"):
                key = "text"
                if key not in blocks:
                    blocks[key] = len(blocks)
                    sequence.append(("content_block_start", {"index": blocks[key], "content_block": {"type": "text", "text": ""}}))
                sequence.append(("content_block_delta", {"index": blocks[key], "delta": {"type": "text_delta", "text": delta["content"]}}))
            for call in delta.get("tool_calls") or []:
                key = "tool:" + str(call["index"])
                fn = call.get("function") or {}
                if key not in blocks:
                    if not call.get("id") or not fn.get("name"):
                        raise ValueError("tool_identity_missing")
                    blocks[key] = len(blocks)
                    sequence.append(("content_block_start", {"index": blocks[key], "content_block": {"type": "tool_use", "id": call["id"], "name": fn["name"], "input": {}}}))
                if fn.get("arguments"):
                    sequence.append(("content_block_delta", {"index": blocks[key], "delta": {"type": "input_json_delta", "partial_json": fn["arguments"]}}))
            if choice.get("finish_reason"):
                reason = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}.get(choice["finish_reason"])
        if not reason or not usage:
            raise ValueError("missing_conversion_terminal_usage")
        translated_usage = _reply_usage(usage, "openai")
        result = [self.anthropic_event("message_start", {"message": {"id": ident, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": translated_usage}})]
        result.extend(self.anthropic_event(kind, data) for kind, data in sequence)
        result.extend(self.anthropic_event("content_block_stop", {"index": index}) for index in blocks.values())
        result.append(self.anthropic_event("message_delta", {"delta": {"stop_reason": reason, "stop_sequence": None}, "usage": {"output_tokens": translated_usage["output_tokens"]}}))
        result.append(self.anthropic_event("message_stop", {}))
        return result


def _target(provider, endpoint, payload):
    if provider.get("enabled") is False:
        raise HTTPException(409, "Provider is disabled")
    protocol = provider.get("protocol")
    protocol = {"openai-compatible": "openai", "openai_compatible": "openai", "anthropic-compatible": "anthropic"}.get(protocol, protocol)
    allowed = {"chat/completions", "responses"} if protocol == "openai" else {"messages"} if protocol == "anthropic" else set()
    source_protocol = "anthropic" if endpoint == "messages" else "openai"
    if endpoint not in allowed:
        if endpoint == "responses" or protocol not in {"openai", "anthropic"}:
            raise HTTPException(422, "This endpoint/protocol conversion is unsupported")
        payload = _convert_request(payload, source_protocol, protocol)
        endpoint = "messages" if protocol == "anthropic" else "chat/completions"
    # Stateful upstream background work cannot be safely cancelled by this proxy.
    if payload.get("background") is True or (payload.get("stream") is not None and type(payload['stream']) is not bool):
        raise HTTPException(422, "Background generation or invalid stream mode is unsupported")
    if not isinstance(payload.get("model"), str) or not payload["model"].strip():
        raise HTTPException(422, "A model is required")
    from app.services.provider_endpoints import declared_endpoints
    try:
        supported = declared_endpoints(provider)
    except ConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    if supported is not None and endpoint not in supported:
        raise HTTPException(422, "Provider does not declare support for this endpoint")
    base = str(provider.get("base_url", "")).rstrip("/")
    parsed = urlsplit(base)
    try:
        ip_obj = ip_address(parsed.hostname or "")
        local = ip_obj.is_loopback and getattr(ip_obj, "ipv4_mapped", None) is None
    except ValueError:
        local = parsed.hostname == "localhost"
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.scheme not in {"https", "http"} or (parsed.scheme == "http" and not local):
        raise HTTPException(422, "Provider requires HTTPS or explicitly configured loopback HTTP")
    from app.services.provider_checks import _blocked_special_ip
    if _blocked_special_ip(parsed.hostname, allow_loopback=True):
        raise HTTPException(422, "Provider URL must not target a private or link-local address")
    mapped = dict(payload)
    mapped["model"] = (provider.get("model_map") or {}).get(mapped["model"], mapped["model"])
    secret = provider.get("api_key")
    if isinstance(secret, str) and secret and any(secret in str(value) for value in (payload.get("model"), mapped["model"])):
        raise HTTPException(422, "Selected model contains unsafe provider metadata")
    # OpenAI streaming usage must be requested, while all user options are retained.
    if protocol == "openai" and endpoint == "chat/completions" and mapped.get("stream"):
        opts = mapped.get("stream_options") or {}
        if not isinstance(opts, dict):
            raise HTTPException(422, "stream_options must be an object")
        mapped["stream_options"] = {**opts, "include_usage": True}
    headers = {"content-type": "application/json", "accept": "text/event-stream" if mapped.get("stream") else "application/json"}
    if protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if provider.get("api_key"):
            headers["x-api-key"] = provider["api_key"]
    elif provider.get("api_key"):
        headers["authorization"] = "Bearer " + provider["api_key"]
    # A base URL is an API base (e.g. https://host/v1), never a complete endpoint.
    return base + "/" + endpoint, headers, mapped, protocol


class StreamMeter:
    """Bounded SSE inspector; forwarded bytes are not rewritten or stored."""
    def __init__(self, protocol, endpoint, secret=None):
        self.protocol = protocol
        self.secret = secret
        self.endpoint = endpoint
        self.buffer = b""
        self.usage = {}
        self.first = self.last = None
        self.content_chunks = 0
        self.done = False
        self.usage_final = False
        self.invalid = False
        self.upstream_id = None
        self.model = None

    def _content(self):
        self.usage_final = False
        stamp = now_iso()
        self.first = self.first or stamp
        self.last = stamp
        self.content_chunks += 1

    def feed(self, chunk):
        self.buffer += chunk
        # SSE has both LF and CRLF framing. Bound an incomplete event, not a body.
        self.buffer = self.buffer.replace(b"\r\n", b"\n")
        while b"\n\n" in self.buffer:
            event, self.buffer = self.buffer.split(b"\n\n", 1)
            if len(event) > MAX_EVENT:
                self.invalid = True
                raise ValueError("oversize_sse_event")
            values = [line[5:].lstrip() for line in event.split(b"\n") if line.startswith(b"data:")]
            if not values:
                continue
            data = b"\n".join(values)
            if data == b"[DONE]":
                self.done = True
                continue
            try:
                value = json.loads(data)
                self.consume(value)
            except (ValueError, TypeError, AttributeError):
                self.invalid = True
                raise ValueError("invalid_sse_event") from None
        if len(self.buffer) > MAX_EVENT:
            self.invalid = True
            raise ValueError("oversize_sse_event")

    def consume(self, value):
        if not isinstance(value, dict):
            raise ValueError()
        # Gateway-controlled IDs/models are metadata, not trusted log values.
        # Reject actual credentials (also opaque OAuth/gateway keys) before a
        # stream byte is forwarded or any receipt captures these fields.
        metadata = [value.get("id"), value.get("model")]
        for envelope in (value.get("message"), value.get("response")):
            if isinstance(envelope, dict):
                metadata.extend((envelope.get("id"), envelope.get("model")))
        if any(v is not None and (not isinstance(v, str) or len(v) > 256 or any(ord(c) < 32 for c in v)) for v in metadata):
            self.invalid = True
            raise ValueError("invalid_provider_metadata")
        if isinstance(self.secret, str) and self.secret and any(isinstance(v, str) and self.secret in v for v in metadata):
            self.invalid = True
            raise ValueError("unsafe_provider_metadata")
        self.upstream_id = value.get("id") or self.upstream_id
        self.model = value.get("model") or self.model
        usage = value.get("usage")
        message = value.get("message") or {}
        response = value.get("response") or {}
        if self.protocol == "anthropic" and value.get("type") == "message_start":
            self.upstream_id = message.get("id") or self.upstream_id
            self.model = message.get("model") or self.model
            usage = message.get("usage") or usage
        if response:
            usage = response.get("usage") or usage
            self.upstream_id = response.get("id") or self.upstream_id
            self.model = response.get("model") or self.model
        if isinstance(usage, dict):
            # Anthropic supplies input at message_start and output at message_delta.
            self.usage.update(usage)
        kind = value.get("type", "")
        if kind in {"response.completed", "message_stop"}:
            self.done = True
        if kind in {"response.failed", "response.incomplete", "error"} or value.get("status") in {"failed", "incomplete", "cancelled"}:
            self.invalid = True
        if kind in {"response.output_text.delta", "response.function_call_arguments.delta"} and value.get("delta"):
            self._content()
        elif kind == "content_block_delta":
            delta = value.get("delta") or {}
            if delta.get("text") or delta.get("partial_json"):
                self._content()
        elif kind == "content_block_start" and (value.get("content_block") or {}).get("type") == "tool_use":
            self._content()
        elif value.get("choices"):
            for choice in value["choices"]:
                delta = choice.get("delta") or {}
                if delta.get("content") or delta.get("tool_calls") or delta.get("function_call"):
                    self._content()
                    break
        if isinstance(usage, dict):
            if self.protocol == "anthropic" and kind == "message_delta" and usage.get("output_tokens") is not None:
                self.usage_final = self.usage.get("input_tokens") is not None
            elif self.protocol == "openai" and (kind == "response.completed" or value.get("choices") == [] or any(c.get("finish_reason") for c in value.get("choices") or [])):
                self.usage_final = (self.usage.get("prompt_tokens", self.usage.get("input_tokens")) is not None and self.usage.get("completion_tokens", self.usage.get("output_tokens")) is not None)


def _receipt(aid, rid, provider_id, model, protocol, started, *, status, phase, requested_model=None, meter=None, ended=None, error=None, host_id=None):
    item = {"origin": "proxy", "observation_id": f"{aid}:{phase}", "attempt_id": aid,
            "logical_request_id": rid, "provider_id": provider_id, "model": model,
            "billing_model": meter.model if meter and meter.model else model,
            "requested_model": requested_model or model, "actual_model": meter.model if meter and meter.model else model,
            "protocol": protocol, "host_id": host_id, "started_at": started, "ended_at": ended,
            "status": status, "error_code": error, "raw_usage": meter.usage if meter else {}}
    if meter:
        item.update(first_content_at=meter.first, last_content_at=meter.last, content_chunks=meter.content_chunks,
                    upstream_request_id=meter.upstream_id)
    try:
        usage_service.ingest([item])
    except HTTPException as exc:
        if exc.status_code != 422 or not meter:
            raise
        # Malformed upstream usage cannot erase the attempt or leak raw payload.
        item["raw_usage"] = {}
        item["status"] = "unknown"
        item["error_code"] = "invalid_upstream_usage"
        usage_service.ingest([item])


async def _read_body(request):
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BODY:
            raise HTTPException(413, "Request body exceeds router limit")
        chunks.append(chunk)
    try:
        body = json.loads(b"".join(chunks))
    except (ValueError, UnicodeError):
        raise HTTPException(422, "Expected JSON request") from None
    if not isinstance(body, dict):
        raise HTTPException(422, "Expected a JSON object")
    return body


def circuit_status(scope=None):
    circuits = get_app_setting("routing_v2_circuits" + (":" + scope if scope else ""), {}) or {}
    now = time.time()
    return {pid: {**state, "state": "open" if state.get("open_until", 0) > now else "half_open" if state.get("open_until") else "closed"} for pid, state in circuits.items()}


def _circuit_result(provider_id, success, config):
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        key = "routing_v2_circuits" + (":" + config["_scope"] if config.get("_scope") else "")
        states = get_app_setting(key, {}, conn=conn) or {}
        old = states.get(provider_id, {})
        failures = 0 if success else old.get("failure_count", 0) + 1
        states[provider_id] = {"failure_count": failures, "open_until": time.time() + config["circuit_cooldown_seconds"] if failures >= config["circuit_threshold"] else 0, "updated_at": now_iso()}
        set_app_setting(key, states, conn=conn)


def _candidates(config, model):
    ids = config["model_routes"].get(model) or [config["provider_id"], *config["fallback_provider_ids"]]
    ids = list(dict.fromkeys(ids))
    if config["routing_strategy"] == "round_robin" and len(ids) > 1:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursors = get_app_setting("routing_v2_round_robin", {}, conn=conn) or {}
            index = cursors.get(model, 0) % len(ids)
            cursors[model] = (index + 1) % len(ids)
            set_app_setting("routing_v2_round_robin", cursors, conn=conn)
        ids = ids[index:] + ids[:index]
    circuits = circuit_status(config.get("_scope"))
    ids = [pid for pid in ids if circuits.get(pid, {}).get("state") != "open"]
    if not ids:
        raise HTTPException(503, "All configured providers have open circuits")
    return ids


def host_profile(host_id, headers):
    from app.services.host_routing import HostRouting
    from app.services.management_service import service
    authorization = headers.get("authorization")
    if not authorization and headers.get("x-api-key"):
        authorization = 'Bearer ' + headers['x-api-key']
    try:
        return HostRouting(service()).runtime(host_id, authorization)
    except ConfigurationError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


async def forward(request: Request, endpoint: str, host_id=None):
    profile = host_profile(host_id, request.headers) if host_id else None
    if profile:
        if endpoint != profile['endpoint']:
            raise HTTPException(422, 'Assistant gateway API does not match the configured native interface')
    else:
        authenticate(request.headers.get("authorization"))
    config = settings() if profile is None else None
    if config is not None and not config["enabled"]:
        raise HTTPException(409, "Local routing is disabled")
    payload = await _read_body(request)
    if profile:
        from app.services.host_routing import HostRouting
        from app.services.management_service import service
        try:
            # Body upload can yield while a saved account/model changes. Keep
            # this request's starting route, but never resolve newer credentials
            # against an obsolete reviewed profile.
            HostRouting(service()).validate_references(profile)
            config = HostRouting.dispatch(profile, payload.get('model'))
        except ConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
    def receipt(*args, **kwargs):
        return _receipt(*args, **kwargs, host_id=host_id)
    if not isinstance(payload.get("model"), str):
        raise HTTPException(422, "A model is required")
    configured_ids = config['model_routes'].get(payload['model']) or [config['provider_id'], *config['fallback_provider_ids']]
    if (payload.get('previous_response_id') or payload.get('conversation')) and len(set(configured_ids)) > 1:
        raise HTTPException(422, 'Provider-linked response state cannot use fallback or round-robin routing')
    candidates = _candidates(config, payload["model"])
    # Resolve and validate the selected set before sending any request, so a
    # configuration error cannot silently turn a fallback into protocol conversion.
    targets = []
    for pid in candidates:
        try:
            provider = resolve_provider(pid)
            if inspect.isawaitable(provider):
                provider = await provider
        except ConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        except SecretUnavailable:
            raise HTTPException(503, "Keychain is unavailable or locked; no plaintext fallback was used") from None
        outgoing_payload = payload
        if profile:
            if provider.get('version') != profile['entity_versions'].get(pid):
                raise HTTPException(409, 'Route definition changed during dispatch; request was not sent')
            provider = {**provider, 'model_map': {}}
            outgoing_payload = {**payload, 'model': config['_models'][pid]}
        url, headers, outgoing, protocol = _target(provider, endpoint, outgoing_payload)
        if protocol == "anthropic" and request.headers.get("anthropic-beta"):
            headers["anthropic-beta"] = request.headers["anthropic-beta"]
        targets.append((pid, url, headers, outgoing, protocol, provider.get("api_key")))
    if len({entry[-2] for entry in targets}) != 1:
        raise HTTPException(422, "Fallback providers must use the same native protocol")
    rid = new_id("request")
    for index, (pid, url, headers, outgoing, protocol, provider_secret) in enumerate(targets):
        aid, started = new_id("attempt"), now_iso()
        receipt(aid, rid, pid, outgoing["model"], protocol, started, requested_model=payload["model"], status="running", phase="start")
        meter = StreamMeter(protocol, endpoint, secret=provider_secret)
        converting = (endpoint == "messages" and protocol == "openai") or (endpoint == "chat/completions" and protocol == "anthropic")
        converter = ConvertedStream(protocol) if converting else None
        client = client_factory(config["request_timeout_seconds"])
        upstream = None
        _active.add(aid)
        more = index + 1 < len(targets)
        try:
            try:
                upstream = await client.send(client.build_request("POST", url, headers=headers, json=outgoing), stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # A connection was never established: no request body was sent.
                receipt(aid, rid, pid, outgoing["model"], protocol, started, requested_model=payload["model"], status="failed", phase="finish", meter=meter, ended=now_iso(), error="connection_not_established")
                _circuit_result(pid, False, config)
                await client.aclose()
                _active.discard(aid)
                if more:
                    continue
                raise HTTPException(502, "Configured providers could not be connected; no automatic replay occurred after transmission") from None
            if upstream.status_code >= 300:
                receipt(aid, rid, pid, outgoing["model"], protocol, started, requested_model=payload["model"], status="failed", phase="finish", meter=meter, ended=now_iso(), error=f"upstream_http_{upstream.status_code}")
                _circuit_result(pid, False, config)
                code = upstream.status_code
                await upstream.aclose()
                await client.aclose()
                _active.discard(aid)
                # Only a rate-limit rejection is a known retryable rejection here.
                # 5xx/read/write timeouts could follow side effects: never replay.
                if code == 429 and more:
                    continue
                return Response(content=json.dumps({"error": {"type": "upstream_error", "message": "Upstream rejected this request", "status": code, "attempt_id": aid}}), status_code=code, media_type="application/json")
            if outgoing.get("stream"):
                if "text/event-stream" not in upstream.headers.get("content-type", ""):
                    raise ValueError("unexpected_stream_content_type")
                async def body():
                    state, error = "unknown", None
                    pending_frame = b""
                    try:
                        async for chunk in upstream.aiter_bytes():
                            if await request.is_disconnected():
                                state, error = "cancelled", "client_disconnected"
                                break
                            pending_frame += chunk
                            while True:
                                separators = [(pending_frame.find(mark), mark) for mark in (b"\n\n", b"\r\n\r\n") if mark in pending_frame]
                                if not separators:
                                    break
                                position, marker = min(separators, key=lambda pair: pair[0])
                                frame, pending_frame = pending_frame[:position + len(marker)], pending_frame[position + len(marker):]
                                # Validate one complete frame before forwarding
                                # any bytes: a split metadata field must not leak
                                # its credential prefix while awaiting its suffix.
                                meter.feed(frame)
                                if converter:
                                    for converted in converter.feed(frame):
                                        yield converted
                                else:
                                    yield frame
                            if len(pending_frame) > MAX_EVENT:
                                raise ValueError("oversize_sse_event")
                        else:
                            state = "succeeded" if meter.done and meter.usage_final and not meter.invalid and not meter.buffer.strip() and not pending_frame.strip() and (not converter or converter.done) else "unknown"
                            error = None if state == "succeeded" else "incomplete_stream"
                    except (asyncio.CancelledError, GeneratorExit):
                        state, error = "cancelled", "client_disconnected"
                        raise
                    except (httpx.HTTPError, ValueError, OSError, KeyError, HTTPException):
                        state, error = "unknown", "upstream_stream_interrupted"
                    finally:
                        try:
                            receipt(aid, rid, pid, outgoing["model"], protocol, started, requested_model=payload["model"], status=state, phase="finish", meter=meter, ended=now_iso(), error=error)
                            if state != "cancelled":
                                _circuit_result(pid, state == "succeeded", config)
                        finally:
                            await upstream.aclose()
                            await client.aclose()
                            _active.discard(aid)
                return StreamingResponse(body(), media_type="text/event-stream", headers={"x-senrelay-attempt-id": aid, "cache-control": "no-store"})
            chunks, size = [], 0
            async for chunk in upstream.aiter_bytes():
                if await request.is_disconnected():
                    raise asyncio.CancelledError()
                size += len(chunk)
                if size > MAX_RESPONSE:
                    raise ValueError("oversize_response")
                chunks.append(chunk)
            raw = b"".join(chunks)
            try:
                parsed = json.loads(raw)
                meter.consume(parsed)
                if converter:
                    raw = json.dumps(_convert_reply(parsed, protocol), ensure_ascii=False).encode()
            except (ValueError, TypeError, AttributeError, KeyError, HTTPException):
                raise ValueError("invalid_upstream_json") from None
            final_status = "failed" if meter.invalid else "succeeded"
            receipt(aid, rid, pid, outgoing["model"], protocol, started, requested_model=payload["model"], status=final_status, phase="finish", meter=meter, ended=now_iso())
            _circuit_result(pid, final_status == "succeeded", config)
            await upstream.aclose()
            await client.aclose()
            _active.discard(aid)
            return Response(raw, media_type="application/json", headers={"x-senrelay-attempt-id": aid, "cache-control": "no-store"})
        except asyncio.CancelledError:
            receipt(aid, rid, pid, outgoing["model"], protocol, started, requested_model=payload["model"], status="cancelled", phase="finish", meter=meter, ended=now_iso(), error="client_disconnected")
            if upstream:
                await upstream.aclose()
            await client.aclose()
            _active.discard(aid)
            raise
        except (httpx.HTTPError, ValueError, OSError):
            receipt(aid, rid, pid, outgoing["model"], protocol, started, requested_model=payload["model"], status="unknown", phase="finish", meter=meter, ended=now_iso(), error="upstream_outcome_unknown")
            _circuit_result(pid, False, config)
            if upstream:
                await upstream.aclose()
            await client.aclose()
            _active.discard(aid)
            raise HTTPException(502, "Upstream outcome is unknown; request was not automatically retried") from None


def recover_interrupted_attempts():
    """Explicit startup recovery; running records are uncertain, never retried."""
    usage_service.ensure_schema()
    rows = usage_service.requests({"status": "running"}, limit=100000)["items"]
    for row in rows:
        if row["id"] not in _active:
            usage_service.ingest([{ "origin": "proxy", "observation_id": row["id"] + ":recovery", "attempt_id": row["id"],
                "provider_id": row.get("provider_id"), "model": row.get("model"), "started_at": row["started_at"],
                "status": "unknown", "error_code": "service_interrupted"}])
    return len(rows)
