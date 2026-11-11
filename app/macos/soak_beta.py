#!/usr/bin/env python3
"""24h App and backend RSS/CPU soak against an isolated candidate only.

Refuses the daily App, default dist/, Application Support data tree, and paths inside the repository checkout.
Writes samples to a new external directory (for example under /tmp/). Exits 2 when growth budgets fail.
Does not replace or stop a daily 8420 service.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent


@dataclass(frozen=True)
class GrowthBudgets:
    rss_growth_bytes: int = 128 * 1024 * 1024
    consecutive_rss_samples: int = 10
    cpu_busy_ratio: float = 0.80
    consecutive_cpu_samples: int = 10


@dataclass(frozen=True)
class IsolationPolicy:
    forbidden_prefixes: tuple[Path, ...]
    require_adhoc: bool = True
    require_arm64: bool = True
    require_unnotarized: bool = True
    repo_root: Path | None = None


def default_policy(repo: Path) -> IsolationPolicy:
    home = Path.home()
    return IsolationPolicy(
        forbidden_prefixes=(
            Path('/Applications'),
            home / 'Applications',
            home / 'Library/Application Support/ContextCore',
            repo / 'app/macos/dist',
        ),
        repo_root=repo,
    )


def _under(path: Path, prefix: Path) -> bool:
    try:
        path.resolve().relative_to(prefix.expanduser().resolve())
        return True
    except (ValueError, OSError):
        return False


def binary_already_running(bundle: Path) -> bool:
    binary = bundle.expanduser().resolve() / 'Contents' / 'MacOS' / 'Senrelay'
    prefix = str(binary) + ' '
    try:
        result = subprocess.run(['/bin/ps', '-axo', 'command='], capture_output=True,
                                text=True, timeout=5, check=True)
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Cannot establish whether the candidate is already running') from exc
    return any(line.strip().startswith(prefix) for line in result.stdout.splitlines())


def isolation_errors(bundle: Path, output: Path, policy: IsolationPolicy,
                     evidence_root: Path | None = None) -> list[str]:
    errors = []
    try:
        resolved = bundle.expanduser().resolve()
    except OSError as exc:
        return [f'bundle path is unreadable: {exc}']
    if resolved.suffix != '.app' or not (resolved / 'Contents/MacOS/Senrelay').is_file():
        errors.append('bundle must be a Senrelay.app with Contents/MacOS/Senrelay')
    for prefix in policy.forbidden_prefixes:
        if _under(resolved, prefix):
            errors.append(f'bundle is under daily or default path {prefix}')
    receipt = resolved.parent / 'BUILD.json'
    if not receipt.is_file():
        errors.append('sibling BUILD.json is required so the soak binds a freeze identity')
    else:
        try:
            build = json.loads(receipt.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f'BUILD.json is unreadable: {exc}')
            build = {}
        if policy.require_arm64 and build.get('architecture') != 'arm64':
            errors.append('BUILD.json architecture must be arm64')
        if policy.require_adhoc and build.get('signature') != 'ad-hoc':
            errors.append('BUILD.json signature must be ad-hoc')
        if policy.require_unnotarized and build.get('notarized') is not False:
            errors.append('BUILD.json must record notarized=false')
    try:
        if binary_already_running(resolved):
            errors.append('a process is already running this bundle binary; refuse to attach or launch a second soak')
    except RuntimeError as exc:
        errors.append(str(exc))
    try:
        out = output.expanduser().resolve()
    except OSError as exc:
        errors.append(f'output path is unreadable: {exc}')
        return errors
    if out.exists():
        errors.append('output directory must be new so an existing soak is not replaced')
    repo_check = policy.repo_root or REPO
    if _under(out, repo_check):
        errors.append(f'output must be outside repository {repo_check}')
    for prefix in policy.forbidden_prefixes:
        if _under(out, prefix):
            errors.append(f'output is under daily or default path {prefix}')
    if evidence_root is not None and not _under(out, evidence_root):
        errors.append(f'output must be under {evidence_root}')
    return errors


def _cpu_ns(proc: dict) -> int | None:
    if not proc.get('observed'):
        return None
    user = proc.get('user_cpu_ns')
    system = proc.get('system_cpu_ns')
    if type(user) is not int or type(system) is not int:
        return None
    return user + system


def _rss(proc: dict) -> int | None:
    if not proc.get('observed'):
        return None
    value = proc.get('rss_bytes')
    return value if type(value) is int else None


def growth_failure(samples: list[dict], budgets: GrowthBudgets) -> str | None:
    """Return a failure reason when App or backend RSS/CPU budgets trip."""
    app_base = service_base = None
    rss_streak = cpu_streak = 0
    previous = None
    for sample in samples:
        app = sample.get('app') or {}
        service = sample.get('service') or {}
        app_rss = _rss(app)
        service_rss = _rss(service)
        if (app_rss is None or service_rss is None
                or _cpu_ns(app) is None or _cpu_ns(service) is None):
            return 'App or service resource observation is unavailable'
        if app_base is None:
            app_base, service_base = app_rss, service_rss
        app_over = app_rss - app_base > budgets.rss_growth_bytes
        service_over = service_rss - service_base > budgets.rss_growth_bytes
        rss_streak = rss_streak + 1 if app_over or service_over else 0
        if rss_streak >= budgets.consecutive_rss_samples:
            which = 'app' if app_over else 'service'
            return f'{which} RSS exceeded {budgets.rss_growth_bytes} bytes above baseline for {rss_streak} samples'
        if previous is not None:
            wall = sample.get('elapsed_seconds')
            prev_wall = previous.get('elapsed_seconds')
            app_cpu = _cpu_ns(app)
            service_cpu = _cpu_ns(service)
            prev_app = _cpu_ns(previous.get('app') or {})
            prev_service = _cpu_ns(previous.get('service') or {})
            if None not in (wall, prev_wall, app_cpu, service_cpu, prev_app, prev_service):
                delta = wall - prev_wall
                if delta > 0:
                    app_busy = ((app_cpu - prev_app) / 1e9) / delta
                    service_busy = ((service_cpu - prev_service) / 1e9) / delta
                    spinning = app_busy >= budgets.cpu_busy_ratio or service_busy >= budgets.cpu_busy_ratio
                    cpu_streak = cpu_streak + 1 if spinning else 0
                    if cpu_streak >= budgets.consecutive_cpu_samples:
                        return (
                            f'app or service CPU stayed at or above {budgets.cpu_busy_ratio:.0%} '
                            f'for {cpu_streak} samples'
                        )
        previous = sample
    return None


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text().splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def read_resource_samples(native_run: Path) -> list[dict]:
    """Prefer the streaming ledger. Fall back to the bounded JSON window."""
    rows = _read_jsonl(native_run / 'resources.jsonl')
    if rows:
        return rows
    snapshot = native_run / 'resources.json'
    if snapshot.is_file():
        payload = json.loads(snapshot.read_text())
        samples = payload.get('samples')
        if isinstance(samples, list):
            return samples
    return []


def completion_failure(native_run: Path, samples: list[dict], seconds: float,
                       interval: float, observed: float) -> str | None:
    """A child's zero exit is not evidence of completed observation."""
    try:
        receipt = json.loads((native_run / 'checkpoint.json').read_text())
        native = receipt['native']
        if receipt['status'] != 'completed' or native['status'] != 'completed':
            return 'Native observation did not complete continuously'
        if receipt.get('requested_seconds') != seconds or native.get('requested_seconds') != seconds:
            return 'Native observation duration does not match request'
        if observed < seconds or native.get('elapsed_seconds', 0) < seconds:
            return 'Observation ended before the requested duration'
        if native.get('observation_gaps') != 0:
            return 'Native observation contains gaps'
        if not receipt.get('owned_process_cleanup'):
            return 'Native child cleanup is incomplete'
        if len(samples) < 2:
            return 'Insufficient resource observations'
        times = [sample['elapsed_seconds'] for sample in samples]
        if times[0] > max(10, 3 * interval) or times[-1] < seconds - max(10, 3 * interval):
            return 'Resource observation does not cover the requested duration'
        if any(b <= a or b - a > max(10, 3 * interval) for a, b in zip(times, times[1:])):
            return 'Resource observation contains gaps or nonmonotonic samples'
        if seconds >= 86400 and not (receipt.get('continuous_24h_observed') is True
                                    and native.get('continuous_24h_observed') is True):
            return 'The child did not establish continuous 24h observation'
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return f'Native completion evidence is missing or invalid: {exc}'
    return None


