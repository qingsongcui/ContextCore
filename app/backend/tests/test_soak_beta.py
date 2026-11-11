"""Isolation and growth contracts for the Beta soak harness."""
import importlib.util
from pathlib import Path
import json
import sys

spec = importlib.util.spec_from_file_location(
    'soak_beta', Path(__file__).resolve().parents[2] / 'macos' / 'soak_beta.py')
soak = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = soak
spec.loader.exec_module(soak)


def _bundle(tmp_path: Path, **build):
    app = tmp_path / 'isolated' / 'Senrelay.app'
    binary = app / 'Contents' / 'MacOS' / 'Senrelay'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'fake')
    receipt = {
        'architecture': 'arm64', 'signature': 'ad-hoc', 'notarized': False,
        'release_id': 'test', **build,
    }
    (app.parent / 'BUILD.json').write_text(json.dumps(receipt) + '\n')
    return app


def test_refuses_daily_and_default_dist(tmp_path, monkeypatch):
    repo = tmp_path / 'ContextCore'
    dist = repo / 'app' / 'macos' / 'dist'
    app = dist / 'Senrelay.app'
    binary = app / 'Contents' / 'MacOS' / 'Senrelay'
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b'x')
    (dist / 'BUILD.json').write_text(json.dumps({
        'architecture': 'arm64', 'signature': 'ad-hoc', 'notarized': False,
    }))
    policy = soak.IsolationPolicy(forbidden_prefixes=(dist,))
    evidence = tmp_path / '验收证据'
    errors = soak.isolation_errors(app, evidence / 'run', policy, evidence)
    assert any('daily or default path' in item for item in errors)


def test_refuses_missing_build_json_and_wrong_arch(tmp_path):
    app = _bundle(tmp_path, architecture='x86_64')
    evidence = tmp_path / '验收证据'
    policy = soak.default_policy(tmp_path / 'ContextCore')
    errors = soak.isolation_errors(app, evidence / 'run', policy, evidence)
    assert any('arm64' in item for item in errors)


def test_refuses_bundle_whose_binary_is_already_running(tmp_path, monkeypatch):
    app = _bundle(tmp_path)
    evidence = tmp_path / '验收证据'
    policy = soak.IsolationPolicy(forbidden_prefixes=(tmp_path / 'Applications',))
    monkeypatch.setattr(soak, 'binary_already_running', lambda bundle: True)
    errors = soak.isolation_errors(app, evidence / 'run', policy, evidence)
    assert any('already running' in item for item in errors)


def test_accepts_isolated_adhoc_arm64_bundle(tmp_path):
    app = _bundle(tmp_path)
    evidence = tmp_path / '验收证据'
    policy = soak.IsolationPolicy(forbidden_prefixes=(tmp_path / 'Applications',))
    errors = soak.isolation_errors(app, evidence / 'run', policy, evidence)
    assert errors == []


def test_rss_growth_fails_after_consecutive_samples():
    budgets = soak.GrowthBudgets(rss_growth_bytes=100, consecutive_rss_samples=3)
    samples = []
    for n in range(5):
        rss = 50 if n == 0 else 200
        samples.append({
            'elapsed_seconds': float(n),
            'app': {'observed': True, 'rss_bytes': rss, 'user_cpu_ns': 0, 'system_cpu_ns': 0},
            'service': {'observed': True, 'rss_bytes': 10, 'user_cpu_ns': 0, 'system_cpu_ns': 0},
        })
    assert soak.growth_failure(samples, budgets).startswith('app RSS exceeded')


def test_stable_rss_does_not_fail():
    budgets = soak.GrowthBudgets(rss_growth_bytes=100, consecutive_rss_samples=3)
    samples = [{
        'elapsed_seconds': float(n),
        'app': {'observed': True, 'rss_bytes': 50, 'user_cpu_ns': n, 'system_cpu_ns': 0},
        'service': {'observed': True, 'rss_bytes': 10, 'user_cpu_ns': n, 'system_cpu_ns': 0},
    } for n in range(6)]
    assert soak.growth_failure(samples, budgets) is None


def test_cpu_spin_on_both_processes_fails():
    budgets = soak.GrowthBudgets(cpu_busy_ratio=0.8, consecutive_cpu_samples=3)
    samples = []
    cpu = 0
    for n in range(5):
        samples.append({
            'elapsed_seconds': float(n),
            'app': {'observed': True, 'rss_bytes': 10, 'user_cpu_ns': cpu, 'system_cpu_ns': 0},
            'service': {'observed': True, 'rss_bytes': 10, 'user_cpu_ns': cpu, 'system_cpu_ns': 0},
        })
        cpu += 1_000_000_000
    assert 'CPU stayed' in soak.growth_failure(samples, budgets)


def test_reads_bounded_json_when_jsonl_absent(tmp_path):
    native_run = tmp_path / 'native-run'
    native_run.mkdir()
    (native_run / 'resources.json').write_text(json.dumps({
        'samples': [{'elapsed_seconds': 0, 'app': {'observed': True, 'rss_bytes': 1}}],
    }))
    samples = soak.read_resource_samples(native_run)
    assert samples[0]['app']['rss_bytes'] == 1


