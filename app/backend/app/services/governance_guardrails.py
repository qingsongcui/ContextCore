"""Long-term memory admission guardrails, canonical tombstones, and claim reconciliation helpers."""
from __future__ import annotations

import hashlib
import re
import sqlite3
from typing import Any

from app.database import new_id, now_iso

AUTHORITATIVE_ORIGINS = frozenset({"user_explicit", "user_confirmed", "verified_outcome"})
VALID_ORIGIN_AUTHORITIES = frozenset(
    {"user_explicit", "user_confirmed", "verified_outcome", "source_backed", "manual", "import"}
)
FORBIDDEN_ORIGIN_AUTHORITIES = frozenset(
    {"ai_unconfirmed_proposal", "ai_guess", "temp_request", "raw_tool_log"}
)
PROTECTED_CONFLICT_CATEGORIES = frozenset({"decision", "preference"})

_TITLE_PREFIX_RE = re.compile(
    r"^(?:用户偏好与规范|用户偏好|使用习惯与偏好|使用习惯|技术与架构决策|决定与约束|架构决策|"
    r"踩坑经验与教训|踩坑经验|经验教训|项目背景与上下文|项目背景|任务进展与阶段背景|"
    r"任务推进节点|会话进展提炼|preference|decision|lesson|background|knowledge)[:：\s\-]+",
    re.IGNORECASE,
)

_LEADIN_FILLER_RE = re.compile(
    r"^(?:please\s+|always\s+|we\s+should\s+|make\s+sure\s+to\s+|remember\s+to\s+|"
    r"请始终|请务必|请统一|始终|务必|统一|一律|记住|注意)[:：,\s]*",
    re.IGNORECASE,
)

_NON_WORD_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)
_MULTI_WS_RE = re.compile(r"\s+")

_PERMANENT_SCOPE_RE = re.compile(
    r"(以后都|今后都|往后都|长期规则|永久|全局统一|在所有任务中|from\s+now\s+on|in\s+all\s+future|permanently)",
    re.IGNORECASE,
)

_TEMP_CONSTRAINT_RE = re.compile(
    r"(?:"
    r"(?:这次|本次|这回|本轮|当前任务|当前分支|眼下|暂时|临时|仅限本次|仅在本次|只在这一次|仅当前任务)"
    r"\s*(?:先)?\s*(?:别|不要|不用|不改|不动|不碰|跳过|忽略|只|先|暂不)"
    r"|"
    r"^(?:先别|先不要|暂时不要|暂时别|临时不要)(?:改|动|碰|删|修|重构|升级|提交|发版|处理)"
    r"|"
    r"(?:仅限当前任务|仅在当前任务生效|不要升级为长期规则)"
    r"|"
    r"\b(?:for\s+this\s+task\s+only|for\s+this\s+session\s+only|this\s+time\s+only|"
    r"for\s+now,?\s+(?:do\s+not|don'?t|skip|avoid)|"
    r"this\s+time,?\s+(?:do\s+not|don'?t|skip|avoid)|"
    r"temporarily\s+(?:do\s+not|don'?t|skip|avoid|disable))\b"
    r")",
    re.IGNORECASE,
)

_PROPOSAL_RE = re.compile(
    r"(?:"
    r"(?:我建议|建议采用|建议先|建议改为|可以考虑|不妨尝试|我们可以试着|或许可以用|方案建议[:：]|待用户确认|尚未确认|未获确认)"
    r"|"
    r"\b(?:i\s+suggest|i\s+recommend|we\s+could\s+try|you\s+might\s+want\s+to|"
    r"consider\s+using|proposed\s+plan:|awaiting\s+user\s+confirmation|unconfirmed\s+proposal)\b"
    r")",
    re.IGNORECASE,
)

_GUESS_RE = re.compile(
    r"(?:"
    r"(?:可能是因为|原因可能是|我猜测?|猜测是|大概是因为?|也许是因为|疑似是因为?|看起来像是|尚未验证|待验证假设|未经验证)"
    r"|"
    r"\b(?:might\s+be\s+(?:caused\s+by|due\s+to)?|probably\s+(?:caused\s+by|due\s+to)|"
    r"my\s+guess\s+is|i\s+guess|unverified\s+hypothesis|possibly\s+due\s+to|needs\s+verification)\b"
    r")",
    re.IGNORECASE,
)

