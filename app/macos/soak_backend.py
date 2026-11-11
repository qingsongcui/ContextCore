"""Exercise a frozen backend on synthetic data; never start a daily service.

This is backend-only evidence. App/WebView, sleep/wake and real hosts remain
separate acceptance. Interrupted/suspended observation cannot pass as 24h.
"""
from __future__ import annotations
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from sample_resources import usage


def run(bundle: Path, output: Path, seconds: float, interval: float):
    output.mkdir(parents=True, exist_ok=False)
    home = output / 'home'
    home.mkdir()
    data = output / 'data'
    expected = json.loads((bundle / 'Contents/Resources/desktop-release.json').read_text())
    binary = bundle / 'Contents/Resources/service/senrelay-service'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    state = {'status': 'starting', 'scope': 'frozen_backend_synthetic_only',
             'release_id': expected['release_id'], 'runner_pid': os.getpid(),
             'port': port, 'duration_seconds': seconds, 'interval_seconds': interval,
             'whole_app_webview_verified': False, 'cycles': 0, 'observation_gaps': [],
             'continuous_24h_observed': False,
             'observed_seconds': 0, 'budgets': {'rss_growth_bytes': 128*1024*1024,
             'consecutive_growth_samples': 10, 'latency_ms': 5000,
             'note': 'Exploratory QA budgets, not a published product guarantee'}}
    def save():
        tmp = output / 'checkpoint.tmp'
        tmp.write_text(json.dumps(state, indent=2) + '\n')
        tmp.replace(output / 'checkpoint.json')
    save()
    def request(path, method='GET', body=None, expected_status=200):
        headers = {'Origin': base}
        if method != 'GET':
            headers['X-CSRF-Token'] = request('/api/session')['csrf_token']
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(base+path, method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with opener.open(req, timeout=10) as response:
                status, result = response.status, json.load(response)
        except urllib.error.HTTPError as error:
            status, result = error.code, json.load(error)
        if status != expected_status:
            raise RuntimeError(f'{method} {path}: expected {expected_status}, got {status}: {result}')
        return result
    env = {'HOME': str(home), 'USERPROFILE': str(home),
           'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'}
    proc = None
    with (output/'service.log').open('w') as log, (output/'samples.jsonl').open('w', buffering=1) as samples:
        def start():
            # Match Senrelay.swift / verify_bundle: packaged entry is `-m app.cli serve`.
            child = subprocess.Popen(
                [str(binary), '-m', 'app.cli', 'serve', '--data-dir', str(data), '--port', str(port)],
                env=env, stdout=log, stderr=log)
            try:
                # Cold PyInstaller start can exceed 20s under load; App probe uses longer retries.
                for _ in range(180):
                    if child.poll() is not None:
                        raise RuntimeError('Isolated service exited during startup: ' + (output/'service.log').read_text()[-4000:])
                    try:
                        if request('/health')['ok']:
                            assert request('/api/release')['release_id'] == expected['release_id']
                            return child
                    except (urllib.error.URLError, TimeoutError, socket.timeout):
                        pass
                    time.sleep(.5)
                raise RuntimeError('Isolated service did not become ready')
            except BaseException:
                if child.poll() is None: child.terminate()
                child.wait(timeout=15)
                raise
        try:
            proc = start()
            state['service_pid'] = proc.pid
            assert request('/api/projects') == [], 'Fresh QA database must be empty'
            project = request('/api/projects', 'POST', {'name': 'Synthetic stability project'})
            ids = []
            for n in range(100):
                item = request('/api/content', 'POST', {'project_id': project['id'],
                    'title': f'Stability record {n}', 'content': f'SOAK_REFERENCE_{n} '+('synthetic line '*256)})
                ids.append(item['id'])
            versions = dict.fromkeys(ids, 1)
            baseline = usage(proc.pid)
            assert baseline['observed']
            state.update(status='running', started_at=time.time(), baseline=baseline,
                         project_id=project['id'], service_pid=proc.pid)
            started = time.monotonic()
            previous_wall = time.time()
            consecutive_growth = 0
            last_deleted = None
            save()
            while time.monotonic() - started < seconds:
                wall = time.time()
                gap = wall - previous_wall
                previous_wall = wall
                if gap > max(3*interval, 10):
                    state['observation_gaps'].append({'at': wall, 'seconds': gap})
                else:
                    state['observed_seconds'] += max(0, gap)
                t0 = time.monotonic()
                if proc.poll() is not None: raise RuntimeError('Service stopped during observation')
                assert request('/api/release')['release_id'] == expected['release_id']
                n = state['cycles']
                cid = ids[n % len(ids)]
                item = request('/api/content/'+cid, 'PATCH', {'tags': ['soak', str(n % 10)], 'expected_version': versions[cid]})
                versions[cid] += 1
                assert item['version'] == versions[cid]
                search = request('/api/retrieval/search', 'POST', {'query': f'SOAK_REFERENCE_{n%100}', 'mode': 'lexical'})
                assert any(hit['id'] == cid for hit in search['items'])
                request('/api/usage/summary')
                request('/api/processing/jobs?limit=20')
                assert request('/api/content/index?project_id='+project['id']+'&limit=1')['total'] == 100
                # A changing unique payload avoids confusing distinct fixtures with replay.
                payload = {'project_id': project['id'], 'title': f'Temporary {n}', 'content': f'Delete cycle {n}'}
                temporary = request('/api/content', 'POST', payload)
                request('/api/content/'+temporary['id']+'?expected_version=1', 'DELETE')
                request('/api/content', 'POST', payload, expected_status=410)
                last_deleted = temporary['id']
                measured = usage(proc.pid)
                assert measured['observed'] and measured['start_abstime'] == baseline['start_abstime']
                latency = (time.monotonic()-t0)*1000
                consecutive_growth = consecutive_growth+1 if measured['rss_bytes']-baseline['rss_bytes'] > state['budgets']['rss_growth_bytes'] else 0
                disk = sum(p.stat().st_size for p in data.rglob('*') if p.is_file() and not p.is_symlink())
                samples.write(json.dumps({'cycle': n, 'wall_time': wall, 'elapsed_seconds': time.monotonic()-started,
                    'process': measured, 'cycle_latency_ms': latency, 'data_bytes': disk})+'\n')
                state.update(cycles=n+1, last_sample_at=wall, last_sample=measured,
                             elapsed_seconds=time.monotonic()-started)
                if consecutive_growth >= 10: raise RuntimeError('RSS growth budget exceeded for ten consecutive samples')
                if latency > state['budgets']['latency_ms']:
                    raise RuntimeError('Workload latency budget exceeded')
                save()
                # Bounded sleeps allow termination and live progress readback.
                deadline = min(started+seconds, time.monotonic()+interval)
                while time.monotonic() < deadline: time.sleep(max(0, min(1, deadline-time.monotonic())))
            state['elapsed_seconds'] = time.monotonic()-started
            if state['observation_gaps']:
                raise RuntimeError('Observation interrupted by a wall-clock gap')
            if state['elapsed_seconds'] < seconds or not state['cycles']:
                raise RuntimeError('Requested observation duration is incomplete')
            # Crash only the process this runner created; WAL recovery must keep
            # all acknowledged writes and deletion tombstones.
            proc.kill(); proc.wait(timeout=15)
            proc = start()
            state['service_pid'] = proc.pid
            assert request('/api/content/index?project_id='+project['id']+'&limit=1')['total'] == 100
            for cid in ids: assert request('/api/content/'+cid)['version'] == versions[cid]
            if last_deleted: request('/api/content/'+last_deleted, expected_status=404)
            state['restart_persistence'] = True
            state['crash_recovery_persistence'] = True
            state['status'] = 'completed'
            state['continuous_24h_observed'] = seconds >= 86400 and not state['observation_gaps'] and state['observed_seconds'] >= seconds-interval*2
        except BaseException as error:
            state.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', error=str(error))
            raise
        finally:
            if proc is not None:
                if proc.poll() is None: proc.terminate()
                try: proc.wait(timeout=15)
                except subprocess.TimeoutExpired: proc.kill(); proc.wait(timeout=5)
            state['service_stopped'] = True
            state['finished_at'] = time.time()
            save()
    print(json.dumps({k: state.get(k) for k in ('status','cycles','continuous_24h_observed','restart_persistence')}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=86400)
    parser.add_argument('--interval', type=float, default=60)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 259200 or not .1 <= args.interval <= 60:
        parser.error('Duration must be 1–259200 seconds, interval 0.1–60 seconds')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt('Termination requested')))
    run(args.bundle.resolve(), args.output.resolve(), args.seconds, args.interval)
