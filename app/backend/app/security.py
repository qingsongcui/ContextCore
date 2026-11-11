import hashlib
import hmac
import secrets
import threading
import time
from contextvars import ContextVar
from ipaddress import ip_address
from urllib.parse import urlsplit
from fastapi import Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# In-memory session store: session_id -> {csrf_token, created_at, expires_at}
_SESSIONS: dict[str, dict] = {}
SESSION_COOKIE_NAME = "cc_session"
SESSION_TTL_SECONDS = 86400 * 7  # 7 days
MAX_SESSIONS = 1024
_session_lock = threading.RLock()
authenticated_agent: ContextVar[str | None] = ContextVar("contextcore_authenticated_agent", default=None)

ALLOWED_HOSTS = {
    "127.0.0.1",
    "localhost",
    "::1",
}

ALLOWED_ORIGINS = {
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:8420",
    "http://127.0.0.1:8420",
}


def is_loopback_ip(ip: str | None) -> bool:
    if not ip:
        return False
    if ip == "localhost":
        return True
    try:
        return ip_address(ip).is_loopback
    except ValueError:
        return False


def create_session() -> tuple[str, str]:
    session_id = secrets.token_hex(24)
    csrf_token = secrets.token_urlsafe(32)
    now = time.time()
    with _session_lock:
        _reap_sessions(now)
        while len(_SESSIONS) >= MAX_SESSIONS:
            _SESSIONS.pop(next(iter(_SESSIONS)))
        _SESSIONS[session_id] = {
            "csrf_token": csrf_token,
            "created_at": now,
            "expires_at": now + SESSION_TTL_SECONDS,
        }
    return session_id, csrf_token


def _reap_sessions(now: float) -> None:
    for session_id, data in list(_SESSIONS.items()):
        if now > data['expires_at']:
            _SESSIONS.pop(session_id, None)


def get_csrf_token_for_session(session_id: str | None) -> str | None:
    with _session_lock:
        _reap_sessions(time.time())
        if not session_id or session_id not in _SESSIONS:
            return None
        # Keep recent browser tabs when the capacity bound evicts old sessions.
        data = _SESSIONS.pop(session_id)
        _SESSIONS[session_id] = data
        return data['csrf_token']


def validate_csrf_token(token: str, session_id: str | None = None) -> bool:
    expected = get_csrf_token_for_session(session_id)
    return bool(token and expected and hmac.compare_digest(token, expected))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.strip().encode("utf-8")).hexdigest()


class SecurityMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # 1. Loopback verification
        client_host = request.client.host if request.client else "unknown"
        if not is_loopback_ip(client_host):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={"detail": "Forbidden: Access restricted to loopback interface"},
            )

        # Parse bracketed IPv6 correctly and reject missing or deceptive authorities.
        authority = request.headers.get("host", "")
        try:
            parsed_host = urlsplit(f"//{authority}")
            host = parsed_host.hostname
            valid_host = bool(
                host in ALLOWED_HOSTS
                and parsed_host.username is None
                and parsed_host.password is None
                and not parsed_host.path
                and not parsed_host.query
                and not parsed_host.fragment
                and (parsed_host.port is None or parsed_host.port > 0)
            )
        except ValueError:
            valid_host = False
        if not valid_host:
            return JSONResponse(status_code=403, content={"detail": "Host not allowed"})

        origin = request.headers.get("origin")
        same_origin = f"{request.url.scheme}://{authority}"
        path = request.url.path
        oauth_callback = request.method == "GET" and path == "/api/manage/oauth/callback"
        if origin and origin not in ALLOWED_ORIGINS and origin != same_origin and not oauth_callback:
            return JSONResponse(status_code=403, content={"detail": "Origin not allowed"})
        sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if sec_fetch_site == "cross-site" and not oauth_callback:
            return JSONResponse(status_code=403, content={"detail": "Cross-site access denied"})
        if sec_fetch_site == "same-site" and origin not in ALLOWED_ORIGINS and not oauth_callback:
            return JSONResponse(status_code=403, content={"detail": "Cross-origin same-site access denied"})
        sec_fetch_dest = (request.headers.get("sec-fetch-dest") or "").strip().lower()
        if path.startswith("/api/") and not oauth_callback and sec_fetch_dest in {
            "image", "script", "style", "iframe", "frame", "object", "embed", "audio", "video",
        }:
            return JSONResponse(status_code=403, content={"detail": "Passive subresource or embed access denied"})
        parts = path.strip("/").split("/")
        is_hook = len(parts) == 4 and parts[:2] == ["api", "connections"] and parts[3] == "hook"
        is_mcp = path == "/mcp" or path.startswith("/mcp/")
        if is_mcp or is_hook:
            from app.services.connection_service import verify_agent_token

            authorization = request.headers.get("authorization", "")
            token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else request.headers.get("x-agent-token", "").strip()
            if not token:
                return JSONResponse(status_code=401, content={"detail": "Enabled agent credential required"})
            try:
                agent_id = verify_agent_token(token, purpose="lifecycle" if is_hook else "context_tools")
            except Exception:
                return JSONResponse(status_code=503, content={"detail": "Credential validation unavailable"})
            if not agent_id or (is_hook and parts[2] != agent_id):
                return JSONResponse(status_code=401, content={"detail": "Invalid or revoked agent credential"})
            request.state.agent_id = agent_id
        elif path.startswith("/api/routing/v1/"):
            # Model clients cannot use browser CSRF as routing authorization.
            # The router checks its independent revocable bearer credential.
            pass
        elif path.startswith("/api/") and request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            csrf_token = request.headers.get("x-csrf-token", "")
            session_cookie = request.cookies.get(SESSION_COOKIE_NAME)
            if not validate_csrf_token(csrf_token, session_cookie):
                return JSONResponse(status_code=403, content={"detail": "CSRF token missing, expired, or invalid"})

        identity = authenticated_agent.set(getattr(request.state, "agent_id", None))
        try:
            return await call_next(request)
        finally:
            authenticated_agent.reset(identity)
