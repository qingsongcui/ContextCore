import hashlib
import json
import re
import unicodedata
from typing import Any
from fastapi import HTTPException
from app.database import db, now_iso, new_id, row_to_dict, log_audit, encode, decode, get_app_setting


VALID_CATEGORIES = {"preference", "background", "decision", "task", "lesson", "knowledge"}
MEMORY_STANCES = {"fact", "preference", "decision", "suggestion", "assumption", "negation", "quote"}
# These are recorded and labeled, but they are not an active user decision.
NON_DECISION_STANCES = {"fact", "suggestion", "assumption", "negation", "quote"}


def compute_content_hash(project_id: str | None, category: str, title: str, content: str) -> str:
    raw = f"content:{project_id or ''}:{category}:{title.strip()}:{content.strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compute_normalized_content_hash(project_id: str | None, category: str, title: str, content: str) -> str:
    norm_title = re.sub(r"\s+", " ", (title or "").strip())
    norm_content = re.sub(r"\s+", " ", (content or "").strip())
    raw = f"content:{project_id or ''}:{category}:{norm_title}:{norm_content}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_content_tombstone(conn, project_id: str | None, category: str, title: str, content: str) -> bool:
    raw_hash = compute_content_hash(project_id, category, title, content)
    norm_hash = compute_normalized_content_hash(project_id, category, title, content)
    row = conn.execute(
        "SELECT 1 FROM tombstones WHERE entity_type IN ('content', 'memory', 'document') AND dedupe_key IN (?, ?)",
        (raw_hash, norm_hash),
    ).fetchone()
    return row is not None


def queue_distillation(conversation_id: str, *, _connection=None,
                       event_key: str | None = None,
                       event_payload_hash: str | None = None) -> dict[str, Any]:
    from contextlib import nullcontext
    from app.services import processing_jobs as jobs
    with nullcontext(_connection) if _connection is not None else db() as conn:
        if _connection is None:
            conn.execute("BEGIN IMMEDIATE")
        jobs.initialize_processing_jobs(conn)
        conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
        job = jobs.register_job(conn, conv, new_id("dist"), event_key=event_key,
                                event_payload_hash=event_payload_hash)
        row = conn.execute("SELECT * FROM distillation_queue WHERE id=?", (job["id"],)).fetchone()
        if row is None:
            now = now_iso()
            conn.execute("""INSERT INTO distillation_queue
                (id,conversation_id,status,payload,created_at,updated_at)
                VALUES(?,?,'pending',?,?,?)""",
                (job["id"],conversation_id,encode({"content_hash":conv["content_hash"],
                 "agent":conv["agent"],"title":conv["title"]}),now,now))
            log_audit(actor="daemon",action="distillation.queue",target=job["id"],conn=conn)
        elif job["state"] == "pending" and not job["invoked"]:
            conn.execute("UPDATE distillation_queue SET payload=?,updated_at=? WHERE id=?",
                (encode({"content_hash":conv["content_hash"],"agent":conv["agent"],"title":conv["title"]}),now_iso(),job["id"]))
        if job["state"] in ("pending", "running"):
            conn.execute("UPDATE conversations SET processing_status='pending',processing_error=NULL WHERE id=?", (conversation_id,))
        return row_to_dict(conn.execute("SELECT * FROM distillation_queue WHERE id=?",(job["id"],)).fetchone())


def get_pending_distillations(limit: int = 5) -> list[dict[str, Any]]:
    from app.services import processing_jobs as jobs
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.initialize_processing_jobs(conn)
        jobs.recover_processing_jobs(conn)
        rows = conn.execute("""SELECT dq.id,j.updated_at AS job_updated_at
            FROM distillation_queue dq JOIN conversations c ON dq.conversation_id=c.id
            JOIN processing_jobs j ON j.id=dq.id
            JOIN connections source ON source.id=c.agent
            WHERE j.state='pending' AND j.invoked=0 AND source.enabled=1 AND source.paused=0
            ORDER BY dq.created_at LIMIT ?""", (max(limit, 1) * 4,)).fetchall()
        return [row_to_dict(r) for r in rows]


def _record_conflict(
    conn,
    project_id: str | None,
    existing_id: str | None,
    title: str,
    content: str,
    category: str,
    tags: list[str],
    quote: str | None,
    reason: str,
    actor: str,
    now: str,
) -> str | None:
    norm_content = _normalized_claim(content)
    if existing_id:
        pending_rows = conn.execute(
            "SELECT id, proposed FROM conflicts WHERE existing_id = ? AND status = 'pending'",
            (existing_id,),
        ).fetchall()
        for pending_row in pending_rows:
            prop = decode(pending_row["proposed"], {})
            if _normalized_claim(str(prop.get("content") or "")) == norm_content:
                return pending_row["id"]
        resolved_confs = conn.execute(
            "SELECT proposed FROM conflicts WHERE existing_id = ? AND status = 'resolved'",
            (existing_id,),
        ).fetchall()
        if any(
            _normalized_claim(str(decode(rc["proposed"], {}).get("content") or "")) == norm_content
            for rc in resolved_confs
        ):
            return None

    conf_id = new_id("conf")
    conn.execute(
        """
        INSERT INTO conflicts (id, project_id, existing_id, proposed, reason, status, resolution, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?)
        """,
        (
            conf_id,
            project_id,
            existing_id,
            encode({"title": title, "content": content, "category": category, "tags": tags, "quote": quote}),
            reason,
            now,
        ),
    )
    log_audit(actor=actor, action="conflict.create", target=conf_id, conn=conn)
    return conf_id


def _unconfirmed_user_evidence(text: str) -> bool:
    return bool(re.search(
        r"(?:尚未|还未|未曾|没有|不曾|并未).{0,12}(?:确认|决定|同意)|"
        r"(?:建议|假设|如果|待确认|仅供参考|引用|转述)|^\s*>|"
        r"\b(?:if|suggest|suggestion|propose|proposal|hypothetical|unconfirmed|quoted)\b|"
        r"\b(?:not|never|haven't|hasn't|don't|do not)\s+(?:yet\s+)?(?:confirmed|decided|approved)",
        text, re.IGNORECASE,
    ))


_DECISION_NEGATION_RE = re.compile(
    r"(?:不要|不能|不应|不再|禁止|切勿|严禁|(?<![级特识区类分判鉴派性国个告辞离差甄辨])别(?![人名墅致针扭])"
    r"|\bnever\s+|\bdo\s+not\s+|\bdon't\s+|\bmust\s+not\s+|\bnot\s+(?:use|adopt)\b)",
    re.IGNORECASE,
)


def _has_preceding_negation(quote_span: str, ev_msg_text: str) -> bool:
    if not quote_span or not ev_msg_text or quote_span not in ev_msg_text:
        return False
    start = 0
    while (pos := ev_msg_text.find(quote_span, start)) != -1:
        raw_prefix = ev_msg_text[max(0, pos - 12) : pos]
        # Do not bleed across hard sentence boundaries.
        clause_prefix = re.split(r"[。！？!?\n；;]", raw_prefix)[-1]
        window = clause_prefix + quote_span
        if _DECISION_NEGATION_RE.search(window):
            return True
        start = pos + 1
    return False


def _unconfirmed_decision_evidence(text: str, ev_msg_text: str = "") -> bool:
    if _unconfirmed_user_evidence(text) or bool(_DECISION_NEGATION_RE.search(text)):
        return True
    if ev_msg_text:
        if _unconfirmed_user_evidence(ev_msg_text):
            return True
        if text and _has_preceding_negation(text, ev_msg_text):
            return True
    return False


