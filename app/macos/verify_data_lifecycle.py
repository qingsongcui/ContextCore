"""Exercise persistence, legacy identity migration and deletion in the bundle."""
import argparse
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import time

import httpx


def verify(bundle: Path):
    service = bundle.resolve() / 'Contents/Resources/service/senrelay-service'
    with tempfile.TemporaryDirectory(prefix='senrelay-data-lifecycle-') as directory:
        root = Path(directory)
        home = root / 'home'
        home.mkdir(mode=0o700)
        data = root / 'data'
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = {'HOME': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'}
        process = None
        with (root / 'service.log').open('w') as log, httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=15) as client:
            def start():
                nonlocal process
                process = subprocess.Popen([str(service), '--data-dir', str(data), '--port', str(port)], env=env, cwd=root, stdout=log, stderr=log)
                client.cookies.clear()
                client.headers.pop('X-CSRF-Token', None)
                for _ in range(100):
                    assert process.poll() is None, 'Bundled service exited during startup'
                    try:
                        response = client.get('/api/session')
                        if response.status_code == 200:
                            client.headers['X-CSRF-Token'] = response.json()['csrf_token']
                            return
                    except httpx.TransportError:
                        pass
                    time.sleep(.1)
                raise AssertionError('Bundled service startup timed out')

            def stop():
                if process and process.poll() is None:
                    result = subprocess.run([str(service), '-m', 'app.cli', 'stop', '--data-dir', str(data)], env=env, cwd=root, capture_output=True, timeout=25)
                    assert result.returncode == 0, 'Bundled CLI stop failed'
                    process.wait(timeout=15)
                    assert process.returncode == 0, 'Unclean service exit'

            def request(method, path, body=None, status=200):
                response = client.request(method, path, json=body)
                assert response.status_code == status, (method, path, response.status_code, response.text[:300])
                return response.json()

            try:
                start()
                project = request('POST', '/api/projects', {'name': 'Synthetic persistence fixture'})
                chat = request('POST', '/api/conversations', {'project_id': project['id'], 'agent': 'codex', 'title': 'Synthetic source', 'messages': [{'id': 'synthetic-m1', 'role': 'user', 'content': 'Synthetic source text', 'timestamp': '2026-10-28T00:00:00Z'}]})
                payload = {'project_id': project['id'], 'kind': 'document', 'title': 'synthetic.txt', 'content': 'Original synthetic document'}
                item = request('POST', '/api/content', payload)
                request('PATCH', '/api/content/' + item['id'], {'content': 'Edited synthetic document', 'expected_version': 1})
                memory = request('POST', '/api/content', {'project_id': project['id'], 'title': 'Independent memory', 'content': 'Synthetic requirement', 'source_id': chat['id'], 'source_kind': 'conversation'})
                stop()
                # Simulate the old schema lacking identity tracking, never real data.
                with sqlite3.connect(data / 'contextcore.db') as conn:
                    conn.execute('DROP TRIGGER content_identity_insert')
                    conn.execute('DROP TRIGGER content_identity_update')
                    conn.execute('DROP TABLE content_identity_history')
                start()
                assert request('GET', '/api/content/' + item['id'])['content'] == 'Edited synthetic document'
                request('DELETE', '/api/content/' + item['id'] + '?expected_version=2')
                request('DELETE', '/api/conversations/' + chat['id'])
                assert request('GET', '/api/content/' + memory['id'])['sources'][0]['deleted']
                stop()
                start()
                request('GET', '/api/content/' + item['id'], status=404)
                request('POST', '/api/content', payload, status=410)
                request('GET', '/api/conversations/' + chat['id'], status=404)
                assert request('GET', '/api/content/' + memory['id'])['content'] == 'Synthetic requirement'
                with sqlite3.connect(data / 'contextcore.db') as conn:
                    assert conn.execute('SELECT COUNT(*) FROM content_versions WHERE content_id=?', (item['id'],)).fetchone()[0] == 0
                    assert conn.execute('SELECT COUNT(*) FROM content_identity_history WHERE content_id=?', (item['id'],)).fetchone()[0] == 0
                stop()
            finally:
                if process and process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=15)
        return {'synthetic_only': True, 'persistence': 'passed', 'legacy_identity_migration': 'passed', 'deleted_old_body_reimport': 'refused', 'conversation_deletion_preserves_memory': 'passed', 'three_starts_and_cli_stops': 'passed'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle), indent=2))
