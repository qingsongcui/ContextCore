"""Explicit provider diagnostics and metadata-only model catalogs.

GET reads a bounded local cache. Only POST refresh/check makes HTTP requests.
Model discovery uses the official /models envelopes, never a probe generation.
Endpoint references (checked 2026-10-24):
https://developers.openai.com/api/reference/resources/models/methods/list
https://platform.claude.com/docs/en/api/models/list
"""
from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import socket
import time
from datetime import datetime, timezone, timedelta
from ipaddress import ip_address
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException
from app.database import db, get_app_setting, set_app_setting, log_audit, now_iso, new_id
from app.services import usage_service

CACHE_SECONDS = 300
SAFE_METADATA_VERSION = 2
MAX_METADATA = 1024 * 1024
MAX_MODELS = 1000
SUPPORTED = {"openai", "anthropic"}


def resolve_provider(provider_id):
    from app.services.management_service import resolve_provider as resolve
    return resolve(provider_id)


def provider_metadata(provider_id):
    """Read metadata without asking Keychain to unlock on a background GET."""
    from app.services.management_service import service
    manager = service()
    value = manager.get("provider", provider_id)
    if not value.get("enabled", True):
        raise HTTPException(409, "Provider is disabled")
    from app.services.provider_credentials import selected_source
    if value.get('account_id') and selected_source(value) == 'account':
        account = manager.get("account", value["account_id"])
        value = {**value, "_account_version": account.get("version"), "_account_status": account.get("status"), "_account_expiry": account.get("expires_at")}
        if account.get("kind") == "oauth" and (account.get("status") != "authorized" or account.get("expires_at") is not None and account["expires_at"] <= time.time()):
            raise HTTPException(409, "Official account credentials are expired or unavailable")
    return value


def client_factory():
    return httpx.AsyncClient(timeout=httpx.Timeout(10, connect=5), follow_redirects=False, trust_env=False)


def _is_blocked_ip_obj(parsed_ip, *, allow_loopback: bool = True) -> bool:
    mapped = getattr(parsed_ip, "ipv4_mapped", None)
    if mapped is not None:
        # Never treat IPv4-mapped IPv6 (e.g. ::ffff:127.0.0.1) as allowed loopback.
        if mapped.is_loopback:
            return True
        parsed_ip = mapped
    if parsed_ip.is_loopback:
        return not allow_loopback
    return bool(
        parsed_ip.is_private
        or parsed_ip.is_link_local
        or parsed_ip.is_reserved
        or parsed_ip.is_multicast
        or parsed_ip.is_unspecified
    )


def _blocked_special_ip(
    hostname: str | None,
    *,
    allow_loopback: bool = True,
    resolve_dns: bool = True,
) -> bool:
    host = (hostname or "").strip().rstrip(".")
    if not host:
        return True
    try:
        parsed = ip_address(host)
    except ValueError:
        parsed = None
    if parsed is not None:
        return _is_blocked_ip_obj(parsed, allow_loopback=allow_loopback)

    if host.lower() == "localhost":
        return not allow_loopback

    if not resolve_dns:
        return False

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, OSError, UnicodeError):
        return False

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        addr_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            resolved_ip = ip_address(addr_str)
        except ValueError:
            continue
        if _is_blocked_ip_obj(resolved_ip, allow_loopback=False):
            return True
    return False


def _base(provider):
    base = str(provider.get("base_url", "")).rstrip("/")
    parts = urlsplit(base)
    try:
        ip_obj = ip_address(parts.hostname or "")
        local = ip_obj.is_loopback and getattr(ip_obj, "ipv4_mapped", None) is None
    except ValueError:
        local = parts.hostname == "localhost"
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.scheme not in {"https", "http"} or parts.scheme == "http" and not local:
        raise HTTPException(422, "Use HTTPS or an explicitly configured loopback HTTP API base")
    if _blocked_special_ip(parts.hostname, allow_loopback=True):
        raise HTTPException(422, "Provider URL must not target a private or link-local address")
    if provider.get("protocol") not in SUPPORTED:
        raise HTTPException(422, "Provider checks support OpenAI-compatible and Anthropic protocols only")
    if provider.get("enabled") is False:
        raise HTTPException(409, "Provider is disabled")
    return base


def _headers(provider, credentials=True):
    result = {"accept": "application/json"}
    if provider["protocol"] == "anthropic":
        result["anthropic-version"] = "2023-06-01"
        if credentials and provider.get("api_key"):
            result["x-api-key"] = provider["api_key"]
    elif credentials and provider.get("api_key"):
        result["authorization"] = "Bearer " + provider["api_key"]
    return result


