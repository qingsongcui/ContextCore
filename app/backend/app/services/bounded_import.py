"""Bounded request upload handling and concurrent, off-loop document parsing."""
from __future__ import annotations

import asyncio
import threading
import weakref
from typing import Any

from fastapi import HTTPException, UploadFile

from app.services import importer_service

UPLOAD_READ_CHUNK_BYTES = 64 * 1024
MIB = 1024 * 1024
_LOOP_SEMAPHORES: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()
_LOOP_SEMAPHORES_LOCK = threading.Lock()


def _semaphore_for_current_loop() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _LOOP_SEMAPHORES_LOCK:
        semaphore = _LOOP_SEMAPHORES.get(loop)
        if semaphore is None:
            semaphore = asyncio.Semaphore(importer_service.MAX_IMPORT_CONCURRENCY)
            _LOOP_SEMAPHORES[loop] = semaphore
        return semaphore


def _upload_limit_label() -> str:
    limit = importer_service.MAX_UPLOAD_BYTES
    return f"{limit // MIB} MiB" if limit % MIB == 0 else f"{limit} bytes"


async def _read_upload(upload: UploadFile) -> bytearray:
    size = getattr(upload, "size", None)
    if size is not None and size > importer_service.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Upload exceeds the {_upload_limit_label()} import limit",
        )

    payload = bytearray()
    while True:
        chunk = await upload.read(UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        if len(payload) + len(chunk) > importer_service.MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds the {_upload_limit_label()} import limit",
            )
        payload.extend(chunk)
    return payload


async def import_upload(
    upload: UploadFile, filename: str, project_id: str | None = None
) -> dict[str, Any]:
    """Read at most the configured upload size, then parse in a bounded worker."""
    semaphore = _semaphore_for_current_loop()
    async with semaphore:
        # Keep raw upload buffers inside the same capacity budget as parsers;
        # otherwise many requests could each retain 32 MiB while waiting.
        payload = await _read_upload(upload)
        worker = asyncio.create_task(asyncio.to_thread(
            importer_service.import_document,
            file_bytes=payload,
            filename=filename,
            project_id=project_id,
        ))
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # Keep the capacity token until the actual worker has stopped; a
            # disconnected request must not allow unbounded orphaned parsers.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if worker.done():
                try:
                    worker.result()
                except BaseException:
                    pass
            raise
