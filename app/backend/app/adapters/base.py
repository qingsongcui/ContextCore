"""
ContextCore Host Adapters - Base classes, security validation, and capability matrix.

All adapters operate in read-only mode and do not modify host configurations or user files.
All paths are strictly checked for authorization and symlink escape.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import stat as stat_module
import codecs
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Literal

from app.integrations.constants import HostId

AdapterErrorCode = Literal[
    "not_installed",
    "permission_denied",
    "unsupported_format",
    "schema_version_unsupported",
    "root_not_granted",
    "symlink_escape",
    "app_server_unavailable",
    "truncated",
    "file_changed",
    "unknown",
    "partial_sync",
]


@dataclass(frozen=True)
class AgentAdapterError(Exception):
    """Normalized exception raised by host adapters."""

    host: str
    code: AdapterErrorCode
    path: str | None = None
    detail: str = ""
    retryable: bool = False

    def __str__(self) -> str:
        loc = f" at {self.path}" if self.path else ""
        return f"[{self.host}] {self.code}{loc}: {self.detail}"


@dataclass(frozen=True)
class PartialHistoryError(AgentAdapterError):
    """Raised when a batch session read encounters unrecoverable files alongside valid sessions."""

    sessions: list[dict[str, Any]] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)

# Maximum file size for streaming session reading (2 GiB safety limit; line cap remains 8 MiB)
MAX_SESSION_FILE_SIZE = 2 * 1024 * 1024 * 1024
# Maximum file size for SQLite database reading (16 GiB safety limit)
MAX_DB_FILE_SIZE = 16 * 1024 * 1024 * 1024


# Maximum line length in session file (8 MiB safety limit)
MAX_LINE_LENGTH = 8 * 1024 * 1024

# Maximum process summary length (2 KB)
MAX_SUMMARY_LENGTH = 2048
PROCESS_SUMMARY_OMISSION = "- **omitted**: later process records were not copied into this summary"


def bound_process_summary(text: str, limit: int = MAX_SUMMARY_LENGTH, truncated: bool = False) -> str:
    """Keep a captured process summary inside its cap without splitting a record.

    Records that do not fit are left out. The omission line says this stored
    summary is not the rest of the process.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    notice = "\n" + PROCESS_SUMMARY_OMISSION
    if not truncated and len(text) <= limit:
        return text
    if text and len(text) + len(notice) <= limit:
        return text + notice
    room = limit - len(notice)
    if room < 1:
        return PROCESS_SUMMARY_OMISSION[:limit]
    head = text[:room]
    newline = head.rfind("\n")
    head = head[:newline] if newline >= 0 else ""
    body = (head + notice) if head else PROCESS_SUMMARY_OMISSION
    return body if len(body) <= limit else PROCESS_SUMMARY_OMISSION[:limit]


def raw_path_str(p: str | Path) -> str:
    """Return string path without triggering pathlib.Path._load_parts() / sys.intern()."""
    if isinstance(p, str):
        return p
    raw_paths = getattr(p, "_raw_paths", None)
    if isinstance(raw_paths, list) and len(raw_paths) == 1 and isinstance(raw_paths[0], str):
        return raw_paths[0]
    return str(p)


