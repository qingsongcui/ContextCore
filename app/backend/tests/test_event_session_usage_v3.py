"""Synthetic regressions for bounded notifications, sessions and usage semantics."""
import asyncio
import threading

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import event_bus, security
from app.database import create_schema
from app.routers.usage import router
from app.services import usage_service


def test_burst_to_slow_sse_subscriber_is_bounded_and_signals_refresh():
    async def exercise():
        errors = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: errors.append(context))
        queue = await event_bus.subscribe()
        try:
            # The producer outruns an event loop with a slow SSE consumer.
            def produce():
                for index in range(10_000):
                    event_bus.publish_event('conversation.changed', {'id': str(index)})
            thread = threading.Thread(target=produce)
            thread.start()
            thread.join()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            messages = []
            while not queue.empty():
                messages.append(queue.get_nowait())
            assert not errors
            assert len(messages) <= 200
            assert any(message.name == 'changed' for message in messages)
        finally:
            event_bus.unsubscribe(queue)
            loop.set_exception_handler(previous_handler)
    asyncio.run(exercise())


def test_saturated_consumer_receives_a_generic_refresh_instead_of_dropping_latest():
    async def exercise():
        queue = await event_bus.subscribe()
        try:
            for index in range(200):
                queue.put_nowait(event_bus.Event('audit.created', {'id': str(index)}))
            event_bus.publish_event('content.changed', {'id': 'latest'})
            await asyncio.sleep(0)
            messages = [queue.get_nowait() for _ in range(queue.qsize())]
            assert any(message.name == 'changed' for message in messages)
        finally:
            event_bus.unsubscribe(queue)
    asyncio.run(exercise())


def test_transaction_events_are_bounded_but_never_published_on_rollback():
    async def exercise():
        queue = await event_bus.subscribe()
        owner = object()
        try:
            for index in range(10_000):
                event_bus.queue_event(owner, 'content.changed', {'id': str(index)})
            assert len(event_bus._pending_events[id(owner)]) <= 200
            event_bus.discard_pending_events(owner)
            event_bus.publish_pending_events(owner)
            await asyncio.sleep(0)
            assert queue.empty()
            event_bus.queue_event(owner, 'content.changed', {'id': 'committed'})
            event_bus.publish_pending_events(owner)
            await asyncio.sleep(0)
            assert queue.get_nowait().data['id'] == 'committed'
        finally:
            event_bus.discard_pending_events(owner)
            event_bus.unsubscribe(queue)
    asyncio.run(exercise())


def test_unsubscribed_or_closed_loop_never_breaks_a_committed_publish():
    loop = asyncio.new_event_loop()
    queue = loop.run_until_complete(event_bus.subscribe())
    loop.close()
    event_bus.publish_event('content.changed', {'id': 'closed-loop'})
    event_bus.unsubscribe(queue)


def test_create_session_reaps_all_expired_entries(monkeypatch):
    monkeypatch.setattr(security, '_SESSIONS', {})
    moment = [1_000_000.0]
    monkeypatch.setattr(security.time, 'time', lambda: moment[0])
    for _ in range(1000):
        security.create_session()
    moment[0] += security.SESSION_TTL_SECONDS + 1
    session, token = security.create_session()
    assert len(security._SESSIONS) == 1
    assert security.validate_csrf_token(token, session)


def test_session_capacity_uses_lru_and_preserves_recently_active_cookie(monkeypatch):
    monkeypatch.setattr(security, '_SESSIONS', {})
    monkeypatch.setattr(security, 'MAX_SESSIONS', 4, raising=False)
    first, first_token = security.create_session()
    old, _ = security.create_session()
    security.create_session()
    security.create_session()
    assert security.get_csrf_token_for_session(first) == first_token
    security.create_session()
    assert len(security._SESSIONS) == 4
    assert security.get_csrf_token_for_session(old) is None
    assert security.get_csrf_token_for_session(first) == first_token


@pytest.mark.parametrize('invalid', ['false', 'true', 0, 1, [], {}])
def test_usage_api_rejects_nonboolean_cache_semantics_without_partial_writes(invalid):
    create_schema()
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    observation = {'origin': 'manual', 'observation_id': 'invalid',
                   'started_at': '2026-10-24T00:00:00Z', 'input_includes_cache': invalid,
                   'raw_usage': {'input_tokens': 100, 'cache_read_tokens': 20}}
    response = client.post('/api/usage/ingest', json={'observations': [observation]})
    assert response.status_code == 422
    assert usage_service.requests()['total'] == 0


def test_false_true_and_unknown_cache_semantics_keep_their_documented_counts():
    raw = {'input_tokens': 100, 'cache_read_tokens': 20, 'output_tokens': 5}
    assert usage_service.normalize_usage(raw, input_includes_cache=False)['input_tokens'] == 120
    assert usage_service.normalize_usage(raw, input_includes_cache=False)['uncached_input_tokens'] == 100
    assert usage_service.normalize_usage(raw, input_includes_cache=True)['input_tokens'] == 100
    assert usage_service.normalize_usage(raw, input_includes_cache=True)['uncached_input_tokens'] == 80
    assert usage_service.normalize_usage(raw, input_includes_cache=None)['input_tokens'] == 100