def run(bundle: Path, output: Path, seconds: float, interval: float, budgets: GrowthBudgets) -> int:
    policy = default_policy(REPO)
    errors = isolation_errors(bundle, output, policy)
    if errors:
        print(json.dumps({'status': 'refused', 'errors': errors}, indent=2))
        return 3
    output.mkdir(parents=True, exist_ok=False)
    receipt = json.loads((bundle.resolve().parent / 'BUILD.json').read_text())
    native = HERE / 'soak_native.py'
    child = subprocess.Popen(
        [sys.executable, str(native), '--bundle', str(bundle.resolve()),
         '--output', str(output / 'native-run'), '--seconds', str(seconds),
         '--interval', str(interval)],
        cwd=str(HERE),
    )
    state = {
        'status': 'running',
        'bundle': str(bundle.resolve()),
        'build': receipt,
        'budgets': asdict(budgets),
        'runner_pid': os.getpid(),
        'soak_native_pid': child.pid,
        'requested_seconds': seconds,
    }
    (output / 'soak-beta.json').write_text(json.dumps(state, indent=2) + '\n')
    native_run = output / 'native-run'
    code = 1
    started = time.monotonic()
    state['continuous_24h_observed'] = False
    try:
        while True:
            finished = child.poll()
            samples = read_resource_samples(native_run)
            reason = growth_failure(samples, budgets)
            state.update(sample_count=len(samples), last_checked_at=time.time())
            if reason:
                state.update(status='failed_growth', error=reason)
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=40)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=10)
                code = 2
                break
            if finished is not None:
                samples = read_resource_samples(native_run)
                reason = growth_failure(samples, budgets)
                if reason:
                    state.update(status='failed_growth', error=reason, sample_count=len(samples))
                    code = 2
                elif finished == 0:
                    failure = completion_failure(native_run, samples, seconds, interval,
                                                 time.monotonic() - started)
                    if failure:
                        state.update(status='failed_incomplete', error=failure)
                        code = 1
                    else:
                        state.update(status='completed', sample_count=len(samples),
                                     continuous_24h_observed=seconds >= 86400)
                        code = 0
                else:
                    state.update(status='failed', soak_native_exit=finished, sample_count=len(samples))
                    code = 1
                break
            time.sleep(min(5, interval))
    except BaseException as exc:
        state.update(status='interrupted' if isinstance(exc, KeyboardInterrupt) else 'failed',
                     error=repr(exc), continuous_24h_observed=False)
        code = 1
    finally:
        if child.poll() is None:
            child.send_signal(signal.SIGTERM)
            try:
                child.wait(timeout=40)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        state['finished_at'] = time.time()
        (output / 'soak-beta.json').write_text(json.dumps(state, indent=2) + '\n')
        print(json.dumps({k: state.get(k) for k in
                          ('status', 'error', 'sample_count', 'continuous_24h_observed',
                           'soak_native_exit') if k in state or k == 'status'}))
    return code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--seconds', type=float, default=86400)
    parser.add_argument('--interval', type=float, default=60)
    args = parser.parse_args(argv)
    if not 10 <= args.seconds <= 259080 or not 1 <= args.interval <= 60:
        parser.error('Require duration [10,259080], interval [1,60]')
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    return run(args.bundle, args.output, args.seconds, args.interval, GrowthBudgets())


if __name__ == '__main__':
    raise SystemExit(main())
