"""Provider is always mocked: source/policy/governance races must discard output."""
from unittest.mock import AsyncMock, patch

import pytest
from app.database import create_schema, db, seed_data, set_app_setting
from app.services import connection_service, content_service, conversation_service, distillation_service, processing_jobs
from app.transport import DistillExecutionError, DistillResult


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def race_env(tmp_path, monkeypatch):
    path = tmp_path / 'races.db'
    monkeypatch.setenv('CONTEXTCORE_DB',str(path))
    create_schema(path)
    seed_data(path)
    connection_service.patch_connection('omp', enabled=True, confirmed=True)
    set_app_setting('compute_host','omp')
    set_app_setting('compute_authorized',True)
    conv = conversation_service.upsert_conversation(agent='omp',title='Race source',external_id='race',
        messages=[{'id':'u1','role':'user','content':'Remember this synthetic fact.'}])
    return conv,distillation_service.queue_distillation(conv['id'])


def output():
    return DistillResult(status='success', memories=[{'category':'knowledge','title':'Late result','content':'Must not overwrite newer state.'}])


def assert_no_late_memory():
    with db() as conn:
        assert not conn.execute("SELECT id FROM contents WHERE title='Late result'").fetchone()


@pytest.mark.anyio
async def test_source_edit_then_restore_is_still_stale(race_env):
    conv,q = race_env
    async def model(*args,**kwargs):
        with db() as conn:
            original = conn.execute('SELECT title FROM conversations WHERE id=?',(conv['id'],)).fetchone()[0]
            conn.execute("UPDATE conversations SET title='Temporary edit' WHERE id=?",(conv['id'],))
            conn.execute('UPDATE conversations SET title=? WHERE id=?',(original,conv['id']))
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        result = await distillation_service.process_distillation_queue_item(q['id'])
    assert result['ok'] is False
    assert processing_jobs.list_processing_jobs()[0]['state'] == 'cancelled'
    assert_no_late_memory()


@pytest.mark.anyio
async def test_authorization_revoke_regrant_is_still_stale(race_env):
    _,q = race_env
    async def model(*args,**kwargs):
        set_app_setting('compute_authorized',False)
        set_app_setting('compute_authorized',True)
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert not (await distillation_service.process_distillation_queue_item(q['id']))['ok']
    assert_no_late_memory()


@pytest.mark.anyio
async def test_target_edit_and_restore_is_still_stale(race_env):
    _,q = race_env
    target = content_service.create_content(title='User decision',content='Original',category='decision')
    async def model(*args,**kwargs):
        changed = content_service.update_content(target['id'], content='User correction', expected_version=1)
        content_service.restore_version(target['id'],version=1,expected_version=changed['version'])
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert not (await distillation_service.process_distillation_queue_item(q['id']))['ok']
    assert_no_late_memory()


@pytest.mark.anyio
async def test_target_delete_during_model_does_not_generate_old_memory(race_env):
    _,q = race_env
    target = content_service.create_content(title='Deleted decision',content='Old',category='decision')
    async def model(*args,**kwargs):
        content_service.delete_content(target['id'])
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert not (await distillation_service.process_distillation_queue_item(q['id']))['ok']
    assert_no_late_memory()


@pytest.mark.anyio
async def test_transport_exception_is_unknown_and_replay_never_calls_model(race_env):
    _,q = race_env
    model = AsyncMock(side_effect=DistillExecutionError(
        "Provider outcome is not known", send_started=True, result_received=False, kind="transport",
    ))
    with patch('app.transport.run_distill',model):
        result = await distillation_service.process_distillation_queue_item(q['id'])
        assert result['status'] == 'unknown'
        assert await distillation_service.process_distillation_queue_item(q['id']) is None
        assert await distillation_service.process_pending_distillations() == 0
    assert model.await_count == 1
    assert processing_jobs.list_processing_jobs()[0]['state'] == 'unknown'


