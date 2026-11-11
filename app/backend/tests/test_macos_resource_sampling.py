"""Prevent incomplete process observations becoming false zero-memory claims."""
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('macos_resources', Path(__file__).resolve().parents[2] / 'macos/sample_resources.py')
resources = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resources)


def test_missing_webkit_is_unknown_not_zero():
    result = resources.webkit_summary([])
    assert result['rss_bytes'] is None
    assert result['physical_footprint_bytes'] is None
    assert result['attribution'] == 'unknown_or_incomplete'


def test_partial_webkit_measurement_is_not_a_total():
    result = resources.webkit_summary([{'pid': 2, 'observed': True, 'rss_bytes': 100, 'physical_footprint_bytes': 80}, {'pid': 3, 'observed': False}])
    assert result['rss_bytes'] is None
    assert len(result['processes']) == 2


def test_observed_descendants_do_not_claim_whole_webview():
    result = resources.webkit_summary([{'pid': 2, 'observed': True, 'rss_bytes': 100, 'physical_footprint_bytes': 80}])
    assert result['rss_bytes'] == 100
    assert result['physical_footprint_bytes'] == 80
    assert result['attribution'] == 'descendant_processes_only'
    assert 'not a whole-WebView total' in result['coverage']


def test_process_ancestry_excludes_other_apps_and_system_managed_webkit():
    rows = {10: {'ppid': 1}, 11: {'ppid': 10}, 12: {'ppid': 11}, 13: {'ppid': 1}, 14: {'ppid': 13}}
    assert resources.descendants(rows, 10) == {11, 12}
    assert resources.descendants({10: {'ppid': 11}, 11: {'ppid': 10}}, 10) == {11}


def test_pid_reuse_stops_before_recording_an_unrelated_process(monkeypatch, tmp_path):
    calls = {10: 0, 20: 0}
    def fake_usage(pid):
        calls[pid] += 1
        return {'pid': pid, 'observed': True, 'start_abstime': 1 if calls[pid] == 1 else 2}
    monkeypatch.setattr(resources, 'usage', fake_usage)
    monkeypatch.setattr(resources, 'process_table', lambda: {10: {'state': 'S'}, 20: {'state': 'S'}})
    output = tmp_path / 'sample.json'
    result = resources.sample(10, 20, 1, .1, output)
    assert result['samples'] == []
    assert result['stop_reason'] == 'selected_process_exited_or_pid_reused'
    assert output.is_file()


def test_zombie_process_stops_sampling_even_if_kernel_retains_usage(monkeypatch, tmp_path):
    monkeypatch.setattr(resources, 'usage', lambda pid: {'pid': pid, 'observed': True, 'start_abstime': 1})
    monkeypatch.setattr(resources, 'process_table', lambda: {10: {'state': 'Z'}, 20: {'state': 'S'}})
    result = resources.sample(10, 20, 1, .1, tmp_path / 'sample.json')
    assert result['samples'] == []
    assert result['stop_reason'] == 'selected_process_exited_or_pid_reused'


def test_coalition_mode_rejects_direct_child_launch(monkeypatch):
    monkeypatch.setattr(resources, 'launchservices_coalition', lambda pid: 42)
    result = resources.coalition_webkit({10:{'ppid':20}}, 10)
    assert result['attribution'] == 'unknown' and result['rss_bytes'] is None


def test_coalition_mode_excludes_other_apps_and_records_unknown_members(monkeypatch):
    monkeypatch.setattr(resources, 'launchservices_coalition', lambda pid: {10:42,11:42,12:99,13:None}[pid])
    monkeypatch.setattr(resources, 'usage', lambda pid: {'pid':pid,'observed':True,'rss_bytes':100,'physical_footprint_bytes':80})
    rows={10:{'ppid':1,'name':'App'},11:{'ppid':1,'name':'com.apple.WebKit.WebContent'},12:{'ppid':1,'name':'com.apple.WebKit.WebContent'},13:{'ppid':1,'name':'com.apple.WebKit.Networking'}}
    result=resources.coalition_webkit(rows,10)
    assert [p['pid'] for p in result['processes']]==[11]
    assert result['rss_bytes']==100 and result['unresolved_candidates']==1
    assert result['attribution']=='isolated_launchservices_coalition'


def test_unknown_root_coalition_keeps_memory_unknown(monkeypatch):
    monkeypatch.setattr(resources,'launchservices_coalition',lambda pid:None)
    result=resources.coalition_webkit({10:{'ppid':1,'name':'App'}},10)
    assert result['rss_bytes'] is None and result['attribution']=='unknown'


def test_long_sample_ledger_streams_all_observations_but_retains_bounded_memory():
    import io,json
    report={'samples':[]};stream=io.StringIO()
    for n in range(1000): resources.retain_sample(report, {'cycle':n},stream)
    assert report['sample_count']==1000
    assert len(report['samples'])==100
    assert report['samples'][0]['cycle']==900
    lines=stream.getvalue().splitlines()
    assert len(lines)==1000 and json.loads(lines[0])['cycle']==0 and json.loads(lines[-1])['cycle']==999