_RAW_TOOL_LOG_RE = re.compile(
    r"(?:"
    r"Traceback\s*\(most\s+recent\s+call\s+last\):"
    r"|"
    r"File\s+\"[^\"]+\",\s+line\s+\d+,\s+in\s+"
    r"|"
    r"\x1b\[[0-9;]*[a-zA-Z]"
    r"|"
    r"(?:^|\n)\s*\$\s+(?:git|npm|pnpm|yarn|pytest|python3?|cargo|go|curl|ls|cat)\s+.*\n(?:.*\n){2,}"
    r"|"
    r"(?:^|\n)={5,}\s+(?:test\s+session\s+starts|FAILURES|short\s+test\s+summary\s+info)\s+={5,}"
    r")",
    re.MULTILINE,
)

_SESSION_FILLER_TITLE_RE = re.compile(
    r"^(?:任务进展与阶段背景|会话进展提炼|自动会话摘要)[:：\s]",
    re.IGNORECASE,
)

_NEGATION_RE = re.compile(
    r"(?:禁止|不要|不得|严禁|避免|不用|禁用|切勿|绝不|移除|弃用|"
    r"\b(?:never|do\s+not|don'?t|must\s+not|avoid|forbid|forbidden|ban|disable|stop\s+using)\b)",
    re.IGNORECASE,
)

_EXCLUSIVE_TECH_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"npm", "pnpm", "yarn", "bun"}),
    frozenset({"sqlite", "postgres", "postgresql", "mysql", "mariadb", "mongodb"}),
    frozenset({"jest", "vitest", "mocha"}),
    frozenset({"tabs", "spaces"}),
    frozenset({"redux", "zustand", "mobx", "pinia"}),
    frozenset({"rest", "graphql", "grpc"}),
    frozenset({"unittest", "pytest"}),
    frozenset({"pip", "uv", "poetry", "pipenv"}),
)


def _raw_content_hash(project_id: str | None, category: str | None, title: str, content: str) -> str:
    raw = f"content:{project_id or ''}:{category or 'knowledge'}:{(title or '').strip()}:{(content or '').strip()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalized_content_hash(project_id: str | None, category: str | None, title: str, content: str) -> str:
    t = _MULTI_WS_RE.sub(" ", (title or "").strip())
    c = _MULTI_WS_RE.sub(" ", (content or "").strip())
    raw = f"content:{project_id or ''}:{category or 'knowledge'}:{t}:{c}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _normalize_topic_text(title: str) -> str:
    stripped = _TITLE_PREFIX_RE.sub("", (title or "").strip()).lower()
    return _NON_WORD_RE.sub("", stripped)


def _normalize_claim_text(content: str) -> str:
    # Slice bounded prefix so large document bodies never allocate multi-megabyte temporaries.
    head = (content or "")[:1024].strip()
    head = re.sub(r"^[\-\*\•\d\.\)\s]+", "", head)
    head = _TITLE_PREFIX_RE.sub("", head)
    for _ in range(3):
        next_head = _LEADIN_FILLER_RE.sub("", head).strip()
        if next_head == head:
            break
        head = next_head
    return _NON_WORD_RE.sub("", head.lower())


def canonical_topic_key(project_id: str | None, title: str) -> str:
    scope = (project_id or "global").strip() or "global"
    topic = _normalize_topic_text(title)
    return f"{scope}:{topic}"


def canonical_memory_key(
    project_id: str | None,
    category: str | None,
    title: str,
    content: str = "",
) -> str:
    """Compute a normalized canonical memory fingerprint scoped to (project/global, category)."""
    scope = (project_id or "global").strip() or "global"
    cat = (category or "knowledge").strip().lower() or "knowledge"
    norm_title = _normalize_topic_text(title)
    norm_claim = _normalize_claim_text(content)
    payload = f"cmk:v1:{scope}:{cat}:{norm_title}:{norm_claim}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"cmk_{digest}"


