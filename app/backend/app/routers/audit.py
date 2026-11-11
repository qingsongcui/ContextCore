from fastapi import APIRouter
from app.models import Audit
from app.database import db, row_to_dict
from app.date_filters import upper_date_bound

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("", response_model=list[Audit])
def list_audit_logs_endpoint(
    actor: str | None = None,
    action: str | None = None,
    since: str | None = None,
    until: str | None = None,
):
    clauses = []
    params = []

    if actor:
        clauses.append("actor = ?")
        params.append(actor)
    if action:
        clauses.append("action = ?")
        params.append(action)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        operator, boundary = upper_date_bound(until)
        clauses.append(f"created_at {operator} ?")
        params.append(boundary)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM audit_logs {where} ORDER BY created_at DESC LIMIT 100"

    with db() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
        return [row_to_dict(r) for r in rows]
