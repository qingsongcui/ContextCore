"""Verify the actual WKWebView, native menus, error states, and recovery flows without showing a QA window."""
import argparse
import http.server
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time

import httpx

here = Path(__file__).resolve().parent
repo_backend = here.parent / 'backend'
sys.path.insert(0, str(here))
from recovery_matrix import SCENARIOS, scenario_status_text, webview_fault_messages
parser = argparse.ArgumentParser()
parser.add_argument('--bundle', type=Path, default=here / 'dist/Senrelay.app')
parser.add_argument('--output-dir', type=Path, default=here / 'build/native-verification')
parser.add_argument('--language', choices=['zh', 'en', 'both'], default='both')
args = parser.parse_args()
bundle, out = args.bundle.resolve(), args.output_dir.resolve()
out.mkdir(parents=True, exist_ok=True)
expected = json.loads((bundle / 'Contents/Resources/desktop-release.json').read_text())
bundled_service = bundle / 'Contents/Resources/service/senrelay-service'
app_binary = bundle / 'Contents/MacOS/Senrelay'


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def wait_ready(client: httpx.Client, proc: subprocess.Popen, label: str) -> None:
    for _ in range(100):
        if proc.poll() is not None:
            raise RuntimeError(f'{label} stopped unexpectedly')
        try:
            client.get('/health').raise_for_status()
            return
        except httpx.TransportError:
            time.sleep(0.2)
    raise RuntimeError(f'{label} did not become ready')


