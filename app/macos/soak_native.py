"""Hidden native page/resource observation against an owned synthetic service.

This does not cover visible-window, sleep/wake, credentials or real hosts.
Never launches or replaces the daily App. Output must be a new directory.
"""
from __future__ import annotations
import argparse
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.request


def process_commands():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,command='], capture_output=True, text=True, check=True)
    return {int(parts[0]): parts[1] for line in result.stdout.splitlines()
            if len(parts := line.strip().split(None, 1)) == 2}


def run(bundle, output, seconds, interval, terminate_webkit=False, language="zh", interrupt_service=False):
    manifest = json.loads((bundle/'Contents/Resources/desktop-release.json').read_text())
    output.mkdir(parents=True, exist_ok=False)
    home = output/'home'; home.mkdir()
    native = output/'native'; native.mkdir()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def request(path, body=None):
        headers = {'Origin': base}
        if body is not None:
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': request('/api/session')['csrf_token']})
        req = urllib.request.Request(base+path, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        with opener.open(req, timeout=10) as response: return json.load(response)
    state = {'status': 'starting', 'runner_pid': os.getpid(), 'port': port,
        'bundle': str(bundle), 'manifest': manifest, 'requested_seconds': seconds,
        'continuous_24h_observed': False, 'scope': 'hidden_native_synthetic_page_rotation',
        'native_binary_sha256': hashlib.sha256((bundle/'Contents/MacOS/Senrelay').read_bytes()).hexdigest(),
        'observer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    def save():
        temporary = output/'checkpoint.tmp'
        temporary.write_text(json.dumps(state, indent=2)+'\n'); temporary.replace(output/'checkpoint.json')
    service = launch = sampler = None
    app_pid = None
    binary = str(bundle/'Contents/MacOS/Senrelay')
    def owns_app():
        command = process_commands().get(app_pid, '')
        return command.startswith(binary+' ') and str(native) in command
    def stop(child):
        if child is not None:
            if child.poll() is None: child.terminate()
            try: child.wait(timeout=15)
            except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=5)
    save()
    with (output/'service.log').open('w') as log, (output/'native.log').open('w') as native_log, (output/'sampler.log').open('w') as sample_log:
        try:
            def start_service():
                return subprocess.Popen(
                    [str(bundle/'Contents/Resources/service/senrelay-service'),
                     '-m', 'app.cli', 'serve', '--data-dir', str(output/'data'), '--port', str(port)],
                    env={'HOME': str(home), 'USERPROFILE': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                         'LANG': 'en_US.UTF-8'}, stdout=log, stderr=log)
            service = start_service()
            state['service_pid'] = service.pid; save()
            for _ in range(180):
                if service.poll() is not None: raise RuntimeError('Owned service exited')
                try:
                    if request('/health')['ok']: break
                except (OSError, ValueError, TimeoutError, socket.timeout): pass
                time.sleep(.5)
            else: raise RuntimeError('Service startup timed out')
            assert request('/api/release')['release_id'] == manifest['release_id']
            assert request('/api/projects') == []
            project = request('/api/projects', {'name': 'Synthetic native stability'})
            seeded = []
            for n in range(100):
                item = request('/api/content', {'project_id': project['id'], 'title': f'Native stability {n}',
                    'content': f'NATIVE_SOAK_{n} '+('synthetic line '*256)})
                seeded.append(item)
            launch = subprocess.Popen(['/usr/bin/open', '-n', '-j', '-g', '-W', '-a', str(bundle), '--args',
                '--verify-ui', str(native), '--verify-port', str(port), '--verify-language', language,
                '--verify-soak-seconds', str(seconds), '--verify-soak-interval', str(interval),
                '--verify-home-dir', str(home), '--verify-data-dir', str(output/'data')] +
                (['--verify-scenario', 'actual-webkit-termination'] if terminate_webkit else
                 ['--verify-scenario', 'actual-service-interruption'] if interrupt_service else []), stdout=native_log, stderr=native_log)
            for _ in range(100):
                matches = [pid for pid, cmd in process_commands().items() if cmd.startswith(binary+' ') and str(native) in cmd]
                if len(matches) > 1: raise RuntimeError('Ambiguous QA App identity')
                if matches: app_pid = matches[0]; break
                if launch.poll() is not None: raise RuntimeError('Hidden App exited before PID discovery')
                time.sleep(.1)
            if app_pid is None: raise RuntimeError('Hidden App PID not found')
            state.update(app_pid=app_pid, status='running', started_at=time.time()); save()
            def start_sampler():
                return subprocess.Popen([sys.executable, str(Path(__file__).with_name('sample_resources.py')),
                    '--app-pid', str(app_pid), '--service-pid', str(service.pid), '--seconds', str(min(259200, seconds+120)),
                    '--interval', str(min(interval, 30)), '--isolated-launchservices', '--output', str(output/'resources.json')],
                    stdout=sample_log, stderr=sample_log)
            if not interrupt_service:
                sampler = start_sampler()
                state['sampler_pid'] = sampler.pid; save()
            deadline = time.monotonic()+seconds+120
            while launch.poll() is None:
                if interrupt_service and 'interrupted_service_pid' not in state and (native/'service-interruption-ready').exists():
                    if service.poll() is not None: raise RuntimeError('Owned service exited before injection')
                    state['interrupted_service_pid'] = service.pid; save()
                    service.kill(); service.wait(timeout=15)
                if interrupt_service and 'interrupted_service_pid' in state and 'restarted_service_pid' not in state:
                    if (native/'service-offline.json').exists():
                        service = start_service()
                        state.update(restarted_service_pid=service.pid, service_pid=service.pid)
                        sampler = start_sampler()
                        state['sampler_pid'] = sampler.pid; save()
                    elif time.monotonic() > deadline:
                        raise RuntimeError('Offline UI receipt timed out')
                    elif launch.poll() is None:
                        time.sleep(.2)
                        continue
                if terminate_webkit and 'terminated_webkit' not in state and (native/'webkit-termination-ready').exists():
                    from sample_resources import coalition_webkit, process_table, usage, launchservices_coalition
                    rows = process_table()
                    group = coalition_webkit(rows, app_pid)
                    targets = [item for item in group['processes'] if item['observed'] and
                        'com.apple.webkit.webcontent' in rows[item['pid']]['name'].lower()]
                    if not owns_app() or len(targets) != 1:
                        raise RuntimeError('Cannot uniquely attribute owned WebContent process')
                    target = targets[0]
                    current = usage(target['pid'])
                    if not current['observed'] or current['start_abstime'] != target['start_abstime'] or launchservices_coalition(target['pid']) != group['coalition_id']:
                        raise RuntimeError('WebContent identity changed before fault injection')
                    state['terminated_webkit'] = dict(target, coalition_id=group['coalition_id'], signal='SIGKILL')
                    save()
                    os.kill(target['pid'], signal.SIGKILL)
                if service.poll() is not None: raise RuntimeError('Owned service stopped')
                if sampler is not None and sampler.poll() is not None:
                    finished = native/'native-soak.json'
                    native_done = finished.exists() and json.loads(finished.read_text()).get('status') in ('completed', 'completed_with_gaps')
                    if sampler.returncode != 0 or not native_done or owns_app():
                        raise RuntimeError('Sampler stopped before App')
                if time.monotonic() > deadline: raise RuntimeError('Native observation timed out')
                receipt = native/'native-soak.json'
                if receipt.exists(): state['native'] = json.loads(receipt.read_text())
                state['last_checked_at'] = time.time(); save()
                time.sleep(1)
            if launch.returncode != 0: raise RuntimeError('LaunchServices returned failure')
            if sampler is None: raise RuntimeError('Sampler never started')
            if sampler.wait(timeout=40) != 0: raise RuntimeError('Resource sampler failed')
            assert json.loads((native/'native-ui.json').read_text())['ok'] is True
            if interrupt_service:
                assert state.get('interrupted_service_pid') != state.get('restarted_service_pid')
                assert (native/'service-recovered').exists()
                state['actual_service_recovered'] = True
                state['offline_ui'] = json.loads((native/'service-offline.json').read_text())
            if terminate_webkit:
                assert state.get('terminated_webkit'), 'No actual termination performed'
                error = json.loads((native/'error-state-0.json').read_text())
                assert error['status_visible'] and error['web_hidden'] and error['retry_visible']
                assert error['status_text'] == ('页面进程已退出。请重新连接，已保存的项目数据仍在本机。' if language == 'zh' else 'The workspace process stopped. Reconnect to reopen it. Saved project data remains on this Mac.')
                state['actual_webkit_recovered'] = True
            receipt = json.loads((native/'native-soak.json').read_text())
            if receipt.get('status') != 'completed' or receipt.get('observation_gaps') != 0:
                raise RuntimeError('Native observation was interrupted or contains gaps')
            if receipt.get('requested_seconds') != seconds or receipt.get('elapsed_seconds', 0) < seconds:
                raise RuntimeError('Native observation duration is incomplete')
            if seconds >= 86400 and receipt.get('continuous_24h_observed') is not True:
                raise RuntimeError('Native continuous 24h observation is incomplete')
            pages = set()
            with (native/'native-soak-pages.jsonl').open() as lines:
                for line in lines: pages.add(json.loads(line)['page']['page'])
            assert pages == {'projects','agents','usage','settings'}, pages
            assert request('/api/content/index?project_id='+project['id']+'&limit=1')['total'] == 100
            for item in seeded:
                restored = request('/api/content/'+item['id'])
                assert all(restored[key] == item[key] for key in ('content', 'title', 'version', 'project_id'))
            state['unchanged_seed_records'] = len(seeded)
            resources = json.loads((output/'resources.json').read_text())
            coverage = {'samples': 0, 'attributed_samples': 0, 'unknown_samples': 0,
                'unresolved_samples': 0, 'max_webkit_rss_bytes': None, 'max_webkit_physical_footprint_bytes': None}
            def collect(entry):
                coverage['samples'] += 1
                group = entry.get('webkit_coalition') or {}
                measured = group.get('rss_bytes') is not None and group.get('physical_footprint_bytes') is not None
                coverage['attributed_samples' if measured else 'unknown_samples'] += 1
                coverage['unresolved_samples'] += bool(group.get('unresolved_candidates'))
                if measured:
                    for field in ('rss_bytes', 'physical_footprint_bytes'):
                        key = 'max_webkit_'+field
                        coverage[key] = max(coverage[key] or 0, group[field])
            if resources.get('sample_file'):
                with Path(resources['sample_file']).open() as lines:
                    for line in lines: collect(json.loads(line))
            else:
                for entry in resources['samples']: collect(entry)
            assert coverage['samples'] == resources['sample_count']
            assert coverage['attributed_samples'] > 0, 'No WebKit resource attribution'
            state['resource_coverage'] = coverage
            state.update(status=receipt['status'], native=receipt, pages=sorted(pages),
                resource_samples=resources['sample_count'], continuous_24h_observed=receipt['continuous_24h_observed'])
        except BaseException as error:
            state.update(status='interrupted' if isinstance(error, KeyboardInterrupt) else 'failed', error=repr(error))
            raise
        finally:
            if app_pid is not None and owns_app(): os.kill(app_pid, signal.SIGTERM)
            stop(launch); stop(sampler); stop(service)
            state.update(finished_at=time.time(), owned_process_cleanup=True); save()
    print(json.dumps(state), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=86400)
    parser.add_argument('--interval', type=float, default=60)
    parser.add_argument('--terminate-webkit', action='store_true', help='Kill only the uniquely attributed hidden QA WebContent process, then verify reconnection')
    parser.add_argument('--language', choices=['zh', 'en'], default='zh')
    parser.add_argument('--interrupt-service', action='store_true', help='Crash only the owned isolated backend, observe offline UI, and restart it')
    args = parser.parse_args()
    if args.terminate_webkit and args.interrupt_service: parser.error('Select one fault scenario per run')
    if not 10 <= args.seconds <= 259080 or not 1 <= args.interval <= 60:
        parser.error('Require duration [10,259080], interval [1,60]')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    run(args.bundle.resolve(), args.output.resolve(), args.seconds, args.interval, args.terminate_webkit, args.language, args.interrupt_service)
