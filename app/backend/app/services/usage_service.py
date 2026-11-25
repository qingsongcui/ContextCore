"""Metadata-only request ledger. No history is read and no inference is performed here."""
from __future__ import annotations

import hashlib
import os
import threading
from array import array
from collections import OrderedDict
from copy import deepcopy
import json
from datetime import datetime, timedelta, timezone
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
    CREATE INDEX IF NOT EXISTS usage_v2_route_outcome_time ON usage_v2_attempts(host_id,json_extract(data,'$.route_version'),json_extract(data,'$.gateway_outcome'),COALESCE(json_extract(data,'$.ended_at'),started_at) DESC,id DESC) WHERE host_id IS NOT NULL AND json_extract(data,'$.route_version') IS NOT NULL;
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_provider_page ON usage_v2_attempts(provider_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_model_page ON usage_v2_attempts(model, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_project_page ON usage_v2_attempts(project_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_account_page ON usage_v2_attempts(account_id, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_attempt_status_page ON usage_v2_attempts(status, started_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS usage_v2_provider_snapshot_name ON usage_v2_attempts(provider_id, started_at DESC,id DESC) WHERE json_type(data,'$.provider_name')='text';
    CREATE INDEX IF NOT EXISTS usage_v2_account_snapshot_name ON usage_v2_attempts(account_id, started_at DESC,id DESC) WHERE json_type(data,'$.account_name')='text';
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
    CREATE INDEX IF NOT EXISTS usage_v2_price_model_time ON usage_v2_prices(provider_id, model, effective_from);
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


def _time(value: str | None, default=None, *, field: str | None = None):
    if value is None:
        return default
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError):
        prefix = f"{field}: " if field else ""
        raise HTTPException(422, f"{prefix}Timestamps must be ISO8601 with timezone")


def _integer(value, *, field: str | None = None):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        prefix = f"{field}: " if field else ""
        raise HTTPException(422, f"{prefix}Token counts must be nonnegative integers or null")
    return value


def normalize_usage(raw: dict | None, protocol="openai", input_includes_cache=None):
    if input_includes_cache is not None and type(input_includes_cache) is not bool:
        raise HTTPException(422, "input_includes_cache must be a boolean or null")
    raw = raw or {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "raw_usage must be an object")
    def pick_named(*names):
        return next(((n, raw[n]) for n in names if raw.get(n) is not None), (names[0], None))
    inp_name, inp_val = pick_named("input_tokens", "prompt_tokens")
    inp = _integer(inp_val, field=inp_name)
    out_name, out_val = pick_named("output_tokens", "completion_tokens")
    out = _integer(out_val, field=out_name)
    details = raw.get("input_tokens_details") or raw.get("prompt_tokens_details") or {}
    out_details = raw.get("output_tokens_details") or raw.get("completion_tokens_details") or {}
    if not isinstance(details, dict) or not isinstance(out_details, dict):
        raise HTTPException(422, "Usage details must be objects")
    read_name, read_val = pick_named("cache_read_tokens", "cache_read_input_tokens")
    read = _integer(read_val, field=read_name)
    if read is None and details.get("cached_tokens") is not None:
        read = _integer(details["cached_tokens"], field="cached_tokens")
    write_name, write_val = pick_named("cache_write_tokens", "cache_creation_input_tokens")
    write = _integer(write_val, field=write_name)
    reasoning = _integer(pick_named("reasoning_tokens")[1], field="reasoning_tokens")
    if reasoning is None and out_details.get("reasoning_tokens") is not None:
        reasoning = _integer(out_details["reasoning_tokens"], field="reasoning_tokens")
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


def _decimal(value, *, field: str | None = None):
    if value is None:
        return None
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0 or abs(result.as_tuple().exponent) > 30 or len(result.as_tuple().digits) > 30:
            raise ValueError()
        return format(result, "f")
    except (ValueError, InvalidOperation):
        prefix = f"{field}: " if field else ""
        raise HTTPException(422, f"{prefix}Prices must be finite nonnegative decimal values")


def add_price(data):
    allowed = {"provider_id", "model", "effective_from", "effective_to", "currency", "input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million"}
    if set(data) - allowed:
        raise HTTPException(422, "Unsupported price fields")
    if not data.get("provider_id") or not data.get("model") or any(not isinstance(data[k], str) or len(data[k]) > 256 for k in ("provider_id", "model")):
        raise HTTPException(422, "Provider and billing model are required")
    start = _time(data.get("effective_from"), now_iso(), field="effective_from")
    end = _time(data.get("effective_to"), field="effective_to")
    if end and end <= start:
        raise HTTPException(422, "Price end must follow start")
    currency = str(data.get("currency", "USD")).upper()
    if len(currency) != 3 or not currency.isascii() or not currency.isalpha():
        raise HTTPException(422, "Currency must be a three-letter code")
    result = {"id": new_id("price"), "provider_id": data["provider_id"], "model": data["model"],
              "effective_from": start, "effective_to": end, "currency": currency, "source": "manual"}
    for key in ("input_per_million", "output_per_million", "cache_read_per_million", "cache_write_per_million"):
        result[key] = _decimal(data.get(key), field=key)
    with db() as conn:
        ensure_schema(conn)
        conn.execute("INSERT INTO usage_v2_prices VALUES (?,?,?,?,?,?,?)", (result["id"], result["provider_id"], result["model"], start, end, currency, encode(result)))
        log_audit(actor="user", action="usage.price.create", target=result["id"], conn=conn)
    return result


def list_prices():
    with db() as conn:
        ensure_schema(conn)
        return [decode(r["data"], {}) for r in conn.execute("SELECT data FROM usage_v2_prices ORDER BY effective_from DESC")]


def _price(conn, provider, model, at, *, catalog_revision=None):
    query = ("SELECT data FROM usage_v2_prices WHERE provider_id=? AND model=? AND effective_from<=? "
             "AND (effective_to IS NULL OR effective_to>?)")
    params = [provider, model, at, at]
    if catalog_revision is not None:
        query += " AND rowid<=?"
        params.append(catalog_revision)
    row = conn.execute(query + " ORDER BY effective_from DESC,rowid DESC LIMIT 1", params).fetchone()
    return decode(row["data"], {}) if row else None


def _receipt_price(conn, merged, prior, observation):
    model = merged.get("billing_model") or merged.get("model")
    outcome = observation.get("gateway_outcome")
    revision = prior.get("price_catalog_revision")
    if outcome == "running" and revision is None:
        # An upstream alias may resolve to a different billing model only once
        # the reply arrives. Preserve the catalog as it stood at dispatch.
        revision = conn.execute("SELECT COALESCE(MAX(rowid),0) FROM usage_v2_prices").fetchone()[0]
    if revision is not None:
        merged["price_catalog_revision"] = revision
    if not prior:
        return _price(conn, merged.get("provider_id"), model, merged["started_at"], catalog_revision=revision)
    if outcome and outcome != "running" and prior.get("gateway_outcome") == "running":
        actual = observation.get("actual_model")
        if actual and actual == model and actual != observation.get("model"):
            if revision is None:
                return None  # pre-upgrade pending attempts lack a dispatch catalog boundary
            return _price(conn, merged.get("provider_id"), actual, merged["started_at"], catalog_revision=revision)
    # Ordinary supplemental metadata cannot introduce a new price, including
    # where the original price was unknown.
    return prior.get("cost", {}).get("price_snapshot")


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


ROUTE_SNAPSHOT = ("connection_mode", "route_version", "attempt_number", "provider_name", "credential_source", "account_id", "account_name")


def _observed_performance(usage, records):
    # Never subtract timestamps from different observers. A user-imported
    # proxy receipt is reported timing; only this gateway owns its clock.
    clocks = {}
    for record in records:
        measured = record["origin"] == "proxy" and bool(record.get("gateway_outcome"))
        clock = (record["origin"], measured)
        if clock not in clocks:
            clocks[clock] = {"origin": record["origin"], "usage": usage}
        for field in ("started_at", "first_content_at", "last_content_at", "ended_at", "content_chunks"):
            if record.get(field) is not None:
                clocks[clock][field] = record[field]
    result = {key: None for key in METRICS}
    methods = {}
    for (_, measured), clock in sorted(clocks.items(), key=lambda item: item[0][1]):
        sample = _metrics(clock)
        for key in METRICS:
            if sample[key] is not None:
                result[key] = sample[key]
                methods[key] = "stream" if measured else "reported"
    sources = set(methods.values())
    return {**result, "measurement_by_metric": methods,
            "measurement": next(iter(sources)) if len(sources) == 1 else "mixed" if sources else None,
            "speed_note": "requires token count and at least two timed content chunks"
            if result["output_tokens_per_second"] is None else None}


def _safe_observation(item):
    allowed = {"origin", "observation_id", "logical_request_id", "attempt_id", "provider_id", "model", "requested_model", "actual_model", "billing_model", "host_id", "project_id", "account_id", "upstream_request_id", "started_at", "ended_at", "first_content_at", "last_content_at", "content_chunks", "status", "protocol", "raw_usage", "input_includes_cache", "error_code", "cumulative", "source_id", "sequence", "coverage", "confidence"}
    allowed.update(ROUTE_SNAPSHOT)
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
    for field in ("route_version", "attempt_number"):
        if field in result and (type(result[field]) is not int or not 1 <= result[field] <= 2**63 - 1):
            raise HTTPException(422, f"{field} must be a positive integer")
    for field, choices in (("connection_mode", ("routing", "aggregation")), ("credential_source", ("account", "provider"))):
        if field in result and result[field] not in choices:
            raise HTTPException(422, f"Unsupported {field}")
    result.update(origin=origin, status=status, started_at=_time(item["started_at"], field="started_at"))
    for key in ("ended_at", "first_content_at", "last_content_at"):
        result[key] = _time(item.get(key), field=key)
    result["content_chunks"] = _integer(item.get("content_chunks"), field="content_chunks")
    # Retain only documented numeric usage semantics, never arbitrary raw JSON.
    result["usage"] = normalize_usage(item.get("raw_usage"), item.get("protocol", "openai"), item.get("input_includes_cache"))
    result.pop("raw_usage", None)
    if result.get("error_code") and (len(str(result["error_code"])) > 64 or not all(c.isalnum() or c in "_.-" for c in str(result["error_code"]))):
        raise HTTPException(422, "error_code must be a short metadata identifier")
    for key in ("observation_id", "logical_request_id", "attempt_id", "provider_id", "provider_name", "model", "requested_model", "actual_model", "billing_model", "host_id", "project_id", "account_id", "account_name", "upstream_request_id", "source_id"):
        value = result.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value)):
            raise HTTPException(422, "Identifiers must be short strings without control characters")
    return result


def _cumulative_delta(current, previous):
    # Keep the last known value of each counter across partial snapshots. The
    # emitted sample still reports null for every field its source omitted.
    fields = (*COUNTERS, "uncached_input_tokens")
    missing = set(previous.get("missing", ())) | {k for k in fields if previous.get(k) is None}
    primary_input = "input_tokens" if current.get("input_includes_cache") else "uncached_input_tokens"
    reset = any(current.get(k) is not None and previous.get(k) is not None and current[k] < previous[k]
                for k in (primary_input, "output_tokens"))
    delta, cursor = current.copy(), current.copy()
    cursor["missing"] = [k for k in fields if current.get(k) is None]
    for key in fields:
        value, prior = current.get(key), previous.get(key)
        if value is None:
            cursor[key] = None if reset else prior
        elif prior is not None and (not reset or key not in missing):
            delta[key] = value - prior if value >= prior else value
    if current.get("input_includes_cache"):
        # A cache delta spanning an observation gap cannot be assigned to the
        # last input interval. Preserve the fresh baseline for the next sample.
        caches = ("cache_read_tokens", "cache_write_tokens")
        if previous and not reset:
            for key in caches:
                if key in missing or "input_tokens" in missing:
                    delta[key] = None
        cache = sum(delta[k] or 0 for k in caches)
        input_count = delta["input_tokens"]
        if input_count is not None and input_count < cache:
            for key in caches:
                delta[key] = None
        delta["uncached_input_tokens"] = (input_count - cache if input_count is not None
                                           and any(delta[k] is not None for k in caches) else None)
    elif delta["uncached_input_tokens"] is not None:
        # Anthropic reports base input and cache as disjoint counters. Missing
        # cache telemetry must not look like a reset of the running input total.
        delta["input_tokens"] = delta["uncached_input_tokens"] + (delta["cache_read_tokens"] or 0) + (delta["cache_write_tokens"] or 0)
    return delta, cursor


def ingest(observations, *, local_gateway=False):
    if not isinstance(observations, list) or len(observations) > 1000:
        raise HTTPException(422, "Supply at most 1000 explicit usage observations")
    cleaned = [_safe_observation(item) for item in observations]
    if local_gateway:
        for data in cleaned:
            data["gateway_outcome"] = data["status"]
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
                data["usage"], current = _cumulative_delta(data["usage"], prior)
                conn.execute("INSERT INTO usage_v2_cursors VALUES (?,?,?) ON CONFLICT(source_id) DO UPDATE SET sequence=excluded.sequence,counters=excluded.counters", (source, seq, encode(current)))
            aid = data.get("attempt_id")
            alias = conn.execute("SELECT attempt_id FROM usage_v2_attempt_aliases WHERE alias_id=?", (aid,)).fetchone() if aid else None
            if alias:
                aid = alias["attempt_id"]
            existing = conn.execute("SELECT * FROM usage_v2_attempts WHERE id=?", (aid,)).fetchone() if aid else None
            if existing:
                for field in ("account_id", "provider_id", "upstream_request_id"):
                    if existing[field] and data.get(field) and existing[field] != data[field]:
                        raise HTTPException(409, "Attempt conflicts with recorded identity")
            correlated = []
            ambiguous_upstream = False
            if data.get("upstream_request_id") and data.get("provider_id"):
                matches = conn.execute("SELECT * FROM usage_v2_attempts WHERE provider_id=? AND upstream_request_id=?", (data["provider_id"], data["upstream_request_id"])).fetchall()
                # An upstream ID alone cannot combine two known accounts or two
                # independently dispatched attempts, even with identical model/ID.
                for row in matches:
                    if row["account_id"] and data.get("account_id") and row["account_id"] != data["account_id"]:
                        continue
                    if row["id"] != aid and data.get("logical_request_id") and data["logical_request_id"] != row["logical_request_id"]:
                        prior_identity = conn.execute("SELECT 1 FROM usage_v2_observations WHERE attempt_id=? "
                                                      "AND json_extract(data,'$.logical_request_id') IS NOT NULL LIMIT 1", (row["id"],)).fetchone()
                        if prior_identity:
                            ambiguous_upstream = True
                            continue
                    origins = decode(row["data"], {}).get("origins", [])
                    if data["origin"] == "proxy" and "proxy" in origins and aid and aid != row["id"]:
                        continue
                    correlated.append(row)
                if len(correlated) > 1:
                    ambiguous_upstream = True
                    correlated = [row for row in correlated if row["id"] == aid]
            # Late upstream IDs must reconcile a proxy start and a native report in
            # either arrival order. The existing proxy attempt owns the identity.
            if not existing and correlated:
                existing = next((r for r in correlated if "proxy" in decode(r["data"], {}).get("origins", [])), correlated[0])
                if aid and aid != existing["id"]:
                    conn.execute("INSERT OR REPLACE INTO usage_v2_attempt_aliases VALUES (?,?)", (aid, existing["id"]))
                aid = existing["id"]
            aid = aid or new_id("attempt")
            rid = existing["logical_request_id"] if existing else data.get("logical_request_id") or new_id("request")
            prior_rid = rid
            if existing and data.get("logical_request_id") not in (None, rid):
                # A late, identified gateway receipt may name the request for
                # an earlier native-only observation whose group we generated.
                named = conn.execute("SELECT 1 FROM usage_v2_observations WHERE attempt_id=? "
                                     "AND json_extract(data,'$.logical_request_id') IS NOT NULL LIMIT 1", (aid,)).fetchone()
                if not named and data["origin"] == "proxy" and correlated:
                    rid = data["logical_request_id"]
                else:
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
            merged = {"id": aid, "logical_request_id": rid, "usage": normalize_usage({}), "origins": sorted({r["origin"] for r in records}), "observations": len(records)}
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
                dispatched = next((r for r in timing_records if r.get("gateway_outcome") and r.get("route_version")), None)
                if dispatched:
                    for key in (*ROUTE_SNAPSHOT, "provider_id", "requested_model", "model"):
                        if key in dispatched:
                            merged[key] = dispatched[key]
                        else:
                            merged.pop(key, None)
                    if dispatched.get("host_id"):
                        merged["host_id"] = dispatched["host_id"]
            if merged.get("cumulative"):
                merged["unattributed_sample"] = all(r["origin"] == "native_log" and r.get("cumulative") and
                                                     not any(r.get(k) for k in ("attempt_id", "logical_request_id", "upstream_request_id"))
                                                     for r in records)
            merged["possible_duplicate"] = ambiguous_upstream or not (merged.get("provider_id") and merged.get("upstream_request_id"))
            merged["observation_refs"] = [{"origin": r["origin"], "observation_id": r["observation_id"], "status": r["status"]} for r in records]
            prior = decode(existing["data"], {}) if existing else {}
            price = _receipt_price(conn, merged, prior, data)
            merged["cost"] = _cost(merged["usage"], price)
            merged["performance"] = _observed_performance(merged["usage"], records)
            merged["usage_complete"] = merged["usage"].get("input_tokens") is not None and merged["usage"].get("output_tokens") is not None
            cols = [aid, rid, merged.get("provider_id"), merged.get("model"), merged.get("host_id"), merged.get("project_id"), merged.get("account_id"), merged.get("upstream_request_id"), merged["started_at"], merged["status"], encode(merged)]
            conn.execute("INSERT INTO usage_v2_attempts VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET logical_request_id=excluded.logical_request_id,provider_id=excluded.provider_id,model=excluded.model,host_id=excluded.host_id,project_id=excluded.project_id,account_id=excluded.account_id,upstream_request_id=excluded.upstream_request_id,started_at=excluded.started_at,status=excluded.status,data=excluded.data", cols)
            if prior_rid != rid:
                conn.execute("DELETE FROM usage_v2_requests WHERE id=? AND NOT EXISTS "
                             "(SELECT 1 FROM usage_v2_attempts WHERE logical_request_id=?)", (prior_rid, prior_rid))
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


DIMENSIONS = ("host_id", "provider_id", "model", "account_id", "project_id")
METRICS = ("ttft_ms", "end_to_end_ms", "output_tokens_per_second")


# Old databases did not mark cumulative-only native samples. Inspect their
# stored receipt identities lazily rather than migrating all historical JSON.
UNATTRIBUTED_SAMPLE = """CASE WHEN json_extract(data,'$.cumulative')=1
    THEN COALESCE(json_extract(data,'$.unattributed_sample'),
        json_extract(data,'$.origins')='["native_log"]' AND upstream_request_id IS NULL AND NOT EXISTS (
            SELECT 1 FROM usage_v2_observations obs WHERE obs.attempt_id=usage_v2_attempts.id AND
            COALESCE(json_extract(obs.data,'$.logical_request_id'),json_extract(obs.data,'$.attempt_id'),
                     json_extract(obs.data,'$.upstream_request_id')) IS NOT NULL)) ELSE 0 END"""


def dimension_sql(key):
    if key == "status":
        return f"CASE WHEN {UNATTRIBUTED_SAMPLE} THEN 'unknown' ELSE status END"
    if key == "model":
        return "COALESCE(NULLIF(json_extract(data,'$.actual_model'),''),model)"
    return key


# Ordering is shared by final-outcome filters, logical rows and drilldown.
# Native logs without dispatch ordinals use timestamp, then insertion order.
ATTEMPT_ORDER = "json_extract(data,'$.attempt_number'),started_at,rowid"
FINAL_ATTEMPT_ORDER = "json_extract(data,'$.attempt_number') DESC,started_at DESC,rowid DESC"


def _where(filters=None):
    filters = filters or {}
    where, params = [], []
    for key in (*DIMENSIONS, "status", "logical_request_id"):
        if filters.get(key) is not None:
            column = dimension_sql(key) if "scope" in filters else key
            where.append(f"{column}=?")
            params.append(filters[key])
    if filters.get("request_status") is not None:
        if filters["request_status"] not in STATUSES:
            raise HTTPException(422, "Unknown logical request outcome")
        where.append(f"(CASE WHEN {UNATTRIBUTED_SAMPLE} THEN 'unknown' ELSE "
                     "(SELECT last.status FROM usage_v2_attempts last WHERE "
                     "last.logical_request_id=usage_v2_attempts.logical_request_id ORDER BY "
                     + FINAL_ATTEMPT_ORDER + " LIMIT 1) END)=?")
        params.append(filters["request_status"])
    for key in sorted(set(filters.get("missing") or [])):
        if key not in DIMENSIONS or filters.get(key) is not None:
            raise HTTPException(422, "Invalid or contradictory missing dimension")
        where.append(f"{dimension_sql(key)} IS NULL")
    start, end = (_time(filters.get(key), field=key) for key in ("start", "end"))
    if start and end and end <= start:
        raise HTTPException(422, "End must follow start")
    if start:
        where.append("started_at>=?")
        params.append(start)
    if end:
        where.append("started_at<?")
        params.append(end)
    if filters.get("origin") is not None:
        if filters["origin"] not in ORIGINS:
            raise HTTPException(422, "Unknown observation source")
        where.append("EXISTS (SELECT 1 FROM json_each(data,'$.origins') WHERE value=?)")
        params.append(filters["origin"])
    scope = filters.get("scope", "all")
    if scope not in {"user", "system", "all"}:
        raise HTTPException(422, "Unknown usage scope")
    if scope != "all":
        # A native receipt may corroborate an internal probe. Its extra source
        # does not turn that probe into a second, user-initiated call.
        system = "EXISTS (SELECT 1 FROM json_each(data,'$.origins') WHERE value IN ('provider_check','contextcore_compute'))"
        where.append(system if scope == "system" else "NOT " + system)
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
    # The cache holds interactive views. Broad exports, multi-year ranges and
    # high-cardinality dimensions still return in full without retaining them.
    if isinstance(value, list) and len(value) > 2048:
        return
    if isinstance(value, dict) and any(isinstance(rows, list) and len(rows) > 2048 for rows in value.values()):
        return
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


def _observe_host(hosts, host_id, at, complete, origins, sample):
    item = hosts.setdefault(host_id, {"host_id": host_id, "first_observed_at": at,
                                     "last_observed_at": at, "observed_attempts": 0,
                                     "metered_attempts": 0, "unattributed_samples": 0, "origins": set()})
    item["first_observed_at"] = min(item["first_observed_at"], at)
    item["last_observed_at"] = max(item["last_observed_at"], at)
    item["observed_attempts"] += not sample
    item["metered_attempts"] += bool(complete) and not sample
    item["unattributed_samples"] += bool(sample)
    item["origins"].update(origins or ())


def _coverage_hosts(hosts):
    return [{**hosts[k], "origins": sorted(hosts[k]["origins"])} for k in sorted(hosts, key=lambda k: k or "")]


def coverage(filters=None):
    where, params = _where(filters)
    with db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN")
        key = ("coverage", _cache_key(conn, where, params))
        found = _cached(key)
        if found is not None:
            return found
        attempts = metered = samples = 0
        origins, hosts = set(), {}
        cursor = conn.execute(f"SELECT json_extract(data,'$.usage_complete','$.origins'),host_id,started_at,{UNATTRIBUTED_SAMPLE} FROM usage_v2_attempts" + where, params)
        for row in cursor:
            complete, source_origins = json.loads(row[0])
            sample = bool(row[3])
            attempts += not sample
            metered += bool(complete) and not sample
            samples += sample
            origins.update(source_origins or ())
            _observe_host(hosts, row[1], row[2], complete, source_origins, sample)
        result = {**_coverage_result(attempts, metered, origins), "unattributed_samples": samples, "hosts": _coverage_hosts(hosts)}
        _remember(key, result)
        return result


def _distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"samples": 0, "p50": None, "p95": None, "mean": None}
    size = len(values)
    # Use the same nearest-rank convention for both observed percentiles.
    return {"samples": size, "p50": values[(size - 1) // 2], "p95": values[(95 * size - 1) // 100],
            "mean": sum(values) / size}


def _totals():
    return {"logical_requests": 0, "attempts": 0, "unattributed_samples": 0, "known_tokens": 0, "known_token_attempts": 0,
            "unknown_usage_count": 0, "unknown_cost_count": 0, "cost_by_currency": {}}


def _count_attempt(bucket, new_request, inp, out, amount, currency, *, sample):
    bucket["logical_requests"] += new_request and not sample
    bucket["attempts"] += not sample
    bucket["unattributed_samples"] += bool(sample)
    bucket["known_tokens"] += (inp or 0) + (out or 0)
    bucket["known_token_attempts"] += inp is not None or out is not None
    bucket["unknown_usage_count"] += inp is None or out is None
    if amount is None or not currency:
        bucket["unknown_cost_count"] += 1
    else:
        costs = bucket["cost_by_currency"]
        costs[currency] = costs.get(currency, Decimal(0)) + Decimal(amount)


def _format_totals(bucket):
    return {**bucket, "cost_by_currency": {c: format(v, "f") for c, v in bucket["cost_by_currency"].items()}}


def _calendar(days, filters, zone):
    first = (filters or {}).get("start")
    end = (filters or {}).get("end")
    start_day = datetime.fromisoformat(_time(first)).astimezone(zone).date() if first else min(days, default=None)
    # The upper bound is exclusive even when it falls on a DST transition.
    end_day = (datetime.fromisoformat(_time(end)) - timedelta(microseconds=1)).astimezone(zone).date() if end else max(days, default=None)
    if start_day is None or end_day is None:
        return []
    day_count = (end_day - start_day).days + 1
    if day_count > 10000:
        raise HTTPException(422, "Select a shorter range for the daily calendar (maximum 10000 dates)")
    result = []
    for i in range(day_count):
        day = start_day + timedelta(days=i)
        if day in days:
            totals = _format_totals(days[day])
        else:
            totals = dict.fromkeys(_totals())
            totals["cost_by_currency"] = {}
        result.append({**totals, "date": day.isoformat(), "observed": day in days,
                       "unknown_usage_attempts": totals["unknown_usage_count"]})
    return result


def summary(filters=None, tz="Asia/Shanghai", *, calendar=False):
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "Unknown timezone")
    where, params = _where(filters)
    with db() as conn:
        ensure_schema(conn)
        conn.execute("BEGIN")
        base_key = _cache_key(conn, where, params)
        key = ("summary", base_key, tz, calendar)
        found = _cached(key)
        if found is not None:
            return found
        total, days, hosts, origins = _totals(), {}, {}, set()
        missing_input = missing_output = known_input = known_output = metered = 0
        statuses = {s: 0 for s in sorted(STATUSES)}
        performance = {method: {k: array("d") for k in METRICS} for method in ("stream", "reported")}
        # Stream related attempts together. Each logical request belongs to its
        # first matching day, while metering follows the date of each attempt.
        cursor = conn.execute(f"""SELECT logical_request_id,started_at,status,host_id,
            json_extract(data,'$.usage.input_tokens','$.usage.output_tokens',
              '$.cost.amount','$.cost.currency','$.performance.ttft_ms',
              '$.performance.end_to_end_ms','$.performance.output_tokens_per_second',
              '$.usage_complete','$.origins','$.performance.measurement_by_metric'), {UNATTRIBUTED_SAMPLE}
            FROM usage_v2_attempts""" + where + " ORDER BY logical_request_id,started_at,id", params)
        previous_request = None
        for row in cursor:
            inp, out, amount, currency, ttft, duration, speed, complete, sources, methods = json.loads(row[4])
            sample = bool(row[5])
            is_first = row[0] != previous_request
            previous_request = row[0]
            day = datetime.fromisoformat(row[1]).astimezone(zone).date()
            if day not in days:
                days[day] = _totals()
            for bucket in (total, days[day]):
                _count_attempt(bucket, is_first, inp, out, amount, currency, sample=sample)
            metered += bool(complete) and not sample
            origins.update(sources or ())
            _observe_host(hosts, row[3], row[1], complete, sources, sample)
            known_input += inp or 0
            known_output += out or 0
            missing_input += inp is None
            missing_output += out is None
            if not sample and row[2] in statuses:
                statuses[row[2]] += 1
            for metric, value in zip(METRICS, (ttft, duration, speed)):
                if value is not None:
                    # Historical rows predate per-clock evidence and retain reported status.
                    source = (methods or {}).get(metric, "reported")
                    performance["stream" if source == "stream" else "reported"][metric].append(value)
        coverage_result = {**_coverage_result(total["attempts"], metered, origins), "unattributed_samples": total["unattributed_samples"], "hosts": _coverage_hosts(hosts)}
        observed_days = [{**_format_totals(days[d]), "date": d.isoformat(), "observed": True,
                          "unknown_usage_attempts": days[d]["unknown_usage_count"]} for d in sorted(days)]
        result = {**_format_totals(total), "input_tokens": None if missing_input else known_input,
                  "output_tokens": None if missing_output else known_output, "status_counts": statuses,
                  "performance": {k: _distribution(performance["stream"][k] + performance["reported"][k]) for k in METRICS},
                  "performance_by_measurement": {method: {k: _distribution(v) for k, v in metrics.items()}
                                                 for method, metrics in performance.items()},
                  "heatmap": _calendar(days, filters, zone) if calendar else observed_days,
                  "timezone": tz, "percentile_method": "nearest_rank", "coverage": coverage_result}
        _remember(key, result)
        _remember(("coverage", base_key), coverage_result)
        _remember(("count", base_key), total["attempts"] + total["unattributed_samples"])
        _remember(("logical_count", base_key), total["logical_requests"] + total["unattributed_samples"])
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
        protocol = "anthropic" if host_id in {"claude-code"} else "openai"
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
        if host_id in {"claude-code"}:
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