def validate_path_str(target_path: str | Path, roots: list[str | Path]) -> str:
    """
    Validate that target_path is within one of the authorized roots using pure os.path
    operations so CPython 3.12 pathlib does not intern ephemeral filenames via sys.intern().
    """
    target_raw = raw_path_str(target_path)
    if not roots:
        raise AgentAdapterError(
            host="unknown",
            code="root_not_granted",
            path=target_raw,
            detail="No authorized roots provided.",
        )

    try:
        resolved_target = os.path.realpath(os.path.expanduser(target_raw), strict=os.path.ALLOW_MISSING)
    except Exception as exc:
        raise AgentAdapterError(
            host="unknown",
            code="permission_denied",
            path=target_raw,
            detail=f"Failed to resolve target path: {exc}",
        ) from exc

    resolved_roots: list[tuple[str, bool]] = []
    for r in roots:
        try:
            r_real = os.path.realpath(os.path.expanduser(raw_path_str(r)), strict=True)
            st = os.stat(r_real)
            if stat_module.S_ISDIR(st.st_mode):
                resolved_roots.append((r_real, False))
            elif stat_module.S_ISREG(st.st_mode):
                resolved_roots.append((r_real, True))
        except (OSError, FileNotFoundError):
            continue

    if not resolved_roots:
        raise AgentAdapterError(
            host="unknown",
            code="root_not_granted",
            path=target_raw,
            detail="None of the provided roots exist or are authorized files/directories.",
        )

    is_contained = False
    for root_str, is_file in resolved_roots:
        if is_file:
            if resolved_target == root_str:
                is_contained = True
                break
        else:
            try:
                common = os.path.commonpath([resolved_target, root_str])
                if common == root_str:
                    is_contained = True
                    break
            except ValueError:
                # Different drive letters on Windows or incompatible paths
                continue

    if not is_contained:
        raise AgentAdapterError(
            host="unknown",
            code="symlink_escape",
            path=target_raw,
            detail=f"Path {resolved_target} escapes authorized roots {[r for r, _ in resolved_roots]}.",
        )

    return resolved_target


def validate_path(target_path: str | Path, roots: list[str | Path]) -> Path:
    """
    Validate that target_path is within one of the authorized roots.
    Strictly verifies canonical paths to prevent directory traversal and symlink escape.
    """
    return Path(validate_path_str(target_path, roots))


def deduplicate_roots(roots: list[str | Path]) -> list[Path]:
    """
    Resolve and deduplicate authorized roots, eliminating redundant sub-directories
    and identical paths while preserving order.
    """
    resolved: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for r in roots:
        try:
            canon = os.path.realpath(os.path.expanduser(raw_path_str(r)), strict=True)
            st = os.stat(canon)
        except (OSError, FileNotFoundError):
            continue
        if canon in seen:
            continue
        seen.add(canon)
        resolved.append((canon, stat_module.S_ISDIR(st.st_mode)))

    deduped: list[Path] = []
    for cand_str, _ in resolved:
        is_sub = False
        for other_str, other_is_dir in resolved:
            if cand_str == other_str or not other_is_dir:
                continue
            try:
                if os.path.commonpath([cand_str, other_str]) == other_str:
                    is_sub = True
                    break
            except ValueError:
                continue
        if not is_sub:
            deduped.append(Path(cand_str))
    return deduped if deduped else [Path(c) for c, _ in resolved]


def validate_file_safety(file_path: str | Path, host: str = "unknown") -> None:
    """Validate that the target path is a safe, readable regular file within limits."""
    raw_p = raw_path_str(file_path)
    try:
        st = os.stat(raw_p)
    except FileNotFoundError:
        raise AgentAdapterError(
            host=host,
            code="root_not_granted",
            path=raw_p,
            detail="Session file not found.",
        ) from None
    except PermissionError:
        raise AgentAdapterError(
            host=host,
            code="permission_denied",
            path=raw_p,
            detail="Permission denied accessing file.",
        ) from None

    if not stat_module.S_ISREG(st.st_mode):
        raise AgentAdapterError(
            host=host,
            code="unsupported_format",
            path=raw_p,
            detail="Target is not a regular file (directory, FIFO, or special device).",
        )

    ext = os.path.splitext(raw_p)[1]
    name = os.path.basename(raw_p)
    limit = MAX_DB_FILE_SIZE if ext in (".vscdb", ".db", ".sqlite") or name == "state.vscdb" else MAX_SESSION_FILE_SIZE
    if st.st_size > limit:
        raise AgentAdapterError(
            host=host,
            code="truncated",
            path=raw_p,
            detail=f"File size {st.st_size} exceeds maximum limit of {limit} bytes.",
        )