def _official(provider):
    parts = urlsplit(_base(provider))
    return parts.scheme == "https" and parts.port in (None, 443) and parts.path.rstrip("/") == "/v1" and parts.hostname == {"openai": "api.openai.com", "anthropic": "api.anthropic.com"}[provider["protocol"]]


def _fingerprint(provider):
    fields = ("id", "version", "base_url", "protocol", "wire_api", "supported_endpoints", "credential_ref", "account_id", "_account_version", "_account_status", "_account_expiry")
    return hashlib.sha256(json.dumps({k: provider.get(k) for k in fields}, sort_keys=True).encode()).hexdigest()


def catalog(provider_id):
    metadata = provider_metadata(provider_id)
    _base(metadata)
    cache = get_app_setting("provider_catalog_v2:" + provider_id, None)
    if not cache or cache.get("safe_metadata_version") != SAFE_METADATA_VERSION or cache.get("fingerprint") != _fingerprint(metadata):
        return {"provider_id": provider_id, "status": "not_fetched", "items": [], "fetched_at": None, "expires_at": None, "coverage": "unknown", "remote_request": False}
    expiry = datetime.fromisoformat(cache["expires_at"])
    return {k: v for k, v in cache.items() if k not in {"fingerprint", "safe_metadata_version"}} | {"status": "fresh" if expiry > datetime.now(timezone.utc) else "stale", "remote_request": False}


def _safe_text(value, maximum=256):
    return value if isinstance(value, str) and 0 < len(value) <= maximum and not any(ord(c) < 32 for c in value) else None


def _reject_secret_echo(value, provider):
    """Do not trust provider-controlled IDs/names merely because they are metadata.

    Match the actual credential in memory, including arbitrary gateway keys and
    OAuth tokens without a recognizable prefix. Never place it in an exception.
    """
    secret = provider.get("api_key")
    if not isinstance(secret, str) or not secret:
        return
    def contains(item):
        if isinstance(item, dict):
            return any(contains(v) for v in item.values())
        if isinstance(item, (list, tuple)):
            return any(contains(v) for v in item)
        return item is not None and secret in str(item)
    if contains(value):
        raise ValueError("unsafe_provider_metadata")


def _models(value, provider=None):
    # Only documented OpenAI/Anthropic data[] envelopes are interpreted.
    if not isinstance(value, dict) or not isinstance(value.get("data"), list):
        return None
    data = value["data"]
    if len(data) > MAX_MODELS:
        return None
    result = []
    for item in data:
        if not isinstance(item, dict) or not _safe_text(item.get("id")):
            return None
        model = {"id": item["id"]}
        name = _safe_text(item.get("display_name") or item.get("name"), 200)
        if name:
            model["name"] = name
        # Preserve an explicitly named context bound; do not infer it from ID.
        for field in ("context_window", "context_length", "max_input_tokens"):
            context = item.get(field)
            if isinstance(context, int) and not isinstance(context, bool) and context > 0:
                model["context_window"] = context
                model["context_source_field"] = field
                break
        _reject_secret_echo(model, provider or {})
        result.append(model)
    return result


async def _json(response):
    chunks, length = [], 0
    async for chunk in response.aiter_bytes():
        length += len(chunk)
        if length > MAX_METADATA:
            raise ValueError("response_too_large")
        chunks.append(chunk)
    try:
        return json.loads(b"".join(chunks))
    except (ValueError, UnicodeError):
        raise ValueError("unsupported_response_envelope") from None


async def _resolve(provider_id):
    value = resolve_provider(provider_id)
    return await value if inspect.isawaitable(value) else value


async def _fetch_models(provider, after_id=None):
    base = _base(provider)
    params = {}
    if after_id is not None:
        try:
            _reject_secret_echo(after_id, provider)
        except ValueError:
            raise HTTPException(422, "Pagination identifier contains unsafe provider metadata") from None
    if after_id is not None:
        if provider["protocol"] != "anthropic" or not _safe_text(after_id):
            raise HTTPException(422, "after_id pagination is supported only for the Anthropic models API")
        params["after_id"] = after_id
    async with client_factory() as client:
        async with client.stream("GET", base + "/models", headers=_headers(provider), params=params) as response:
            code = response.status_code
            if code < 200 or code >= 300:
                return code, None, None
            value = await _json(response)
            return code, _models(value, provider), value


