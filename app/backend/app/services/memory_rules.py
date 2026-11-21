"""Conservative, local interpretation of explicit working conventions."""
import re

_PERSONAL = re.compile(r"^(?:(?:我(?:的)?|我们(?:的)?)?(?:所有|全部|每个)项目|跨项目通用|for all (?:my )?projects\b|across all (?:my )?projects\b)", re.I)
_PERIOD = re.compile(r"^(?:最终决定|决定|本项目)?(?P<object>[\w· -]{1,32}?)(?:改用|改按|改为|切换为|采用|使用|按)(?P<value>自然周|财务周|自然月|财务月|日历周|会计周)(?:进行)?(?:统计|汇总|核算)$")
_LANGUAGE = re.compile(r"^(?:(?:我的)?(?:所有|全部|每个)项目|本项目|此项目)?(?:以后|今后)?(?:所有)?(?:报告|周报)?(?:的)?(?:结论)?(?:以后)?(?:默认|统一|一律|都)?(?:使用|采用|改用|用)(?:中文|英文|英语|汉语)(?:写|撰写|输出|编写)?(?:报告|周报|结论)?$")


_ASSIGNMENT = re.compile(
    r"^(?:最终决定|决定|本项目)?(?P<object>[\w· -]{1,28}(?:格式|单位|引擎|工具|框架|数据库|分支|目录|编码|协议|时区|语言|端口))"
    r"(?:统一)?(?:改用|改按|改为|切换为|采用|使用|设为)(?P<value>[\w./+ -]{1,50})$", re.I)
_EXPLICIT_CHANGE = re.compile(r"改用|改按|改为|切换为|switch(?:ed)? to|changed? to", re.I)


def is_explicit_change(text):
    return bool(_EXPLICIT_CHANGE.search(text or ""))


def personal_declaration(text):
    return isinstance(text, str) and bool(_PERSONAL.match(text.strip()))


def rule_topic(text):
    text = (text or "").strip().rstrip("。.")
    match = _PERIOD.fullmatch(text)
    if match:
        return "period:" + match["object"]
    assignment = _ASSIGNMENT.fullmatch(text)
    if assignment:
        return "setting:" + assignment["object"].strip()
    if _LANGUAGE.fullmatch(text):
        # A language preference scoped only to a weekly report is independent
        # from the language of unrelated deliverables.
        return "language:weekly" if "周报" in text else "language:report"
    return None


def is_asserted(tags):
    return not any(tag in {"stance:suggestion", "stance:assumption", "stance:quote", "stance:negation"} for tag in tags or [] if isinstance(tag, str))


def current_rules(items):
    """A project-specific convention takes precedence over its personal default."""
    items = [it for it in items if is_asserted(it.get("tags"))]
    local_topics = {rule_topic(it.get("content")) for it in items if it.get("project_id") and not it.get("applicability")
                    and it.get("category") in {"preference", "decision"}}
    local_topics.discard(None)
    return [it for it in items if it.get("project_id") or it.get("category") not in {"preference", "decision"}
            or rule_topic(it.get("content")) not in local_topics]


def reading_title(title, content, tags):
    if "implicit_auto" not in tags:
        return title
    claim = (content or "").strip()
    if not claim:
        return title
    period = _PERIOD.fullmatch(claim.rstrip("。."))
    if period:
        return f"{period['object']}按{period['value']}{claim.rstrip('。.')[-2:]}"
    return re.sub(r"^(?:最终决定|排查发现|项目背景[：:]|背景[：:]|教训[：:])\s*", "", claim, count=1) or claim


def migrate_beta4_rules(conn):
    """Repair only unchanged, single-source heuristic memories with confirmed provenance."""
    from itertools import groupby
    from app.database import decode, encode, get_app_setting, set_app_setting, log_audit, now_iso
    from app.services.content_service import _record_current_version_snapshot
    from app.services.distillation_service import compute_content_hash, _has_content_tombstone
    from app.services.governance_guardrails import canonical_memory_key

    marker = "personal_workbench_memory_migration_v1"
    if get_app_setting(marker, False, conn=conn):
        return
    rows = conn.execute("""
        SELECT c.*, s.quote, v.id AS conversation, v.started_at AS source_time, v.content_hash AS source_hash
        FROM contents c JOIN content_sources s ON s.content_id=c.id AND s.deleted=0 AND s.source_kind='conversation'
        JOIN conversations v ON v.id=s.source_id
        WHERE c.kind='memory' AND c.status='active' AND c.category IN ('preference','decision')
          AND c.version=1 AND c.origin_authority='user_confirmed' AND c.content=s.quote
          AND c.tags LIKE '%implicit_auto%' AND v.project_id=c.project_id
          AND (SELECT count(*) FROM content_sources all_src WHERE all_src.content_id=c.id)=1
        ORDER BY c.project_id, v.started_at, c.created_at, c.id
    """).fetchall()
    by_topic = []
    for row in rows:
        tags = decode(row["tags"], [])
        if ("role:user" not in tags or "implicit_auto" not in tags
                or f"source_version:{row['source_hash']}" not in tags
                or f"stance:{row['category']}" not in tags):
            continue
        content = row["content"]
        if row["category"] == "preference" and personal_declaration(content):
            if (_has_content_tombstone(conn, None, "preference", row["title"], content)
                    or conn.execute("SELECT 1 FROM contents WHERE project_id IS NULL AND category='preference' AND content=?", (content,)).fetchone()):
                continue
            conn.execute("UPDATE contents SET project_id=NULL,version=version+1,content_hash=?,canonical_key=? WHERE id=?", (
                compute_content_hash(None, "preference", row["title"], content),
                canonical_memory_key(None, "preference", row["title"], content), row["id"]))
            _record_current_version_snapshot(conn, row["id"], "system", now_iso())
            log_audit(actor="system", action="content.migrate_personal_scope", target=row["id"], conn=conn)
        elif row["category"] == "decision" and rule_topic(content):
            positions = [int(t.split(":")[1]) for t in tags if isinstance(t, str) and re.fullmatch(r"message_index:\d+", t)]
            if len(positions) == 1:
                by_topic.append(((row["project_id"], rule_topic(content)), row["source_time"],
                                 row["conversation"], positions[0], row))
    for _, group in groupby(sorted(by_topic, key=lambda r: r[:4]), key=lambda r: r[0]):
        records = list(group)
        for previous, current in zip(records, records[1:]):
            old, new = previous[-1], current[-1]
            ordered = previous[1] < current[1] or (previous[2] == current[2] and previous[3] < current[3])
            if ordered and is_explicit_change(new["content"]) and old["content"] != new["content"]:
                conn.execute("UPDATE contents SET status='historical',version=version+1,superseded_by_id=? WHERE id=?", (new["id"], old["id"]))
                _record_current_version_snapshot(conn, old["id"], "system", now_iso())
                links = list(dict.fromkeys([*decode(new["related_ids"], []), old["id"]]))
                conn.execute("UPDATE contents SET related_ids=? WHERE id=?", (encode(links), new["id"]))
                log_audit(actor="system", action="content.migrate_supersession", target=old["id"], conn=conn)
    set_app_setting(marker, True, conn=conn)