def _resolve_clonefile():
    if sys.platform != "darwin":
        return None
    try:
        libc_path = ctypes.util.find_library("c")
        if not libc_path:
            return None
        libc = ctypes.CDLL(libc_path, use_errno=True)
        fn = getattr(libc, "clonefile", None)
        if fn is None:
            return None
        fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32]
        fn.restype = ctypes.c_int
        return fn
    except Exception:
        return None


_CLONEFILE_FN = _resolve_clonefile()
_CLONE_NOFOLLOW = 0x0001


def _clone_or_copy_file(src: Path, dst: Path) -> None:
    """Copy src to dst using APFS copy-on-write clonefile when available, else 64 KiB streaming copy."""
    if _CLONEFILE_FN is not None:
        try:
            rc = _CLONEFILE_FN(os.fsencode(src), os.fsencode(dst), _CLONE_NOFOLLOW)
            if rc == 0:
                os.chmod(dst, 0o600)
                return
        except OSError:
            pass
    try:
        if os.path.lexists(dst):
            os.unlink(dst)
    except OSError:
        pass
    with open(src, "rb") as rf, open(dst, "wb") as wf:
        shutil.copyfileobj(rf, wf, length=65536)
    os.chmod(dst, 0o600)


class _IsolatedSqliteSnapshot:
    """Class-based context manager so frozen AgentAdapterError propagates without contextlib __traceback__ mutation."""

    def __init__(self, path: Path, *, host: str = "unknown") -> None:
        self.path = path
        self.host = host
        self._tmp_dir: tempfile.TemporaryDirectory[str] | None = None

    def __enter__(self) -> Path:
        path = self.path
        host = self.host
        validate_file_safety(path, host=host)
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{path}{suffix}")
            if sidecar.is_symlink():
                raise AgentAdapterError(
                    host=host,
                    code="symlink_escape",
                    path=str(path),
                    detail=f"{host} SQLite sidecar symlink is not allowed.",
                )

        def _lstat_sig(p: Path) -> tuple[int, int, int, int] | None:
            try:
                st = os.lstat(p)
            except FileNotFoundError:
                return None
            if stat_module.S_ISLNK(st.st_mode):
                raise AgentAdapterError(
                    host=host,
                    code="symlink_escape",
                    path=str(path),
                    detail=f"{host} SQLite path became a symlink.",
                )
            if not stat_module.S_ISREG(st.st_mode):
                raise AgentAdapterError(
                    host=host,
                    code="unsupported_format",
                    path=str(path),
                    detail=f"{host} SQLite path is not a regular file.",
                )
            return (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size)

        backoffs = (0.02, 0.05, 0.1)
        tmp_dir = tempfile.TemporaryDirectory(prefix=f"cc-sqlsnap-{host}-")
        try:
            staged_db = Path(tmp_dir.name) / "snap.db"
            stable = False
            for attempt in range(4):
                if attempt > 0:
                    time.sleep(backoffs[attempt - 1])
                for suffix in ("", "-wal", "-shm", "-journal"):
                    leftover = Path(f"{staged_db}{suffix}")
                    try:
                        if os.path.lexists(leftover):
                            os.unlink(leftover)
                    except OSError:
                        pass

                pre_db = _lstat_sig(path)
                if pre_db is None:
                    raise AgentAdapterError(
                        host=host,
                        code="file_changed",
                        path=str(path),
                        detail=f"{host} SQLite database disappeared before snapshot.",
                        retryable=True,
                    )
                wal_path = Path(f"{path}-wal")
                journal_path = Path(f"{path}-journal")
                pre_wal = _lstat_sig(wal_path)
                pre_journal = _lstat_sig(journal_path)
                wal_copied = pre_wal is not None and pre_wal[3] > 0
                journal_copied = pre_journal is not None and pre_journal[3] > 0

                try:
                    _clone_or_copy_file(path, staged_db)
                    if wal_copied:
                        _clone_or_copy_file(wal_path, Path(f"{staged_db}-wal"))
                    if journal_copied:
                        _clone_or_copy_file(journal_path, Path(f"{staged_db}-journal"))
                except OSError as exc:
                    if attempt == 3:
                        raise AgentAdapterError(
                            host=host,
                            code="file_changed",
                            path=str(path),
                            detail=f"{host} SQLite snapshot failed during concurrent modification: {exc}",
                            retryable=True,
                        ) from exc
                    continue

                post_db = _lstat_sig(path)
                post_wal = _lstat_sig(wal_path)
                post_journal = _lstat_sig(journal_path)

                db_stable = post_db == pre_db
                journal_stable = post_journal == pre_journal
                if wal_copied:
                    assert pre_wal is not None
                    wal_stable = (
                        post_wal is not None
                        and post_wal[:2] == pre_wal[:2]
                        and post_wal[3] >= pre_wal[3]
                    )
                    wal_appeared = False
                else:
                    wal_stable = True
                    wal_appeared = post_wal is not None and post_wal[3] > 0

                if db_stable and journal_stable and wal_stable and not (wal_appeared and attempt < 3):
                    stable = True
                    break

            if not stable:
                raise AgentAdapterError(
                    host=host,
                    code="file_changed",
                    path=str(path),
                    detail=f"{host} SQLite database was actively modified during snapshot.",
                    retryable=True,
                )

            self._tmp_dir = tmp_dir
            return staged_db
        except BaseException:
            tmp_dir.cleanup()
            raise

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> Literal[False]:
        if self._tmp_dir is not None:
            try:
                self._tmp_dir.cleanup()
            finally:
                self._tmp_dir = None
        return False


