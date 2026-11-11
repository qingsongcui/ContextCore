"""Component selection uses isolated homes, never real host configuration."""
import json
from pathlib import Path

import pytest

from app.integrations.constants import SUPPORTED_HOSTS
from app.integrations.installer import apply_host_integration, uninstall_host_integration


@pytest.mark.parametrize('host', SUPPORTED_HOSTS)
@pytest.mark.parametrize('component', ['mcp', 'hooks', 'skill'])
def test_install_and_remove_only_selected_component(tmp_path, host, component):
    result = apply_host_integration(host, token='synthetic-token', confirmed=True,
                                    home=tmp_path, components=[component])
    paths = {'mcp': Path(result.mcp_path), 'hooks': Path(result.hooks_path), 'skill': Path(result.skill_path)}
    assert paths[component].exists()
    assert all(not p.exists() for name, p in paths.items() if name != component)
    assert Path(result.token_path).exists() == (component != 'skill')
    assert json.loads(Path(result.manifest_path).read_text())['components'] == [component]
    removed = uninstall_host_integration(host, home=tmp_path, confirmed=True, components=[component])
    assert removed.status == 'uninstalled'
    assert not Path(result.token_path).exists()
    assert not Path(result.manifest_path).exists()
    assert all(not p.exists() for name, p in paths.items() if name != component)


@pytest.mark.parametrize('host', SUPPORTED_HOSTS)
def test_incremental_install_and_remove_preserve_unselected_files(tmp_path, host):
    result = apply_host_integration(host, token='synthetic-token', confirmed=True,
                                    home=tmp_path, components=['mcp'])
    mcp = Path(result.mcp_path)
    original = mcp.read_bytes()
    result = apply_host_integration(host, token='synthetic-token', confirmed=True,
                                    home=tmp_path, components=['skill'])
    assert mcp.read_bytes() == original
    assert not Path(result.hooks_path).exists()
    skill = Path(result.skill_path)
    skill_original = skill.read_bytes()
    removed = uninstall_host_integration(host, home=tmp_path, confirmed=True, components=['mcp'])
    assert removed.status == 'uninstalled'
    assert skill.read_bytes() == skill_original
    assert not Path(result.token_path).exists()
    assert json.loads(Path(result.manifest_path).read_text())['components'] == ['skill']
    assert uninstall_host_integration(host, home=tmp_path, confirmed=True).status == 'uninstalled'


def test_removing_skill_keeps_token_for_existing_mcp(tmp_path):
    result = apply_host_integration('omp', token='synthetic-token', confirmed=True,
                                    home=tmp_path, components=['mcp', 'skill'])
    before = Path(result.token_path).read_bytes()
    uninstall_host_integration('omp', home=tmp_path, confirmed=True, components=['skill'])
    assert Path(result.token_path).read_bytes() == before
    assert Path(result.mcp_path).exists()


def test_unselected_foreign_extension_is_not_read_or_removed(tmp_path):
    hook = tmp_path/'.omp/agent/hooks/pre/contextcore.ts'
    hook.parent.mkdir(parents=True)
    hook.write_text('foreign user extension')
    result = apply_host_integration('omp', token='synthetic-token', confirmed=True,
                                    home=tmp_path, components=['mcp'])
    assert not Path(result.skill_path).exists()
    uninstall_host_integration('omp', home=tmp_path, confirmed=True)
    assert hook.read_text() == 'foreign user extension'


def test_credential_rotation_cannot_break_unselected_hook(tmp_path):
    result = apply_host_integration('omp', token='original-token', confirmed=True,
                                    home=tmp_path, components=['hooks'])
    with pytest.raises(ValueError, match='unselected component'):
        apply_host_integration('omp', token='replacement-token', confirmed=True,
                               home=tmp_path, components=['mcp'])
    assert Path(result.token_path).read_text().strip() == 'original-token'
    assert not Path(result.mcp_path).exists()


@pytest.mark.parametrize('components', [[], ['unknown'], ['mcp', 'mcp'], 'mcp'])
def test_invalid_component_selection_writes_nothing(tmp_path, components):
    before = set(tmp_path.rglob('*'))
    with pytest.raises(ValueError):
        apply_host_integration('omp', token='synthetic', confirmed=True,
                               home=tmp_path, components=components)
    assert set(tmp_path.rglob('*')) == before


@pytest.mark.parametrize('host', ['omp', 'opencode'])
def test_mcp_ownership_does_not_allow_overwriting_foreign_extension(tmp_path, host):
    result = apply_host_integration(host, token='synthetic-token', confirmed=True,
                                    home=tmp_path, components=['mcp'])
    hook = Path(result.hooks_path)
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text('foreign extension')
    before = Path(result.manifest_path).read_bytes()
    with pytest.raises(ValueError, match='unowned'):
        apply_host_integration(host, token='synthetic-token', confirmed=True,
                               home=tmp_path, components=['hooks'])
    assert hook.read_text() == 'foreign extension'
    assert Path(result.manifest_path).read_bytes() == before


def test_antigravity_hook_update_refuses_external_edits(tmp_path):
    result = apply_host_integration('antigravity', token='synthetic-token', confirmed=True,
                                    home=tmp_path, components=['hooks'])
    path = Path(result.hooks_path)
    data = json.loads(path.read_text())
    data['contextcore-session']['user_setting'] = 'keep'
    path.write_text(json.dumps(data))
    before = {p: p.read_bytes() for p in [path, Path(result.token_path), Path(result.manifest_path)]}
    with pytest.raises(ValueError, match='Externally modified hook'):
        apply_host_integration('antigravity', token='synthetic-token', confirmed=True,
                               home=tmp_path, components=['hooks'])
    assert all(p.read_bytes() == value for p, value in before.items())
