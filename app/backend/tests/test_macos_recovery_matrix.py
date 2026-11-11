"""Recovery behavior on isolated services; native UI remains a separate receipt."""
import importlib.util
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

APP = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_current_source_candidate_survives_lifecycle_faults(tmp_path):
    lifecycle = load('candidate_lifecycle', APP/'scripts/exercise_install_lifecycle.py')
    frontend = tmp_path/'synthetic-ui'
    frontend.mkdir()
    (frontend/'index.html').write_text('<html>Synthetic lifecycle UI</html>')
    result = lifecycle.run(tmp_path/'acceptance', backend=APP/'backend', frontend=frontend)
    assert result['status'] == 'completed'
    assert result['owned_service_stopped'] is True
    assert result['continuous_24h_observed'] is False
    # Each named check follows real HTTP writes, process transitions, and readback.
    assert {'same_candidate_graceful_restart_persistence',
            'same_candidate_crash_recovery_persistence',
            'port_conflict_rejected_then_restart',
            'failed_update_original_still_runnable',
            'duplicate_service_rejected_preserving_owner',
            'uninstall_preserves_data_simulated_no_launchctl'} <= set(result['checks'])
    assert not (tmp_path/'acceptance/conflict-data/contextcore.db').exists()
    assert not (tmp_path/'acceptance/data/service.pid').exists()


def test_lifecycle_refuses_existing_output_without_touching_data(tmp_path):
    lifecycle = load('refuse_candidate_lifecycle', APP/'scripts/exercise_install_lifecycle.py')
    marker = tmp_path/'contextcore.db'
    marker.write_bytes(b'UNCHANGED_SYNTHETIC_DATABASE')
    with pytest.raises(ValueError, match='new isolated'):
        lifecycle.run(tmp_path, backend=APP/'backend', frontend=tmp_path)
    assert marker.read_bytes() == b'UNCHANGED_SYNTHETIC_DATABASE'


@pytest.mark.parametrize('fault', ['contract', 'frontend'])
def test_frozen_candidate_preflight_rejects_damage_without_database_access(tmp_path, monkeypatch, fault):
    from app.frontend_release import _digest_tree
    entry = load('frozen_candidate_preflight', APP/'macos/backend_entry.py')
    frontend = tmp_path/'frontend'
    frontend.mkdir()
    (frontend/'index.html').write_text('synthetic frontend')
    manifest = {'release_id': 'synthetic-candidate', 'api_contract': 2,
                'frontend_sha256': _digest_tree(frontend)}
    (tmp_path/'desktop-release.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(sys, '_MEIPASS', str(tmp_path), raising=False)
    monkeypatch.setattr(sys, 'argv', ['senrelay-service', '-m', 'app.cli', 'check-install'])
    with patch('app.runtime.is_service_running', side_effect=AssertionError('preflight must not inspect/stop live service')):
        assert entry.main() == 0
        if fault == 'contract':
            manifest['api_contract'] = 999
            (tmp_path/'desktop-release.json').write_text(json.dumps(manifest))
        else:
            (frontend/'index.html').write_text('corrupted')
        with pytest.raises(ValueError):
            entry.main()
    assert not (tmp_path/'contextcore.db').exists()
    assert not (tmp_path/'service.pid').exists()
