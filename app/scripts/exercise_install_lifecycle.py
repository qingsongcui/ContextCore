#!/usr/bin/env python3
"""Exercise an isolated candidate on synthetic data, never launchctl or daily data.

Use --backend/--frontend for current source, or --bundle for a frozen candidate.
The output directory must not exist. This is not clean-Mac/native/24h evidence.
"""
from __future__ import annotations

import argparse
import http.cookiejar
import importlib.util
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request


def load_prepare():
    spec = importlib.util.spec_from_file_location('lifecycle_prepare', Path(__file__).with_name('prepare_release.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def run(output: Path, *, backend: Path | None = None, frontend: Path | None = None,
        bundle: Path | None = None) -> dict:
    output = output.expanduser().resolve()
    if output.exists():
        raise ValueError('Output must be a new isolated directory')
    forbidden = [Path('/Applications'), Path.home()/'Applications',
                 Path.home()/'Library/Application Support/ContextCore']
    if any(output.is_relative_to(path.resolve()) for path in forbidden):
        raise ValueError('Output must not be under daily installation or data paths')
    if bundle is None and (backend is None or frontend is None):
        raise ValueError('Select a bundle or explicit backend and frontend')
    output.mkdir(parents=True)
    home = output/'home'
    home.mkdir()
    data = output/'data'
    prepare = load_prepare()
    if bundle:
        bundle = bundle.expanduser().resolve()
        expected = json.loads((bundle/'Contents/Resources/desktop-release.json').read_text())
        binary = bundle/'Contents/Resources/service/senrelay-service'
        prefix = [str(binary), '-m', 'app.cli']
        record = None
    else:
        record = prepare.prepare(backend, frontend, data)
        prepare.activate_release(record, data)
        expected = {key: record[key] for key in ('release_id', 'api_contract', 'frontend_sha256')}
        prefix = [sys.executable, '-I', '-B', record['launcher_path'], '--manifest',
                  str(Path(record['backend_dir']).parent/'manifest.json'), '--', '-m', 'app.cli']
    env = {'HOME': str(home), 'USERPROFILE': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
           'LANG': 'en_US.UTF-8', 'PYTHONUTF8': '1'}
    port = free_port()
    base = f'http://127.0.0.1:{port}'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    state = {'status': 'running', 'scope': 'isolated_synthetic_candidate', 'expected_release': expected,
             'checks': [], 'continuous_24h_observed': False,
             'receipt_gaps': ['clean Mac/Gatekeeper/install-in-place', 'live credentials and hosts',
                              'native App/WebView UI', 'sleep/wake/reboot', '24h wallclock observation']}
    def save():
        temporary = output/'receipt.tmp'
        temporary.write_text(json.dumps(state, indent=2)+'\n')
        temporary.replace(output/'receipt.json')
    def request(path, method='GET', body=None, status=200):
        headers = {'Origin': base}
        if method != 'GET':
            headers['X-CSRF-Token'] = request('/api/session')['csrf_token']
        if body is not None:
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(base+path, method=method, headers=headers,
            data=json.dumps(body).encode() if body is not None else None)
        try:
            with opener.open(req, timeout=5) as response:
                code, payload = response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            code, payload = exc.code, json.load(exc)
        if code != status:
            raise RuntimeError(f'{method} {path}: {code}, expected {status}: {payload}')
        return payload
    def command(selected_data=data, selected_port=port, selected_record=record):
        selected_prefix = prefix
        if selected_record:
            selected_prefix = [sys.executable, '-I', '-B', selected_record['launcher_path'], '--manifest',
                str(Path(selected_record['backend_dir']).parent/'manifest.json'), '--', '-m', 'app.cli']
        args = [*selected_prefix, 'serve', '--data-dir', str(selected_data), '--port', str(selected_port)]
        if selected_record:
            args += ['--frontend-dir', selected_record['frontend_dir']]
        return args
    child = None
    def stop(crash=False):
        nonlocal child
        if child is not None:
            if child.poll() is None:
                child.kill() if crash else child.terminate()
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
                raise RuntimeError('Graceful isolated shutdown timed out')
            child = None
    save()
    try:
        with (output/'service.log').open('w') as log:
            def start():
                nonlocal child
                child = subprocess.Popen(command(), env=env, stdout=log, stderr=log)
                deadline = time.monotonic()+90
                while time.monotonic() < deadline:
                    if child.poll() is not None:
                        raise RuntimeError('Candidate exited before readiness; see service.log')
                    try:
                        release = request('/api/release')
                        if release != expected:
                            raise RuntimeError(f'Candidate version mismatch: {release}')
                        return
                    except (urllib.error.URLError, TimeoutError):
                        time.sleep(.1)
                raise RuntimeError('Candidate readiness timed out')
            start()
            if request('/api/projects') != []:
                raise RuntimeError('Isolated candidate did not start with an empty database')
            project = request('/api/projects', 'POST', {'name': 'Synthetic lifecycle'})
            memory = request('/api/content', 'POST', {'project_id': project['id'],
                'title': 'Synthetic lifecycle memory', 'content': 'KEEP_SYNTHETIC_LIFECYCLE'})
            def readback():
                restored = request('/api/content/'+memory['id'])
                if any(restored[key] != memory[key] for key in ('content', 'title', 'version', 'project_id')):
                    raise RuntimeError('Acknowledged candidate memory changed across restart')
                if not any(item['id'] == project['id'] for item in request('/api/projects')):
                    raise RuntimeError('Acknowledged project disappeared across restart')
            state['checks'].append('fresh_candidate_write_read')
            # Second launch cannot steal the same data tree or PID metadata.
            pid_before = (data/'service.pid').read_bytes()
            duplicate = subprocess.run(command(selected_port=free_port()), env=env,
                stdout=log, stderr=log, timeout=90)
            if duplicate.returncode == 0 or (data/'service.pid').read_bytes() != pid_before:
                raise RuntimeError('Duplicate service overwrote live ownership metadata')
            readback()
            state['checks'].append('duplicate_service_rejected_preserving_owner')
            stop()
            start()
            readback()
            state['checks'].append('same_candidate_graceful_restart_persistence')
            stop(crash=True)
            start()
            readback()
            state['checks'].append('same_candidate_crash_recovery_persistence')
            conflict_data = output/'conflict-data'
            conflict_record = None
            if record:
                conflict_record = prepare.prepare(backend, frontend, conflict_data)
                prepare.activate_release(conflict_record, conflict_data)
            stop()
            with socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(('127.0.0.1', port))
                occupied.listen()
                conflict = subprocess.run(command(conflict_data, port, conflict_record), env=env,
                    stdout=log, stderr=log, timeout=90)
                if conflict.returncode == 0:
                    raise RuntimeError('Occupied port was accepted')
                if (conflict_data/'contextcore.db').exists() or (conflict_data/'service.pid').exists():
                    raise RuntimeError('Occupied-port candidate touched database or ownership metadata')
            start()
            readback()
            state['checks'].append('port_conflict_rejected_then_restart')
            stop()
            if record:
                active = (data/'current-release.json').read_bytes()
                invalid = {**record, 'api_contract': 999}
                try:
                    prepare.activate_release(invalid, data)
                except ValueError:
                    pass
                else:
                    raise RuntimeError('Invalid update was activated')
                if (data/'current-release.json').read_bytes() != active:
                    raise RuntimeError('Rejected update changed active selection')
                start()
                readback()
                state['checks'].append('failed_update_original_still_runnable')
                stop()
            # Exercise the actual uninstall implementation against an owned,
            # unregistered synthetic plist; launchctl is never called.
            lifecycle_spec = importlib.util.spec_from_file_location('lifecycle_check',
                Path(__file__).with_name('check_macos_install_lifecycle.py'))
            lifecycle = importlib.util.module_from_spec(lifecycle_spec)
            lifecycle_spec.loader.exec_module(lifecycle)
            lifecycle.simulate_uninstall_preserves_data(data)
            start()
            readback()
            state['checks'].append('uninstall_preserves_data_simulated_no_launchctl')
            stop()
        state['status'] = 'completed'
    except BaseException as exc:
        state.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed', error=repr(exc))
        raise
    finally:
        try:
            stop()
        finally:
            state['owned_service_stopped'] = child is None
            save()
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--bundle', type=Path)
    parser.add_argument('--backend', type=Path)
    parser.add_argument('--frontend', type=Path)
    args = parser.parse_args(argv)
    if bool(args.bundle) == bool(args.backend or args.frontend) or (not args.bundle and not (args.backend and args.frontend)):
        parser.error('Select --bundle OR both --backend and --frontend')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt('Termination requested')))
    try:
        print(json.dumps(run(args.output, bundle=args.bundle, backend=args.backend, frontend=args.frontend), indent=2))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'Lifecycle acceptance failed: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
