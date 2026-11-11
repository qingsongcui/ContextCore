import json
import os
from pathlib import Path
import pytest
from app.integrations import cc_hook as hook


@pytest.fixture
def queue_home():
    root = Path.home()/'.contextcore'
    root.mkdir(mode=0o700)
    credentials = root/'credentials'; credentials.mkdir(mode=0o700)
    token = credentials/'codex.token'; token.write_text('synthetic-token'); token.chmod(0o600)
    return root


def test_coalesced_bounded_queue_and_delivery(queue_home, monkeypatch):
    calls=[]
    monkeypatch.setattr(hook, 'post_to_contextcore', lambda *a, **k: calls.append((a,k)))
    for i in range(1000):
        assert hook.enqueue_notification('codex','prompt',{'session_id':'synthetic','text':str(i),'context_request':True})
    assert calls == []
    slots = list((queue_home/'notifications').glob('codex-*.json'))
    assert len(slots) == hook.MAX_QUEUED_PER_HOST
    assert all(s.stat().st_size <= hook.MAX_NOTIFICATION_BYTES for s in slots)
    assert hook.drain_notifications() == hook.MAX_QUEUED_PER_HOST
    expected_texts = [str(i) for i in range(1000 - hook.MAX_QUEUED_PER_HOST, 1000)]
    assert [c[0][2]['text'] for c in calls] == expected_texts
    assert all(c[0][2]['context_request'] is False for c in calls)
    assert hook.drain_notifications() == 0


def test_busy_slot_drops_immediately(queue_home, monkeypatch):
    calls=[]
    monkeypatch.setattr(hook, 'post_to_contextcore', lambda *a, **k: calls.append((a,k)))
    assert hook.enqueue_notification('codex','prompt',{'session_id':'s1','text':'first'})
    assert hook.enqueue_notification('codex','prompt',{'session_id':'s1','text':'second'})
    slots = sorted((queue_home/'notifications').glob('codex-*.json'))
    assert len(slots) == 2
    assert hook.drain_notifications() == 2
    assert [c[0][2]['text'] for c in calls] == ['first', 'second']


def test_oversize_wrong_port_and_rotated_token(queue_home, monkeypatch):
    calls=[]; monkeypatch.setattr(hook,'post_to_contextcore',lambda *a,**k:calls.append(a))
    assert not hook.enqueue_notification('codex','prompt',{'text':'x'*hook.MAX_NOTIFICATION_BYTES})
    assert hook.enqueue_notification('codex','prompt',{},base_url='http://127.0.0.1:18420')
    assert hook.drain_notifications() == 0
    (queue_home/'credentials/codex.token').write_text('new-token')
    assert hook.drain_notifications('http://127.0.0.1:18420') == 0
    assert not calls


def test_symlink_slot_never_overwrites_other_file(queue_home, tmp_path):
    folder=queue_home/'notifications';folder.mkdir(mode=0o700)
    victim=tmp_path/'untouched'; victim.write_text('unchanged')
    (folder/'codex.json').symlink_to(victim)
    assert not hook.enqueue_notification('codex','prompt',{})
    assert victim.read_text() == 'unchanged'


def test_stale_or_malformed_slot_discarded(queue_home, monkeypatch):
    monkeypatch.setattr(hook,'post_to_contextcore',lambda *a,**k:pytest.fail('must not send stale data'))
    hook.enqueue_notification('codex','prompt',{})
    slot=next((queue_home/'notifications').glob('codex-*.json'));item=json.loads(slot.read_text());item['created_at']=0
    slot.write_text(json.dumps(item))
    assert hook.drain_notifications() == 0
    assert not slot.exists()
    hook.enqueue_notification('codex','prompt',{})
    slot2=next((queue_home/'notifications').glob('codex-*.json'))
    slot2.write_text('{broken')
    assert hook.drain_notifications() == 0
    assert not slot2.exists()


def test_existing_private_files_under_readable_product_directory(queue_home):
    queue_home.chmod(0o755)
    assert hook.enqueue_notification('codex','prompt',{})
    assert (queue_home/'notifications').stat().st_mode & 0o077 == 0


def test_cli_does_not_call_network_or_inject_response(queue_home, monkeypatch, capsys):
    import io
    import sys
    monkeypatch.setattr(hook,'post_to_contextcore',lambda *a,**k:pytest.fail('host callback must not use network'))
    monkeypatch.setattr(sys,'argv',['cc_hook.py','codex','prompt'])
    monkeypatch.setattr(sys,'stdin',io.StringIO('{"session_id":"synthetic","text":"hello"}'))
    assert hook.main() == 0
    assert capsys.readouterr().out == '{}'
    assert len(list((queue_home/'notifications').glob('codex-*.json'))) == 1


def test_process_exits_before_http_delivery(queue_home):
    import http.server
    import subprocess
    import sys
    import threading
    received=[]
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.end_headers()
        def log_message(self, *args):
            pass
    server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}'
    try:
        result=subprocess.run([sys.executable,hook.__file__,'--base-url',url,'codex','prompt'],
                              input='{"session_id":"synthetic","text":"local fixture"}',
                              capture_output=True,text=True,timeout=8)
        assert result.returncode == 0 and result.stdout == '{}'
        assert not received
        assert hook.drain_notifications(url) == 1
        assert received[0]['text'] == 'local fixture'
        assert received[0]['context_request'] is False
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