def stop_bundled_service(data_dir: Path, home_dir: Path) -> None:
    subprocess.run(
        [str(bundled_service), '-m', 'app.cli', 'stop', '--data-dir', str(data_dir)],
        capture_output=True,
        text=True,
        timeout=20,
        env={'HOME': str(home_dir), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'},
    )


def assert_common_ui(receipt: dict, language: str) -> None:
    assert receipt['ok'] is True
    assert receipt['serviceOnline'] is True
    assert receipt['serviceLabel'] == ('本机服务运行中' if language == 'zh' else 'Local service running')
    assert receipt['language'] == receipt['native_language'] == language
    assert receipt['native_about_title'] == ('关于 Senrelay' if language == 'zh' else 'About Senrelay')
    assert ('编辑' if language == 'zh' else 'Edit') in receipt['native_menu_titles']
    assert ('窗口' if language == 'zh' else 'Window') in receipt['native_menu_titles']
    assert receipt['themeSwitchOk'] is True
    assert receipt['live_language_sync']['ok'] is True
    assert len(receipt['pagesVisited']) == 4
    assert all(page['heading'] and not page['hasErrorBanner'] for page in receipt['pagesVisited'])
    assert receipt['window_resize_checks']['min_800x560'] == {'width': 800, 'height': 560}
    assert receipt['window_resize_checks']['medium_1080x700'] == {'width': 1080, 'height': 700}
    assert receipt['window_resize_checks']['restored_1240x820'] == {'width': 1240, 'height': 820}


with tempfile.TemporaryDirectory(prefix='senrelay-native-') as temporary:
    scratch = Path(temporary).resolve()
    home = scratch / 'home'
    home.mkdir()
    port = free_port()
    with (out / 'service.log').open('w') as log:
        proc = subprocess.Popen(
            [str(bundled_service), '-m', 'app.cli', 'serve', '--data-dir', str(scratch / 'data'), '--port', str(port)],
            stdout=log,
            stderr=log,
            env={'HOME': str(home), 'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LANG': 'en_US.UTF-8'},
        )
        try:
            with httpx.Client(base_url=f'http://127.0.0.1:{port}', trust_env=False, timeout=2) as client:
                wait_ready(client, proc, 'Bundled native QA service')
                assert client.get('/api/release').json() == expected
                csrf = client.get('/api/session').json()['csrf_token']
                client.post(
                    '/api/projects',
                    json={'name': 'Planning workspace'},
                    headers={'X-CSRF-Token': csrf, 'Origin': f'http://127.0.0.1:{port}'},
                ).raise_for_status()
            results = []
            languages = ['zh', 'en'] if args.language == 'both' else [args.language]
            for language in languages:
                destination = out / language
                destination.mkdir(parents=True, exist_ok=True)
                result = subprocess.run(
                    [str(app_binary), '--verify-ui', str(destination), '--verify-port', str(port), '--verify-language', language],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                (destination / 'app.log').write_text(result.stdout + '\n' + result.stderr)
                if result.returncode:
                    raise RuntimeError(f'Native {language} verification failed; see {destination / "app.log"}')
                receipt = json.loads((destination / 'native-ui.json').read_text())
                assert_common_ui(receipt, language)
                faults = receipt['recovered_faults']
                assert len(faults) == 3
                expected_messages = webview_fault_messages(language)
                for idx, expected_msg in enumerate(expected_messages):
                    assert faults[idx]['status_visible'] is True
                    assert faults[idx]['web_hidden'] is True
                    assert faults[idx]['retry_visible'] is True
                    assert faults[idx]['status_text'] == expected_msg
                results.append({'language': language, 'native_ui': receipt})
        finally:
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise

    # Scenario 2: Service not running initially -> autostart bundled service, then persist after app quit.
    autostart_dir = out / 'scenario-autostart'
    autostart_dir.mkdir(parents=True, exist_ok=True)
    autostart_data = scratch / 'autostart-data'
    autostart_home = scratch / 'autostart-home'
    autostart_home.mkdir(parents=True, exist_ok=True)
    autostart_port = free_port()
    try:
        res_auto = subprocess.run(
            [
                str(app_binary),
                '--verify-ui',
                str(autostart_dir),
                '--verify-scenario',
                'autostart',
                '--verify-port',
                str(autostart_port),
                '--verify-data-dir',
                str(autostart_data),
                '--verify-home-dir',
                str(autostart_home),
                '--verify-language',
                'zh',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        (autostart_dir / 'app.log').write_text(res_auto.stdout + '\n' + res_auto.stderr)
        if res_auto.returncode:
            raise RuntimeError(f'Native autostart verification failed; see {autostart_dir / "app.log"}')
        auto_receipt = json.loads((autostart_dir / 'native-ui.json').read_text())
        assert auto_receipt['autostarted_service'] is True
        assert_common_ui(auto_receipt, 'zh')
        # Verify service stays running after app exits (quit/reopen persistence).
        with httpx.Client(base_url=f'http://127.0.0.1:{autostart_port}', trust_env=False, timeout=2) as client:
            assert client.get('/api/release').json() == expected
    finally:
        stop_bundled_service(autostart_data, autostart_home)

    # Scenario 3: Version mismatch -> in-app upgrade stops older service, starts bundled service, preserves data.
    mismatch_results = []
    for language in (['zh', 'en'] if args.language == 'both' else [args.language]):
        mismatch_dir = out / f'scenario-version-mismatch-{language}'
        mismatch_dir.mkdir(parents=True, exist_ok=True)
        mismatch_data = scratch / f'mismatch-data-{language}'
        mismatch_home = scratch / f'mismatch-home-{language}'
        mismatch_home.mkdir(parents=True, exist_ok=True)
        mismatch_port = free_port()
        with (mismatch_dir / 'legacy-service.log').open('w') as legacy_log:
            legacy_proc = subprocess.Popen(
                [sys.executable, '-m', 'app.cli', 'serve', '--data-dir', str(mismatch_data), '--port', str(mismatch_port)],
                stdout=legacy_log,
                stderr=legacy_log,
                env={
                    'HOME': str(mismatch_home),
                    'PATH': '/usr/bin:/bin:/usr/sbin:/sbin',
                    'LANG': 'en_US.UTF-8',
                    'PYTHONPATH': str(repo_backend),
                },
            )
            try:
                with httpx.Client(base_url=f'http://127.0.0.1:{mismatch_port}', trust_env=False, timeout=2) as client:
                    wait_ready(client, legacy_proc, 'Legacy service for version mismatch test')
                    legacy_release = client.get('/api/release').json()
                    assert legacy_release['release_id'] != expected['release_id']
                    csrf = client.get('/api/session').json()['csrf_token']
                    headers = {'X-CSRF-Token': csrf, 'Origin': f'http://127.0.0.1:{mismatch_port}'}
                    proj = client.post('/api/projects', json={'name': f'Upgrade preserved project ({language})'}, headers=headers)
                    proj.raise_for_status()
                    project_id = proj.json()['id']
                    client.post(
                        '/api/content',
                        json={
                            'project_id': project_id,
                            'kind': 'memory',
                            'category': 'background',
                            'title': 'Upgrade persistence check',
                            'content': 'Must remain intact after in-app service upgrade.',
                        },
                        headers=headers,
                    ).raise_for_status()
                    client.post(
                        '/api/manage/providers',
                        json={
                            'name': f'Upgrade preserved provider ({language})',
                            'protocol': 'openai',
                            'base_url': 'http://127.0.0.1:18999/v1',
                        },
                        headers=headers,
                    ).raise_for_status()
                # A broken replacement executable is rejected before stop. The
                # original service must still answer with the same saved data.
                rejected_dir = mismatch_dir/'rejected-update'
                rejected = subprocess.run([str(app_binary), '--verify-ui', str(rejected_dir),
                    '--verify-scenario', 'version-mismatch-recover',
                    '--verify-service-bin', str(scratch/'missing-update-service'),
                    '--verify-port', str(mismatch_port), '--verify-data-dir', str(mismatch_data),
                    '--verify-home-dir', str(mismatch_home), '--verify-language', language],
                    capture_output=True, text=True, timeout=30)
                assert rejected.returncode != 0
                assert legacy_proc.poll() is None
                rejected_receipt = json.loads((rejected_dir/'error-state.json').read_text())
                assert rejected_receipt['status_visible'] and rejected_receipt['retry_visible']
                with httpx.Client(base_url=f'http://127.0.0.1:{mismatch_port}', trust_env=False, timeout=2) as client:
                    assert client.get('/api/release').json() == legacy_release
                    assert any(item['id'] == project_id for item in client.get('/api/projects').json())
                for phase in ('preflight', 'stop'):
                    hung_dir = mismatch_dir/f'hung-{phase}'
                    helper_pid = scratch/f'hung-{phase}-{language}.pid'
                    helper = scratch/f'hung-{phase}-{language}.sh'
                    helper.write_text('#!/bin/sh\n' +
                        ('if [ "$3" = "check-install" ]; then exit 0; fi\n' if phase == 'stop' else '') +
                        'trap "" TERM\n' +
                        f'printf "%s" "$$" > "{helper_pid}"\n' +
                        'exec /bin/sleep 300\n')
                    helper.chmod(0o700)
                    try:
                        hung = subprocess.run([str(app_binary), '--verify-ui', str(hung_dir),
                            '--verify-scenario', 'version-mismatch-recover',
                            '--verify-service-bin', str(helper), '--verify-command-timeout', '2',
                            '--verify-port', str(mismatch_port), '--verify-data-dir', str(mismatch_data),
                            '--verify-home-dir', str(mismatch_home), '--verify-language', language],
                            capture_output=True, text=True, timeout=20)
                        assert hung.returncode != 0
                        assert legacy_proc.poll() is None
                        hung_receipt = json.loads((hung_dir/'error-state.json').read_text())
                        assert hung_receipt['status_visible'] and hung_receipt['retry_visible']
                        assert hung_receipt['upgrade_visible'] and hung_receipt['web_hidden']
                        assert helper_pid.is_file(), 'Timed-out helper never started'
                        try:
                            os.kill(int(helper_pid.read_text()), 0)
                        except ProcessLookupError:
                            pass
                        else:
                            raise RuntimeError('Timed-out owned helper is still running')
                        with httpx.Client(base_url=f'http://127.0.0.1:{mismatch_port}', trust_env=False, timeout=2) as client:
                            assert client.get('/api/release').json() == legacy_release
                            assert any(item['id'] == project_id for item in client.get('/api/projects').json())
                    finally:
                        # Failure cleanup is restricted to the synthetic helper
                        # PID and its exact injected command, never the service.
                        if helper_pid.is_file():
                            pid = int(helper_pid.read_text())
                            command = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'command='],
                                capture_output=True, text=True, timeout=5).stdout.strip()
                            if command == '/bin/sleep 300':
                                os.kill(pid, 9)
                res_mismatch = subprocess.run(
                    [
                        str(app_binary),
                        '--verify-ui',
                        str(mismatch_dir),
                        '--verify-scenario',
                        'version-mismatch-recover',
                        '--verify-port',
                        str(mismatch_port),
                        '--verify-data-dir',
                        str(mismatch_data),
                        '--verify-home-dir',
                        str(mismatch_home),
                        '--verify-language',
                        language,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                (mismatch_dir / 'app.log').write_text(res_mismatch.stdout + '\n' + res_mismatch.stderr)
                if res_mismatch.returncode:
                    raise RuntimeError(f'Native version mismatch recovery ({language}) failed; see {mismatch_dir / "app.log"}')
                mismatch_receipt = json.loads((mismatch_dir / 'native-ui.json').read_text())
                assert mismatch_receipt['upgraded_service'] is True
                assert_common_ui(mismatch_receipt, language)
                err_state = mismatch_receipt['recovered_faults'][0]
                assert err_state['status_visible'] is True
                assert err_state['web_hidden'] is True
                assert err_state['retry_visible'] is True
                assert err_state['upgrade_visible'] is True
                expected_upgrade_msg = scenario_status_text('version-mismatch-recover', language)
                assert err_state['status_text'] == expected_upgrade_msg
                assert err_state['upgrade_title'] == SCENARIOS['version-mismatch-recover']['upgrade_title'][language]
                # Verify upgraded service is now running expected release and preserved project + memory + provider.
                with httpx.Client(base_url=f'http://127.0.0.1:{mismatch_port}', trust_env=False, timeout=2) as client:
                    assert client.get('/api/release').json() == expected
                    projects = client.get('/api/projects').json()
                    assert any(p['id'] == project_id and p['name'] == f'Upgrade preserved project ({language})' for p in projects)
                    memories = client.get(f'/api/content/index?project_id={project_id}&limit=50&offset=0').json()
                    items = memories.get('items', [])
                    assert any(m['title'] == 'Upgrade persistence check' for m in items)
                    providers = client.get('/api/manage/providers').json()
                    provider_items = providers.get('items', providers) if isinstance(providers, dict) else providers
                    assert any(p['name'] == f'Upgrade preserved provider ({language})' for p in provider_items)
                mismatch_results.append({'language': language, 'error_state': err_state,
                    'preserved_project_id': project_id,
                    'rejected_update_preserved_original': True,
                    'bounded_helper_timeouts': ['preflight', 'stop']})
            finally:
                if legacy_proc.poll() is None:
                    legacy_proc.terminate()
                    try:
                        legacy_proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        legacy_proc.kill()
                stop_bundled_service(mismatch_data, mismatch_home)

    # Scenario 4: Service start failure -> shows startup failure UI, then recovers on Reconnect.
    start_fail_dir = out / 'scenario-service-start-failure'
    start_fail_dir.mkdir(parents=True, exist_ok=True)
    start_fail_data = scratch / 'start-fail-data'
    start_fail_home = scratch / 'start-fail-home'
    start_fail_home.mkdir(parents=True, exist_ok=True)
    start_fail_port = free_port()
    try:
        res_start_fail = subprocess.run(
            [
                str(app_binary),
                '--verify-ui',
                str(start_fail_dir),
                '--verify-scenario',
                'service-start-failure-recover',
                '--verify-service-bin',
                str(scratch / 'missing-senrelay-service'),
                '--verify-port',
                str(start_fail_port),
                '--verify-data-dir',
                str(start_fail_data),
                '--verify-home-dir',
                str(start_fail_home),
                '--verify-language',
                'zh',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        (start_fail_dir / 'app.log').write_text(res_start_fail.stdout + '\n' + res_start_fail.stderr)
        if res_start_fail.returncode:
            raise RuntimeError(f'Native service-start-failure verification failed; see {start_fail_dir / "app.log"}')
        start_fail_receipt = json.loads((start_fail_dir / 'native-ui.json').read_text())
        assert_common_ui(start_fail_receipt, 'zh')
        start_err = start_fail_receipt['recovered_faults'][0]
        assert start_err['status_visible'] is True
        assert start_err['retry_visible'] is True
        assert start_err['upgrade_visible'] is False
        assert start_err['status_text'] == scenario_status_text('service-start-failure-recover', 'zh')
    finally:
        stop_bundled_service(start_fail_data, start_fail_home)

    # Scenario 5: Unreachable port / readiness timeout -> shows readiness failure UI, then recovers on Reconnect.
    unreachable_dir = out / 'scenario-unreachable-port'
    unreachable_dir.mkdir(parents=True, exist_ok=True)
    unreachable_data = scratch / 'unreachable-data'
    unreachable_home = scratch / 'unreachable-home'
    unreachable_home.mkdir(parents=True, exist_ok=True)
    unreachable_port = free_port()
    try:
        res_unreachable = subprocess.run(
            [
                str(app_binary),
                '--verify-ui',
                str(unreachable_dir),
                '--verify-scenario',
                'unreachable-port-recover',
                '--verify-max-attempts',
                '2',
                '--verify-port',
                str(unreachable_port),
                '--verify-data-dir',
                str(unreachable_data),
                '--verify-home-dir',
                str(unreachable_home),
                '--verify-language',
                'en',
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        (unreachable_dir / 'app.log').write_text(res_unreachable.stdout + '\n' + res_unreachable.stderr)
        if res_unreachable.returncode:
            raise RuntimeError(f'Native unreachable-port verification failed; see {unreachable_dir / "app.log"}')
        unreachable_receipt = json.loads((unreachable_dir / 'native-ui.json').read_text())
        assert_common_ui(unreachable_receipt, 'en')
        unreachable_err = unreachable_receipt['recovered_faults'][0]
        assert unreachable_err['status_visible'] is True
        assert unreachable_err['retry_visible'] is True
        assert unreachable_err['upgrade_visible'] is False
        assert unreachable_err['status_text'] == scenario_status_text('unreachable-port-recover', 'en')
    finally:
        stop_bundled_service(unreachable_data, unreachable_home)

    # A real unrelated HTTP listener must not be mislabeled as an old service
    # or offered an upgrade/stop action.
    class ForeignHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'{"foreign_listener": true}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *arguments):
            pass
    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), ForeignHandler) as foreign:
        foreign_thread = threading.Thread(target=foreign.serve_forever, daemon=True)
        foreign_thread.start()
        foreign_dir = out/'scenario-port-conflict'
        foreign_data = scratch/'foreign-data'
        foreign_home = scratch/'foreign-home'
        try:
            result = subprocess.run([str(app_binary), '--verify-ui', str(foreign_dir),
                '--verify-port', str(foreign.server_port), '--verify-data-dir', str(foreign_data),
                '--verify-home-dir', str(foreign_home), '--verify-language', 'en',
                '--verify-scenario', 'port-conflict'], capture_output=True, text=True, timeout=30)
            assert result.returncode != 0
            foreign_receipt = json.loads((foreign_dir/'error-state.json').read_text())
            assert foreign_receipt['status_visible'] and foreign_receipt['retry_visible']
            assert foreign_receipt['web_hidden'] and not foreign_receipt['upgrade_visible']
            assert 'Another program' in foreign_receipt['status_text']
            assert not (foreign_data/'contextcore.db').exists()
            assert not (foreign_data/'service.pid').exists()
            with httpx.Client(trust_env=False) as client:
                assert client.get(f'http://127.0.0.1:{foreign.server_port}/').json()['foreign_listener']
        finally:
            foreign.shutdown()
            foreign_thread.join(timeout=5)


    report = {
        'release': expected,
        'results': results,
        'scenarios': {
            'autostart_and_persist_on_quit': {
                'ok': True,
                'autostarted_service': auto_receipt['autostarted_service'],
                'persisted_after_quit': True,
            },
            'version_mismatch_in_app_upgrade': mismatch_results,
            'service_start_failure_recover': start_err,
            'unreachable_port_recover': unreachable_err,
            'foreign_port_conflict_preserves_listener': foreign_receipt,
        },
        'hidden_verification': True,
        'synthetic_only': True,
    }
    (out / 'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
