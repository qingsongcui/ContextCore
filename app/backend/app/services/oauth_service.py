"""Opt-in official OAuth2 public-client PKCE. No borrowed application identity.

Authorization codes, PKCE verifiers and token bodies never enter SQLite/logs.
Only hashed one-use states, expiry/status and Keychain references are persisted.
"""
from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from urllib.parse import urlencode, urlsplit
from typing import Any

import httpx

from app.integrations.config_transactions import ConfigurationError, canonical
from app.integrations.secret_store import SecretUnavailable

CALLBACK_PATH = "/api/manage/oauth/callback"


def ensure_schema(conn):
    conn.execute("""CREATE TABLE IF NOT EXISTS management_oauth_flows (
        state_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
        provider_id TEXT NOT NULL, verifier_ref TEXT NOT NULL,
        redirect_uri TEXT NOT NULL, expires_at REAL NOT NULL,
        status TEXT NOT NULL, account_version INTEGER NOT NULL,
        config_hash TEXT NOT NULL)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(management_oauth_flows)")}
    if "config_hash" not in columns:
        conn.execute("ALTER TABLE management_oauth_flows ADD COLUMN config_hash TEXT")
    conn.execute("""CREATE TABLE IF NOT EXISTS management_oauth_refresh (
        account_id TEXT PRIMARY KEY, status TEXT NOT NULL,
        started_at REAL NOT NULL)""")


class OAuthService:
    def __init__(self, manager):
        self.manager = manager
        with manager.connect() as conn:
            ensure_schema(conn)

    @staticmethod
    def _https_endpoint(value: Any) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError("Invalid official OAuth endpoint", 422)
        try:
            parsed = urlsplit(value)
            _ = parsed.port
        except (ValueError, TypeError):
            raise ConfigurationError("Invalid official OAuth endpoint", 422) from None
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigurationError("Official OAuth endpoints must be credential-free HTTPS URLs", 422)
        from app.services.provider_checks import _blocked_special_ip
        if _blocked_special_ip(parsed.hostname, allow_loopback=False):
            raise ConfigurationError("Official OAuth endpoints must not target loopback, private, or link-local addresses", 422)
        return value

    @staticmethod
    def validate_config(config: Any) -> dict:
        if not isinstance(config, dict) or config.get("enabled") is not True:
            raise ConfigurationError("No official OAuth client/flow is configured", 422)
        fields = ["authorization_url", "token_url"] + (["revoke_endpoint"] if config.get("revoke_endpoint") else [])
        for field in fields:
            OAuthService._https_endpoint(config.get(field))
        if not isinstance(config.get("client_id"), str) or not config["client_id"] or len(config["client_id"]) > 500:
            raise ConfigurationError("An official registered public client_id is required", 422)
        if config.get("token_endpoint_auth_method", "none") != "none":
            raise ConfigurationError("Only registered public clients with PKCE/S256 are supported", 422)
        scopes = config.get("scopes", [])
        if not isinstance(scopes, list) or not all(isinstance(scope, str) and scope and not any(char.isspace() for char in scope) for scope in scopes):
            raise ConfigurationError("OAuth scopes must be a list of explicit scope names", 422)
        redirect = config.get("redirect_uri", "")
        try:
            parsed = urlsplit(redirect)
            port = parsed.port
        except (ValueError, TypeError):
            raise ConfigurationError("Invalid registered loopback callback", 422) from None
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1") or not port or not 1024 <= port <= 65535 or parsed.path != CALLBACK_PATH or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ConfigurationError("Register the exact local HTTP loopback callback path and serving port", 422)
        return config

    def _account_provider(self, account_id):
        account = self.manager.get("account", account_id)
        if account.get("kind") != "oauth":
            raise ConfigurationError("This account does not use OAuth", 422)
        if not account.get("provider_id"):
            raise ConfigurationError("Select an OAuth provider first", 422)
        provider = self.manager.get("provider", account["provider_id"])
        if not provider.get("enabled", True):
            raise ConfigurationError("OAuth provider is disabled")
        return account, provider

    def authorize(self, account_id: str, callback_url: str | None = None) -> dict:
        account, provider = self._account_provider(account_id)
        raw = provider.get("oauth")
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            return {"account_id": account_id, "status": "unsupported", "authorization_url": None, "reason": "No official Senrelay OAuth client/flow is configured"}
        config = self.validate_config(raw)
        if callback_url is not None and callback_url != config["redirect_uri"]:
            raise ConfigurationError("Registered OAuth redirect_uri does not match the actual local serving address/port", 422)
        if account.get("credential_ref"):
            raise ConfigurationError("Log out this account before authorizing it again")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        verifier_ref = self.manager._store_secret(verifier)
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        expires = time.time() + 600
        data = {key: value for key, value in account.items() if key not in ("id", "version", "created_at", "updated_at")}
        data.update(status="authorizing", oauth_flow=state_hash, authorization_expires_at=expires)
        try:
            with self.manager.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("SELECT 1 FROM management_oauth_flows WHERE account_id=? AND status IN ('pending','exchanging') AND expires_at>?", (account_id, time.time())).fetchone():
                    raise ConfigurationError("An authorization flow is already active")
                changed = conn.execute("UPDATE management_entities SET data=?,version=version+1 WHERE id=? AND version=?", (canonical(data), account_id, account["version"]))
                if changed.rowcount != 1:
                    raise ConfigurationError("Account changed during authorization")
                conn.execute("INSERT INTO management_oauth_flows VALUES(?,?,?,?,?,?,?,?,?)", (state_hash, account_id, provider["id"], verifier_ref, config["redirect_uri"], expires, "pending", account["version"] + 1, hashlib.sha256(canonical(config).encode()).hexdigest()))
        except Exception:
            self._cleanup(verifier_ref, "oauth_flow_not_adopted")
            raise
        query = {"response_type": "code", "client_id": config["client_id"], "redirect_uri": config["redirect_uri"], "scope": " ".join(config.get("scopes", [])), "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        return {"account_id": account_id, "status": "authorizing", "authorization_url": config["authorization_url"] + "?" + urlencode(query), "expires_at": expires, "flow": "oauth2_pkce_s256", "client_identity": "user_registered_official_public_client"}

    async def _token(self, config: dict, form: dict, transport=None) -> dict:
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, transport=transport) as client:
                async with client.stream("POST", config["token_url"], data=form, headers={"Accept": "application/json"}) as response:
                    if response.status_code != 200:
                        raise ConfigurationError("Official token endpoint rejected the grant", 502)
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 128 * 1024:
                            raise ConfigurationError("OAuth token response exceeded the limit", 502)
                        body.extend(chunk)
                    token = json.loads(body)
            if not isinstance(token, dict) or not isinstance(token.get("access_token"), str) or not token["access_token"] or token.get("token_type", "Bearer").lower() != "bearer":
                raise ConfigurationError("Official token response is invalid or unsupported", 502)
            # Persist only protocol fields, never an arbitrary server response.
            return {key: token[key] for key in ("access_token", "refresh_token", "token_type", "expires_in", "scope") if key in token}
        except (httpx.HTTPError, ValueError, TypeError):
            raise ConfigurationError("OAuth token exchange failed; no credentials were logged", 502) from None

    @staticmethod
    def _expiry(token):
        value = token.get("expires_in")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= 366 * 86400:
            return time.time() + value
        return None

    def _cleanup(self, reference: str, reason: str):
        with self.manager.connect() as conn:
            receipt = self.manager._cleanup_receipt(conn, reference, reason)
        self.manager.retry_credential_cleanup(receipt)

    def _flow_result(self, flow: dict, status: str):
        with self.manager.connect() as conn:
            conn.execute("UPDATE management_oauth_flows SET status=? WHERE state_hash=?", (status, flow["state_hash"]))
            row = conn.execute("SELECT data FROM management_entities WHERE id=?", (flow["account_id"],)).fetchone()
            if row:
                data = json.loads(row[0])
                if data.get("oauth_flow") == flow["state_hash"] and data.get("status") == "authorizing":
                    data.update(status="expired" if status == "expired" else "error")
                    data.pop("oauth_flow", None)
                    conn.execute("UPDATE management_entities SET data=?,version=version+1 WHERE id=?", (canonical(data), flow["account_id"]))
        self._cleanup(flow["verifier_ref"], "oauth_flow_finished")

    async def callback(self, state: str | None, code: str | None, error: str | None = None, transport=None) -> dict:
        if not isinstance(state, str) or not 20 <= len(state) <= 200 or (code is not None and len(code) > 8192):
            raise ConfigurationError("Invalid or expired OAuth callback", 400)
        state_hash = hashlib.sha256(state.encode()).hexdigest()
        with self.manager.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM management_oauth_flows WHERE state_hash=?", (state_hash,)).fetchone()
            if not row or row["status"] != "pending":
                raise ConfigurationError("Invalid, expired or already consumed OAuth state", 400)
            flow = dict(row)
            if row["expires_at"] <= time.time():
                conn.execute("UPDATE management_oauth_flows SET status='expired' WHERE state_hash=?", (state_hash,))
                expired = True
            else:
                conn.execute("UPDATE management_oauth_flows SET status='exchanging' WHERE state_hash=?", (state_hash,))
                expired = False
        if expired:
            self._flow_result(flow, "expired")
            raise ConfigurationError("Invalid or expired OAuth callback", 400)
        if error or not code:
            self._flow_result(flow, "failed")
            raise ConfigurationError("OAuth authorization was denied or no code was supplied", 400)
        new_ref = None
        try:
            account, provider = self._account_provider(flow["account_id"])
            config = self.validate_config(provider.get("oauth"))
            if config["redirect_uri"] != flow["redirect_uri"] or hashlib.sha256(canonical(config).encode()).hexdigest() != flow["config_hash"]:
                raise ConfigurationError("OAuth provider configuration changed during authorization")
            verifier = self.manager.vault.get(flow["verifier_ref"])
            token = await self._token(config, {"grant_type": "authorization_code", "client_id": config["client_id"], "redirect_uri": flow["redirect_uri"], "code": code, "code_verifier": verifier}, transport)
            new_ref = self.manager._store_secret(json.dumps(token))
            with self.manager.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM management_entities WHERE id=?", (account["id"],)).fetchone()
                current = json.loads(row["data"]) if row else {}
                provider_row = conn.execute("SELECT data FROM management_entities WHERE id=?", (provider["id"],)).fetchone()
                latest_config = json.loads(provider_row[0]).get("oauth") if provider_row else None
                if latest_config is None or hashlib.sha256(canonical(latest_config).encode()).hexdigest() != flow["config_hash"]:
                    raise ConfigurationError("OAuth provider changed before token adoption")
                state_row = conn.execute("SELECT status FROM management_oauth_flows WHERE state_hash=?", (state_hash,)).fetchone()
                if not row or row["version"] != flow["account_version"] or current.get("oauth_flow") != state_hash or not state_row or state_row[0] != "exchanging":
                    raise ConfigurationError("Account or authorization changed; token was not adopted")
                current.update(status="authorized", credential_ref=new_ref, expires_at=self._expiry(token), token_type="Bearer", scopes=token.get("scope", " ".join(config.get("scopes", []))))
                current.pop("oauth_flow", None)
                conn.execute("UPDATE management_entities SET data=?,version=version+1 WHERE id=?", (canonical(current), account["id"]))
                conn.execute("UPDATE management_oauth_flows SET status='completed' WHERE state_hash=?", (state_hash,))
            self._cleanup(flow["verifier_ref"], "oauth_flow_completed")
            return {"account_id": account["id"], "status": "authorized", "expires_at": current["expires_at"], "quota": {"status": "unknown"}}
        except Exception:
            if new_ref:
                self._cleanup(new_ref, "oauth_token_not_adopted")
            self._flow_result(flow, "failed")
            raise ConfigurationError("OAuth authorization could not be completed; restart the official flow", 502) from None

    async def refresh(self, account_id: str, transport=None) -> dict:
        account, provider = self._account_provider(account_id)
        config = self.validate_config(provider.get("oauth"))
        if not account.get("credential_ref") or account.get("status") not in ("authorized", "expired", "error"):
            raise ConfigurationError("This account has no refreshable official credential")
        token = json.loads(self.manager.vault.get(account["credential_ref"]))
        if not token.get("refresh_token"):
            return {"account_id": account_id, "status": "unsupported", "reason": "Provider did not issue a refresh token"}
        with self.manager.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            provider_row = conn.execute("SELECT version FROM management_entities WHERE id=?", (provider["id"],)).fetchone()
            account_row = conn.execute("SELECT version FROM management_entities WHERE id=?", (account_id,)).fetchone()
            if not provider_row or provider_row[0] != provider["version"] or not account_row or account_row[0] != account["version"]:
                raise ConfigurationError("OAuth provider/account changed before refresh")
            active = conn.execute("SELECT * FROM management_oauth_refresh WHERE account_id=?", (account_id,)).fetchone()
            if active and active["status"] == "refreshing":
                raise ConfigurationError("A refresh is active or its outcome is unknown; do not replay a rotating token")
            conn.execute("INSERT OR REPLACE INTO management_oauth_refresh VALUES(?,'refreshing',?)", (account_id, time.time()))
        new_ref = None
        try:
            refreshed = await self._token(config, {"grant_type": "refresh_token", "refresh_token": token["refresh_token"], "client_id": config["client_id"]}, transport)
            refreshed.setdefault("refresh_token", token["refresh_token"])
            new_ref = self.manager._store_secret(json.dumps(refreshed))
            with self.manager.connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute("SELECT * FROM management_entities WHERE id=?", (account_id,)).fetchone()
                provider_row = conn.execute("SELECT version FROM management_entities WHERE id=?", (provider["id"],)).fetchone()
                if not row or row["version"] != account["version"] or not provider_row or provider_row[0] != provider["version"]:
                    raise ConfigurationError("Account/provider changed during refresh")
                data = json.loads(row["data"])
                data.update(credential_ref=new_ref, expires_at=self._expiry(refreshed), status="authorized")
                conn.execute("UPDATE management_entities SET data=?,version=version+1 WHERE id=?", (canonical(data), account_id))
                conn.execute("UPDATE management_oauth_refresh SET status='completed' WHERE account_id=?", (account_id,))
            self._cleanup(account["credential_ref"], "oauth_token_rotated")
            return {"account_id": account_id, "status": "authorized", "expires_at": data["expires_at"], "quota": {"status": "unknown"}}
        except Exception:
            if new_ref:
                self._cleanup(new_ref, "oauth_token_not_adopted")
            # A request may have reached the upstream and rotated its token.
            # Keep the durable lease unknown and require official re-login.
            with self.manager.connect() as conn:
                conn.execute("UPDATE management_oauth_refresh SET status='refreshing' WHERE account_id=?", (account_id,))
            raise ConfigurationError("Refresh outcome is unknown; use official login instead of replaying the refresh token", 502) from None

    def cancel(self, account_id: str) -> dict:
        account = self.manager.get("account", account_id)
        with self.manager.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            flows = conn.execute("SELECT * FROM management_oauth_flows WHERE account_id=? AND status IN ('pending','exchanging')", (account_id,)).fetchall()
            conn.execute("UPDATE management_oauth_flows SET status='cancelled' WHERE account_id=? AND status IN ('pending','exchanging')", (account_id,))
            row = conn.execute("SELECT data FROM management_entities WHERE id=?", (account_id,)).fetchone()
            if not row:
                raise ConfigurationError("OAuth account was removed", 404)
            data = json.loads(row[0])
            data.pop("oauth_flow", None)
            # Cancellation is for the flow, not an implicit logout of an
            # existing authorized account.
            data["status"] = "authorized" if data.get("credential_ref") else "cancelled"
            conn.execute("UPDATE management_entities SET data=?,version=version+1 WHERE id=?", (canonical(data), account_id))
        for flow in flows:
            self._cleanup(flow["verifier_ref"], "oauth_flow_completed")
        from app.services.provider_credentials import account_status
        return {"account_id": account_id, "status": account_status(data), "expires_at": data.get('expires_at')}

    async def revoke(self, account_id: str, confirmed: bool, transport=None) -> dict:
        if confirmed is not True:
            raise ConfigurationError("Confirm this official credential revocation before contacting the provider", 422)
        account = self.manager.get("account", account_id)
        if account.get("kind") != "oauth" or not account.get("provider_id"):
            raise ConfigurationError("Account has no official OAuth credential", 422)
        provider = self.manager.get("provider", account["provider_id"])
        raw = provider.get("oauth")
        if not isinstance(raw, dict) or not raw.get("revoke_endpoint"):
            return {"account_id": account_id, "status": "unsupported", "remote_session_revocation": "not_attempted", "reason": "No explicit official revoke_endpoint is configured"}
        config = self.validate_config(raw)
        if not account.get("credential_ref"):
            return {"account_id": account_id, "status": "logged_out", "remote_session_revocation": "not_attempted", "reason": "No local token is available to revoke"}
        token = json.loads(self.manager.vault.get(account["credential_ref"]))
        use_refresh = bool(token.get("refresh_token"))
        form = {"token": token.get("refresh_token") or token["access_token"], "token_type_hint": "refresh_token" if use_refresh else "access_token", "client_id": config["client_id"]}
        outcome = "unknown"
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False, transport=transport) as client:
                async with client.stream("POST", config["revoke_endpoint"], data=form, headers={"Accept": "application/json"}) as response:
                    # RFC 7009 returns 200 for both valid and already-invalid
                    # tokens; report acceptance, not proof of all sessions ending.
                    outcome = "accepted" if response.status_code == 200 else "rejected"
        except httpx.HTTPError:
            pass
        if outcome == "rejected":
            from app.services.provider_credentials import account_status
            return {"account_id": account_id, "status": account_status(account), "expires_at": account.get('expires_at'), "remote_session_revocation": "rejected", "reason": "Official endpoint rejected the request; local credentials remain available for explicit logout"}
        # After acceptance or an unknown network outcome, stop local use while
        # clearly retaining the distinction from remote/browser session state.
        result = self.logout(account_id)
        result["remote_session_revocation"] = outcome
        result["reason"] = "Official revocation accepted; local credentials removed" if outcome == "accepted" else "Remote outcome is unknown; local credentials removed, verify remaining sessions with the provider"
        return result

    def logout(self, account_id: str) -> dict:
        self.cancel(account_id)
        account = self.manager.get("account", account_id)
        if account.get("credential_ref"):
            self.manager.remove_credential("account", account_id)
        with self.manager.connect() as conn:
            row = conn.execute("SELECT data FROM management_entities WHERE id=?", (account_id,)).fetchone()
            data = json.loads(row[0])
            data.update(status="logged_out", expires_at=None)
            conn.execute("UPDATE management_entities SET data=?,version=version+1 WHERE id=?", (canonical(data), account_id))
            conn.execute("DELETE FROM management_oauth_refresh WHERE account_id=?", (account_id,))
        return {"account_id": account_id, "status": "logged_out", "remote_session_revocation": "not_attempted", "reason": "Local credentials removed; provider browser sessions remain under the official provider"}