@pytest.mark.anyio
async def test_returned_error_text_does_not_invent_send_uncertainty_or_retry(race_env):
    _,q = race_env
    with patch('app.transport.run_distill',AsyncMock(return_value=DistillResult(status='failed',error='Host CLI execution timed out or cancelled'))):
        result = await distillation_service.process_distillation_queue_item(q['id'])
    assert result['status'] == 'error'
    assert processing_jobs.list_processing_jobs()[0]['state'] == 'failed'
    assert distillation_service.get_pending_distillations() == []


@pytest.mark.anyio
async def test_success_receipt_prevents_same_source_reprocessing(race_env):
    conv,q = race_env
    model = AsyncMock(return_value=output())
    with patch('app.transport.run_distill',model):
        assert (await distillation_service.process_distillation_queue_item(q['id']))['ok']
        same = distillation_service.queue_distillation(conv['id'])
        assert same['id'] == q['id']
        assert await distillation_service.process_pending_distillations() == 0
    assert model.await_count == 1
    assert processing_jobs.list_processing_jobs()[0]['receipt']


@pytest.mark.anyio
async def test_source_delete_during_model_retains_only_cancelled_metadata(race_env):
    conv,q = race_env
    async def model(*args,**kwargs):
        conversation_service.delete_conversation(conv['id'])
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert not (await distillation_service.process_distillation_queue_item(q['id']))['ok']
    assert processing_jobs.list_processing_jobs()[0]['state'] == 'cancelled'
    assert_no_late_memory()


@pytest.mark.anyio
async def test_preexecution_pause_can_resume_without_new_event(race_env):
    _,q = race_env
    connection_service.patch_connection('omp',enabled=True,paused=True,confirmed=True)
    model = AsyncMock(return_value=output())
    with patch('app.transport.run_distill',model):
        assert not (await distillation_service.process_distillation_queue_item(q['id']))['ok']
        assert distillation_service.get_pending_distillations() == []
        assert model.await_count == 0
        connection_service.patch_connection('omp',enabled=True,paused=False,confirmed=True)
        assert await distillation_service.process_pending_distillations() == 1
    assert model.await_count == 1


@pytest.mark.anyio
async def test_unrelated_new_knowledge_does_not_cancel_current_result(race_env):
    _,q = race_env
    async def model(*args,**kwargs):
        content_service.create_content(title='Unrelated',content='Independent',category='knowledge')
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert (await distillation_service.process_distillation_queue_item(q['id']))['ok']
    assert processing_jobs.list_processing_jobs()[0]['state'] == 'succeeded'


@pytest.mark.anyio
async def test_other_project_decision_edit_does_not_cancel(race_env):
    from app.services import project_service
    _,q = race_env
    project = project_service.create_project(name='Unrelated project')
    target = content_service.create_content(title='Other decision',content='Old',category='decision',project_id=project['id'])
    async def model(*args,**kwargs):
        content_service.update_content(target['id'],content='New',expected_version=1)
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert (await distillation_service.process_distillation_queue_item(q['id']))['ok']


@pytest.mark.anyio
async def test_independent_concurrent_results_do_not_cancel_each_other(race_env):
    import asyncio
    _,first = race_env
    other = conversation_service.upsert_conversation(agent='omp',title='Independent',external_id='independent',
        messages=[{'role':'user','content':'Independent fact.'}])
    second = distillation_service.queue_distillation(other['id'])
    entered,finish = asyncio.Event(),asyncio.Event()
    async def model(host,payload,**kwargs):
        if payload['conversation_id'] == other['id']:
            entered.set()
            await finish.wait()
        return DistillResult(status='success',memories=[{'category':'knowledge','title':payload['title'],'content':'Independent output.'}])
    with patch('app.transport.run_distill',side_effect=model):
        second_task = asyncio.create_task(distillation_service.process_distillation_queue_item(second['id']))
        await entered.wait()
        assert (await distillation_service.process_distillation_queue_item(first['id']))['ok']
        finish.set()
        assert (await second_task)['ok']


