"""Filesystem notifications wake reconciliation; they never grant new roots."""
import asyncio
import contextlib
from pathlib import Path

from app.database import db, decode


def authorized_watch_roots() -> tuple[str, ...]:
    roots = set()
    with db() as conn:
        rows = conn.execute("SELECT paths FROM connections WHERE enabled=1 AND paused=0")
        for row in rows:
            for raw in decode(row["paths"], []):
                path = Path(raw).expanduser()
                if path.exists():
                    roots.add(str((path.parent if path.is_file() else path).resolve()))
    return tuple(sorted(roots))


async def watch_authorized_histories(wakeup: asyncio.Event):
    try:
        from watchfiles import awatch
    except ImportError:
        return  # The periodic reconciler remains authoritative.
    while True:
        roots = await asyncio.to_thread(authorized_watch_roots)
        if not roots:
            await asyncio.sleep(2)
            continue
        try:
            last_root_check = asyncio.get_running_loop().time()
            check_interval = 25.0
            async with contextlib.aclosing(
                awatch(*roots, debounce=500, step=50, rust_timeout=5000,
                       yield_on_timeout=True, ignore_permission_denied=True)
            ) as watcher:
                async for changes in watcher:
                    now = asyncio.get_running_loop().time()
                    if changes:
                        wakeup.set()
                    if changes or (now - last_root_check >= check_interval):
                        last_root_check = now
                        current = await asyncio.to_thread(authorized_watch_roots)
                        if current != roots:
                            wakeup.set()
                            break  # Recreate watchers after authorization changes.
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError):
            # Overflow, deleted roots and sleep/wake recover via a full reconcile.
            wakeup.set()
            await asyncio.sleep(2)
