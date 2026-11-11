import pytest

from app.database import create_schema
from app.services import project_service


@pytest.fixture
def projects(tmp_path, monkeypatch):
    monkeypatch.setenv('CONTEXTCORE_DB', str(tmp_path / 'projects.sqlite'))
    create_schema()
    return project_service


def test_repeated_explicit_project_metadata_reuses_project(projects):
    first = projects.reconstruct_or_resolve_project(None, {'name': 'Actual project'})
    second = projects.reconstruct_or_resolve_project(None, {'name': 'Actual project'})
    assert first == second
    assert len(projects.list_projects()) == 1


def test_session_title_is_not_project_identity(projects):
    assert projects.reconstruct_or_resolve_project(None, {'title': 'Fix login'}) is None
    assert projects.list_projects() == []


def test_ambiguous_project_name_stays_unassigned(projects):
    projects.create_project(name='Shared name')
    projects.create_project(name='Shared name')
    assert projects.reconstruct_or_resolve_project(None, {'name': 'Shared name'}) is None
    assert len(projects.list_projects()) == 2


def test_directory_without_project_metadata_uses_directory_identity(projects, tmp_path):
    directory = str(tmp_path / 'repository')
    first = projects.reconstruct_or_resolve_project(directory)
    second = projects.reconstruct_or_resolve_project(directory)
    assert first == second
    assert projects.get_project(first)['name'] == 'repository'


def test_merge_cannot_reintroduce_content_deleted_in_target(projects):
    from fastapi import HTTPException
    from app.services import content_service
    target = projects.create_project(name='Target')
    source = projects.create_project(name='Source')
    deleted = content_service.create_content(
        title='Deleted decision', content='Do not resurrect', category='decision', project_id=target['id'],
    )
    content_service.delete_content(deleted['id'])
    retained = content_service.create_content(
        title='Deleted decision', content='Do not resurrect', category='decision', project_id=source['id'],
    )
    with pytest.raises(HTTPException) as error:
        projects.merge_projects([source['id']], target['id'])
    assert error.value.status_code == 410
    assert content_service.get_content(retained['id'])['project_id'] == source['id']
    assert projects.get_project(source['id'])['status'] == 'active'


def test_symlink_directory_alias_matches_without_rewriting_binding(projects, tmp_path):
    directory = tmp_path / 'actual-workspace'
    directory.mkdir()
    alias = tmp_path / 'workspace-alias'
    alias.symlink_to(directory, target_is_directory=True)
    project = projects.create_project(name='Alias workspace', directories=[str(alias)])
    resolved, ambiguous = projects.resolve_project_by_path(str(directory / 'subfolder'))
    assert resolved['id'] == project['id']
    assert ambiguous is False
    assert projects.get_project(project['id'])['directories'] == [str(alias)]
    assert alias.is_symlink()


def test_alias_collision_remains_ambiguous_at_both_paths(projects, tmp_path):
    directory = tmp_path / 'actual-workspace'
    directory.mkdir()
    alias = tmp_path / 'workspace-alias'
    alias.symlink_to(directory, target_is_directory=True)
    projects.create_project(name='Parent', directories=[str(tmp_path)])
    projects.create_project(name='First', directories=[str(directory)])
    projects.create_project(name='Second', directories=[str(alias)])
    resolved, ambiguous = projects.resolve_project_by_path(str(directory))
    assert resolved is None and ambiguous is True
    alias_resolved, alias_ambiguous = projects.resolve_project_by_path(str(alias))
    assert alias_resolved is None and alias_ambiguous is True


def test_directory_free_rename_multi_directory_archive_keeps_search(projects, tmp_path):
    from app.services import content_service, retrieval_service
    bare = projects.create_project(name='No folder')
    assert bare['directories'] == []
    assert bare['status'] == 'active'
    first = tmp_path / 'one'
    second = tmp_path / 'two'
    first.mkdir()
    second.mkdir()
    renamed = projects.update_project(
        bare['id'], name='Renamed workspace', directories=[str(first), str(second)],
    )
    assert renamed['name'] == 'Renamed workspace'
    assert renamed['directories'] == [str(first.resolve()), str(second.resolve())]
    item = content_service.create_content(
        title='Keep after archive', content='ARCHIVE_STILL_SEARCHABLE',
        category='decision', project_id=bare['id'], agent='codex',
    )
    archived = projects.update_project(bare['id'], status='archived')
    assert archived['status'] == 'archived' and archived['archived_at']
    assert projects.list_projects(status='active') == []
    assert projects.list_projects(status='archived')[0]['id'] == bare['id']
    found = retrieval_service.search('ARCHIVE_STILL_SEARCHABLE', project_id=bare['id'])
    assert item['id'] in {row['id'] for row in found['items']}
    restored = projects.update_project(bare['id'], status='active')
    assert restored['status'] == 'active' and restored['archived_at'] is None


def test_project_archive_and_directory_state_survive_new_process(projects, tmp_path):
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys
    item = projects.create_project(name='Synthetic persistent project')
    directories = [tmp_path / 'one', tmp_path / 'two']
    for directory in directories:
        directory.mkdir()
    projects.update_project(item['id'], name='Renamed persistent project', directories=[str(p) for p in directories])
    projects.update_project(item['id'], status='archived')
    code = '''import json,sys
from app.services.project_service import get_project,list_projects
value=get_project(sys.argv[1])
assert value['name']=='Renamed persistent project'
assert value['status']=='archived' and value['archived_at']
assert len(value['directories'])==2
assert any(p['id']==value['id'] for p in list_projects(status='archived'))
assert not any(p['id']==value['id'] for p in list_projects(status='active'))
print(json.dumps({'project':value['id'],'status':value['status']}))
'''
    backend = Path(__file__).parents[1]
    result = subprocess.run([sys.executable, '-c', code, item['id']], cwd=tmp_path,
        env={**os.environ, 'PYTHONPATH': str(backend), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['status'] == 'archived'