async def refresh_catalog(provider_id, *, after_id=None):
    # Resolve current account/credential state even if a metadata cache exists.
    metadata = provider_metadata(provider_id)
    provider = await _resolve(provider_id)
    try:
        code, items, envelope = await _fetch_models(provider, after_id)
    except (httpx.HTTPError, ValueError, OSError):
        return {"provider_id": provider_id, "status": "unknown", "items": [], "http_status": None, "coverage": "unknown", "error_code": "catalog_unavailable", "remote_request": True}
    if items is None:
        return {"provider_id": provider_id, "status": "unknown", "items": [], "http_status": code, "coverage": "unknown", "error_code": "models_rejected" if code >= 300 else "unsupported_models_envelope", "remote_request": True}
    now = datetime.now(timezone.utc)
    has_more = envelope.get("has_more") if isinstance(envelope.get("has_more"), bool) else None
    result = {"provider_id": provider_id, "status": "fresh", "items": items, "fetched_at": now.isoformat(), "expires_at": (now + timedelta(seconds=CACHE_SECONDS)).isoformat(), "coverage": "page" if has_more else "reported_catalog", "has_more": has_more, "after_id": after_id,
              "next_after_id": items[-1]["id"] if has_more and items else None, "http_status": code,
              "source": "official_models_endpoint" if _official(provider) else "compatible_models_endpoint", "authentication": "not_proven_by_catalog", "remote_request": True}
    with db() as conn:
        set_app_setting("provider_catalog_v2:" + provider_id, {**result, "fingerprint": _fingerprint(metadata), "safe_metadata_version": SAFE_METADATA_VERSION}, conn=conn)
        log_audit(actor="user", action="provider.catalog.refresh", target=provider_id, conn=conn)
    return result


def _observation(provider_id, aid, rid, model, actual, protocol, started, status, phase, *, ended=None, usage=None, error=None, upstream=None):
    item = {"origin": "provider_check", "observation_id": aid + ":" + phase, "logical_request_id": rid, "attempt_id": aid, "provider_id": provider_id, "requested_model": model, "model": model, "actual_model": actual or model, "billing_model": actual or model,
            "protocol": protocol, "started_at": started, "ended_at": ended, "status": status, "raw_usage": usage or {}, "error_code": error, "upstream_request_id": upstream}
    try:
        usage_service.ingest([item])
    except HTTPException as exc:
        if exc.status_code != 422 or usage is None:
            raise
        item.update(raw_usage={}, status="unknown", error_code="invalid_upstream_usage")
        usage_service.ingest([item])