def canonical_cross_category_key(
    project_id: str | None,
    title: str,
    content: str = "",
) -> str:
    """Compute a category-agnostic canonical fingerprint for agent/distillation resurrection blocking."""
    scope = (project_id or "global").strip() or "global"
    norm_title = _normalize_topic_text(title)
    norm_claim = _normalize_claim_text(content)
    payload = f"cmkx:v1:{scope}:{norm_title}:{norm_claim}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"cmkx_{digest}"


def canonical_claim_key(project_id: str | None, content: str) -> str | None:
    """Compute a title-independent claim key for substantive memory bodies (>=12 normalized chars)."""
    norm_claim = _normalize_claim_text(content)
    if len(norm_claim) < 12:
        return None
    scope = (project_id or "global").strip() or "global"
    digest = hashlib.sha256(f"cmk_claim:v1:{scope}:{norm_claim}".encode("utf-8")).hexdigest()[:32]
    return f"cmkc_{digest}"


def is_tombstoned_canonical_key(conn: sqlite3.Connection, canonical_key: str | None) -> bool:
    if not canonical_key:
        return False
    row = conn.execute(
        "SELECT 1 FROM tombstones WHERE entity_type = 'memory' AND dedupe_key = ? LIMIT 1",
        (canonical_key,),
    ).fetchone()
    return row is not None


def is_tombstoned_memory(
    conn: sqlite3.Connection,
    project_id: str | None,
    category: str | None,
    title: str,
    content: str,
    *,
    include_cross_category: bool = False,
) -> bool:
    """Return True if exact content hash, normalized hash, or canonical memory key was tombstoned."""
    keys = [
        _raw_content_hash(project_id, category, title, content),
        _normalized_content_hash(project_id, category, title, content),
        canonical_memory_key(project_id, category, title, content),
    ]
    if include_cross_category:
        keys.append(canonical_cross_category_key(project_id, title, content))
        claim_key = canonical_claim_key(project_id, content)
        if claim_key:
            keys.append(claim_key)
    unique_keys = tuple(dict.fromkeys(keys))
    placeholders = ",".join("?" for _ in unique_keys)
    row = conn.execute(
        f"""
        SELECT 1 FROM tombstones
        WHERE entity_type IN ('content', 'memory', 'document')
          AND dedupe_key IN ({placeholders})
        LIMIT 1
        """,
        unique_keys,
    ).fetchone()
    return row is not None


def record_memory_tombstones(
    conn: sqlite3.Connection,
    item: dict[str, Any],
    actor: str = "user",
    *,
    reason: str = "user_deleted",
    now: str | None = None,
) -> list[str]:
    """Record canonical memory tombstones (`entity_type='memory'`) for an item and its versions."""
    deleted_at = now or now_iso()
    project_id = item.get("project_id")
    category = item.get("category") or "knowledge"
    kind = item.get("kind") or "memory"
    title = item.get("title") or ""
    content_head = item.get("content") or ""

    keys_to_record: list[str] = []
    stored_ck = item.get("canonical_key")
    if isinstance(stored_ck, str) and stored_ck.strip():
        keys_to_record.append(stored_ck.strip())

    keys_to_record.append(canonical_memory_key(project_id, category, title, content_head))
    if kind == "memory":
        keys_to_record.append(canonical_cross_category_key(project_id, title, content_head))
        ck_claim = canonical_claim_key(project_id, content_head)
        if ck_claim:
            keys_to_record.append(ck_claim)

    content_id = item.get("id")
    if content_id:
        # Use SQL substr() so historical large bodies are never materialized in Python RAM.
        for ver in conn.execute(
            "SELECT title, substr(content, 1, 1024) AS content_head FROM content_versions WHERE content_id = ? ORDER BY version",
            (content_id,),
        ):
            v_title = ver["title"] or ""
            v_head = ver["content_head"] or ""
            keys_to_record.append(canonical_memory_key(project_id, category, v_title, v_head))
            if kind == "memory":
                keys_to_record.append(canonical_cross_category_key(project_id, v_title, v_head))
                v_claim = canonical_claim_key(project_id, v_head)
                if v_claim:
                    keys_to_record.append(v_claim)

    deduped = list(dict.fromkeys(k for k in keys_to_record if k))
    for key in deduped:
        conn.execute(
            """
            INSERT OR IGNORE INTO tombstones
                (id, entity_type, dedupe_key, scope_project_id, deleted_at, deleted_by, reason)
            VALUES (?, 'memory', ?, ?, ?, ?, ?)
            """,
            (new_id("tomb"), key, project_id, deleted_at, actor or "user", reason),
        )
    return deduped


