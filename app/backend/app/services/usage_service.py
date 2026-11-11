"""Metadata-only request ledger. No history is read and no inference is performed here."""
from __future__ import annotations

import hashlib
import os
import threading
from array import array
from collections import OrderedDict
from copy import deepcopy
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import HTTPException
from app.database import db, new_id, now_iso, encode, decode, log_audit

STATUSES = {"running", "succeeded", "failed", "cancelled", "unknown"}
ORIGINS = {"proxy", "native_log", "contextcore_compute", "provider_check", "manual"}
COUNTERS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")


def ensure_schema(conn=None):
    if conn is None:
        with db() as owned:
            ensure_schema(owned)
        return
    statements = """
    CREATE TABLE IF NOT EXISTS usage_v2_requests (
      id TEXT PRIMARY KEY, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usage_v2_attempts (
      id TEXT PRIMARY KEY, logical_request_id TEXT NOT NULL REFERENCES usage_v2_requests(id),
      provider_id TEXT, model TEXT, host_id TEXT, project_id TEXT, account_id TEXT,
      upstream_request_id TEXT, started_at TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_time ON usage_v2_attempts(started_at);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_page ON usage_v2_attempts(started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_logical ON usage_v2_attempts(logical_request_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_host_page ON usage_v2_attempts(host_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_provider_page ON usage_v2_attempts(provider_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_model_page ON usage_v2_attempts(model, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_project_page ON usage_v2_attempts(project_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_account_page ON usage_v2_attempts(account_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_status_page ON usage_v2_attempts(status, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_upstream ON usage_v2_attempts(provider_id, upstream_request_id);
    CREATE TABLE IF NOT EXISTS usage_v2_observations (
      origin TEXT NOT NULL, observation_id TEXT NOT NULL, attempt_id TEXT NOT NULL,
      payload_hash TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(origin, observation_id)
    );
    CREATE INDEX IF NOT EXISTS usage_v2_observation_attempt ON usage_v2_observations(attempt_id);
    CREATE TABLE IF NOT EXISTS usage_v2_attempt_aliases (alias_id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS usage_v2_alias_attempt ON usage_v2_attempt_aliases(attempt_id);
    CREATE TABLE IF NOT EXISTS usage_v2_revision (singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL);
    CREATE TABLE IF NOT EXISTS usage_v2_prices (
      id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, model TEXT NOT NULL,
      effective_from TEXT NOT NULL, effective_to TEXT, currency TEXT NOT NULL, data TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS usage_v2_cursors (
      source_id TEXT PRIMARY KEY, sequence INTEGER NOT NULL, counters TEXT NOT NULL
    );
    """
    for statement in statements.split(";"):
        if statement.strip():
            conn.execute(statement)
    # Triggers also invalidate cached statistics for writes outside ingest(). They
    # participate in the caller transaction, so rollback never advances a revision.
    for operation in ("INSERT", "UPDATE", "DELETE"):
        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS usage_v2_revision_{operation.lower()}
          AFTER {operation} ON usage_v2_attempts BEGIN
            INSERT INTO usage_v2_revision VALUES(1,1) ON CONFLICT(singleton) DO UPDATE SET revision=revision+1;
          END""")


def _time(value: str | None, default=None):
    if value is None:
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        raise HTTPException(422, "Timestamps must be ISO8601 with timezone")


def _integer(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HTTPException(422, "Token counts must be nonnegative integers or null")
    return value


def normalize_usage(raw: dict | None, protocol="openai", input_includes_cache=None):
    if input_includes_cache is not None and type(input_includes_cache) is not bool:
        raise HTTPException(422, "input_includes_cache must be a boolean or null")
    raw = raw or {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "raw_usage must be an object")
    def pick(*names):
        return next((raw[n] for n in names if raw.get(n) is not None), None)
    inp = _integer(pick("input_tokens", "prompt_tokens"))
    out = _integer(pick("output_tokens", "completion_tokens"))
    details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    out_details = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
    if not isinstance(details, dict) or not isinstance(out_details, dict):
        raise HTTPException(422, "Usage details must be objects")
    read = _integer(pick("cache_read_tokens", "cache_read_input_tokens"))
    if read is None and details.get("cached_tokens") is not None:
        read = _integer(details["cached_tokens"])
    write = _integer(pick("cache_write_tokens", "cache_creation_input_tokens"))
    reasoning = _integer(pick("reasoning_tokens"))
    if reasoning is None and out_details.get("reasoning_tokens") is not None:
        reasoning = _integer(out_details["reasoning_tokens"])
    includes = protocol != "anthropic" if input_includes_cache is None else input_includes_cache
    # Unknown cache counters stay null in the response. For OpenAI absent details
    # mean cache treatment is unknown, not that a discounted token count is zero.
    uncached = inp
    if includes and inp is not None:
        if read is None and write is None:
            uncached = None
        else:
            uncached = inp - (read or 0) - (write or 0)
            if uncached < 0:
                raise HTTPException(422, "Cache subset exceeds input tokens")
    total_input = (inp + (read or 0) + (write or 0)) if inp is not None and not includes else inp
    if reasoning is not None and out is not None and reasoning > out:
        raise HTTPException(422, "Reasoning tokens must be a subset of output tokens")
    return dict(input_tokens=total_input, output_tokens=out, uncached_input_tokens=uncached,
                cache_read_tokens=read, cache_write_tokens=write, reasoning_tokens=reasoning,
                input_includes_cache=includes, reasoning_included_in_output=True)


def _decimal(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError()
        return format(result, "f")
    except (ValueError, InvalidOperation):
        raise HTTPException(422, "Prices must be finite nonnegative decimal values")


def add_price(data):
    allowed = {"provider_id", "model", "effective_from", "effective_to", "currency", "input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million"}
    if set(data) - allowed:
        raise HTTPException(422, "Unsupported price fields")
    if not data.get("provider_id") or not data.get("model") or any(not isinstance(data[k], str) or len(data[k]) > 256 for k in ("provider_id", "model")):
        raise HTTPException(422, "Provider and billing model are required")
    start = _time(data.get("effective_from"), now_iso())
    end = _time(data.get("effective_to"))
    if end and end <= start:
        raise HTTPException(422, "Price end must follow start")
    currency = str(data.get("currency", "USD")).upper()
    if len(currency) != 3 or not currency.isalpha():
        raise HTTPException(422, "Currency must be a three-letter code")
    result = {"id": new_id("price"), "provider_id": data["provider_id"], "model": data["model"],
              "effective_from": start, "effective_to": end, "currency": currency}
    for key in ("input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million"):
        result[key] = _decimal(data.get(key))
    with db() as conn:
        ensure_schema(conn)
        conn.execute("INSERT INTO usage_v2_prices VALUES (?,?,?,?,?,?,?)", (result["id"], result["provider_id"], result["model"], start, end, currency, encode(result)))
        log_audit(actor="user", action="usage.price.create", target=result["id"], conn=conn)
    return result


def list_prices():
    with db() as conn:
        ensure_schema(conn)
        return [decode(r["data"], {}) for r in conn.execute("SELECT data FROM usage_v2_prices ORDER BY effective_from DESC")]


def _price(conn, provider, model, at):
    row = conn.execute("SELECT data FROM usage_v2_prices WHERE provider_id=? AND model=? AND effective_from<=? AND (effective_to IS NULL OR effective_to>?) ORDER BY effective_from DESC, rowid DESC LIMIT 1", (provider, model, at, at)).fetchone()
    return decode(row["data"], {}) if row else None


def _cost(usage, price):
    if not price:
        return {"amount": None, "currency": None, "status": "unpriced", "price_snapshot": None}
    if usage.get("input_tokens") is None or usage.get("output_tokens") is None:
        return {"amount": None, "currency": price["currency"], "status": "incomplete_usage", "price_snapshot": price}
    # Cached input is discounted only when the source explicitly supplies counts.
    # With distinct cache prices and missing cache counts, exact cost is unknown.
    if price.get("cache_read_per_million") is not None and usage.get("cache_read_tokens") is None:
        return {"amount": None, "currency": price["currency"], "status": "incomplete_cache_usage", "price_snapshot": price}
    if price.get("cache_write_per_million") is not None and usage.get("cache_write_tokens") is None:
        return {"amount": None, "currency": price["currency"], "status": "incomplete_cache_usage", "price_snapshot": price}
    amount = Decimal(0)
    for token, rate in (("uncached_input_tokens", "input_per_million"), ("output_tokens", "output_per_million"), ("cache_read_tokens", "cache_read_per_million"), ("cache_write_tokens", "cache_write_per_million")):
        if token == "uncached_input_tokens" and usage.get("uncached_input_tokens") is None:
            count = (usage.get("input_tokens") or 0) - (usage.get("cache_read_tokens") or 0) - (usage.get("cache_write_tokens") or 0)
        else:
            count = usage.get(token) or 0
        value = price.get(rate)
        if count and value is None:
            # No separate cache tariff: use ordinary input tariff, never free.
            value = price.get("input_per_million") if token.startswith("cache_") else None
        if count and value is None:
            return {"amount": None, "currency": price["currency"], "status": "incomplete_price", "price_snapshot": price}
        if value is not None:
            amount += Decimal(count) * Decimal(value) / Decimal(1_000_000)
    return {"amount": format(amount, "f"), "currency": price["currency"], "status": "estimated", "price_snapshot": price}


def _metrics(data):
    start, first, last, end = [data.get(k) for k in ("started_at", "first_content_at", "last_content_at", "ended_at")]
    def seconds(a, b):
        if not a or not b:
            return None
        value = (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()
        return value if value >= 0 else None
    ttft, duration, generation = seconds(start, first), seconds(start, end), seconds(first, last)
    count = data.get("content_chunks")
    speed = None
    if generation and count is not None and count > 1 and data["usage"].get("output_tokens") is not None:
        speed = data["usage"]["output_tokens"] / generation
    return {"ttft_ms": ttft * 1000 if ttft is not None else None,
            "end_to_end_ms": duration * 1000 if duration is not None else None,
            "output_tokens_per_second": speed,
            "measurement": "stream" if data.get("timing_origin", data.get("origin")) == "proxy" else "reported",
            "speed_note": None if speed is not None else "requires token count and at least two timed content chunks"}


def _safe_observation(item):
    allowed = {"origin", "observation_id", "logical_request_id", "attempt_id", "provider_id", "model", "requested_model", "actual_model", "billing_model", "host_id", "project_id", "account_id", "upstream_request_id", "started_at", "ended_at", "first_content_at", "last_content_at", "content_chunks", "status", "protocol", "raw_usage", "input_includes_cache", "error_code", "cumulative", "source_id", "sequence", "coverage", "confidence"}
    if set(item) - allowed:
        raise HTTPException(422, "Only usage metadata is accepted; request/response bodies and credentials are not ledger fields")
    origin = item.get("origin", "native_log")
    if origin not in ORIGINS or not item.get("observation_id"):
        raise HTTPException(422, "A valid origin and stable observation_id are required")
    status = item.get("status", "succeeded")
    if status not in STATUSES:
        raise HTTPException(422, "Invalid attempt status")
    if not item.get("started_at"):
        raise HTTPException(422, "A stable started_at timestamp is required for replay-safe ingestion")
    result = {k: v for k, v in item.items() if k in allowed}
    result.update(origin=origin, status=status, started_at=_time(item["started_at"]))
    for key in ("ended_at", "first_content_at", "last_content_at"):
        result[key] = _time(item.get(key))
    result["content_chunks"] = _integer(item.get("content_chunks"))
    # Retain only documented numeric usage semantics, never arbitrary raw JSON.
    result["usage"] = normalize_usage(item.get("raw_usage"), item.get("protocol", "openai"), item.get("input_includes_cache"))
    result.pop("raw_usage", None)
    if result.get("error_code") and (len(str(result["error_code"])) > 64 or not all(c.isalnum() or c in "_.-" for c in str(result["error_code"]))):
        raise HTTPException(422, "error_code must be a short metadata identifier")
    for key in ("observation_id", "logical_request_id", "attempt_id", "provider_id", "model", "requested_model", "actual_model", "billing_model", "host_id", "project_id", "account_id", "upstream_request_id", "source_id"):
        value = result.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value)):
            raise HTTPException(422, "Identifiers must be short strings without control characters")
    return result


def ingest(observations):
    if not isinstance(observations, list) or len(observations) > 1000:
        raise HTTPException(422, "Supply at most 1000 explicit usage observations")
    cleaned = [_safe_observation(item) for item in observations]
    accepted, duplicates, results = 0, 0, []
    with db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        for data in cleaned:
            digest = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            old = conn.execute("SELECT payload_hash, attempt_id FROM usage_v2_observations WHERE origin=? AND observation_id=?", (data["origin"], data["observation_id"])).fetchone()
            if old:
                if old["payload_hash"] != digest:
                    raise HTTPException(409, "Observation identity was reused with different metadata")
                duplicates += 1
                results.append({"attempt_id": old["attempt_id"], "status": "duplicate"})
                continue
            if data.get("cumulative"):
                source = data.get("source_id")
                seq = data.get("sequence")
                if not source or isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
                    raise HTTPException(422, "Cumulative reports require source_id and nonnegative sequence")
                cursor = conn.execute("SELECT * FROM usage_v2_cursors WHERE source_id=?", (source,)).fetchone()
                if cursor and seq <= cursor["sequence"]:
                    raise HTTPException(409, "Cumulative sequence must advance; replay uses the same observation_id")
                prior = decode(cursor["counters"], {}) if cursor else {}
                current = data["usage"].copy()
                for key in (*COUNTERS, "uncached_input_tokens"):
                    value = current.get(key)
                    prev = prior.get(key)
                    if value is not None and prev is not None:
                        data["usage"][key] = value - prev if value >= prev else value
                conn.execute("INSERT INTO usage_v2_cursors VALUES (?,?,?) ON CONFLICT(source_id) DO UPDATE SET sequence=excluded.sequence,counters=excluded.counters", (source, seq, encode(current)))
            aid = data.get("attempt_id")
            alias = conn.execute("SELECT attempt_id FROM usage_v2_attempt_aliases WHERE alias_id=?", (aid,)).fetchone() if aid else None
            if alias:
                aid = alias["attempt_id"]
            existing = conn.execute("SELECT * FROM usage_v2_attempts WHERE id=?", (aid,)).fetchone() if aid else None
            correlated = []
            if data.get("upstream_request_id") and data.get("provider_id"):
                correlated = conn.execute("SELECT * FROM usage_v2_attempts WHERE provider_id=? AND upstream_request_id=?", (data["provider_id"], data["upstream_request_id"])).fetchall()
            # Late upstream IDs must reconcile a proxy start and a native report in
            # either arrival order. The existing proxy attempt owns the identity.
            if not existing and correlated:
                existing = next((r for r in correlated if "proxy" in decode(r["data"], {}).get("origins", [])), correlated[0])
                if aid and aid != existing["id"]:
                    conn.execute("INSERT OR REPLACE INTO usage_v2_attempt_aliases VALUES (?,?)", (aid, existing["id"]))
                aid = existing["id"]
            aid = aid or new_id("attempt")
            rid = existing["logical_request_id"] if existing else data.get("logical_request_id") or new_id("request")
            if existing and data.get("logical_request_id") not in (None, rid) and not correlated and not alias:
                raise HTTPException(409, "Attempt already belongs to a different logical request")
            for other in correlated:
                if other["id"] == aid:
                    continue
                conn.execute("UPDATE usage_v2_observations SET attempt_id=? WHERE attempt_id=?", (aid, other["id"]))
                conn.execute("UPDATE usage_v2_attempt_aliases SET attempt_id=? WHERE attempt_id=?", (aid, other["id"]))
                conn.execute("INSERT OR REPLACE INTO usage_v2_attempt_aliases VALUES (?,?)", (other["id"], aid))
                conn.execute("DELETE FROM usage_v2_attempts WHERE id=?", (other["id"],))
                conn.execute("DELETE FROM usage_v2_requests WHERE id=? AND NOT EXISTS (SELECT 1 FROM usage_v2_attempts WHERE logical_request_id=?)", (other["logical_request_id"], other["logical_request_id"]))
            conn.execute("INSERT OR IGNORE INTO usage_v2_requests VALUES (?,?)", (rid, data["started_at"]))
            conn.execute("INSERT INTO usage_v2_observations VALUES (?,?,?,?,?)", (data["origin"], data["observation_id"], aid, digest, encode(data)))
            records = [decode(r["data"], {}) for r in conn.execute("SELECT data FROM usage_v2_observations WHERE attempt_id=? ORDER BY rowid", (aid,))]
            # Proxy timing wins; source fields with greater coverage supplement it.
            records.sort(key=lambda r: (0 if r["status"] == "running" else 1 if r["status"] in {"unknown", "cancelled"} else 3 if r["origin"] == "proxy" else 2))
            merged = {"id": aid, "logical_request_id": rid, "usage": normalize_usage({}), "origins": sorted({r["origin"] for r in records}), "observations": len(records), "possible_duplicate": not bool(data.get("upstream_request_id"))}
            for record in records:
                for key, value in record.items():
                    if key == "usage":
                        merged["usage"].update({k: v for k, v in value.items() if v is not None})
                    elif value is not None and key not in {"attempt_id", "logical_request_id"}:
                        merged[key] = value
            # A late running receipt cannot downgrade a terminal receipt.
            terminal = [r for r in records if r["status"] != "running"]
            if terminal:
                merged["status"] = terminal[-1]["status"]
            timing_records = [r for r in records if r["origin"] == "proxy"]
            if timing_records:
                for record in timing_records:
                    for key in ("started_at", "first_content_at", "last_content_at", "ended_at", "content_chunks"):
                        if record.get(key) is not None:
                            merged[key] = record[key]
                merged["timing_origin"] = "proxy"
            merged["observation_refs"] = [{"origin": r["origin"], "observation_id": r["observation_id"], "status": r["status"]} for r in records]
            price = decode(existing["data"], {}).get("cost", {}).get("price_snapshot") if existing else None
            merged["cost"] = _cost(merged["usage"], price or _price(conn, merged.get("provider_id"), merged.get("billing_model") or merged.get("model"), merged["started_at"]))
            merged["performance"] = _metrics(merged)
            merged["usage_complete"] = merged["usage"].get("input_tokens") is not None and merged["usage"].get("output_tokens") is not None
            cols = [aid, rid, merged.get("provider_id"), merged.get("model"), merged.get("host_id"), merged.get("project_id"), merged.get("account_id"), merged.get("upstream_request_id"), merged["started_at"], merged["status"], encode(merged)]
            conn.execute("INSERT INTO usage_v2_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET provider_id=excluded.provider_id,model=excluded.model,host_id=excluded.host_id,project_id=excluded.project_id,account_id=excluded.account_id,upstream_request_id=excluded.upstream_request_id,started_at=excluded.started_at,status=excluded.status,data=excluded.data", cols)
            accepted += 1
            results.append({"attempt_id": aid, "logical_request_id": rid, "status": "accepted"})
        if accepted:
            log_audit(actor="user", action="usage.ingest", target=f"batch:{accepted}", conn=conn)
    return {"accepted": accepted, "duplicates": duplicates, "items": results}


# Small result caches contain aggregates only, never attempt/observation bodies.
# Keys include the SQLite identity and transactionally maintained ledger revision.
# No time-based stale period: any committed insert/update/delete invalidates them.
_AGGREGATES = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_LIMIT = 32


def _where(filters=None):
    filters = filters or {}
    where, params = [], []
    for key in ("host_id", "provider_id", "model", "project_id", "account_id", "status", "logical_request_id"):
        if filters.get(key) is not None:
            where.append(f"{key}=?")
            params.append(filters[key])
    for key, sign in (("start", ">="), ("end", "<")):
        if filters.get(key):
            where.append(f"started_at{sign}?")
            params.append(_time(filters[key]))
    return (" WHERE " + " AND ".join(where) if where else ""), tuple(params)


def _cache_key(conn, where, params):
    filename = conn.execute("PRAGMA database_list").fetchone()["file"]
    try:
        stat = os.stat(filename)
        identity = (filename, stat.st_dev, stat.st_ino)
    except OSError:
        identity = (filename,)
    row = conn.execute("SELECT revision FROM usage_v2_revision WHERE singleton=1").fetchone()
    revision = row[0] if row else 0
    return identity, revision, where, params


def _cached(key):
    with _CACHE_LOCK:
        value = _AGGREGATES.get(key)
        if value is not None:
            _AGGREGATES.move_to_end(key)
            return deepcopy(value)
    return None


def _remember(key, value):
    with _CACHE_LOCK:
        _AGGREGATES[key] = deepcopy(value)
        _AGGREGATES.move_to_end(key)
        while len(_AGGREGATES) > _CACHE_LIMIT:
            _AGGREGATES.popitem(last=False)


def requests(filters=None, limit=50, offset=0):
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise HTTPException(422, "Positive limit and nonnegative offset are required")
    where, params = _where(filters)
    with db() as conn:
        ensure_schema(conn)
        # Both reads share one snapshot, including while ingestion commits.
        conn.execute("BEGIN")
        count_key = ("count", _cache_key(conn, where, params))
        total = _cached(count_key)
        if total is None:
            total = conn.execute("SELECT COUNT(*) FROM usage_v2_attempts" + where, params).fetchone()[0]
            _remember(count_key, total)
        cursor = conn.execute("SELECT data FROM usage_v2_attempts" + where + " ORDER BY started_at DESC,id DESC LIMIT ? OFFSET ?", (*params, limit, offset))
        items = [decode(row[0], {}) for row in cursor]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def _coverage_result(attempts, metered, origins):
    return {"observed_attempts": attempts, "metered_attempts": metered,
            "unknown_usage_attempts": attempts - metered, "origins": sorted(origins),
            "scope": "Only explicitly ingested metadata and enabled router traffic; no private history is scanned",
            "all_host_usage": "unknown", "legacy_usage_meters": "excluded_contextcore_compute_legacy"}


def coverage(filters=None):
    where, params = _where(filters)
    with db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN")
        key = ("coverage", _cache_key(conn, where, params))
        found = _cached(key)
        if found is not None:
            return found
        attempts = metered = 0
        origins = set()
        # Compact SQLite projection, never loads the full data/observation refs.
        cursor = conn.execute("SELECT json_extract(data,'$.usage_complete','$.origins') FROM usage_v2_attempts" + where, params)
        for row in cursor:
            complete, source_origins = json.loads(row[0])
            attempts += 1
            metered += bool(complete)
            origins.update(source_origins or [])
        result = _coverage_result(attempts, metered, origins)
        _remember(key, result)
        return result


def _distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"samples": 0, "p50": None, "p95": None, "mean": None}
    return {"samples": len(values), "p50": values[(len(values) - 1) // 2], "p95": values[min(len(values)-1, int((len(values)-1)*.95+.999999))], "mean": sum(values)/len(values)}


def summary(filters=None, tz="Asia/Shanghai"):
    try:
        zone = ZoneInfo(tz)
    except ZoneInfoNotFoundError:
        raise HTTPException(422, "Unknown timezone")
    where, params = _where(filters)
    with db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN")
        base_key = _cache_key(conn, where, params)
        key = ("summary", base_key, tz)
        found = _cached(key)
        if found is not None:
            return found
        costs, days, logical_ids, origins = {}, {}, set(), set()
        attempts = metered = missing_input = missing_output = unknown_cost = 0
        known_input = known_output = 0
        statuses = {s: 0 for s in sorted(STATUSES)}
        performance = {k: array("d") for k in ("ttft_ms", "end_to_end_ms", "output_tokens_per_second")}
        # One scan and one JSON1 parse per attempt, returning only nine scalars
        # and origins. Large snapshots/observation_refs never enter Python.
        cursor = conn.execute("""SELECT logical_request_id,started_at,status,
            json_extract(data,'$.usage.input_tokens','$.usage.output_tokens',
              '$.cost.amount','$.cost.currency','$.performance.ttft_ms',
              '$.performance.end_to_end_ms','$.performance.output_tokens_per_second',
              '$.usage_complete','$.origins')
            FROM usage_v2_attempts""" + where, params)
        for row in cursor:
            inp, out, amount, currency, ttft, duration, speed, complete, source_origins = json.loads(row[3])
            attempts += 1
            logical_ids.add(row[0])
            metered += bool(complete)
            origins.update(source_origins or [])
            known_input += inp or 0
            known_output += out or 0
            missing_input += inp is None
            missing_output += out is None
            unknown_cost += amount is None
            if amount is not None:
                costs[currency] = costs.get(currency, Decimal(0)) + Decimal(amount)
            if row[2] in statuses:
                statuses[row[2]] += 1
            for metric, value in zip(performance, (ttft, duration, speed)):
                if value is not None:
                    performance[metric].append(value)
            day = datetime.fromisoformat(row[1]).astimezone(zone).strftime("%Y-%m-%d")
            item = days.setdefault(day, {"date": day, "attempts": 0, "known_tokens": 0, "unknown_usage_attempts": 0})
            item["attempts"] += 1
            item["known_tokens"] += (inp or 0) + (out or 0)
            item["unknown_usage_attempts"] += not bool(complete)
        coverage_result = _coverage_result(attempts, metered, origins)
        result = {"logical_requests": len(logical_ids), "attempts": attempts,
                  "input_tokens": None if missing_input else known_input,
                  "output_tokens": None if missing_output else known_output,
                  "known_tokens": known_input + known_output, "unknown_usage_count": attempts - metered,
                  "cost_by_currency": {c: format(v, "f") for c, v in costs.items()},
                  "unknown_cost_count": unknown_cost, "status_counts": statuses,
                  "performance": {k: _distribution(v) for k, v in performance.items()},
                  "heatmap": [days[k] for k in sorted(days)], "timezone": tz, "coverage": coverage_result}
        _remember(key, result)
        # The separate endpoint reuses this exact snapshot rather than scanning
        # the ledger again immediately after summary (and vice versa when warm).
        _remember(("coverage", base_key), coverage_result)
        _remember(("count", base_key), attempts)
        return result


def extract_usage_from_messages(messages, *, host_id, source_id, project_id=None, provider_id=None, account_id=None, started_at=None):
    """Pure parser for an already-authorized batch, never opens a path.

    Accepts raw Codex JSONL rows, Claude assistant rows, OpenCode message-info
    records and OMP assistant messages. Missing counters stay null. Callers must
    supply a stable source identity and preserve raw usage metadata in adapters.
    """
    if not source_id or not isinstance(messages, list):
        raise HTTPException(422, "A source_id and explicit messages list are required")
    events = []
    for index, row in enumerate(messages):
        if not isinstance(row, dict):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else row
        message = row.get("message") if isinstance(row.get("message"), dict) else payload
        info = row.get("info") if isinstance(row.get("info"), dict) else {}
        raw = message.get("usage") or payload.get("usage") or info.get("tokens") or row.get("tokens")
        cumulative = False
        protocol = "anthropic" if host_id in {"claude_code", "claude-code"} else "openai"
        if payload.get("type") == "token_count" or row.get("type") == "token_usage_record":
            token_info = payload.get("info") or payload
            raw = token_info.get("total_token_usage")
            cumulative = bool(raw)
            if raw is None:
                raw = token_info.get("last_token_usage")
        if not isinstance(raw, dict):
            continue
        mapped = dict(raw)
        aliases = {"input": "input_tokens", "output": "output_tokens", "cacheRead": "cache_read_tokens", "cacheWrite": "cache_write_tokens", "cached_input_tokens": "cache_read_tokens", "reasoning_output_tokens": "reasoning_tokens", "reasoning": "reasoning_tokens"}
        for old, new in aliases.items():
            if old in raw and new not in mapped:
                mapped[new] = raw[old]
        cache = raw.get("cache")
        if isinstance(cache, dict):
            if cache.get("read") is not None:
                mapped["cache_read_tokens"] = cache["read"]
            if cache.get("write") is not None:
                mapped["cache_write_tokens"] = cache["write"]
        # Pi/OMP reports uncached input separately, as Anthropic does.
        if host_id in {"omp", "opencode"}:
            includes_cache = False
        else:
            includes_cache = protocol != "anthropic"
        stamp = row.get("timestamp") or message.get("timestamp") or info.get("time", {}).get("created") or started_at
        if isinstance(stamp, (int, float)):
            stamp = datetime.fromtimestamp(stamp / 1000 if stamp > 100_000_000_000 else stamp, timezone.utc).isoformat()
        if not stamp:
            # No fabricated current timestamp: a rescanned record must be stable.
            continue
        event_id = row.get("uuid") or row.get("id") or message.get("id") or info.get("id")
        if not event_id:
            event_id = hashlib.sha256(json.dumps({"index": index, "timestamp": stamp, "usage": mapped}, sort_keys=True).encode()).hexdigest()
        upstream_id = row.get("requestId") or row.get("request_id") or payload.get("request_id")
        # Claude's message ID identifies one physical API response across logs.
        if host_id in {"claude_code", "claude-code"}:
            upstream_id = upstream_id or message.get("id")
        event = {"origin": "native_log", "observation_id": f"{source_id}:{event_id}", "host_id": host_id,
                 "project_id": project_id, "account_id": account_id, "provider_id": provider_id or message.get("provider") or info.get("providerID"),
                 "model": message.get("model") or payload.get("model") or info.get("modelID"),
                 "started_at": _time(stamp), "raw_usage": mapped, "protocol": protocol,
                 "input_includes_cache": includes_cache, "upstream_request_id": upstream_id,
                 "status": "failed" if row.get("error") or info.get("error") else "succeeded"}
        native_record_id = upstream_id or message.get("id") or info.get("id") or row.get("id")
        if native_record_id and not cumulative:
            event["attempt_id"] = "native_" + hashlib.sha256(f"{host_id}:{source_id}:{native_record_id}".encode()).hexdigest()[:32]
        if not cumulative:
            revision = hashlib.sha256(json.dumps({"usage": mapped, "model": event.get("model"), "timestamp": stamp}, sort_keys=True).encode()).hexdigest()[:16]
            event["observation_id"] += ":" + revision
        if cumulative:
            event.update(cumulative=True, source_id=f"{host_id}:{source_id}:total", sequence=index)
        # Validate now so a malformed report does not silently masquerade as zero.
        _safe_observation(event)
        events.append(event)
    return events