async def check(provider_id, data):
    if not isinstance(data, dict) or set(data) - {"mode", "model", "confirmed_paid"}:
        raise HTTPException(422, "Expected mode, optional model and confirmed_paid only")
    mode = data.get("mode")
    if mode not in {"network", "auth", "inference"}:
        raise HTTPException(422, "mode must be network, auth or inference")
    if mode == "inference":
        if data.get("confirmed_paid") is not True:
            raise HTTPException(422, "Inference probe requires explicit confirmed_paid=true")
        if not _safe_text(data.get("model")):
            raise HTTPException(422, "Select one explicit model for the inference probe")
        from app.services.provider_endpoints import inference_endpoint
        from app.integrations.config_transactions import ConfigurationError
        try:
            inference_endpoint(provider_metadata(provider_id))
        except ConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
    # Network checks never request a credential from Keychain and never send one.
    provider = provider_metadata(provider_id) if mode == "network" else await _resolve(provider_id)
    base = _base(provider)
    result = {"provider_id": provider_id, "mode": mode, "status": "unknown", "http_status": None, "authentication": "unknown", "inference_performed": False}
    if mode == "network":
        started = time.monotonic()
        try:
            async with client_factory() as client:
                async with client.stream("HEAD", base + "/models", headers=_headers(provider, False)) as response:
                    result.update(status="reachable", http_status=response.status_code, redirect_blocked=300 <= response.status_code < 400)
        except (httpx.HTTPError, OSError):
            result.update(status="unreachable", error_code="connection_unavailable")
        result.update(network_elapsed_ms=(time.monotonic() - started) * 1000, note="HTTP reachability does not establish credential or model validity")
    elif mode == "auth":
        try:
            code, items, _ = await _fetch_models(provider)
            result["http_status"] = code
            if code == 401:
                result.update(status="rejected", authentication="rejected", note="Credential rejected by models endpoint")
            elif code == 403:
                result.update(status="unknown", note="Models access forbidden; key validity is not established")
            elif 200 <= code < 300 and items is not None and provider.get("api_key") and _official(provider):
                result.update(status="authenticated", authentication="authenticated", note="Credential accepted by the official models endpoint; inference permission remains untested")
            else:
                result.update(note="A compatible or public models response cannot establish credential validity")
        except (httpx.HTTPError, ValueError, OSError):
            result.update(error_code="authentication_unverified", note="No inference was attempted")
    else:
        if data.get("confirmed_paid") is not True:
            raise HTTPException(422, "Inference probe requires explicit confirmed_paid=true")
        model = _safe_text(data.get("model"))
        if not model:
            raise HTTPException(422, "Select one explicit model for the inference probe")
        actual = (provider.get("model_map") or {}).get(model, model)
        try:
            _reject_secret_echo([model, actual], provider)
        except ValueError:
            raise HTTPException(422, "Selected model contains unsafe provider metadata") from None
        from app.services.provider_endpoints import inference_endpoint
        from app.integrations.config_transactions import ConfigurationError
        try:
            surface = inference_endpoint(provider)
        except ConfigurationError as exc:
            raise HTTPException(exc.status_code, str(exc)) from None
        endpoint = '/' + surface
        if surface == 'responses':
            body = {"model": actual, "input": "Reply OK.", "stream": False, "max_output_tokens": 16}
        else:
            body = {"model": actual, "messages": [{"role": "user", "content": "Reply OK."}], "stream": False}
            body["max_tokens" if provider["protocol"] == "anthropic" else "max_completion_tokens"] = 1
        result['endpoint'] = surface
        aid, rid, started = new_id("probe"), new_id("request"), now_iso()
        _observation(provider_id, aid, rid, model, actual, provider["protocol"], started, "running", "start")
        before = time.monotonic()
        result.update(inference_attempted=True, inference_performed=None)
        try:
            async with client_factory() as client:
                async with client.stream("POST", base + endpoint, headers={**_headers(provider), "content-type": "application/json"}, json=body) as response:
                    result.update(http_status=response.status_code, inference_performed=True)
                    if not 200 <= response.status_code < 300:
                        _observation(provider_id, aid, rid, model, actual, provider["protocol"], started, "failed", "finish", ended=now_iso(), error=f"upstream_http_{response.status_code}")
                        result.update(status="failed", error_code="inference_rejected")
                    else:
                        value = await _json(response)
                        if not isinstance(value, dict):
                            raise ValueError("unsupported_inference_envelope")
                        if surface == 'responses':
                            valid = value.get('object') == 'response' and value.get('status') in ('completed', 'incomplete') and isinstance(value.get('output'), list)
                        else:
                            valid = (value.get("type") == "message" and isinstance(value.get("content"), list)) if provider["protocol"] == "anthropic" else isinstance(value.get("choices"), list) and bool(value["choices"])
                        if not valid or value.get("error"):
                            raise ValueError("unsupported_inference_envelope")
                        # Model and response IDs are controlled by the gateway.
                        # Check before any ledger write, API readback or cache use.
                        _reject_secret_echo([value.get("model"), value.get("id")], provider)
                        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
                        response_model = _safe_text(value.get("model")) or actual
                        _observation(provider_id, aid, rid, model, response_model, provider["protocol"], started, "succeeded", "finish", ended=now_iso(), usage=usage, upstream=_safe_text(value.get("id")))
                        result.update(status="succeeded", model=model, actual_model=response_model, authentication="accepted_for_inference")
        except asyncio.CancelledError:
            _observation(provider_id, aid, rid, model, actual, provider["protocol"], started, "cancelled", "finish", ended=now_iso(), error="probe_cancelled")
            raise
        except (httpx.ConnectError, httpx.ConnectTimeout):
            _observation(provider_id, aid, rid, model, actual, provider["protocol"], started, "failed", "finish", ended=now_iso(), error="connection_not_established")
            result.update(status="failed", error_code="connection_not_established", inference_performed=False)
        except (httpx.HTTPError, ValueError, OSError):
            _observation(provider_id, aid, rid, model, actual, provider["protocol"], started, "unknown", "finish", ended=now_iso(), error="probe_outcome_unknown")
            result.update(status="unknown", error_code="probe_outcome_unknown")
        result.update(attempt_id=aid, logical_request_id=rid, end_to_end_ms=(time.monotonic() - before) * 1000, note="One fixed low-output probe; no retries, fallback, or model substitution")
        row = usage_service.requests({"logical_request_id": rid}, limit=1)["items"][0]
        result["usage"], result["cost"] = row["usage"], row["cost"]
        # A malformed usage envelope cannot be represented as a fully successful check.
        if row["status"] == "unknown":
            result["status"] = "unknown"
    with db() as conn:
        log_audit(actor="user", action="provider.check." + mode, target=provider_id, result=result["status"], conn=conn)
    return result