def test_unknown_rss_is_not_treated_as_zero():
    budgets = soak.GrowthBudgets(rss_growth_bytes=1, consecutive_rss_samples=2)
    samples = [
        {'elapsed_seconds': 0, 'app': {'observed': False}, 'service': {'observed': True, 'rss_bytes': 10}},
        {'elapsed_seconds': 1, 'app': {'observed': False}, 'service': {'observed': True, 'rss_bytes': 10_000}},
    ]
    assert 'unavailable' in soak.growth_failure(samples, budgets)


def _completion(tmp_path, seconds=30):
    native = {'status': 'completed', 'requested_seconds': seconds,
              'elapsed_seconds': seconds, 'observation_gaps': 0,
              'continuous_24h_observed': seconds >= 86400}
    receipt = {'status': 'completed', 'requested_seconds': seconds,
               'native': native, 'owned_process_cleanup': True,
               'continuous_24h_observed': seconds >= 86400}
    (tmp_path/'checkpoint.json').write_text(json.dumps(receipt))
    samples = [{'elapsed_seconds': n} for n in range(0, seconds + 1, 5)]
    return receipt, samples


def test_short_completed_smoke_is_not_continuous_24h(tmp_path):
    receipt, samples = _completion(tmp_path)
    assert soak.completion_failure(tmp_path, samples, 30, 5, 30) is None
    assert receipt['continuous_24h_observed'] is False
    assert soak.completion_failure(tmp_path, samples, 86400, 5, 30) is not None


def test_zero_exit_without_receipt_cannot_pass(tmp_path):
    assert soak.completion_failure(tmp_path, [], 30, 5, 30) is not None


def test_incomplete_gapped_or_unclean_observation_cannot_pass(tmp_path):
    for field, value in [('status', 'interrupted'), ('owned_process_cleanup', False),
                         ('requested_seconds', 10)]:
        receipt, samples = _completion(tmp_path)
        receipt[field] = value
        (tmp_path/'checkpoint.json').write_text(json.dumps(receipt))
        assert soak.completion_failure(tmp_path, samples, 30, 5, 30) is not None
    receipt, samples = _completion(tmp_path)
    receipt['native']['observation_gaps'] = 1
    (tmp_path/'checkpoint.json').write_text(json.dumps(receipt))
    assert soak.completion_failure(tmp_path, samples, 30, 5, 30) is not None
    _completion(tmp_path)
    assert soak.completion_failure(tmp_path, samples, 30, 5, 29) is not None
    assert soak.completion_failure(tmp_path, [samples[0], samples[-1]], 30, 5, 30) is not None


def test_single_child_cpu_spin_is_failure():
    samples = [{'elapsed_seconds': n,
        'app': {'observed': True, 'rss_bytes': 10, 'user_cpu_ns': n*1_000_000_000, 'system_cpu_ns': 0},
        'service': {'observed': True, 'rss_bytes': 10, 'user_cpu_ns': 0, 'system_cpu_ns': 0}}
        for n in range(5)]
    assert 'CPU stayed' in soak.growth_failure(
        samples, soak.GrowthBudgets(consecutive_cpu_samples=3))


def test_process_inspection_failure_refuses_candidate(tmp_path, monkeypatch):
    import subprocess
    def unavailable(*args, **kwargs):
        raise subprocess.TimeoutExpired('ps', 5)
    monkeypatch.setattr(soak.subprocess, 'run', unavailable)
    bundle = _bundle(tmp_path)
    root = tmp_path/'evidence'
    errors = soak.isolation_errors(bundle, root/'run',
        soak.IsolationPolicy(forbidden_prefixes=()), root)
    assert any('Cannot establish' in error for error in errors)


def test_zero_exit_child_with_missing_evidence_fails_closed(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(soak, 'isolation_errors', lambda *args: [])
    class Exited:
        pid = 12345
        def poll(self):
            return 0
    monkeypatch.setattr(soak.subprocess, 'Popen', lambda *args, **kwargs: Exited())
    output = tmp_path/'output'
    assert soak.run(bundle, output, 30, 5, soak.GrowthBudgets()) == 1
    receipt = json.loads((output/'soak-beta.json').read_text())
    assert receipt['status'] == 'failed_incomplete'
    assert receipt['continuous_24h_observed'] is False


def test_interruption_stops_owned_child_and_never_passes(tmp_path, monkeypatch):
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(soak, 'isolation_errors', lambda *args: [])
    class Interrupted:
        pid = 12345
        stopped = False
        calls = 0
        def poll(self):
            self.calls += 1
            if self.calls == 1:
                raise KeyboardInterrupt()
            return 0 if self.stopped else None
        def send_signal(self, sig):
            self.stopped = True
        def wait(self, timeout):
            return 0
    child = Interrupted()
    monkeypatch.setattr(soak.subprocess, 'Popen', lambda *args, **kwargs: child)
    output = tmp_path/'interrupted'
    assert soak.run(bundle, output, 86400, 5, soak.GrowthBudgets()) == 1
    receipt = json.loads((output/'soak-beta.json').read_text())
    assert child.stopped
    assert receipt['status'] == 'interrupted'
    assert receipt['continuous_24h_observed'] is False
