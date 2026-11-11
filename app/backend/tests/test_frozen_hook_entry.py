"""Both service entry points dispatch finite hooks and reject script execution."""
import importlib.util
from pathlib import Path
import sys
import pytest
from app.integrations import cc_hook


@pytest.fixture(params=['legacy', 'native'])
def entry(request):
    app_root = Path(__file__).parents[2]
    path = app_root / ('backend/desktop_backend.py' if request.param == 'legacy' else 'macos/backend_entry.py')
    spec = importlib.util.spec_from_file_location('isolated_entry', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_entry_dispatches_hook_and_restores_arguments(entry, monkeypatch):
    original = ['synthetic-service', '--hook', 'codex', 'stop']
    monkeypatch.setattr(sys, 'argv', original)
    seen = []
    monkeypatch.setattr(cc_hook, 'main', lambda: seen.append(sys.argv[:]))
    entry.main()
    assert seen == [['synthetic-service', 'codex', 'stop']]
    assert sys.argv is original


@pytest.mark.parametrize('arguments', [
    ['-c', 'raise RuntimeError()'], ['arbitrary.py'],
    ['--hook', 'codex', 'arbitrary.py'], ['--hook', 'unknown', 'stop'],
    ['--hook', 'codex', 'stop', 'extra'],
])
def test_entry_rejects_arbitrary_script_or_invalid_hook(entry, arguments, monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['synthetic-service', *arguments])
    monkeypatch.setattr(cc_hook, 'main', lambda: pytest.fail('Invalid hook reached execution'))
    with pytest.raises(SystemExit):
        entry.main()
