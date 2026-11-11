import asyncio
import json
from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse, ServerSentEvent
from app.event_bus import subscribe, unsubscribe

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("")
async def sse_events():
    async def event_generator():
        queue = await subscribe()
        try:
            # Yield initial connection confirmation
            init_data = json.dumps({"connected": True})
            yield ServerSentEvent(event="connect", data=init_data)
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=25.0)
                    queue.task_done()
                    if event.name == "__shutdown__":
                        break
                    data_str = json.dumps(event.data, ensure_ascii=False)
                    yield ServerSentEvent(event=event.name, data=data_str)
                except asyncio.TimeoutError:
                    # Heartbeat
                    yield ServerSentEvent(comment="heartbeat")
        finally:
            unsubscribe(queue)

    return EventSourceResponse(
        event_generator(),
        ping=0,
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