_BARE_ACKNOWLEDGEMENTS = {"好的", "好", "ok", "okay", "yes", "继续", "行", "可以", "嗯", "收到", "没问题"}
_REJECTED_SUMMARY_STANCES = {"suggestion", "assumption", "negation", "quote"}


def _normalized_claim(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold()).strip(".,!?;:。！？；：")


def _claim_supported_by_quote(claim_text: str, quote_text: str) -> bool:
    claim = _normalized_claim(claim_text)
    quote_norm = _normalized_claim(quote_text)
    if not claim or not quote_norm:
        return False
    if quote_norm in _BARE_ACKNOWLEDGEMENTS or len(quote_norm) < 2:
        return claim == quote_norm
    if len(claim) >= 4 and claim in quote_norm:
        return True
    if quote_norm in claim and len(quote_norm) >= 8 and len(quote_norm) >= len(claim) * 0.5:
        return True
    return False


def apply_distilled_memories(
    conn,
    conv: dict[str, Any],
    memories: list[dict[str, Any]],
    actor: str,
    now: str,
) -> list[str]:
    """Atomically validate and apply distilled memories into contents, versions, and conflicts.

    Rules enforced:
    1. Explicit confirmed supersession:
       - Nested 'supersedes' object with valid target id, integer expected_version,
         and confidence == 'explicit_confirmed'.
       - Target content must exist in same project, be status 'active', and version == expected_version.
       - Evidence quote must be non-empty substring of a referenced USER message.
       - Atomically retires old decision (status -> 'historical') and creates new decision
         (status -> 'active', version 1, related_ids = [old_id]).
    2. Ambiguous replacement:
       - Confidence == 'ambiguous', stale version, cross-project target, unconfirmed user evidence,
         or matching active title without explicit supersedes object routes to conflicts table
         (status 'pending'), NEVER silent duplicate or silent retirement.
    3. Assistant suggestions without explicit user confirmation:
       - Cannot supersede active decisions or become active decisions autonomously.
    4. Tombstone nonresurrection:
       - Deleted memories with tombstoned content hashes are rejected.
    """
    project_id = conv["project_id"]
    conv_id = conv["id"]
    conv_title = conv.get("title") or ""
    raw_messages = decode(conv["messages"], []) if isinstance(conv.get("messages"), str) else (conv.get("messages") or [])
    created_ids: list[str] = []

    for m in memories:
        if not isinstance(m, dict):
            continue
        # Every entry point (model queue, structured submission and automatic
        # checkpoints) uses the same stance and provenance boundary.
        m = dict(m)
        stance = str(m.get("stance") or "").strip().lower()
        tags = m.get("tags") if isinstance(m.get("tags"), list) else []
        tags = [tag for tag in tags if isinstance(tag, str) and not tag.strip().casefold().startswith(
            ("role:", "message_index:", "source_version:", "stance:")
        )]
        if stance in MEMORY_STANCES:
            tags.append(f"stance:{stance}")
            if stance in NON_DECISION_STANCES and (
                stance != "fact" or m.get("category") in {"decision", "preference"}
            ):
                m["category"] = "knowledge"
            elif stance in {"preference", "decision"}:
                m["category"] = stance
        if conv_id:
            tags.append(f"source_version:{conv.get('content_hash') or ''}")
        source_index = m.get("source_message_index")
        user_index = m.get("user_message_index")
        if source_index is not None and user_index is not None and (
            type(user_index) is not int or source_index != user_index
        ):
            # Both fields describe the same supporting source turn, not an
            # assistant proposal plus an unrelated user's alleged approval.
            continue
        index = m.get("source_message_index")
        if index is None:
            index = m.get("user_message_index")
        quote = m.get("quote")
        if index is None and isinstance(quote, str) and quote.strip():
            matches = [
                i for i, msg in enumerate(raw_messages)
                if isinstance(msg, dict) and isinstance(msg.get("content"), str)
                and quote in msg["content"]
            ]
            if len(matches) == 1:
                index = matches[0]
            else:
                continue
        if index is not None:
            if type(index) is not int or not 0 <= index < len(raw_messages):
                continue
            message = raw_messages[index]
            if not isinstance(message, dict):
                continue
            if (
                not isinstance(quote, str) or not quote.strip()
                or not isinstance(message.get("content"), str) or quote not in message["content"]
            ):
                continue
            tags.extend([f"role:{message.get('role') or 'unknown'}", f"message_index:{index}"])
        m["tags"] = list(dict.fromkeys(tags))
        cat = m.get("category", "knowledge")
        if cat not in VALID_CATEGORIES:
            continue
        m_title = (m.get("title") or "").strip()
        m_content = (m.get("content") or "").strip()
        if not m_title or not m_content:
            continue

        c_hash = compute_content_hash(project_id, cat, m_title, m_content)
        # Tombstones check across 'content', 'memory', and 'document' (raw and normalized whitespace)
        if _has_content_tombstone(conn, project_id, cat, m_title, m_content):
            continue

        # Extract quote and user message index
        quote = m.get("quote")
        user_msg_idx = m.get("user_message_index")
        if user_msg_idx is None:
            user_msg_idx = index

        # Validate user message index and user role
        is_valid_user_idx = (
            isinstance(user_msg_idx, int)
            and not isinstance(user_msg_idx, bool)
            and 0 <= user_msg_idx < len(raw_messages)
        )

        user_msg_text = ""
        user_confirmed = False

        if is_valid_user_idx:
            ref_msg = raw_messages[user_msg_idx]
            if isinstance(ref_msg, dict) and ref_msg.get("role") == "user":
                user_msg_text = ref_msg.get("content", "")
                if isinstance(quote, str) and quote.strip() and (quote in user_msg_text):
                    user_confirmed = True
        elif m.get("user_confirmed") is True and not quote and raw_messages:
            for rev_idx in range(len(raw_messages) - 1, -1, -1):
                cand_msg = raw_messages[rev_idx]
                if isinstance(cand_msg, dict) and cand_msg.get("role") == "user":
                    cand_text = str(cand_msg.get("content") or "").strip()
                    cand_rejected = (
                        _unconfirmed_decision_evidence(cand_text)
                        if (cat == "decision" or isinstance(m.get("supersedes"), dict))
                        else _unconfirmed_user_evidence(cand_text)
                    )
                    if cand_text and _claim_supported_by_quote(m_content, cand_text) and not cand_rejected:
                        user_msg_idx = rev_idx
                        user_msg_text = cand_text
                        quote = cand_text
                        user_confirmed = True
                        break
        if user_confirmed:
            user_confirmed = _claim_supported_by_quote(m_content, str(quote or ""))
        elif not conv_id and m.get("user_confirmed") is True and stance not in NON_DECISION_STANCES:
            user_confirmed = not (
                _unconfirmed_decision_evidence(m_content)
                if (cat == "decision" or isinstance(m.get("supersedes"), dict))
                else _unconfirmed_user_evidence(m_content)
            )

        if cat == "preference" and (conv_id or quote or index is not None):
            if (
                not user_confirmed
                or _unconfirmed_user_evidence(user_msg_text)
                or (
                    _has_preceding_negation(str(quote or ""), user_msg_text)
                    and not _DECISION_NEGATION_RE.search(m_content)
                )
            ):
                continue
        if user_confirmed:
            if _unconfirmed_user_evidence(user_msg_text) or _unconfirmed_user_evidence(str(quote or "")):
                user_confirmed = False
            elif cat == "decision" or isinstance(m.get("supersedes"), dict):
                if _unconfirmed_decision_evidence(str(quote or ""), user_msg_text):
                    user_confirmed = False
            elif _has_preceding_negation(str(quote or ""), user_msg_text) and not _DECISION_NEGATION_RE.search(m_content):
                user_confirmed = False

        supersedes = m.get("supersedes")

        if isinstance(supersedes, dict):
            target_id = supersedes.get("id")
            expected_ver = supersedes.get("expected_version")
            evidence_quote = supersedes.get("evidence_quote")
            confidence = supersedes.get("confidence")
            reason = supersedes.get("reason") or "Superseded by user decision"

            is_valid_version = isinstance(expected_ver, int) and not isinstance(expected_ver, bool)
            target = conn.execute("SELECT * FROM contents WHERE id = ?", (target_id,)).fetchone() if target_id else None

            has_valid_evidence_quote = (
                isinstance(evidence_quote, str)
                and bool(evidence_quote.strip())
                and bool(user_msg_text)
                and (evidence_quote in user_msg_text)
                and _claim_supported_by_quote(m_content, evidence_quote)
                and not _unconfirmed_decision_evidence(evidence_quote, user_msg_text)
            )

            is_explicit_confirmed = (
                confidence == "explicit_confirmed"
                and user_confirmed
                and has_valid_evidence_quote
                and target is not None
                and cat == "decision"
                and target["kind"] == "memory"
                and target["category"] == "decision"
                and target["project_id"] == project_id
                and target["status"] == "active"
                and is_valid_version
                and target["version"] == expected_ver
            )

            if is_explicit_confirmed:
                # Atomically retire old active decision
                cursor = conn.execute(
                    "UPDATE contents SET status = 'historical', updated_at = ? WHERE id = ? AND version = ? AND status = 'active'",
                    (now, target_id, expected_ver),
                )
                if cursor.rowcount == 0:
                    # Concurrent change detected; route to conflict
                    target_now = conn.execute("SELECT version, status FROM contents WHERE id = ?", (target_id,)).fetchone()
                    race_reason = (
                        f"Supersession race condition: target version is {target_now['version']} "
                        f"(expected {expected_ver}, status {target_now['status']})"
                        if target_now else "Supersession target deleted concurrently"
                    )
                    _record_conflict(conn, project_id, target_id, m_title, m_content, cat, m.get("tags") or [], quote, race_reason, actor, now)
                    continue

                log_audit(actor=actor, action="content.supersede", target=target_id, conn=conn)

                # Insert new active decision with provenance link
                mid = new_id("cnt")
                tags = m.get("tags") or []
                conn.execute(
                    """
                    INSERT INTO contents (
                        id, project_id, kind, category, title, content, tags, agent,
                        status, version, related_ids, content_hash, created_at, updated_at
                    ) VALUES (?, ?, 'memory', ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?)
                    """,
                    (mid, project_id, cat, m_title, m_content, encode(tags), actor, encode([target_id]), c_hash, now, now),
                )
                conn.execute(
                    """
                    INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
                    VALUES (?, ?, 1, ?, ?, ?, ?)
                    """,
                    (new_id("ver"), mid, m_title, m_content, actor, now),
                )
                conn.execute(
                    """
                    INSERT INTO content_sources (
                        id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at
                    ) VALUES (?, ?, ?, 'conversation', ?, ?, 0, ?, ?)
                    """,
                    (new_id("src"), mid, conv_id, conv_title, conv.get("agent") or actor, quote, now),
                )
                log_audit(actor=actor, action="content.create_distill", target=mid, conn=conn)
                created_ids.append(mid)
                continue

            else:
                # Ambiguous replacement -> MUST become a conflict when an active memory exists in project,
                # NOT a silent duplicate or silent retire. If supersedes.id points to a non-existent or
                # cross-project memory and conflict_existing_id is None, drop the invalid supersedes
                # reference and fall through to normal candidate handling instead of inserting NULL existing_id.
                conflict_existing_id = target["id"] if (target and target["project_id"] == project_id) else None
                if not conflict_existing_id:
                    same_title_row = conn.execute(
                        "SELECT id FROM contents WHERE project_id IS ? AND category = ? AND title = ? AND status = 'active'",
                        (project_id, cat, m_title),
                    ).fetchone()
                    if same_title_row:
                        conflict_existing_id = same_title_row["id"]

                if conflict_existing_id is not None:
                    if target and target["project_id"] != project_id:
                        conflict_reason = f"Supersession target '{target_id}' belongs to another project"
                    elif target and not is_valid_version:
                        conflict_reason = f"Supersession target expected_version must be integer, got {type(expected_ver).__name__}"
                    elif target and target["version"] != expected_ver:
                        conflict_reason = f"Stale supersession target version {expected_ver} (current: {target['version']})"
                    elif target and target["status"] != "active":
                        conflict_reason = f"Supersession target '{target_id}' is not active (status: {target['status']})"
                    elif not target:
                        conflict_reason = f"Supersession target '{target_id}' not found in project"
                    elif confidence == "ambiguous":
                        conflict_reason = f"Ambiguous supersession: confidence '{confidence}' ({reason})"
                    elif not user_confirmed or not has_valid_evidence_quote:
                        conflict_reason = "Supersession lacks explicit confirmed user evidence quote"
                    else:
                        conflict_reason = f"Ambiguous supersession: {reason}"

                    _record_conflict(conn, project_id, conflict_existing_id, m_title, m_content, cat, m.get("tags") or [], quote, conflict_reason, actor, now)
                    continue
                supersedes = None

        # supersedes is absent
        if cat == "decision":
            if not user_confirmed:
                # Assistant-only suggested decision cannot have user authority
                existing_active = conn.execute(
                    "SELECT id, content FROM contents WHERE project_id IS ? AND category = 'decision' AND title = ? AND status = 'active'",
                    (project_id, m_title),
                ).fetchone()
                if existing_active and existing_active["content"] != m_content:
                    conflict_reason = f"Assistant-suggested decision on '{m_title}' lacks explicit user confirmation"
                    _record_conflict(conn, project_id, existing_active["id"], m_title, m_content, cat, m.get("tags") or [], quote, conflict_reason, actor, now)
                # Unconfirmed assistant suggestion without active conflict is discarded per PRD
                continue

            # Confirmed user decision without explicit supersedes object
            existing_mem = conn.execute(
                "SELECT id, content FROM contents WHERE project_id IS ? AND category = 'decision' AND title = ? AND status = 'active'",
                (project_id, m_title),
            ).fetchone()
            if existing_mem:
                if existing_mem["content"] == m_content:
                    continue
                # Same title with a different body is not an explicit supersedes object.
                # Phrase overlap is not enough to retire the active decision.
                _record_conflict(
                    conn, project_id, existing_mem["id"], m_title, m_content, cat,
                    m.get("tags") or [], quote,
                    "Same-title decision matches an active memory without an explicit supersedes object",
                    actor, now,
                )
                continue

        else:
            # Non-decision categories
            existing_mem = conn.execute(
                "SELECT id, content FROM contents WHERE project_id IS ? AND category = ? AND title = ? AND status = 'active'",
                (project_id, cat, m_title),
            ).fetchone()
            if existing_mem:
                if existing_mem["content"] == m_content:
                    continue
                if user_confirmed:
                    created_ids.append(_retire_and_replace(
                        conn, project_id, existing_mem["id"], cat, m_title, m_content,
                        m.get("tags") or [], actor, quote, conv_id, conv_title, conv.get("agent") or actor, now,
                    ))
                    continue
                conflict_reason = f"Distilled memory conflicts with active memory '{m_title}'"
                _record_conflict(conn, project_id, existing_mem["id"], m_title, m_content, cat, m.get("tags") or [], quote, conflict_reason, actor, now)
                continue

        # Ordinary new memory insertion
        mid = new_id("cnt")
        tags = m.get("tags") or []
        conn.execute(
            """
            INSERT INTO contents (
                id, project_id, kind, category, title, content, tags, agent,
                status, version, related_ids, content_hash, created_at, updated_at
            ) VALUES (?, ?, 'memory', ?, ?, ?, ?, ?, 'active', 1, '[]', ?, ?, ?)
            """,
            (mid, project_id, cat, m_title, m_content, encode(tags), actor, c_hash, now, now),
        )
        conn.execute(
            """
            INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
            VALUES (?, ?, 1, ?, ?, ?, ?)
            """,
            (new_id("ver"), mid, m_title, m_content, actor, now),
        )
        if conv_id:
            conn.execute(
                """
                INSERT INTO content_sources (
                    id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at
                ) VALUES (?, ?, ?, 'conversation', ?, ?, 0, ?, ?)
                """,
                (new_id("src"), mid, conv_id, conv_title, conv.get("agent") or actor, quote, now),
            )
        log_audit(actor=actor, action="content.create_distill", target=mid, conn=conn)
        created_ids.append(mid)

    return created_ids


