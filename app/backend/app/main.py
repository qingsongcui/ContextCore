import asyncio
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.config import settings
from app.database import create_schema, seed_data, db, decode, now_iso
from app.exceptions import register_exception_handlers
from app.security import SecurityMiddleware
from app.mcp_server import contextcore_mcp
from app.services import conversation_service, project_service, connection_service, distillation_service
from app.routers import (
    session,
    status,
    projects,
    content,
    conversations,
    connections,
    conflicts,
    handoffs,
    audit,
    settings as settings_router,
    events,
)


async def _background_worker(wakeup: asyncio.Event | None = None):
    """Background lifecycle worker: performs background sync and consumes distillation queue off event loop."""
    from app.services.sync_budget import SyncBudget, SyncCancelled
    stop = threading.Event()
    sync_task = None
    while True:
        try:
            if wakeup:
                wakeup.clear()
            def _sync_all():
                import os
                from app.integrations.cc_hook import drain_notifications
                drain_notifications(os.environ.get("CONTEXTCORE_LISTEN_URL", "http://127.0.0.1:8420"))
                pending = False
                with db() as conn:
                    enabled_conns = conn.execute(
                        "SELECT id FROM connections WHERE enabled = 1 AND paused = 0"
                    ).fetchall()
                for c in enabled_conns:
                    if stop.is_set():
                        break
                    cid = c["id"]
                    try:
                        result = connection_service.sync_connection(cid, force=False, budget=SyncBudget(stop))
                        pending = pending or bool(result.get("pending_sources"))
                    except SyncCancelled:
                        break
                    except Exception:
                        # Detailed error is already recorded in connections table by sync_connection
                        pass
                return pending

            # Run blocking filesystem / sync operations off the event loop
            sync_task = asyncio.create_task(asyncio.to_thread(_sync_all))
            pending = await asyncio.shield(sync_task)

            # Consume pending distillation queue entries
            await distillation_service.process_pending_distillations(min_idle_seconds=30)

            from app.services.derivation_service import process_cleanup_jobs
            await asyncio.to_thread(process_cleanup_jobs)

            # Coalesce notifications without a hot loop when the observed host
            # writes continuously. Backfill progresses in durable source batches.
            await asyncio.sleep(5.0)
            if pending:
                continue
            if wakeup is None:
                await asyncio.sleep(25.0)
            else:
                try:
                    await asyncio.wait_for(wakeup.wait(), timeout=25.0)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            stop.set()
            # Cancelling to_thread alone leaves its writer running. Finish the
            # active atomic unit and join before reporting service shutdown.
            if sync_task is not None:
                await asyncio.shield(sync_task)
            break
        except Exception:
            await asyncio.sleep(5.0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        from sse_starlette.sse import AppStatus
        AppStatus.should_exit = False
    except ImportError:
        pass
    try:
        from app.event_bus import reset_shutdown
        reset_shutdown()
    except Exception:
        pass
    create_schema()
    seed_data()
    from app.services.processing_jobs import initialize_processing_jobs, recover_processing_jobs
    initialize_processing_jobs()
    recover_processing_jobs()
    from app.services.routing_service import recover_interrupted_attempts
    recover_interrupted_attempts()
    try:
        from app.routers.connections import discover_connections_endpoint
        discover_connections_endpoint()
    except Exception:
        pass
    from app.services.history_watcher import watch_authorized_histories
    wakeup = asyncio.Event()
    worker_task = asyncio.create_task(_background_worker(wakeup))
    watcher_task = asyncio.create_task(watch_authorized_histories(wakeup))
    try:
        async with AsyncExitStack() as stack:
            await stack.enter_async_context(contextcore_mcp.session_manager.run())
            yield
    finally:
        try:
            from app.event_bus import notify_shutdown
            notify_shutdown()
        except Exception:
            pass
        worker_task.cancel()
        watcher_task.cancel()
        try:
            await watcher_task
        except asyncio.CancelledError:
            pass
        try:
            await worker_task
        except asyncio.CancelledError:
            pass


app = FastAPI(
    title=settings.app_title,
    version=settings.app_version,
    lifespan=lifespan,
)

# 1. CORS configurations for local dev Vite proxy / same-origin
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. Security middleware registered AFTER CORSMiddleware so it wraps outermost and runs FIRST
app.add_middleware(SecurityMiddleware)

# 3. Preserve structured HTTP error details used by recovery flows.
register_exception_handlers(app)

# 4. Bind modular REST routers (clean cutover /api routes)
app.include_router(session.router)
app.include_router(status.router)
app.include_router(projects.router)
app.include_router(content.router)
app.include_router(conversations.router)
app.include_router(connections.router)
app.include_router(conflicts.router)
app.include_router(handoffs.router)
app.include_router(audit.router)
app.include_router(settings_router.router)
app.include_router(events.router)
from app.routers import processing
app.include_router(processing.router)
from app.routers import retrieval, usage, routing, manage, provider_checks, migration
app.include_router(retrieval.router)
app.include_router(usage.router)
app.include_router(routing.router)
app.include_router(manage.router)
app.include_router(provider_checks.router)
app.include_router(migration.router)

# 5. Static uploads directory (protected by loopback/host middleware)
settings.upload_dir.mkdir(parents=True, exist_ok=True)
app.mount("/uploads", StaticFiles(directory=settings.upload_dir), name="uploads")

# 6. MCP streamable HTTP mount
app.mount("/mcp", contextcore_mcp.streamable_http_app())