def isolated_sqlite_snapshot(path: Path, *, host: str = "unknown") -> _IsolatedSqliteSnapshot:
    """Return a context manager yielding a private temporary snapshot of path (+ non-empty -wal/-journal, never -shm).

    Isolating external WAL/rollback databases in a private TemporaryDirectory ensures
    concurrent host checkpoints or -shm truncation/unlinking cannot trigger SIGBUS
    in walFindFrame while preserving full WAL commit visibility.
    """
    return _IsolatedSqliteSnapshot(path, host=host)


# Official capability matrix based on publicly verified host documentation
CAPABILITY_MATRIX: dict[str, dict[str, dict[str, str]]] = {
    "codex": {
        "history": {
            "status": "available",
            "detail": "Supported via local rollout JSONL parser (session_meta, item_completed, response_item) and official app-server JSON-RPC.",
        },
        "capture": {
            "status": "available",
            "detail": "Supported via official hooks (UserPromptSubmit, Stop, SessionEnd).",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and the handoff skill. Lifecycle hooks stay silent.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Supported via Stop hook and app-server items injection.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via $skill-name and UserPromptSubmit prompt interception.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "Codex CLI lacks documented no-tools isolation; compute stays OMP-only.",
        },
    },
    "omp": {
        "history": {
            "status": "available",
            "detail": "Supported via official JSONL session v3 tree/compaction parser.",
        },
        "capture": {
            "status": "available",
            "detail": "Supported via extension input/before_agent_start events and session JSONL.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP and /contextcore. Extension auto events stay silent.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Supported via session JSONL and extension hooks.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via extension input event and /contextcore slash command.",
        },
        "compute": {
            "status": "available",
            "detail": "Supported via omp -p --mode json or RPC.",
        },
    },
    "cursor": {
        "history": {
            "status": "partial",
            "detail": "Supported via local SQLite state.vscdb (composerHeaders and cursorDiskKV bubbleId) and Cursor CLI store.db (meta + protobuf/JSON blobs under ~/.cursor/chats).",
        },
        "capture": {
            "status": "available",
            "detail": "Supported via official hooks (beforeSubmitPrompt, afterAgentResponse, postToolUse).",
        },
        "memory_read": {
            "status": "partial",
            "detail": "Opt-in via MCP and /contextcore. No per-prompt hook injection.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Supported via hooks incremental collection.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via /contextcore skill and MCP tool; no prompt-injection hook.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "Cursor CLI lacks documented no-tools isolation; compute stays OMP-only.",
        },
    },
    "antigravity": {
        "history": {
            "status": "partial",
            "detail": "Supported via local transcript.jsonl and transcript_full.jsonl across 2.0, IDE, and CLI brain directories (<USER_REQUEST> extraction and PLANNER_RESPONSE); internal database formats remain unverified.",
        },
        "capture": {
            "status": "unsupported",
            "detail": "Unsupported for dialogue parsing; hook events provide metadata and tool events only, transcript line schema is undocumented.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and the handoff skill. Lifecycle hooks stay silent.",
        },
        "memory_write": {
            "status": "unsupported",
            "detail": "Unsupported for structured history writeback; Stop hook captures boundary only, raw transcript remains an opaque artifact.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via /<skill-name> slash command; hook payload does not include user prompt text.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "Antigravity CLI lacks documented no-tools isolation; compute stays OMP-only.",
        },
    },
    "claude-code": {
        "history": {
            "status": "available",
            "detail": "Read-only JSONL parser for ~/.claude/projects with parentUuid leaf-chain reconstruction; sidechain/meta excluded; filename is the identity authority.",
        },
        "capture": {
            "status": "partial",
            "detail": "User prompts captured via UserPromptSubmit; assistant replies complete after transcript sync.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and /contextcore. Lifecycle hooks stay silent.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP save_memory tool.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via UserPromptSubmit interception and the /contextcore skill.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "No documented no-tools isolation; compute stays OMP-only.",
        },
    },
    "opencode": {
        "history": {
            "status": "available",
            "detail": "Read-only opencode.db session/message/part reader; subagent sessions excluded; legacy JSON store is a fallback only.",
        },
        "capture": {
            "status": "partial",
            "detail": "Plugin captures prompts via chat.message; assistant text becomes complete after SQLite sync.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and /contextcore. The plugin captures prompts and does not insert chat parts.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP remote server.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via chat.message interception and the /contextcore command.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "No documented no-tools isolation; compute stays OMP-only.",
        },
    },
}