def _distillation_access(conn, conv) -> tuple[str | None, str]:
    source = conn.execute("SELECT enabled,paused FROM connections WHERE id=?", (conv["agent"],)).fetchone()
    if not source or not source["enabled"] or source["paused"]:
        return f"Connection '{conv['agent']}' is not enabled or is paused", "error"
    authorized = bool(get_app_setting("compute_authorized", False, conn=conn))
    host = get_app_setting("compute_host", None, conn=conn)
    compute = conn.execute("SELECT enabled,paused FROM connections WHERE id=?", (host,)).fetchone() if host else None
    if not authorized:
        return "Compute execution not authorized; distillation pending explicit user grant", "pending"
    if host != "omp":
        return f"Unsupported compute host '{host}'; only 'omp' is supported", "pending"
    if not compute or not compute["enabled"] or compute["paused"]:
        return f"Compute host '{host}' connection is not enabled or is paused; distillation pending", "pending"
    return None, "pending"


def _record_processing_state(conn, queue_id, conv_id, status, error):
    conn.execute("UPDATE distillation_queue SET status=?,error=?,updated_at=? WHERE id=?", (status,error,now_iso(),queue_id))
    conn.execute("UPDATE conversations SET processing_status=?,processing_error=? WHERE id=?",
                 ('error' if status == 'error' else 'pending',error,conv_id))


