"""Explicit, read-only legacy impact report. Never repairs or exports raw text."""
import argparse
import json
from pathlib import Path
import sqlite3


def decoded(value, fallback):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return fallback


def assess(path: Path) -> dict:
    # Do not use app.database: its connection helper performs migrations/chmod.
    path = path.expanduser().absolute()
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA query_only=ON')
    findings = []
    try:
        for row in connection.execute("SELECT id,target_agent,confirmed_at,card FROM handoffs WHERE status='confirmed'"):
            card = decoded(row['card'], {})
            proof = card.get('_confirmation') if isinstance(card, dict) else None
            valid = (isinstance(proof, dict) and proof.get('protocol') == 2
                     and proof.get('actor') == row['target_agent']
                     and proof.get('at') == row['confirmed_at'] and proof.get('audit_id'))
            if valid:
                valid = connection.execute(
                    "SELECT 1 FROM audit_logs WHERE id=? AND action='handoff.confirm' AND actor=? AND target=?",
                    (proof['audit_id'], row['target_agent'], row['id']),
                ).fetchone() is not None
            if not valid:
                findings.append({'entity': 'handoff', 'id': row['id'],
                                 'assessment': 'confirmation_review_required',
                                 'reason': 'Explicit target confirmation is not independently proven; this does not prove auto-confirmation.'})
        for row in connection.execute("SELECT id,tags FROM contents WHERE kind='memory' AND category='decision' AND status='active'"):
            tags = decoded(row['tags'], [])
            if not isinstance(tags, list) or 'implicit_auto' not in tags:
                continue
            # A source hash or role tag alone cannot prove an explicit decision.
            findings.append({'entity': 'memory', 'id': row['id'],
                             'assessment': 'source_review_required',
                             'reason': 'Tagged implicit automatic decision; inspect source evidence before any repair.',
                             'source_role': 'user' if 'role:user' in tags else 'assistant' if 'role:assistant' in tags else 'unknown'})
    finally:
        connection.close()
    return {'mode': 'read_only', 'changes': 0, 'findings': findings,
            'limits': ['No raw source text exported.', 'Review candidates are not proven incorrect records.',
                       'Untagged legacy memory provenance remains unknown.', 'Any real-data repair requires a separate record-level preview and authorization.']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(assess(args.database), ensure_ascii=False, indent=2))
