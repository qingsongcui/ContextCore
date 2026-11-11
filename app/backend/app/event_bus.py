import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Event:
    name: str
    data: dict[str, Any]


_LIMIT = 200
_OVERFLOW = ('changed', None)
_lock = threading.RLock()
_shutting_down = False

@dataclass(eq=False)
class _Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[Event]
    pending: dict[tuple, Event] = field(default_factory=dict)
    scheduled: bool = False
    closed: bool = False


_subscribers: list[_Subscriber] = []
_pending_events: dict[int, dict[tuple, Event]] = {}


def _store(events: dict[tuple, Event], event: Event) -> None:
    """These are invalidations, not a durable job log; overflow means refresh all."""
    if _OVERFLOW in events:
        return
    key = (event.name, event.data.get('id') or event.data.get('target'))
    if event.name == 'changed' or (key not in events and len(events) >= _LIMIT):
        events.clear()
        events[_OVERFLOW] = Event('changed', {'reason': 'coalesced'})
    else:
        events[key] = event


def _flush(subscriber: _Subscriber) -> None:
    # Runs exclusively on the subscriber's loop; no Queue methods cross threads.
    with _lock:
        subscriber.scheduled = False
        if subscriber.closed:
            subscriber.pending.clear()
            return
        events = list(subscriber.pending.values())
        subscriber.pending.clear()
    queue = subscriber.queue
    if queue.qsize() + len(events) > _LIMIT:
        while not queue.empty():
            queue.get_nowait()
            queue.task_done()
        events = [Event('changed', {'reason': 'slow_consumer'})]
    for event in events:
        queue.put_nowait(event)


def publish_event(name: str, data: dict[str, Any]) -> None:
    with _lock:
        if _shutting_down:
            return
        for subscriber in list(_subscribers):
            if subscriber.closed or subscriber.loop.is_closed():
                subscriber.closed = True
                _subscribers.remove(subscriber)
                continue
            _store(subscriber.pending, Event(name=name, data=data))
            if not subscriber.scheduled:
                subscriber.scheduled = True
                try:
                    # At most one callback and a bounded buffer per subscriber,
                    # even when a producer outruns the event loop itself.
                    subscriber.loop.call_soon_threadsafe(_flush, subscriber)
                except RuntimeError:
                    subscriber.closed = True
                    subscriber.pending.clear()
                    _subscribers.remove(subscriber)


def queue_event(owner: object, name: str, data: dict[str, Any]) -> None:
    with _lock:
        _store(_pending_events.setdefault(id(owner), {}), Event(name=name, data=data))


def publish_pending_events(owner: object) -> None:
    with _lock:
        events = _pending_events.pop(id(owner), {})
    for event in events.values():
        publish_event(event.name, event.data)


def discard_pending_events(owner: object) -> None:
    with _lock:
        _pending_events.pop(id(owner), None)


async def subscribe() -> asyncio.Queue[Event]:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_LIMIT)
    with _lock:
        if _shutting_down:
            queue.put_nowait(Event('__shutdown__', {}))
        else:
            _subscribers.append(_Subscriber(loop, queue))
    return queue


def notify_shutdown() -> None:
    """Notify all current subscribers that server is shutting down so streams close promptly."""
    global _shutting_down
    with _lock:
        _shutting_down = True
        for subscriber in list(_subscribers):
            subscriber.closed = True
            subscriber.pending.clear()
            _subscribers.remove(subscriber)
            try:
                def _enqueue_shutdown(q: asyncio.Queue[Event]) -> None:
                    if q.full():
                        try:
                            q.get_nowait()
                            q.task_done()
                        except (asyncio.QueueEmpty, ValueError):
                            pass
                    try:
                        q.put_nowait(Event('__shutdown__', {}))
                    except asyncio.QueueFull:
                        pass

                subscriber.loop.call_soon_threadsafe(_enqueue_shutdown, subscriber.queue)
            except RuntimeError:
                pass


def reset_shutdown() -> None:
    """Reset shutdown state for server restart in the same process."""
    global _shutting_down
    with _lock:
        _shutting_down = False


def unsubscribe(queue: asyncio.Queue[Event]) -> None:
    with _lock:
        for subscriber in list(_subscribers):
            if subscriber.queue is queue:
                subscriber.closed = True
                subscriber.pending.clear()
                _subscribers.remove(subscriber)