@pytest.mark.anyio
async def test_known_stale_result_can_be_explicitly_requeued_once(race_env):
    conv,q = race_env
    target = content_service.create_content(title='Policy',content='Old',category='decision')
    async def model(*args,**kwargs):
        content_service.update_content(target['id'],content='User correction',expected_version=1)
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert not (await distillation_service.process_distillation_queue_item(q['id']))['ok']
    new = distillation_service.queue_distillation(conv['id'])
    assert new['id'] != q['id']
    model2 = AsyncMock(return_value=output())
    with patch('app.transport.run_distill',model2):
        assert (await distillation_service.process_distillation_queue_item(new['id']))['ok']
        assert distillation_service.queue_distillation(conv['id'])['id'] == new['id']
        assert await distillation_service.process_pending_distillations() == 0
    assert model2.await_count == 1


@pytest.mark.anyio
async def test_idle_batch_clock_resets_on_source_change_and_flushes_without_new_event(race_env,monkeypatch):
    from datetime import datetime,timedelta,timezone
    conv,q = race_env
    with db() as conn:
        timestamp = conn.execute('SELECT updated_at FROM processing_jobs WHERE id=?',(q['id'],)).fetchone()[0]
    start = datetime.fromisoformat(timestamp)
    clock = [start+timedelta(seconds=29)]
    monkeypatch.setattr(distillation_service,'now_iso',lambda:clock[0].isoformat())
    monkeypatch.setattr(processing_jobs,'now_iso',lambda:clock[0].isoformat())
    model = AsyncMock(return_value=output())
    with patch('app.transport.run_distill',model):
        assert await distillation_service.process_pending_distillations(min_idle_seconds=30) == 0
        conversation_service.upsert_conversation(agent='omp',title='Race source',external_id='race',
            messages=[{'id':'u1','role':'user','content':'Changed pending batch.'}])
        clock[0] = start+timedelta(seconds=31)
        assert await distillation_service.process_pending_distillations(min_idle_seconds=30) == 0
        clock[0] = start+timedelta(seconds=60)
        assert await distillation_service.process_pending_distillations(min_idle_seconds=30) == 1
    assert model.await_count == 1
    assert conversation_service.get_conversation(conv['id'])['updated_at'] == conv['updated_at']


@pytest.mark.anyio
async def test_unrelated_host_policy_change_does_not_cancel(race_env):
    _,q = race_env
    async def model(*args,**kwargs):
        connection_service.patch_connection('cursor',enabled=True,confirmed=True)
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert (await distillation_service.process_distillation_queue_item(q['id']))['ok']


@pytest.mark.anyio
async def test_deleted_target_recreated_with_same_title_cannot_accept_late_result(race_env):
    _,q = race_env
    target = content_service.create_content(title='Late result',content='Old user knowledge',category='knowledge')
    async def model(*args,**kwargs):
        content_service.delete_content(target['id'])
        content_service.create_content(title='Late result',content='New user knowledge',category='knowledge')
        return output()
    with patch('app.transport.run_distill',side_effect=model):
        assert not (await distillation_service.process_distillation_queue_item(q['id']))['ok']
    with db() as conn:
        assert not conn.execute("SELECT id FROM contents WHERE content='Must not overwrite newer state.'").fetchone()


@pytest.mark.anyio
async def test_unknown_cannot_be_requeued_even_with_new_event_identity(race_env):
    conv,q = race_env
    model = AsyncMock(side_effect=DistillExecutionError(
        "Provider outcome is not known", send_started=True, result_received=False, kind="transport",
    ))
    with patch('app.transport.run_distill',model):
        assert (await distillation_service.process_distillation_queue_item(q['id']))['status'] == 'unknown'
        again = distillation_service.queue_distillation(conv['id'],event_key='new-delivery',event_payload_hash='same-source')
        assert again['id'] == q['id']
        assert await distillation_service.process_pending_distillations() == 0
    assert model.await_count == 1


def test_global_memory_duplicate_is_not_reinserted(race_env):
    conv,_ = race_env
    memories = [{'category':'knowledge','title':'Global knowledge','content':'Exactly once.'}]
    assert distillation_service.submit_structured_distillation(conv['id'],memories)['created_count'] == 1
    assert distillation_service.submit_structured_distillation(conv['id'],memories)['created_count'] == 0
