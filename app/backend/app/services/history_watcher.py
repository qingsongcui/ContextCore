"""Filesystem notifications wake reconciliation; they never grant new roots."""
import asyncio
import contextlib
import os
import re
from pathlib import Path
from typing import Any, Callable

from app.database import db, decode
from app.config import settings


_reconcile_wakeups: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Event]] = {}
_RELEVANT_HISTORY_FILE_RE = re.compile(
    r"\.(?:jsonl|json|sqlite\d*(?:-wal|-shm|-journal)?|vscdb(?:-wal|-shm|-journal)?|db(?:-wal|-shm|-journal)?)$",
    re.IGNORECASE,
)
_WATCH_IGNORED_DIRS = frozenset({
    "cache",
    "cacheddata",
    "caches",
    "code cache",
    "gpucache",
    "dawncache",
    "blob_storage",
    "logs",
    "log",
    "crashpad",
    "telemetry",
    "tmp",
    "temp",
    "node_modules",
    "__pycache__",
    "service worker",
})


def request_history_reconcile() -> bool:
    """Wake the existing lifecycle worker from a request thread; never scan inline."""
    target = _reconcile_wakeups.get(str(settings.db_path))
    if target is None:
        return False
    loop, wakeup = target
    try:
        loop.call_soon_threadsafe(wakeup.set)
    except RuntimeError:
        return False
    return True


def _authorized_watch_spec() -> tuple[tuple[str, ...], frozenset[str], frozenset[str]]:
    roots: set[str] = set()
    file_targets: set[str] = set()
    dir_roots: set[str] = set()
    with db() as conn:
        rows = conn.execute("SELECT paths FROM connections WHERE enabled=1 AND paused=0")
        for row in rows:
            for raw in decode(row["paths"], []):
                path = Path(raw).expanduser()
                if not path.exists():
                    continue
                resolved = path.resolve()
                if path.is_file():
                    roots.add(str(resolved.parent))
                    file_targets.add(str(resolved))
                else:
                    resolved_str = str(resolved)
                    roots.add(resolved_str)
                    dir_roots.add(resolved_str)
    return tuple(sorted(roots)), frozenset(file_targets), frozenset(dir_roots)


def authorized_watch_roots() -> tuple[str, ...]:
    return _authorized_watch_spec()[0]


def _is_relevant_history_path(
    _change: Any,
    path_str: str,
    *,
    file_targets: frozenset[str],
    dir_roots: frozenset[str],
) -> bool:
    try:
        resolved = os.path.realpath(path_str)
    except OSError:
        resolved = os.path.abspath(path_str)
    if os.path.isdir(resolved):
        return False

    matched_dir: str | None = None
    for dr in dir_roots:
        if resolved == dr or resolved.startswith(dr + os.sep):
            if matched_dir is None or len(dr) > len(matched_dir):
                matched_dir = dr

    if matched_dir is not None:
        rel = os.path.relpath(resolved, matched_dir)
        rel_parts = [part.lower() for part in Path(rel).parts[:-1]]
        parent_base = os.path.basename(matched_dir).lower()
        for idx, part in enumerate(rel_parts):
            if part == "logs":
                prev = rel_parts[idx - 1] if idx > 0 else parent_base
                if prev != ".system_generated":
                    return False
            elif part in _WATCH_IGNORED_DIRS:
                return False
        return bool(_RELEVANT_HISTORY_FILE_RE.search(os.path.basename(resolved)))

    for ft in file_targets:
        if resolved in (ft, f"{ft}-wal", f"{ft}-shm", f"{ft}-journal"):
            return True
    return False


def _build_watch_filter(
    file_targets: frozenset[str],
    dir_roots: frozenset[str],
) -> Callable[[Any, str], bool]:
    return lambda change, path_str: _is_relevant_history_path(
        change,
        path_str,
        file_targets=file_targets,
        dir_roots=dir_roots,
    )


async def watch_authorized_histories(wakeup: asyncio.Event):
    key = str(settings.db_path)
    target = (asyncio.get_running_loop(), wakeup)
    _reconcile_wakeups[key] = target
    try:
        await _watch_authorized_histories(wakeup)
    finally:
        if _reconcile_wakeups.get(key) == target:
            _reconcile_wakeups.pop(key, None)


async def _watch_authorized_histories(wakeup: asyncio.Event):
    try:
        from watchfiles import awatch
    except ImportError:
        awatch = None  # Root polling and relay wakeups still feed the existing worker.
    previous_spec: tuple[tuple[str, ...], frozenset[str], frozenset[str]] | None = None
    while True:
        spec = await asyncio.to_thread(_authorized_watch_spec)
        roots, file_targets, dir_roots = spec
        if spec != previous_spec:
            previous_spec = spec
            wakeup.set()
        if not roots or awatch is None:
            await asyncio.sleep(2)
            continue
        try:
            last_root_check = asyncio.get_running_loop().time()
            check_interval = 2.0
            watch_filter = _build_watch_filter(file_targets, dir_roots)
            async with contextlib.aclosing(
                # Native stores can live in hidden/generated directories (.system_generated/logs).
                # Filter explicitly by authorized roots and history file suffixes.
                awatch(*roots, watch_filter=watch_filter, debounce=500, step=50, rust_timeout=1000,
                       yield_on_timeout=True, ignore_permission_denied=True)
            ) as watcher:
                async for changes in watcher:
                    now = asyncio.get_running_loop().time()
                    if changes:
                        wakeup.set()
                    if changes or (now - last_root_check >= check_interval):
                        last_root_check = now
                        current = await asyncio.to_thread(_authorized_watch_spec)
                        if current != spec:
                            wakeup.set()
                            break  # Recreate watchers after authorization changes.
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError):
            # Overflow, deleted roots and sleep/wake recover via a full reconcile.
            wakeup.set()
            await asyncio.sleep(2)