def normalize_applicability(applicability: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(applicability, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for raw_key, raw_val in applicability.items():
        key = str(raw_key or "").strip()
        if not key or raw_val is None:
            continue
        if isinstance(raw_val, str):
            val = raw_val.strip()
            if val:
                cleaned[key] = val
        elif isinstance(raw_val, (list, tuple, set)):
            items = [str(v).strip() for v in raw_val if str(v or "").strip()]
            if items:
                cleaned[key] = list(dict.fromkeys(items))
        elif isinstance(raw_val, bool):
            cleaned[key] = raw_val
        elif isinstance(raw_val, (int, float)):
            cleaned[key] = raw_val
        elif isinstance(raw_val, dict):
            nested = normalize_applicability(raw_val)
            if nested:
                cleaned[key] = nested
    return cleaned


def has_non_empty_applicability(applicability: dict[str, Any] | None) -> bool:
    normalized = normalize_applicability(applicability)
    for val in normalized.values():
        if isinstance(val, str) and val.strip():
            return True
        if isinstance(val, list) and len(val) > 0:
            return True
        if isinstance(val, dict) and has_non_empty_applicability(val):
            return True
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            return True
    return False


def classify_write_candidate(
    title: str = "",
    content: str = "",
    *,
    category: str = "knowledge",
    origin_authority: str | None = None,
    is_agent_write: bool = False,
    actor: str | None = None,
    kind: str = "memory",
) -> dict[str, Any]:
    """Gate-0 admission classifier for long-term memory writes.

    - Routes task-only temporary constraints (`"这次先别..."`) to `working_tasks.temp_constraints`.
    - Rejects unconfirmed AI proposals, unverified debugging guesses, raw tool logs/stacktraces,
      and session-filler dumps when written by an Agent without authoritative provenance.
    """
    del actor  # Reserved for caller provenance context.
    clean_title = (title or "").strip()
    clean_content = (content or "").strip()
    raw_origin = (origin_authority or "").strip().lower()
    norm_origin = raw_origin or ("manual" if not is_agent_write else "agent_unverified")

    if kind == "document" or category == "task":
        return {
            "allowed": True,
            "action": "allow",
            "reason": None,
            "origin_authority": raw_origin if raw_origin in VALID_ORIGIN_AUTHORITIES else "manual",
            "extracted_constraint": None,
        }

    if raw_origin == "temp_request":
        return {
            "allowed": False,
            "action": "route_to_working_task",
            "reason": "temporary_task_constraint",
            "origin_authority": raw_origin,
            "extracted_constraint": clean_content or clean_title,
        }

    if raw_origin in FORBIDDEN_ORIGIN_AUTHORITIES:
        return {
            "allowed": False,
            "action": "reject",
            "reason": f"forbidden_origin_authority:{raw_origin}",
            "origin_authority": raw_origin,
            "extracted_constraint": None,
        }

    combined = f"{clean_title}\n{clean_content}".strip()

    # 1. Temporary task-only constraint -> route to short-term working task, NEVER long-term memory.
    if (
        (_TEMP_CONSTRAINT_RE.search(clean_content) or _TEMP_CONSTRAINT_RE.search(clean_title))
        and not _PERMANENT_SCOPE_RE.search(combined)
    ):
        extracted = clean_content if clean_content else clean_title
        return {
            "allowed": False,
            "action": "route_to_working_task",
            "reason": "temporary_task_constraint",
            "origin_authority": norm_origin,
            "extracted_constraint": extracted,
        }

    # 2. Agent-write admission checks when origin_authority is not authoritative.
    if is_agent_write:
        if _SESSION_FILLER_TITLE_RE.search(clean_title) and raw_origin not in AUTHORITATIVE_ORIGINS:
            return {
                "allowed": False,
                "action": "reject",
                "reason": "session_filler_summary",
                "origin_authority": norm_origin,
                "extracted_constraint": None,
            }

        if _RAW_TOOL_LOG_RE.search(clean_content) and raw_origin not in AUTHORITATIVE_ORIGINS:
            return {
                "allowed": False,
                "action": "reject",
                "reason": "raw_tool_output",
                "origin_authority": norm_origin,
                "extracted_constraint": None,
            }

        if raw_origin not in AUTHORITATIVE_ORIGINS:
            if _PROPOSAL_RE.search(combined):
                return {
                    "allowed": False,
                    "action": "reject",
                    "reason": "unconfirmed_ai_proposal",
                    "origin_authority": norm_origin,
                    "extracted_constraint": None,
                }
            if _GUESS_RE.search(combined):
                return {
                    "allowed": False,
                    "action": "reject",
                    "reason": "unverified_guess",
                    "origin_authority": norm_origin,
                    "extracted_constraint": None,
                }

    effective_origin = raw_origin if raw_origin in VALID_ORIGIN_AUTHORITIES else "manual"
    return {
        "allowed": True,
        "action": "allow",
        "reason": None,
        "origin_authority": effective_origin,
        "extracted_constraint": None,
    }


def _char_bigrams(text: str) -> set[str]:
    compact = _NON_WORD_RE.sub("", (text or "").lower())
    if not compact:
        return set()
    if len(compact) == 1:
        return {compact}
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


def _word_tokens(text: str) -> set[str]:
    return {tok for tok in re.findall(r"[a-z0-9_.-]{2,}", (text or "").lower())}


def _extract_positive_tech_choices(text: str) -> dict[int, set[str]]:
    """Extract tech tokens mentioned positively (not immediately negated)."""
    lowered = (text or "").lower()
    words = _word_tokens(lowered)
    choices: dict[int, set[str]] = {}
    for idx, group in enumerate(_EXCLUSIVE_TECH_GROUPS):
        matched = words & group
        if not matched:
            continue
        positive: set[str] = set()
        for tok in matched:
            # Check if negated right before token (e.g. "不用 npm", "instead of npm", "not npm", "禁止 npm")
            neg_pattern = re.compile(
                rf"(?:不用|不要用?|禁止(?:使用)?|弃用|替代|而不是|而非|instead\s+of|rather\s+than|not|never\s+use|avoid|drop)\s+{re.escape(tok)}\b",
                re.IGNORECASE,
            )
            if not neg_pattern.search(lowered):
                positive.add(tok)
        if positive:
            choices[idx] = positive
    return choices


def _has_exclusive_tech_clash(text_a: str, text_b: str) -> tuple[bool, str | None]:
    pos_a = _extract_positive_tech_choices(text_a)
    pos_b = _extract_positive_tech_choices(text_b)
    for idx, set_a in pos_a.items():
        set_b = pos_b.get(idx)
        if set_b and set_a != set_b and not (set_a & set_b):
            return True, f"Conflicting technology choice: {sorted(set_a)[0]} vs {sorted(set_b)[0]}"
    return False, None


def _has_polarity_clash(text_a: str, text_b: str) -> bool:
    neg_a = bool(_NEGATION_RE.search(text_a or ""))
    neg_b = bool(_NEGATION_RE.search(text_b or ""))
    return neg_a != neg_b


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    union = len(a | b)
    return (len(a & b) / union) if union else 0.0


def is_semantically_equivalent(
    existing: dict[str, Any],
    title: str,
    content: str,
    category: str = "knowledge",
) -> bool:
    ex_proj = existing.get("project_id")
    ex_title = existing.get("title") or ""
    ex_content = existing.get("content") or ""
    ex_cat = existing.get("category") or "knowledge"

    if canonical_memory_key(ex_proj, ex_cat, ex_title, ex_content) == canonical_memory_key(
        ex_proj, category, title, content
    ):
        return True

    claim_a = _normalize_claim_text(ex_content)
    claim_b = _normalize_claim_text(content)
    if claim_a and claim_a == claim_b:
        tech_clash, _ = _has_exclusive_tech_clash(ex_content, content)
        if not tech_clash and not _has_polarity_clash(ex_content, content):
            return True

    topic_same = _normalize_topic_text(ex_title) == _normalize_topic_text(title) and bool(
        _normalize_topic_text(title)
    )
    if topic_same:
        tech_clash, _ = _has_exclusive_tech_clash(ex_content, content)
        if tech_clash or _has_polarity_clash(ex_content, content):
            return False
        sim = _jaccard(_char_bigrams(claim_a), _char_bigrams(claim_b))
        if sim >= 0.80:
            return True

    return False


def _is_compatible_elaboration(text_a: str, text_b: str, tech_clash: bool) -> bool:
    """Return True when statements share a compatible base line/prefix without tech clash."""
    if tech_clash:
        return False
    claim_a = _normalize_claim_text(text_a)
    claim_b = _normalize_claim_text(text_b)
    if not claim_a or not claim_b:
        return False
    shorter, longer = (claim_a, claim_b) if len(claim_a) <= len(claim_b) else (claim_b, claim_a)
    if len(shorter) >= 4 and longer.startswith(shorter):
        return True
    lines_a = {_normalize_claim_text(ln) for ln in (text_a or "").splitlines() if _normalize_claim_text(ln)}
    lines_b = {_normalize_claim_text(ln) for ln in (text_b or "").splitlines() if _normalize_claim_text(ln)}
    shared = {ln for ln in (lines_a & lines_b) if len(ln) >= 6}
    if shared:
        return True
    return False


def detect_memory_contradiction(
    existing: dict[str, Any],
    title: str,
    content: str,
    category: str = "knowledge",
) -> tuple[bool, str | None]:
    """Detect whether candidate claim contradicts an active memory item."""
    if is_semantically_equivalent(existing, title, content, category):
        return False, None

    ex_title = existing.get("title") or ""
    ex_content = existing.get("content") or ""
    ex_cat = existing.get("category") or "knowledge"

    topic_a = _normalize_topic_text(ex_title)
    topic_b = _normalize_topic_text(title)
    same_topic = bool(topic_a and topic_b and topic_a == topic_b)
    topic_sim = _jaccard(_char_bigrams(topic_a), _char_bigrams(topic_b))
    body_sim = _jaccard(
        _char_bigrams(f"{topic_a}{_normalize_claim_text(ex_content)}"),
        _char_bigrams(f"{topic_b}{_normalize_claim_text(content)}"),
    )

    combined_a = f"{ex_title} {ex_content}"
    combined_b = f"{title} {content}"
    tech_clash, tech_reason = _has_exclusive_tech_clash(combined_a, combined_b)

    if _is_compatible_elaboration(ex_content, content, tech_clash):
        return False, None

    if same_topic:
        if tech_clash:
            return True, tech_reason
        if ex_cat in PROTECTED_CONFLICT_CATEGORIES or category in PROTECTED_CONFLICT_CATEGORIES:
            return True, f"Conflicting {category} rule on same topic '{title.strip()}'"
        if _has_polarity_clash(ex_content, content):
            return True, f"Opposing directive polarity on topic '{title.strip()}'"

    if (topic_sim >= 0.45 or body_sim >= 0.35) and (
        ex_cat in PROTECTED_CONFLICT_CATEGORIES or category in PROTECTED_CONFLICT_CATEGORIES
    ):
        if tech_clash:
            return True, tech_reason
        if _has_polarity_clash(ex_content, content) and body_sim >= 0.38:
            return True, f"Contradictory directive against active {ex_cat} '{ex_title}'"

    return False, None


def compare_memory_claims(
    existing: dict[str, Any],
    title: str,
    content: str,
    category: str = "knowledge",
) -> tuple[str, str | None]:
    """Classify candidate vs existing active memory into ('equivalent' | 'contradiction' | 'unrelated', reason)."""
    if is_semantically_equivalent(existing, title, content, category):
        return "equivalent", "Semantically equivalent to existing active memory"
    is_conflict, reason = detect_memory_contradiction(existing, title, content, category)
    if is_conflict:
        return "contradiction", reason
    return "unrelated", None