def iter_complete_jsonl(path: Path, *, encoding: str = "utf-8", offset: int = 0, host: str = "unknown", max_line_length: int | None = None) -> Iterator[str]:
    """Yield committed lines from one bounded, stable file snapshot.

    Memory is bounded by one line (MAX_LINE_LENGTH), not by file size. The
    unterminated tail is never decoded/yielded, including partial UTF-8 bytes.
    Mutated/replaced sources fail when iteration completes, before a parser can
    return its session for import. A caller stopping early has not verified a
    complete snapshot and must not commit it as one.
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError("offset must be a nonnegative byte offset")
    line_limit = MAX_LINE_LENGTH if max_line_length is None else max(1, int(max_line_length))
    validate_file_safety(path, host=host)
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    before = path.stat()
    try:
        with path.open("rb", buffering=8192) as handle:
            if identity(os.fstat(handle.fileno())) != identity(before):
                raise AgentAdapterError(host=host, code="file_changed", path=str(path), detail="Source changed before read", retryable=True)
            # Find the last committed newline without loading an unfinished tail.
            end = before.st_size
            boundary = 0
            chunk_size = min(65536, line_limit + 1)
            tail = b""
            while end > offset:
                begin = max(offset, end - chunk_size)
                handle.seek(begin)
                tail = handle.read(end - begin)
                newline = tail.rfind(b"\n")
                if newline >= 0:
                    boundary = begin + newline + 1
                    break
                end = begin
            # Do not retain the final boundary chunk while allocating the
            # first decoded line, especially when that line hits its limit.
            del tail
            handle.seek(offset)
            decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
            line_number = 0
            while handle.tell() < boundary:
                raw = handle.readline(min(line_limit + 1, boundary - handle.tell()))
                if not raw:
                    raise AgentAdapterError(host=host, code="file_changed", path=str(path), detail="Source truncated during read", retryable=True)
                line_number += 1
                if len(raw) > line_limit or not raw.endswith(b"\n"):
                    raise AgentAdapterError(host=host, code="truncated", path=str(path), detail="Committed JSONL line exceeds size limit")
                try:
                    yield decoder.decode(raw, final=False)
                except UnicodeDecodeError:
                    raise AgentAdapterError(host=host, code="unsupported_format", path=str(path), detail=f"Committed JSONL is not valid {encoding} at line {line_number}") from None
            try:
                decoder.decode(b"", final=True)
            except UnicodeDecodeError:
                raise AgentAdapterError(host=host, code="unsupported_format", path=str(path), detail=f"Committed JSONL is not valid {encoding}") from None
            opened_after = os.fstat(handle.fileno())
        after = path.stat()
    except OSError as exc:
        raise AgentAdapterError(host=host, code="file_changed", path=str(path), detail="Source unavailable during snapshot read", retryable=True) from exc
    if identity(before) != identity(opened_after) or identity(before) != identity(after):
        raise AgentAdapterError(host=host, code="file_changed", path=str(path), detail="Source changed during read; retry a stable snapshot", retryable=True)


def read_complete_jsonl(path: Path, *, encoding: str = "utf-8", offset: int = 0) -> str:
    """Read a stable complete-line prefix; a writer's unterminated tail is uncommitted.

    Decoding only committed bytes also tolerates a UTF-8 character split across writes.
    Read errors and file mutation remain explicit rather than silently empty success.
    """
    before = path.stat()
    with path.open("rb", buffering=8192) as handle:
        handle.seek(offset)
        raw = handle.read()
    after = path.stat()
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    if identity(before) != identity(after):
        raise AgentAdapterError(host="unknown", code="file_changed", path=str(path),
                                detail="Source changed during read; retry a stable snapshot", retryable=True)
    boundary = raw.rfind(b"\n") + 1
    return raw[:boundary].decode(encoding)


def usage_counts(value: Any) -> dict[str, Any]:
    """Copy numeric usage metadata only; never retain arbitrary raw response JSON."""
    if not isinstance(value, dict):
        return {}
    allowed = {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
               "cache_read_tokens", "cache_write_tokens", "cache_read_input_tokens", "cache_creation_input_tokens",
               "cached_input_tokens", "reasoning_tokens", "reasoning_output_tokens", "input", "output",
               "cacheRead", "cacheWrite", "reasoning", "read", "write", "cached_tokens"}
    nested = {"cache", "input_tokens_details", "output_tokens_details", "prompt_tokens_details", "completion_tokens_details"}
    result = {key: number for key, number in value.items() if key in allowed and isinstance(number, int) and not isinstance(number, bool)}
    for key in nested:
        if isinstance(value.get(key), dict):
            result[key] = usage_counts(value[key])
    return result


def usage_metadata_record(entry: dict[str, Any]) -> dict[str, Any] | None:
    """Retain documented native usage envelopes without message/tool bodies."""
    text_keys = {"id", "uuid", "timestamp", "model", "provider", "requestId", "request_id", "type"}
    safe = {key: value for key, value in entry.items() if key in text_keys and isinstance(value, (str, int, float))}
    found = False
    for key in ("message", "payload", "info"):
        child = entry.get(key)
        if not isinstance(child, dict):
            continue
        cleaned = {name: value for name, value in child.items()
                   if name in text_keys | {"modelID", "providerID"} and isinstance(value, (str, int, float))}
        for count_key in ("usage", "tokens"):
            counts = usage_counts(child.get(count_key))
            if counts:
                cleaned[count_key] = counts
                found = True
        if key == "info" and isinstance(child.get("time"), dict):
            cleaned["time"] = {name: value for name, value in child["time"].items() if name in {"created", "completed"} and isinstance(value, (str, int, float))}
        if key == "payload" and isinstance(child.get("info"), dict):
            token_info = {}
            for count_key in ("total_token_usage", "last_token_usage"):
                counts = usage_counts(child["info"].get(count_key))
                if counts:
                    token_info[count_key] = counts
                    found = True
            if token_info:
                cleaned["info"] = token_info
        safe[key] = cleaned
    for key in ("usage", "tokens"):
        counts = usage_counts(entry.get(key))
        if counts:
            safe[key] = counts
            found = True
    return safe if found else None