def _record_worker_failure(queue_id, conv_id, owner, *, send_started, result_received, kind):
    from app.services import processing_jobs as jobs

    state = "unknown" if send_started and not result_received else "failed"
    error = "outcome_unknown" if state == "unknown" else (
        "programmer_error" if kind == "implementation" else "compute_failed"
    )
    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not send_started:
            conn.execute(
                "UPDATE processing_jobs SET invoked=0 WHERE id=? AND owner=? AND state='running'",
                (queue_id, owner),
            )
        if jobs.finish_job(conn, queue_id, state, error_code=error, owner=owner):
            _record_processing_state(conn, queue_id, conv_id, "error", error)
    return state, error


async def process_distillation_queue_item(queue_id: str) -> dict[str, Any] | None:
    from app import transport
    from app.services import processing_jobs as jobs
    from app.services.conversation_service import compute_source_identity_key
    import asyncio

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        jobs.initialize_processing_jobs(conn)
        jobs.recover_processing_jobs(conn)
        q_row = conn.execute("""SELECT dq.* FROM distillation_queue dq
            JOIN processing_jobs j ON j.id=dq.id
            WHERE dq.id=? AND j.state='pending' AND j.invoked=0""", (queue_id,)).fetchone()
        if not q_row:
            return None
        conv_id = q_row["conversation_id"]
        conv = conn.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        if not conv:
            return None
        source_key = compute_source_identity_key(conv['agent'],conv['source_path'],conv['title'],conv['external_id'])
        if conn.execute("SELECT id FROM tombstones WHERE entity_type='conversation' AND dedupe_key=?",(source_key,)).fetchone():
            jobs.finish_job(conn,queue_id,'cancelled',error_code='source_tombstoned')
            return {"ok":False,"error":"Source tombstoned"}
        error, status = _distillation_access(conn,conv)
        if error:
            _record_processing_state(conn,queue_id,conv_id,status,error)
            return {"ok":False,"status":status,"error":error}
        job = jobs.claim_job(conn,queue_id)
        if job is None:
            return None
        conn.execute("UPDATE distillation_queue SET status='processing',error=NULL,updated_at=? WHERE id=?", (now_iso(),queue_id))
        conv = dict(conv)
        active_decisions = [dict(r) for r in conn.execute("""SELECT id,project_id,category,title,content,version,content_hash,updated_at
            FROM contents WHERE project_id IS ? AND category='decision' AND status='active' ORDER BY updated_at DESC""",(conv['project_id'],)).fetchall()]

    payload = {
        "conversation_id":conv_id,"project_id":conv['project_id'],"agent":conv['agent'],
        "title":conv['title'],"process_summary":conv['process_summary'] or '',"cwd":conv['cwd'],
        "messages":[{"index":i,"role":m.get('role') or 'unknown',"content":m.get('content','')}
                    for i,m in enumerate(decode(conv['messages'],[]))],
        "existing_active_decisions":active_decisions,
    }
    # Commit an invoked marker before leaving SQLite. A crash from this point
    # may have consumed model work; recovery must inquire, never re-send.
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        current = conn.execute('SELECT * FROM conversations WHERE id=?',(conv_id,)).fetchone()
        if (not current or not jobs.source_is_current(conn,job,current)
                or not jobs.authorization_is_current(conn,job)
                or not jobs.targets_are_current(conn,job)):
            jobs.finish_job(conn,queue_id,'cancelled',error_code='snapshot_changed_before_invoke',owner=job['owner'])
            return {'ok':False,'status':'error','error':'Snapshot changed before compute'}
        if not jobs.mark_invoked(conn,queue_id,job['owner']):
            return {'ok':False,'status':'unknown','error':'Processing lease lost before compute'}
    try:
        result = await transport.run_distill('omp',payload,allow_execution=True)
    except transport.DistillExecutionError as exc:
        state, error = _record_worker_failure(
            queue_id, conv_id, job["owner"], send_started=exc.send_started,
            result_received=exc.result_received, kind=exc.kind,
        )
        if exc.kind == "implementation":
            raise
        return {"ok": False, "status": "unknown" if state == "unknown" else "error", "error": error}
    except asyncio.CancelledError as exc:
        # Cancellation must propagate. Without transport-boundary facts it may
        # have interrupted a live child, so preserve the no-replay fence.
        _record_worker_failure(
            queue_id, conv_id, job["owner"], send_started=getattr(exc, "send_started", True),
            result_received=getattr(exc, "result_received", False), kind="cancellation",
        )
        raise
    except Exception:
        _record_worker_failure(
            queue_id, conv_id, job["owner"], send_started=False,
            result_received=False, kind="implementation",
        )
        raise

    now = now_iso()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        if result.status in ('failed','success') or result.usage or result.raw_output:
            tokens = result.usage.get('tokens') if result.usage else None
            conn.execute("INSERT INTO usage_meters(id,host_id,tokens,requests,note,created_at) VALUES(?,?,?,1,?,?)",
                         (new_id('use'),'omp',tokens,f'Distill {result.status} for conversation {conv_id}',now))
        ledger = conn.execute('SELECT * FROM processing_jobs WHERE id=?',(queue_id,)).fetchone()
        # Expired/lost ownership is never allowed to commit, even if a late
        # provider reply eventually arrives after recovery.
        if not ledger or ledger['state'] != 'running' or ledger['owner'] != job['owner'] or ledger['lease_until'] <= now:
            if ledger and ledger['state'] == 'running' and ledger['owner'] == job['owner']:
                jobs.finish_job(conn,queue_id,'unknown',error_code='lease_expired_after_invoke',owner=job['owner'])
            return {'ok':False,'status':'unknown','error':'Processing lease lost during compute'}
        if result.status != 'success':
            error = result.error or f'Distillation status: {result.status}'
            if result.status == 'pending':
                # Transport contract: pending is a deterministic pre-execution
                # refusal. No model was started, so this is safe to reconsider.
                conn.execute('UPDATE processing_jobs SET invoked=0 WHERE id=?',(queue_id,))
                jobs.finish_job(conn,queue_id,'pending',error_code=error,owner=job['owner'])
                status = 'pending'
            else:
                jobs.finish_job(conn,queue_id,'failed',error_code='compute_failed',owner=job['owner'])
                status = 'error'
            _record_processing_state(conn,queue_id,conv_id,'pending' if status == 'pending' else 'error',error)
            return {'ok':False,'status':status,'error':error}
        current = conn.execute('SELECT * FROM conversations WHERE id=?',(conv_id,)).fetchone()
        if not current:
            jobs.finish_job(conn,queue_id,'cancelled',error_code='source_deleted',owner=job['owner'])
            return {'ok':False,'status':'error','error':'Conversation deleted during compute'}
        if not jobs.source_is_current(conn,job,current):
            jobs.finish_job(conn,queue_id,'cancelled',error_code='source_revision_changed',owner=job['owner'])
            return {'ok':False,'status':'pending','error':'Stale result discarded: conversation modified during compute'}
        error, status = _distillation_access(conn,current)
        if error:
            jobs.finish_job(conn,queue_id,'cancelled',error_code='authorization_changed',owner=job['owner'])
            _record_processing_state(conn,queue_id,conv_id,status,error)
            return {'ok':False,'status':status,'error':error}
        if (not jobs.authorization_is_current(conn,job)
                or not jobs.targets_are_current(conn,job,result.memories)):
            jobs.finish_job(conn,queue_id,'cancelled',error_code='governance_or_policy_changed',owner=job['owner'])
            return {'ok':False,'status':'error','error':'Governance or authorization changed during compute; stale result discarded'}
        # All validation and the memory writes share this SQLite write lock.
        created_ids = apply_distilled_memories(conn=conn,conv=dict(current),memories=result.memories,actor=conv['agent'],now=now)
        conn.execute("UPDATE conversations SET processing_status='processed',processing_error=NULL WHERE id=?",(conv_id,))
        conn.execute("UPDATE distillation_queue SET status='completed',result=?,error=NULL,updated_at=? WHERE id=?",(encode(created_ids),now,queue_id))
        jobs.finish_job(conn,queue_id,'succeeded',receipt=encode({'created_ids':created_ids}),owner=job['owner'])
        log_audit(actor='daemon',action='distillation.complete',target=queue_id,conn=conn)
    return {'ok':True,'conversation_id':conv_id,'created_count':len(created_ids),'created_ids':created_ids}


