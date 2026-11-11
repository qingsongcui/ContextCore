import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('legacy_assessment', Path(__file__).resolve().parents[2] / 'app/scripts/assess_legacy_provenance.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class LegacyProvenanceTest(unittest.TestCase):
    def test_review_does_not_repair_or_export_private_text(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'synthetic.sqlite3'
            with sqlite3.connect(path) as conn:
                conn.executescript('''
                    CREATE TABLE handoffs(id,target_agent,confirmed_at,card,status);
                    CREATE TABLE audit_logs(id,action,actor,target);
                    CREATE TABLE contents(id,kind,category,status,tags,content);
                    INSERT INTO handoffs VALUES('legacy','codex','now','{}','confirmed');
                    INSERT INTO handoffs VALUES('pending','codex',NULL,'{}','awaiting_confirmation');
                    INSERT INTO audit_logs VALUES('audit','handoff.confirm','codex','explicit');
                ''')
                conn.execute('INSERT INTO handoffs VALUES(?,?,?,?,?)', ('explicit', 'codex', 'now', json.dumps({'_confirmation': {'protocol': 2, 'actor': 'codex', 'at': 'now', 'audit_id': 'audit'}}), 'confirmed'))
                for identifier, tags in [('auto', ['implicit_auto', 'role:assistant']), ('ordinary', ['role:assistant']), ('user', ['role:user'])]:
                    conn.execute('INSERT INTO contents VALUES(?,?,?,?,?,?)', (identifier, 'memory', 'decision', 'active', json.dumps(tags), 'PRIVATE RAW TEXT'))
            before = hashlib.sha256(path.read_bytes()).hexdigest()
            result = module.assess(path)
            self.assertEqual({x['id'] for x in result['findings']}, {'legacy', 'auto'})
            self.assertEqual(result['changes'], 0)
            self.assertNotIn('PRIVATE RAW TEXT', json.dumps(result))
            self.assertEqual(before, hashlib.sha256(path.read_bytes()).hexdigest())
            with sqlite3.connect(path) as conn:
                conn.execute("DELETE FROM contents WHERE id='auto'")
            self.assertEqual([x['id'] for x in module.assess(path)['findings']], ['legacy'])

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing.sqlite3'
            with self.assertRaises(sqlite3.OperationalError):
                module.assess(path)
            self.assertFalse(path.exists())
