"""Read-only macOS process sampling; missing WebKit attribution stays unknown.

Use only explicitly selected QA process IDs. No application is launched or stopped.
RSS and physical footprint are current kernel readings, not ru_maxrss high-water marks.
"""
from __future__ import annotations
import argparse
import ctypes
import json
import re
from pathlib import Path
import subprocess
import time


class Usage(ctypes.Structure):
    _fields_ = [('uuid', ctypes.c_uint8 * 16)] + [(name, ctypes.c_uint64) for name in (
        'user_time', 'system_time', 'pkg_idle_wkups', 'interrupt_wkups', 'pageins',
        'wired_size', 'resident_size', 'phys_footprint', 'proc_start_abstime', 'proc_exit_abstime')]


def usage(pid):
    lib = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True)
    fn = lib.proc_pid_rusage
    fn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p]
    fn.restype = ctypes.c_int
    result = Usage()
    if fn(pid, 0, ctypes.byref(result)) != 0:
        return {'pid': pid, 'observed': False, 'errno': ctypes.get_errno()}
    return {'pid': pid, 'observed': True, 'rss_bytes': result.resident_size,
            'physical_footprint_bytes': result.phys_footprint,
            'start_abstime': result.proc_start_abstime,
            'user_cpu_ns': result.user_time, 'system_cpu_ns': result.system_time}


def process_table():
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,stat=,comm='], capture_output=True,
                            text=True, check=True, timeout=5)
    rows = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 3)
        if len(fields) == 4:
            rows[int(fields[0])] = {'ppid': int(fields[1]), 'state': fields[2], 'name': fields[3]}
    return rows


def descendants(rows, root):
    found = set()
    frontier = {root}
    while frontier:
        next_ids = {pid for pid, row in rows.items() if row['ppid'] in frontier}
        next_ids -= found | {root}
        found.update(next_ids)
        frontier = next_ids
    return found


def webkit_summary(samples):
    observed = [item for item in samples if item['observed']]
    complete = bool(samples) and len(observed) == len(samples)
    return {'attribution': 'descendant_processes_only' if complete else 'unknown_or_incomplete',
            'rss_bytes': sum(item['rss_bytes'] for item in observed) if complete else None,
            'physical_footprint_bytes': sum(item['physical_footprint_bytes'] for item in observed) if complete else None,
            'processes': samples,
            'coverage': 'System-managed WebKit processes may not be descendants; this is not a whole-WebView total.'}


def launchservices_coalition(pid):
    """Read only the selected process's system group ID, without elevation."""
    try:
        result = subprocess.run(['/usr/bin/lsappinfo', 'info', '-long', str(pid)],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r'LSApplicationMemberCoalitionIDKey"\s*=\s*(\d+)', result.stdout)
    return int(match[1]) if result.returncode == 0 and match else None


def coalition_webkit(rows, app_pid):
    # This mode is opt-in after an isolated LaunchServices launch. Direct CLI
    # launches can share the caller's coalition and must not use this shortcut.
    result = {'attribution': 'unknown', 'processes': [], 'rss_bytes': None,
              'physical_footprint_bytes': None, 'unresolved_candidates': 0}
    if rows.get(app_pid, {}).get('ppid') != 1:
        result['reason'] = 'App is not an independently launched launchd child'
        return result
    group = launchservices_coalition(app_pid)
    if group is None:
        result['reason'] = 'App coalition could not be read'
        return result
    result['coalition_id'] = group
    for pid, row in rows.items():
        if 'com.apple.webkit.' not in row['name'].lower() or row.get('state', '').startswith('Z'):
            continue
        member = launchservices_coalition(pid)
        if member is None:
            result['unresolved_candidates'] += 1
        elif member == group:
            result['processes'].append(usage(pid))
    measured = webkit_summary(result['processes'])
    result.update(rss_bytes=measured['rss_bytes'], physical_footprint_bytes=measured['physical_footprint_bytes'])
    result['attribution'] = 'isolated_launchservices_coalition'
    result['coverage'] = 'Observed matching group members only; sums can include shared pages. Process creation/exit and unresolved candidates limit coverage.'
    return result


def retain_sample(report, entry, stream=None):
    report['sample_count'] = report.get('sample_count', 0) + 1
    if stream is not None:
        stream.write(json.dumps(entry) + '\n')
        stream.flush()
    report['samples'].append(entry)
    if stream is not None and len(report['samples']) > 100:
        del report['samples'][:-100]


def sample(app_pid, service_pid, duration, interval, output, *, isolated_launchservices=False):
    identities = {pid: usage(pid) for pid in (app_pid, service_pid)}
    if any(not item['observed'] for item in identities.values()):
        raise RuntimeError('Both explicitly selected QA processes must be running')
    report = {'metric': 'current kernel RSS and physical footprint in bytes',
              'long_running_24_72h_completed': False, 'requested_duration_seconds': duration,
              'whole_webview_memory': None, 'isolated_launchservices': isolated_launchservices, 'samples': []}
    output.parent.mkdir(parents=True, exist_ok=True)
    stream = output.with_suffix('.jsonl').open('w') if duration > 300 else None
    if stream is not None:
        report['sample_file'] = str(output.with_suffix('.jsonl'))
        report['retained_sample_limit'] = 100
    def persist():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        temporary = output.with_suffix('.tmp')
        temporary.write_text(json.dumps(report, indent=2) + '\n')
        temporary.replace(output)
    started = time.monotonic()
    try:
        while time.monotonic() - started < duration:
            rows = process_table()
            roots = {pid: usage(pid) for pid in identities}
            valid = all(pid in rows and not rows[pid]['state'].startswith('Z')
                        and item['observed'] and item['start_abstime'] == identities[pid]['start_abstime']
                        for pid, item in roots.items())
            if not valid:
                report['stop_reason'] = 'selected_process_exited_or_pid_reused'
                break
            webkit = [usage(pid) for pid in sorted(descendants(rows, app_pid))
                      if 'webkit' in rows[pid]['name'].lower()]
            retain_sample(report, {'elapsed_seconds': round(time.monotonic() - started, 3),
                                      'app': roots[app_pid], 'service': roots[service_pid],
                                      'webkit_descendants': webkit_summary(webkit),
                                      'webkit_coalition': coalition_webkit(rows, app_pid) if isolated_launchservices else None}, stream)
            if stream is not None: persist()
            time.sleep(min(interval, max(0, duration - (time.monotonic() - started))))
        else:
            report['stop_reason'] = 'requested_duration_elapsed'
    finally:
        if stream is not None: stream.close()
        persist()
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app-pid', required=True, type=int)
    parser.add_argument('--service-pid', required=True, type=int)
    parser.add_argument('--seconds', type=float, default=60)
    parser.add_argument('--interval', type=float, default=1)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--isolated-launchservices', action='store_true', help='Only for an independently hidden-launched QA app; never for a direct CLI launch')
    args = parser.parse_args()
    if min(args.app_pid, args.service_pid) <= 1 or not 0 < args.seconds <= 259200 or not .1 <= args.interval <= 60:
        parser.error('Require process IDs > 1, duration (0, 259200], interval [0.1, 60]')
    sample(args.app_pid, args.service_pid, args.seconds, args.interval, args.output, isolated_launchservices=args.isolated_launchservices)
