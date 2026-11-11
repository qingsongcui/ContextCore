"""Activation faults must leave the previously selected release runnable."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'prepare_release.py'
spec = importlib.util.spec_from_file_location('activation_faults_prepare', SCRIPT)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


@pytest.fixture
def candidates(tmp_path):
    backend, frontend, data = tmp_path/'backend', tmp_path/'frontend', tmp_path/'data'
    (backend/'app').mkdir(parents=True)
    (backend/'app/main.py').write_text('# first')
    frontend.mkdir()
    (frontend/'index.html').write_text('first')
    first = prepare.prepare(backend, frontend, data)
    prepare.activate_release(first, data)
    (backend/'app/main.py').write_text('# second')
    (frontend/'index.html').write_text('second')
    second = prepare.prepare(backend, frontend, data)
    return data, first, second


@pytest.mark.parametrize('fault', ['backend', 'frontend', 'launcher', 'record', 'foreign_data'])
def test_invalid_candidate_never_replaces_active_release(candidates, tmp_path, fault):
    from app import runtime
    data, first, second = candidates
    active = data/'current-release.json'
    before = active.read_bytes()
    if fault == 'backend':
        (Path(second['backend_dir'])/'app/main.py').write_text('# corrupted')
    elif fault == 'frontend':
        (Path(second['frontend_dir'])/'index.html').write_text('corrupted')
    elif fault == 'launcher':
        # The shared launcher is tested separately; retain original bytes for the
        # old candidate's independent verification after rejection.
        launcher = Path(second['launcher_path'])
        original = launcher.read_bytes()
        launcher.write_bytes(b'corrupted')
    elif fault == 'record':
        second = {**second, 'api_contract': 999}
    with pytest.raises((ValueError, OSError)):
        prepare.activate_release(second, tmp_path/'foreign' if fault == 'foreign_data' else data)
    assert active.read_bytes() == before
    if fault == 'launcher':
        launcher.write_bytes(original)
    assert runtime.verified_serving_release(data, Path(first['backend_dir'])) == first


def test_atomic_pointer_failure_cleans_temporary_and_keeps_original(candidates):
    data, first, second = candidates
    active = data/'current-release.json'
    before = active.read_bytes()
    with patch.object(prepare.os, 'replace', side_effect=OSError('synthetic disk full')):
        with pytest.raises(OSError, match='disk full'):
            prepare.activate_release(second, data)
    assert active.read_bytes() == before
    assert list(data.glob('.activating-*')) == []
    assert json.loads(active.read_text()) == first


def test_successful_activation_selects_matching_candidate_and_preserves_data(candidates):
    data, first, second = candidates
    marker = data/'synthetic-user-data'
    marker.write_bytes(b'preserve me')
    active = prepare.activate_release(second, data)
    assert json.loads(active.read_text()) == second
    assert marker.read_bytes() == b'preserve me'
    assert Path(first['backend_dir']).is_dir()