async def process_pending_distillations(limit: int = 5, *, min_idle_seconds: float = 0) -> int:
    from datetime import datetime, timezone
    pending = get_pending_distillations(limit)
    if min_idle_seconds > 0:
        instant = datetime.fromisoformat(now_iso())
        def ready(item):
            queued = datetime.fromisoformat(item['job_updated_at'])
            if queued.tzinfo is None:
                queued = queued.replace(tzinfo=timezone.utc)
            return (instant - queued).total_seconds() >= min_idle_seconds
        pending = [item for item in pending if ready(item)]
    count = 0
    for item in pending[:limit]:
        res = await process_distillation_queue_item(item["id"])
        if res and res.get("ok"):
            count += 1
    return count


def submit_structured_distillation(
    conversation_id: str,
    memories: list[dict[str, Any]],
    expected_content_hash: str | None = None,
    actor: str = "host_agent",
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from app.services.conversation_service import compute_source_identity_key

    for m in memories:
        title = m.get("title", "").strip() if isinstance(m, dict) else ""
        content = m.get("content", "").strip() if isinstance(m, dict) else ""
        category = m.get("category", "") if isinstance(m, dict) else ""

        if not title:
            raise HTTPException(status_code=422, detail="Distilled memory item missing required 'title'")
        if not content:
            raise HTTPException(status_code=422, detail="Distilled memory item missing required 'content'")
        if category not in VALID_CATEGORIES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid memory category '{category}'. Allowed: {', '.join(sorted(VALID_CATEGORIES))}",
            )

    now = now_iso()

    with db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        from app.services import processing_jobs as jobs
        jobs.initialize_processing_jobs(conn)
        conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found or deleted")

        if expected_content_hash and conv["content_hash"] != expected_content_hash:
            raise HTTPException(
                status_code=409,
                detail="Conversation content changed during distillation; stale result discarded",
            )

        source_key = compute_source_identity_key(
            conv["agent"], conv["source_path"], conv["title"], conv["external_id"]
        )
        if conn.execute("SELECT id FROM tombstones WHERE entity_type = 'conversation' AND dedupe_key = ?", (source_key,)).fetchone():
            raise HTTPException(status_code=410, detail="Conversation source tombstoned; distillation rejected")

        conn_row = conn.execute("SELECT enabled, paused FROM connections WHERE id = ?", (conv["agent"],)).fetchone()
        if not conn_row or not conn_row["enabled"] or conn_row["paused"]:
            raise HTTPException(
                status_code=403,
                detail=f"Connection '{conv['agent']}' is not enabled or is paused; distillation rejected",
            )

        created_ids = apply_distilled_memories(
            conn=conn,
            conv=dict(conv),
            memories=memories,
            actor=actor,
            now=now,
        )

        if usage:
            uid = new_id("use")
            toks = usage.get("tokens") or usage.get("output_tokens")
            conn.execute(
                """
                INSERT INTO usage_meters (id, host_id, tokens, requests, note, created_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (uid, conv["agent"], toks, f"Manual distillation for conversation {conversation_id}", now),
            )

        conn.execute(
            "UPDATE conversations SET processing_status = 'processed', processing_error = NULL WHERE id = ?",
            (conversation_id,),
        )

        conn.execute(
            """
            UPDATE distillation_queue SET
                status = 'completed',
                result = ?,
                error = NULL,
                updated_at = ?
            WHERE conversation_id = ?
            """,
            (encode(created_ids), now, conversation_id),
        )

        for queued in conn.execute("SELECT id FROM processing_jobs WHERE conversation_id=? AND state IN ('pending','running')", (conversation_id,)).fetchall():
            jobs.finish_job(conn,queued['id'],'succeeded',receipt=encode({'created_ids':created_ids}))

    return {
        "ok": True,
        "conversation_id": conversation_id,
        "created_count": len(created_ids),
        "created_ids": created_ids,
    }


_IMPLICIT_SIGNAL_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    (
        "preference",
        "用户偏好与规范",
        re.compile(
            r"(?:以后都|默认使用|我习惯|我喜欢|不要使用|禁止使用|不要用|请始终|始终使用|统一使用|代码风格|偏好[:：]|记住[:：]|always use|never use|prefer to|do not use|must use)",
            re.IGNORECASE,
        ),
    ),
    (
        "decision",
        "技术与架构决策",
        re.compile(
            r"(?:决定采用|架构决策|技术选型|确定使用|方案定为|契约规定|统一采用|最终决定|决策[:：]|改用|切换为|we decided to|architecture decision|chosen approach)",
            re.IGNORECASE,
        ),
    ),
    (
        "lesson",
        "踩坑经验与教训",
        re.compile(
            r"(?:踩坑|根本原因|原因是|根因是|教训[:：]|避坑|注意不要|排查发现|修复了.*问题|导致.*报错|root cause|pitfall|lesson learned|bug was caused by)",
            re.IGNORECASE,
        ),
    ),
    (
        "background",
        "项目背景与上下文",
        re.compile(
            r"(?:项目背景|本项目是|技术栈是|运行环境是|目录结构是|背景[:：]|服务端口|部署在|project background|tech stack is)",
            re.IGNORECASE,
        ),
    ),
    (
        "background",
        "任务进展与阶段背景",
        re.compile(
            r"(?:下一步需要|待办[:：]|TODO[:：]|已完成[:：]|正在推进|任务目标[:：]|接下来要|next step is|milestone completed)",
            re.IGNORECASE,
        ),
    ),
]


def extract_implicit_memories_from_messages(
    messages: list[dict[str, Any]],
    *,
    existing_summary: str = "",
) -> dict[str, Any]:
    """Extract companion implicit memories and a concise session summary from dialogue turns."""
    extracted: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    user_points: list[str] = []
    assistant_points: list[str] = []

    for message_index, msg in enumerate(messages or []):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "unknown").strip().lower()
        text = str(msg.get("content") or "").strip()
        if not text:
            continue

        first_line = re.sub(r"\s+", " ", text.splitlines()[0]).strip()[:140]
        if role == "user" and first_line:
            user_points.append(first_line)
        elif role == "assistant" and first_line:
            assistant_points.append(first_line)

        # Split into sentences/lines for signal detection
        segments = [seg.strip() for seg in re.split(r"[\n。！？!?；;]+", text) if seg.strip()]
        for seg in segments:
            if len(seg) < 6 or len(seg) > 500:
                continue
            for category, label_prefix, pattern in _IMPLICIT_SIGNAL_PATTERNS:
                if pattern.search(seg):
                    stance = category if category in {"decision", "preference"} else "fact"
                    if re.search(r"引用|转述|^\s*>|\bquoted?\b", text, re.IGNORECASE):
                        stance = "quote"
                    elif re.search(r"假设|如果|\b(?:if|assum\w*|hypothetical)\b", text, re.IGNORECASE):
                        stance = "assumption"
                    elif re.search(r"建议|仅供参考|\b(?:suggest\w*|propos\w*)\b", text, re.IGNORECASE):
                        stance = "suggestion"
                    elif category == "decision" and _unconfirmed_decision_evidence(text):
                        stance = "negation"
                    if category in {"decision", "preference"}:
                        unconfirmed = (_unconfirmed_decision_evidence(text) if category == "decision"
                                       else _unconfirmed_user_evidence(text))
                        if (role != "user" or unconfirmed) and stance in {"decision", "preference"}:
                            stance = "quote"
                    if stance in NON_DECISION_STANCES and stance != "fact":
                        category = "knowledge"
                    clean_seg = re.sub(r"\s+", " ", seg).strip()
                    short_title = clean_seg[:48].rstrip("，,:： ")
                    title = f"{label_prefix}：{short_title}"
                    dedupe = (category, short_title.lower())
                    if dedupe not in seen_keys:
                        seen_keys.add(dedupe)
                        tags = ["implicit_auto", f"role:{role}"]
                        extracted.append(
                            {
                                "category": category,
                                "stance": stance,
                                "title": title[:120],
                                "content": clean_seg,
                                "tags": [*tags, f"message_index:{message_index}"],
                                "quote": seg,
                                "user_message_index": message_index if role == "user" else None,
                                "source_message_index": message_index,
                            }
                        )
                    break

    summary_parts: list[str] = []
    if existing_summary and existing_summary.strip():
        summary_parts.append(existing_summary.strip())
    if user_points:
        summary_parts.append(f"用户目标/提问：{user_points[0]}" + (f" -> {user_points[-1]}" if len(user_points) > 1 else ""))
    if assistant_points:
        summary_parts.append(f"最近进展：{assistant_points[-1]}")
    process_summary = " | ".join(dict.fromkeys(p for p in summary_parts if p))[:600]

    return {
        "process_summary": process_summary,
        "memories": extracted[:15],
    }


def _save_checkpoint_summary(conn, source, summary: str) -> dict[str, Any]:
    """Update derived summary and its revision together, never over a newer capture."""
    from app.services.conversation_service import compute_content_revision_hash

    current = conn.execute("SELECT * FROM conversations WHERE id=?", (source["id"],)).fetchone()
    if not current or any(current[key] != source[key] for key in (
        "content_hash", "project_id", "title", "cwd", "source_path", "process_summary"
    )):
        raise HTTPException(409, "Memory source changed; retry from current source")
    messages = decode(current["messages"], [])
    revision = compute_content_revision_hash(current["title"], messages, summary, current["cwd"])
    conn.execute(
        "UPDATE conversations SET process_summary=?,content_hash=? WHERE id=?",
        (summary, revision, current["id"]),
    )
    if current["content_hash"] != revision or current["process_summary"] != summary:
        queue_distillation(current["id"], _connection=conn)
    return row_to_dict(conn.execute("SELECT * FROM conversations WHERE id=?", (current["id"],)).fetchone())


def _retire_and_replace(
    conn,
    project_id,
    old_id,
    category,
    title,
    content,
    tags,
    actor,
    quote,
    conversation_id,
    conversation_title,
    source_agent,
    now,
) -> str:
    cursor = conn.execute(
        "UPDATE contents SET status = 'historical', updated_at = ? WHERE id = ? AND status = 'active'",
        (now, old_id),
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=409, detail="The memory being replaced is no longer active")
    log_audit(actor=actor, action="content.supersede", target=old_id, conn=conn)
    mid = new_id("cnt")
    content_hash = compute_content_hash(project_id, category, title, content)
    conn.execute(
        """
        INSERT INTO contents (
            id, project_id, kind, category, title, content, tags, agent,
            status, version, related_ids, content_hash, created_at, updated_at
        ) VALUES (?, ?, 'memory', ?, ?, ?, ?, ?, 'active', 1, ?, ?, ?, ?)
        """,
        (mid, project_id, category, title, content, encode(tags), actor, encode([old_id]), content_hash, now, now),
    )
    conn.execute(
        """
        INSERT INTO content_versions (id, content_id, version, title, content, actor, created_at)
        VALUES (?, ?, 1, ?, ?, ?, ?)
        """,
        (new_id("ver"), mid, title, content, actor, now),
    )
    if conversation_id:
        conn.execute(
            """
            INSERT INTO content_sources (
                id, content_id, source_id, source_kind, source_title, source_agent, deleted, quote, created_at
            ) VALUES (?, ?, ?, 'conversation', ?, ?, 0, ?, ?)
            """,
            (new_id("src"), mid, conversation_id, conversation_title, source_agent, quote, now),
        )
    log_audit(actor=actor, action="content.create_distill", target=mid, conn=conn)
    return mid


def _agent_summary_eligible(mem: dict[str, Any]) -> bool:
    """Chatting-agent summaries are not the background OMP distillation path.

    A supplied summary may paraphrase the user. It is accepted without a verbatim
    quote unless it is explicitly a suggestion, hypothesis, negation, quotation,
    or other unconfirmed wording. Heuristic extraction does not use this path.
    """
    if not mem.get("_agent_supplied"):
        return False
    # Quoted or indexed claims keep the provenance gate. This path only rescues
    # a chatting agent's paraphrase, which previously had no quote to match.
    if mem.get("quote") or mem.get("supersedes") or mem.get("supersedes_id"):
        return False
    if mem.get("user_message_index") is not None or mem.get("source_message_index") is not None:
        return False
    stance = str(mem.get("stance") or "").strip().lower()
    if stance in _REJECTED_SUMMARY_STANCES:
        return False
    content_str = str(mem.get("content") or "")
    if _unconfirmed_user_evidence(content_str):
        return False
    if mem.get("category") == "decision" and _unconfirmed_decision_evidence(content_str):
        return False
    if mem.get("ambiguous") is True:
        return False
    return mem.get("category") in VALID_CATEGORIES


def _apply_agent_supplied_summary(conn, *, project_id, conversation_id, actor, mem) -> dict[str, Any]:
    from app.services import content_service

    existing = conn.execute(
        """
        SELECT id, content FROM contents
        WHERE project_id IS ? AND category = ? AND title = ? AND status = 'active'
        """,
        (project_id, mem["category"], mem["title"]),
    ).fetchone()
    supersedes_id = None
    action = "created"
    if existing:
        if existing["content"] == mem["content"]:
            return {
                "id": existing["id"],
                "category": mem["category"],
                "title": mem["title"],
                "content": mem["content"],
                "action": "unchanged",
                "version": None,
                "supersedes_id": None,
            }
        if mem.get("category") == "decision":
            _record_conflict(
                conn, project_id, existing["id"], mem["title"], mem["content"], "decision",
                mem.get("tags") or [], mem.get("quote"),
                "Same-title decision matches an active memory without an explicit supersedes object",
                actor, now_iso(),
            )
            return {
                "id": existing["id"],
                "category": mem["category"],
                "title": mem["title"],
                "content": mem["content"],
                "action": "conflict_recorded",
                "version": None,
                "supersedes_id": None,
            }
        supersedes_id = existing["id"]
        action = "updated"
    created = content_service.create_content(
        title=mem["title"],
        content=mem["content"],
        project_id=project_id,
        kind="memory",
        category=mem["category"],
        tags=mem.get("tags") or [],
        agent=actor,
        source_id=conversation_id,
        source_kind="conversation" if conversation_id else None,
        quote=mem.get("quote"),
        supersedes_id=supersedes_id,
        _connection=conn,
    )
    return {
        "id": created["id"],
        "category": mem["category"],
        "title": created["title"],
        "content": created["content"],
        "action": action,
        "version": created["version"],
        "supersedes_id": supersedes_id,
    }


def auto_summarize_memory(
    *,
    project_id: str | None = None,
    conversation_id: str | None = None,
    session_summary: str | None = None,
    memories: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
    cwd: str | None = None,
    actor: str = "host_agent",
    auto_extract_from_messages: bool = True,
) -> dict[str, Any]:
    """Companion implicit automatic memory summarization & session checkpoint.

    Can be invoked silently by an Agent via Skills/MCP during ongoing dialogue or
    triggered on conversation checkpoints. Supports both explicit structured memories
    and automatic signal extraction from conversation messages.
    """
    from app.services import conversation_service, project_service

    conv: dict[str, Any] | None = None
    message_index_offset = 0
    if conversation_id:
        conv = conversation_service.get_conversation(conversation_id)
        if project_id and conv.get("project_id") != project_id:
            raise HTTPException(422, "Conversation and memory project must match")
        if isinstance(messages, list) and messages:
            supplied = conversation_service._normalize_messages(messages)
            captured = conversation_service._normalize_messages(conv.get("messages") or [])
            # Hooks submit the newest increment, not the whole merged history.
            # Match its semantic suffix and translate local provenance indices.
            message_index_offset = len(captured) - len(supplied)
            fields = ("role", "content", "tool_calls", "tool_call_id", "name")
            if message_index_offset < 0 or any(
                any(message.get(key) != stored.get(key) for key in fields)
                for message, stored in zip(supplied, captured[message_index_offset:])
            ):
                raise HTTPException(409, "Supplied messages do not match the captured conversation")

    resolved_project_id = project_id or (conv.get("project_id") if conv else None)
    effective_cwd = cwd or (conv.get("cwd") if conv else None)
    if not resolved_project_id and effective_cwd:
        try:
            resolved_project_id = project_service.reconstruct_or_resolve_project(effective_cwd)
        except Exception:
            resolved_project_id = None

    effective_messages: list[dict[str, Any]] = []
    if isinstance(messages, list) and messages:
        effective_messages = messages
    elif conv and isinstance(conv.get("messages"), list):
        effective_messages = conv["messages"]

    batch_extracted_memories: list[dict[str, Any]] = []
    batch_summaries: list[str] = []
    summary_source = None
    if not effective_messages and auto_extract_from_messages and not conversation_id:
        with db() as conn:
            if resolved_project_id:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM conversations
                    WHERE project_id = ?
                    ORDER BY COALESCE(updated_at, created_at) DESC
                    LIMIT 15
                    """,
                    (resolved_project_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM conversations
                    WHERE project_id IS NOT NULL AND project_id != ''
                    ORDER BY COALESCE(updated_at, created_at) DESC
                    LIMIT 15
                    """
                ).fetchall()
            for row in rows:
                try:
                    row_msgs = json.loads(row["messages"] or "[]") if isinstance(row["messages"], str) else (row["messages"] or [])
                except Exception:
                    row_msgs = []
                if not isinstance(row_msgs, list) or not row_msgs:
                    continue
                h_res = extract_implicit_memories_from_messages(
                    row_msgs,
                    existing_summary=str(row["process_summary"] or ""),
                )
                row_summary = (h_res.get("process_summary") or "").strip()
                if row_summary:
                    if len(batch_summaries) < 3:
                        batch_summaries.append(row_summary)
                    if not str(row["process_summary"] or "").strip():
                        row = _save_checkpoint_summary(conn, row, row_summary)
                    if summary_source is None:
                        summary_source = row
                row_mems = h_res.get("memories") or []
                for m_item in row_mems:
                    batch_extracted_memories.append(
                        {
                            **m_item,
                            "_project_id": row["project_id"] or resolved_project_id,
                            "_conversation_id": row["id"],
                            "_source_version": row["content_hash"],
                        }
                    )
            if not batch_extracted_memories and batch_summaries and summary_source is not None:
                latest_row = summary_source
                conv_title = str(latest_row["title"] or "近期会话").strip()[:48]
                batch_extracted_memories.append(
                    {
                        "category": "background",
                        "title": f"任务进展与阶段背景：{conv_title}",
                        "content": batch_summaries[0],
                        "tags": ["implicit_auto", f"agent:{latest_row['agent'] or 'unknown'}"],
                        "_project_id": latest_row["project_id"] or resolved_project_id,
                        "_conversation_id": latest_row["id"],
                        "_source_version": latest_row["content_hash"],
                    }
                )

    heuristic = (
        extract_implicit_memories_from_messages(
            effective_messages,
            existing_summary=str((conv or {}).get("process_summary") or ""),
        )
        if auto_extract_from_messages and effective_messages
        else {"process_summary": " | ".join(batch_summaries)[:600], "memories": batch_extracted_memories[:20]}
    )

    final_summary = (session_summary or "").strip() or heuristic.get("process_summary") or ""
    if conv and final_summary:
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conv = _save_checkpoint_summary(conn, conv, final_summary)
    elif not conv and (effective_messages or session_summary) and actor:
        # Create or upsert a lightweight session conversation record so relay can also see it
        created_conv = conversation_service.upsert_conversation(
            agent=actor,
            title=(session_summary or "自动记忆检查点会话")[:80],
            project_id=resolved_project_id,
            cwd=effective_cwd,
            messages=effective_messages,
            process_summary=final_summary,
        )
        conv = created_conv
        conversation_id = created_conv["id"]

    if message_index_offset:
        # Structured tool/model indices are always absolute. Only internally
        # extracted suffix indices need translation into the captured history.
        for extracted in heuristic.get("memories") or []:
            for key in ("source_message_index", "user_message_index"):
                if type(extracted.get(key)) is int:
                    extracted[key] += message_index_offset

    candidate_memories: list[dict[str, Any]] = []
    seen_titles: set[tuple[str, str, str]] = set()
    supplied_and_extracted = (
        [(item, True) for item in (memories or [])]
        + [(item, False) for item in (heuristic.get("memories") or [])]
    )
    for item, supplied in supplied_and_extracted:
        if not isinstance(item, dict):
            continue
        cat = str(item.get("category") or "knowledge").strip().lower()
        if cat not in VALID_CATEGORIES:
            cat = "knowledge"
        stance = str(item.get("stance") or "").strip().lower()
        tags = item.get("tags") if isinstance(item.get("tags"), list) else ["implicit_auto"]
        if stance in MEMORY_STANCES:
            tags = list(dict.fromkeys([*tags, f"stance:{stance}"]))
            if stance in NON_DECISION_STANCES and (
                stance != "fact" or cat in {"decision", "preference"}
            ):
                cat = "knowledge"
            elif stance == "preference":
                cat = "preference"
            elif stance == "decision":
                cat = "decision"
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "").strip()
        if not title and content:
            title = content[:48]
        if not title or not content:
            continue
        key = (cat, title.lower(), content)
        if key in seen_titles:
            continue
        seen_titles.add(key)
        candidate_memories.append(
            {
                "category": cat,
                "stance": stance,
                "title": title[:160],
                "content": content,
                "tags": tags,
                "supersedes": item.get("supersedes"),
                "supersedes_id": item.get("supersedes_id"),
                "quote": item.get("quote"),
                "user_confirmed": item.get("user_confirmed"),
                "user_message_index": item.get("user_message_index"),
                "source_message_index": item.get("source_message_index"),
                "_project_id": item.get("_project_id"),
                "_conversation_id": item.get("_conversation_id"),
                "_source_version": item.get("_source_version"),
                "_agent_supplied": supplied,
            }
        )

    saved_items: list[dict[str, Any]] = []
    skipped_items: list[dict[str, Any]] = []
    for mem in candidate_memories:
        cat = mem["category"]
        eff_pid = mem.get("_project_id") or resolved_project_id
        eff_cid = mem.get("_conversation_id") or conversation_id
        with db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            source_row = conn.execute("SELECT * FROM conversations WHERE id=?", (eff_cid,)).fetchone() if eff_cid else None
            if eff_cid and not source_row:
                skipped_items.append({"category": cat, "title": mem["title"], "action": "skipped_deleted_source"})
                continue
            source = row_to_dict(source_row) if source_row else {"id": None, "project_id": eff_pid, "title": "", "messages": []}
            if source.get("project_id") != eff_pid:
                raise HTTPException(409, "Memory source moved to another project; retry from current source")
            if conv and eff_cid == conv["id"] and source.get("content_hash") != conv.get("content_hash"):
                raise HTTPException(409, "Memory source changed; retry from current source")
            if mem.get("_source_version") and source.get("content_hash") != mem["_source_version"]:
                raise HTTPException(409, "Memory source changed; retry from current source")
            if mem.get("supersedes_id") and not mem.get("supersedes"):
                # An identifier alone is not evidence of a user's change of mind.
                mem["supersedes"] = {"id": mem["supersedes_id"], "confidence": "ambiguous"}
            existing = conn.execute(
                "SELECT id,version FROM contents WHERE project_id IS ? AND category=? AND title=? AND content=? AND status='active'",
                (eff_pid, cat, mem["title"], mem["content"]),
            ).fetchone()
            if existing:
                saved_items.append({"id": existing["id"], "category": cat, "title": mem["title"], "content": mem["content"],
                                    "action": "unchanged", "version": existing["version"], "supersedes_id": None})
                continue
            if _has_content_tombstone(conn, eff_pid, cat, mem["title"], mem["content"]):
                skipped_items.append({"category": cat, "title": mem["title"], "action": "skipped_tombstoned"})
                continue
            if _agent_summary_eligible(mem):
                saved_items.append(_apply_agent_supplied_summary(
                    conn,
                    project_id=eff_pid,
                    conversation_id=eff_cid,
                    actor=actor,
                    mem=mem,
                ))
                continue
            ids = apply_distilled_memories(conn, source, [mem], actor, now_iso())
            if not ids:
                skipped_items.append({"category": cat, "title": mem["title"], "action": "not_applied_by_governance"})
                continue
            for mid in ids:
                item = conn.execute("SELECT * FROM contents WHERE id=?", (mid,)).fetchone()
                previous = (mem.get("supersedes") or {}).get("id")
                replaced = decode(item["related_ids"], []) if item["related_ids"] else []
                saved_items.append({"id": mid, "category": cat, "title": item["title"], "content": item["content"],
                                    "action": "updated" if previous or replaced else "created", "version": item["version"],
                                    "supersedes_id": previous or (replaced[0] if replaced else None)})

    created_count = sum(1 for item in saved_items if item["action"] == "created")
    updated_count = sum(1 for item in saved_items if item["action"] == "updated")
    unchanged_count = sum(1 for item in saved_items if item["action"] == "unchanged")

    if saved_items:
        titles_preview = "、".join(f"[{m['category']}] {m['title']}" for m in saved_items[:4])
        summary_note = f"💡 已自动隐性总结并更新 {len(saved_items)} 条记忆：{titles_preview}"
    elif final_summary:
        summary_note = f"💡 已自动更新当前会话过程总结：{final_summary[:80]}"
    else:
        summary_note = "当前对话轮次未检测到新的显式偏好、决策或阶段性记忆。"

    return {
        "ok": True,
        "trigger": "implicit_skill_memory",
        "actor": actor,
        "project_id": resolved_project_id,
        "conversation_id": conversation_id,
        "process_summary": final_summary,
        "summary_updated": bool(final_summary),
        "saved_count": len(saved_items),
        "created_count": created_count,
        "updated_count": updated_count,
        "unchanged_count": unchanged_count,
        "skipped_count": len(skipped_items),
        "skipped_memories": skipped_items,
        "saved_memories": saved_items,
        "memories": saved_items,
        "summary_note": summary_note,
    }
