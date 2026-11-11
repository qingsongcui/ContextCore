import hashlib
import ipaddress
import json
import json as json_module
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from fastapi import HTTPException
from app.database import db, now_iso, row_to_dict, log_audit, encode, get_app_setting, set_app_setting
from app.integrations.constants import DEFAULT_BASE_URL


def resolve_trusted_base_url(base_url: str | None = None) -> str:
    """Resolve trusted service base URL, reusing request/runtime metadata while rejecting unauthenticated remote origins."""
    if base_url:
        try:
            parsed = urlsplit(base_url.strip())
            if parsed.scheme in ("http", "https") and parsed.hostname:
                hostname = parsed.hostname.lower()
                is_loopback = False
                if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
                    is_loopback = True
                else:
                    try:
                        is_loopback = ipaddress.ip_address(hostname).is_loopback
                    except ValueError:
                        is_loopback = False
                if is_loopback:
                    clean_port = f":{parsed.port}" if parsed.port else ""
                    return f"{parsed.scheme}://{hostname}{clean_port}"
        except Exception:
            pass
        # Reject unauthenticated remote origins; fall through to local runtime metadata

    # Reuse existing runtime metadata
    try:
        from app.runtime import read_pid_file
        pid_info = read_pid_file()
        if pid_info and pid_info.get("port"):
            host = pid_info.get("host", "127.0.0.1")
            if host in ("0.0.0.0", ""):
                host = "127.0.0.1"
            port = pid_info.get("port")
            return f"http://{host}:{port}"
    except Exception:
        pass
    env_url = os.environ.get("CONTEXTCORE_BASE_URL")
    if env_url:
        return env_url.rstrip("/")
    return DEFAULT_BASE_URL


def _is_legitimately_empty_error(host: str, exc: Any) -> bool:
    """Distinguish legitimately empty initial environments from real format corruption."""
    detail = (getattr(exc, "detail", None) or "").lower()
    code = getattr(exc, "code", "")
    if host == "codex" and code == "root_not_granted" and "no local codex jsonl" in detail:
        return True
    if host == "cursor" and code == "unsupported_format" and "no extractable" in detail:
        return True
    return False

# Compatibility export; discovery has one source of truth in HostDefinition.
from app.services.host_registry import HOST_DEFINITIONS
KNOWN_HOST_ROOTS: dict[str, list[str]] = {
    host: [str(Path.home() / path) for path in definition.roots]
    for host, definition in HOST_DEFINITIONS.items()
}


def _capability_detail(agent_id: str, name: str, fallback: str) -> str:
    details = {
        ("codex", "history"): "只读解析授权目录中的 rollout JSONL 和会话索引；兼容已观察到的事件格式，未知格式明确报错。",
        ("codex", "capture"): "可通过官方 hook 采集新增对话（SessionStart / UserPromptSubmit / Stop 已真机验证）。",
        ("codex", "memory_read"): "需要你主动说“接着做”或调用技能，Agent 才会通过 MCP 读取记忆。Hook 不会自动灌入。",
        ("codex", "memory_write"): "授权后可通过 MCP 写入共享记忆。这不是历史会话重建。",
        ("codex", "handoff"): "你在新会话里要求接棒后，Agent 回读六项并等你确认。确认后才可继续。",
        ("codex", "compute"): "官方命令缺少可证明的无工具隔离，当前禁止用它提炼记忆。",
        ("omp", "history"): "只读解析授权目录中的 JSONL 会话，保留原始对话并单独整理过程信息。",
        ("omp", "capture"): "可通过扩展事件采集新增对话。配置写入后才生效。",
        ("omp", "memory_read"): "需要调用 /contextcore 或 MCP 读取记忆。扩展自动事件不会改写你的输入。",
        ("omp", "memory_write"): "授权后可通过 MCP 写入共享记忆。",
        ("omp", "handoff"): "可通过 /contextcore 和扩展事件接棒。不覆盖宿主内置 /handoff。",
        ("omp", "compute"): "使用 --no-tools 等限制调用已登录 OMP，解析最终 JSONL 消息；临时配置禁止自动重试和备用模型切换，失败会明确显示。",
        ("cursor", "history"): "只读解析 state.vscdb 的 composerHeaders 与 cursorDiskKV 消息，已在本机历史验证；格式升级可能需要适配。",
        ("cursor", "capture"): "支持通过本地 SQLite state.vscdb 增量同步新对话。官方 hook 的实际触发与消费尚未真机验证。",
        ("cursor", "memory_read"): "需要 /contextcore-handoff 或 MCP 读取记忆。提交前不能自动附带上下文。",
        ("cursor", "memory_write"): "授权后可通过 MCP 写入共享记忆，但不能保证每次提问前自动读到。",
        ("cursor", "handoff"): "需要显式技能或“接着做”。提交前不能自动附带接棒卡。",
        ("cursor", "compute"): "官方命令缺少可证明的无工具隔离，当前禁止用它提炼记忆。",
        ("antigravity", "history"): "只读解析 brain 会话目录中的 transcript JSONL，支持桌面与 CLI 记录；未提供的原文不会补造。",
        ("antigravity", "capture"): "hook 只有元数据和工具事件，不含用户原话。",
        ("antigravity", "memory_read"): "需要技能或 MCP 读取记忆。生命周期 Hook 不会自动灌入（已真机验证）。",
        ("antigravity", "memory_write"): "授权后可通过 MCP 写入共享记忆。hook 不能回写结构化历史。",
        ("antigravity", "handoff"): "通过技能或 MCP 显式触发六项回读与确认续做（已真机验证）。",
        ("antigravity", "compute"): "官方命令缺少可证明的无工具隔离，当前禁止用它提炼记忆。",
        ("claude-code", "history"): "只读解析 ~/.claude/projects 下的会话记录；只沿最后一条有效分支还原，子代理与内部标记不混进对话。",
        ("claude-code", "capture"): "提交前提示能实时采到你的提问；助手的回复要等会话记录同步后才完整。",
        ("claude-code", "memory_read"): "需要 /contextcore-handoff 或 MCP 读取记忆。Hook 不会自动灌入（MCP 协议已真机验证）。",
        ("claude-code", "memory_write"): "授权后可通过 MCP 写入共享记忆。这不是历史会话重建。",
        ("claude-code", "handoff"): "可在提交前识别接棒意图，也可用 /contextcore-handoff 显式触发。",
        ("claude-code", "compute"): "官方命令缺少可证明的无工具隔离，当前禁止用它提炼记忆。",
        ("opencode", "history"): "只读解析本机会话数据库；子代理会话不算独立对话，旧版文件存储仅作回落。",
        ("opencode", "capture"): "插件能实时采到每次提问；助手正文要等数据库同步后才完整。",
        ("opencode", "memory_read"): "需要 /contextcore 或 MCP 读取记忆。插件只采集提问，不会插入聊天内容（已真机验证）。",
        ("opencode", "memory_write"): "授权后可通过 MCP 写入共享记忆。",
        ("opencode", "handoff"): "需要 /contextcore 或 MCP 接棒。插件只采集提问，不会插入接棒卡。",
        ("opencode", "compute"): "官方命令缺少可证明的无工具隔离，当前禁止用它提炼记忆。",
    }
    return details.get((agent_id, name), fallback)
def _enrich_capabilities(item: dict[str, Any]) -> None:
    try:
        from app.integrations.constants import HOST_CAPABILITY_STATUS
        matrix_caps = HOST_CAPABILITY_STATUS.get(item["id"])
        if matrix_caps:
            item["capabilities"] = {
                name: {
                    "status": "unsupported" if info.get("status") == "unavailable" else info.get("status", "unsupported"),
                    "detail": _capability_detail(item["id"], name, info.get("detail", "")),
                }
                for name, info in matrix_caps.items()
            }
    except Exception:
        return


def _clean_connection_dict(item: dict[str, Any]) -> dict[str, Any]:
    item.pop("token_hash", None)
    item["discovered"] = bool(item.get("discovered", 0))
    item["enabled"] = bool(item.get("enabled", 0))
    item["paused"] = bool(item.get("paused", 0))
    from app.services.connection_plans import read_grants
    with db() as grant_conn:
        grants = read_grants(grant_conn, item['id'])
    item['authorizations'] = {'history': bool(item['enabled']), 'history_paused': bool(item['paused']),
                              **(grants or {}), 'legacy_review_required': grants is None}
    # Never leak secret substring or raw token digits in preview
    if item.get("token_preview"):
        item["token_preview"] = "configured"
    from app.services.host_registry import get_observations, get_reference_evidence
    item["discovery"] = get_app_setting(f"host_discovery:{item['id']}", {})
    item["discovery"]["observations"] = get_observations(item["id"])
    item["discovery"]["reference_acceptance"] = get_reference_evidence(item["id"])
    item["discovery"]["local_observations_present"] = bool(item["discovery"]["observations"])
    item["discovery"]["usage_sync"] = get_app_setting(f"usage_sync:{item['id']}", {"status": "not_observed"})
    _enrich_capabilities(item)
    return item


def list_connections() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM connections ORDER BY id ASC").fetchall()
        return [_clean_connection_dict(row_to_dict(r)) for r in rows]


def get_connection(connection_id: str) -> dict[str, Any]:
    with db() as conn:
        row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Connection '{connection_id}' not found")
        return _clean_connection_dict(row_to_dict(row))

def patch_connection(
    connection_id: str,
    enabled: bool | None = None,
    paused: bool | None = None,
    paths: list[str] | None = None,
    confirmed: bool = False,
) -> dict[str, Any]:
    if not confirmed:
        raise HTTPException(status_code=422, detail="Confirmed flag must be True")

    with db() as conn:
        row = conn.execute("SELECT * FROM connections WHERE id = ?", (connection_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Connection '{connection_id}' not found")

        updates = []
        params = []
        now = now_iso()

        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
            if not enabled:
                updates.append("token_hash = NULL")
                updates.append("token_preview = NULL")
                updates.append("config_preview = NULL")
                from app.services.connection_plans import grants_key, CAPABILITIES
                set_app_setting(grants_key(connection_id), {name: False for name in CAPABILITIES}, conn=conn)

        if paused is not None:
            updates.append("paused = ?")
            params.append(1 if paused else 0)

        if paths is not None:
            updates.append("paths = ?")
            params.append(encode(paths))

        updates.append("updated_at = ?")
        params.append(now)
        params.append(connection_id)

        conn.execute(f"UPDATE connections SET {', '.join(updates)} WHERE id = ?", tuple(params))
        log_audit(actor="user", action="connection.patch", target=connection_id, conn=conn)

    return get_connection(connection_id)


def install_connection(
    connection_id: str,
    confirmed: bool = False,
    paths: list[str] | None = None,
    home: Path | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise HTTPException(status_code=422, detail="confirmed must be True to execute installation")

    conn_data = get_connection(connection_id)
    from app.integrations.installer import _assert_path_safe
    resolved_home = Path(home).resolve() if home else Path.home().resolve()
    credential_path = resolved_home / ".contextcore" / "credentials" / f"{connection_id}.token"
    try:
        _assert_path_safe(credential_path, resolved_home)
        with db() as conn:
            expected_hash = conn.execute("SELECT token_hash FROM connections WHERE id=?", (connection_id,)).fetchone()[0]
        if credential_path.exists():
            if credential_path.stat().st_mode & 0o077 or credential_path.stat().st_size > 4096:
                raise ValueError("Existing credential permissions or size require explicit repair")
            raw_token = credential_path.read_text(encoding="utf-8").strip()
            digest = hashlib.sha256(raw_token.encode()).hexdigest()
            if not raw_token or not expected_hash or not secrets.compare_digest(digest, expected_hash):
                raise ValueError("Existing credential cannot be verified; explicit credential recovery is required")
            reused_credential = True
        elif expected_hash:
            raise ValueError("Configured credential file is missing; explicit credential recovery is required")
        else:
            raw_token = f"cc_agent_{connection_id}_{secrets.token_hex(16)}"
            reused_credential = False
    except (OSError, UnicodeError, ValueError):
        raise HTTPException(status_code=409, detail="Existing integration credential needs explicit repair; no token was rotated") from None
    token_h = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    from app.integrations.installer import apply_host_integration, generate_setup_bundle

    trusted_base = resolve_trusted_base_url(base_url)
    try:
        install_res = apply_host_integration(
            connection_id,
            token=raw_token,
            confirmed=True,
            home=home,
            base_url=trusted_base,
        )
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Installation failed: {str(exc)}") from exc

    bundle = generate_setup_bundle(connection_id, token="<configured>", credential_path=install_res.token_path, base_url=trusted_base)
    now = now_iso()

    try:
        with db() as conn:
            updates = [
                "enabled = 1",
                "token_hash = ?",
                "token_preview = ?",
                "config_preview = ?",
                "updated_at = ?",
            ]
            params = [
                token_h,
                "configured",
                bundle.mcp_config["content"],
                now,
            ]
            if paths is not None:
                updates.append("paths = ?")
                params.append(encode(paths))
            params.append(connection_id)
            conn.execute(f"UPDATE connections SET {', '.join(updates)} WHERE id = ?", tuple(params))
            log_audit(actor="user", action="connection.install", target=connection_id, conn=conn)
    except Exception as exc:
        if not reused_credential:
            try:
                from app.integrations.installer import uninstall_host_integration
                uninstall_host_integration(connection_id, confirmed=True, home=home)
            except Exception:
                pass
        raise HTTPException(status_code=500, detail="Installation state could not be recorded; review integration state before retrying") from None

    return {
        "ok": True,
        "connection": get_connection(connection_id),
        "install": install_res.to_dict(),
    }


def uninstall_connection(
    connection_id: str,
    confirmed: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    if not confirmed:
        raise HTTPException(status_code=422, detail="confirmed must be True to execute uninstallation")

    conn_data = get_connection(connection_id)
    from app.integrations.installer import uninstall_host_integration

    try:
        uninstall_res = uninstall_host_integration(connection_id, confirmed=True, home=home)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"Uninstallation failed: {str(exc)}") from exc

    if uninstall_res.status not in {"uninstalled", "not_installed"}:
        raise HTTPException(status_code=409, detail={
            "message": "Integration removal is incomplete; resolve the conflict before retrying.",
            "uninstall": uninstall_res.to_dict(),
        })

    now = now_iso()
    with db() as conn:
        conn.execute(
            """
            UPDATE connections SET
                enabled = 0,
                token_hash = NULL,
                token_preview = NULL,
                config_preview = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, connection_id),
        )
        log_audit(actor="user", action="connection.uninstall", target=connection_id, conn=conn)

    return {
        "ok": True,
        "connection": get_connection(connection_id),
        "uninstall": uninstall_res.to_dict(),
    }


def discover_connections(*, home: Path | None = None, env: dict[str, str] | None = None, application_dirs: list[Path] | None = None) -> list[dict[str, Any]]:
    from app.services.host_registry import discover_hosts

    observations = discover_hosts(home=home, env=env, application_dirs=application_dirs)
    now = now_iso()
    with db() as conn:
        for observation in observations:
            row = conn.execute("SELECT paths, enabled FROM connections WHERE id = ?", (observation["id"],)).fetchone()
            if row is None:
                continue
            # Suggestions are metadata, not authorization. Existing custom paths and all
            # credential/integration fields are preserved verbatim.
            existing_paths = json.loads(row["paths"] or "[]")
            if not existing_paths and not row["enabled"] and observation["paths"]:
                conn.execute("UPDATE connections SET paths = ? WHERE id = ?", (encode(observation["paths"]), observation["id"]))
            conn.execute("UPDATE connections SET discovered = ?, updated_at = ? WHERE id = ?",
                         (int(observation["discovered"]), now, observation["id"]))
            set_app_setting(f"host_discovery:{observation['id']}", observation["discovery"], conn=conn)
        log_audit(actor="daemon", action="connection.discover", target="all", conn=conn)
    return list_connections()



SOURCE_GLOBS: dict[str, tuple[str, ...]] = {
    "codex": ("**/rollout*.jsonl", "**/session_index.jsonl"),
    "omp": ("**/*.jsonl",),
    "cursor": ("**/*.vscdb", "**/state.vscdb-wal", "**/state.vscdb-shm", "**/store.db", "**/store.db-wal", "**/store.db-shm"),
    "antigravity": ("**/transcript*.jsonl",),
    "claude-code": ("**/*.jsonl",),
    "opencode": ("opencode.db", "opencode.db-wal", "opencode.db-shm", "storage/**/*.json"),
}


def _glob_pattern_regex(pattern: str) -> re.Pattern[str]:
    """Compile the small recursive glob subset used by SOURCE_GLOBS."""
    prefix = ""
    if pattern.startswith("**/"):
        prefix = "(?:.*/)?"
        pattern = pattern[3:]
    pieces: list[str] = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            pieces.append("(?:.*/)?")
            index += 3
        elif pattern.startswith("**", index):
            pieces.append(".*")
            index += 2
        elif pattern[index] == "*":
            pieces.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            pieces.append("[^/]")
            index += 1
        else:
            pieces.append(re.escape(pattern[index]))
            index += 1
    return re.compile("^" + prefix + "".join(pieces) + "$")


def _iter_directory_files(root: Path, patterns: tuple[str, ...]):
    """Walk one directory with O(depth) memory and yield matching regular files."""
    matchers = tuple(_glob_pattern_regex(pattern) for pattern in patterns)
    stack = [os.scandir(root)]
    try:
        while stack:
            try:
                entry = next(stack[-1])
            except StopIteration:
                stack.pop().close()
                continue
            except OSError:
                stack.pop().close()
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(os.scandir(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=True):
                    continue
                candidate = Path(entry.path)
                relative = candidate.relative_to(root).as_posix()
                if any(pattern.fullmatch(relative) for pattern in matchers):
                    yield candidate
            except OSError:
                continue
    finally:
        for iterator in stack:
            iterator.close()


def _iter_source_files(connection_id: str, paths: list[str]):
    """Yield discovered files without retaining a duplicate full-tree inventory."""
    from app.adapters.base import deduplicate_roots

    patterns = SOURCE_GLOBS.get(connection_id, ("**/*",))
    for root in deduplicate_roots([Path(raw).expanduser() for raw in paths]):
        try:
            if connection_id == "claude-code" and root.is_dir() and root.name == ".claude" and (root / "projects").is_dir():
                root = root / "projects"
            if root.is_file():
                yield root
                continue
            if not root.is_dir():
                continue
            yield from _iter_directory_files(root, patterns)
        except OSError:
            continue


def _source_fingerprint(connection_id: str, paths: list[str], *, budget=None) -> str:
    """Cheap stat-only fingerprint. Avoids parsing unchanged host histories every cycle."""
    digest = hashlib.sha256()
    found = False
    for path in _iter_source_files(connection_id, paths):
        if budget:
            budget.checkpoint()
        try:
            stat = path.stat()
        except OSError:
            continue
        found = True
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(stat.st_mtime_ns).encode("ascii"))
        digest.update(b"\0")
        digest.update(str(stat.st_size).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest() if found else ""


def ensure_source_cursor_schema(conn=None) -> None:
    """Idempotent additive migration; cursors contain metadata, never transcripts."""
    if conn is None:
        with db() as local:
            ensure_source_cursor_schema(local)
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS source_sync_cursors (
        host TEXT NOT NULL, source_path TEXT NOT NULL,
        fingerprint TEXT NOT NULL DEFAULT '', file_identity TEXT NOT NULL DEFAULT '',
        complete_offset INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'pending',
        error TEXT, updated_at TEXT NOT NULL,
        PRIMARY KEY(host, source_path)
    )""")


def _snapshot_source(host: str, path: Path, roots: list[str], previous: dict[str, Any] | None = None, *, budget=None) -> dict[str, Any]:
    from app.adapters.base import MAX_LINE_LENGTH, validate_file_safety, validate_path
    canonical = validate_path(path, roots)
    if canonical.is_dir():
        if host != "opencode":
            from app.adapters import AgentAdapterError
            raise AgentAdapterError(host=host, code="unsupported_format", path=str(canonical), detail="Directory snapshots are supported only for OpenCode legacy storage.")
        before = canonical.stat()
        identity = [before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns]
        digest = hashlib.sha256()
        # Legacy OpenCode contains session, message and part JSON. Hash each
        # file's path and metadata as it streams; never keep a tree-sized list.
        for candidate in _iter_directory_files(canonical, ("**/*.json",)):
            if budget:
                budget.checkpoint()
            safe_path = validate_path(candidate, [canonical])
            stat = safe_path.stat()
            digest.update(safe_path.relative_to(canonical).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(encode([stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]).encode("ascii"))
            digest.update(b"\n")
        after = canonical.stat()
        if identity != [after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns]:
            from app.adapters import AgentAdapterError
            raise AgentAdapterError(host=host, code="file_changed", path=str(canonical), detail="Legacy storage changed while indexing", retryable=True)
        return {"source_path": str(canonical), "fingerprint": digest.hexdigest(),
                "file_identity": encode(identity[:2]), "complete_offset": 0}
    validate_file_safety(canonical, host=host)
    st = canonical.stat()
    identity = [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns]
    state = [identity]
    # WAL is part of the database's state. Never use immutable=1 to hide live WAL.
    if canonical.suffix in {".db", ".sqlite", ".vscdb"}:
        wal = Path(str(canonical) + "-wal")
        if wal.is_symlink() or Path(str(canonical) + "-shm").is_symlink():
            from app.adapters import AgentAdapterError
            raise AgentAdapterError(host=host, code="symlink_escape", path=str(wal), detail="SQLite sidecar symlink is not a granted snapshot")
        if wal.exists():
            # Exact SQLite-file grants include its named WAL sidecar, never the
            # rest of the parent directory. Symlink sidecars were refused above.
            validate_path(wal, [str(canonical.parent)])
            w = wal.stat()
            state.append([w.st_dev, w.st_ino, w.st_size, w.st_mtime_ns, w.st_ctime_ns])
    fingerprint = hashlib.sha256(encode(state).encode()).hexdigest()
    offset = st.st_size
    if previous and previous.get("fingerprint") == fingerprint:
        offset = int(previous["complete_offset"])
    elif canonical.suffix == ".jsonl" and st.st_size:
        # Usually the final byte is a newline. Never read an 8 MiB suffix from
        # every unchanged source on every lifecycle tick.
        offset = 0
        with canonical.open("rb") as handle:
            end = st.st_size
            scanned = 0
            while end > 0 and scanned <= MAX_LINE_LENGTH:
                start = max(0, end - min(65536, MAX_LINE_LENGTH + 1 - scanned))
                handle.seek(start)
                tail = handle.read(end - start)
                newline = tail.rfind(b"\n")
                if newline >= 0:
                    offset = start + newline + 1
                    break
                scanned += end - start
                end = start
    return {"source_path": str(canonical), "fingerprint": fingerprint,
            "file_identity": encode(identity[:2]), "complete_offset": offset}


_TERMINAL_SOURCE_ERRORS = frozenset({
    "unsupported_format", "truncated", "symlink_escape", "root_not_granted",
})
_SETTLED_TRANSIENT_ERRORS = frozenset({
    "file_changed", "source_requires_retry",
})


def _source_error_code(value: str | None) -> str:
    if not value:
        return ""
    return value.split(":", 1)[0].strip()


def _source_error_text(failure: dict[str, Any]) -> str:
    code = str(failure.get("code") or "source_requires_retry").strip()
    detail = str(failure.get("detail") or "").strip()
    return f"{code}: {detail}" if detail else code


def _unique_failures(partial_failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for failure in partial_failures:
        path = str(failure.get("path") or "unknown")
        if path in seen:
            continue
        seen.add(path)
        unique.append(failure)
    return unique


def _source_changed(old: dict[str, Any] | None, snapshot: dict[str, Any], force: bool, *, skip_failures_since: str | None = None) -> bool:
    """Retry a stable file only when its last error can succeed without a new snapshot."""
    if force or old is None:
        return True
    # A forced sync may span several bounded planning passes. Pending receipts
    # keep deferred sources discoverable without retaining their paths in RAM.
    if old.get("status") == "pending":
        return True
    if (skip_failures_since and old.get("status") == "error"
            and str(old.get("updated_at") or "") >= skip_failures_since):
        # One foreground invocation skips a group after the first failure,
        # including mid-write fingerprint changes. The next sync retries.
        return False
    if old.get("fingerprint") != snapshot["fingerprint"]:
        return True
    if old.get("status") == "error" and _source_error_code(old.get("error")) not in (
            _TERMINAL_SOURCE_ERRORS | _SETTLED_TRANSIENT_ERRORS):
        return True
    return False


def _opencode_source_plan(roots: list[str], force: bool, *, budget=None, skip_sources: set[str] | None = None, skip_failures_since: str | None = None):
    """Plan one SQLite file or one legacy storage tree as bounded atomic units."""
    from app.adapters import AgentAdapterError
    from app.adapters.base import deduplicate_roots

    root_paths = deduplicate_roots([Path(raw).expanduser() for raw in roots])
    if not root_paths:
        raise AgentAdapterError(host="opencode", code="root_not_granted", detail="None of the configured history roots exist or are authorized.")
    database_paths: dict[str, Path] = {}
    legacy_paths: dict[str, Path] = {}
    for root in root_paths:
        if budget:
            budget.checkpoint()
        if root.is_file():
            if root.name == "opencode.db":
                database_paths[str(root.resolve())] = root
            continue
        if not root.is_dir():
            continue
        for candidate in (root / "opencode.db", root / "opencode" / "opencode.db"):
            if candidate.is_file():
                database_paths[str(candidate.resolve())] = candidate
        storage = root if root.name == "storage" else root / "storage"
        if (storage / "session").is_dir():
            legacy_paths[str(storage.resolve())] = storage

    # As in the compatibility adapter, a readable SQLite database takes
    # precedence over legacy JSON in the same grant.
    targets = list(database_paths.values()) if database_paths else list(legacy_paths.values())
    limit = max(1, int(getattr(budget, "max_sources", 16))) if budget else None
    candidates: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
    changed_count = 0
    first_snapshot = None
    skipped = skip_sources or set()
    with db() as conn:
        ensure_source_cursor_schema(conn)
        for source in targets:
            if budget:
                budget.checkpoint()
            canonical = str(source.resolve())
            try:
                old_row = conn.execute("SELECT * FROM source_sync_cursors WHERE host=? AND source_path=?", ("opencode", canonical)).fetchone()
                old = dict(old_row) if old_row else None
                snapshot = _snapshot_source("opencode", source, roots, old, budget=budget)
            except (AgentAdapterError, OSError) as exc:
                return [], {}, [{"path": str(source), "code": getattr(exc, "code", "permission_denied"), "detail": str(exc)}], 0
            if first_snapshot is None:
                first_snapshot = snapshot
            if canonical in skipped:
                continue
            if not _source_changed(old, snapshot, force, skip_failures_since=skip_failures_since):
                continue
            changed_count += 1
            if budget:
                _write_source_pending(conn, "opencode", snapshot)
            key = (bool(old and old.get("status") == "error"), str(old.get("updated_at") or "") if old else "", canonical)
            candidates.append((key, canonical, snapshot))
            candidates.sort(key=lambda item: item[0])
            if limit is not None and len(candidates) > limit:
                candidates.pop()

    snapshots = {path: snapshot for _, path, snapshot in candidates}
    selected = list(snapshots)
    pending = max(0, changed_count - len(candidates))
    if not selected and first_snapshot is not None:
        snapshots[first_snapshot["source_path"]] = first_snapshot
    return selected, snapshots, [], pending


def _source_plan(host: str, roots: list[str], force: bool, *, budget=None, skip_sources: set[str] | None = None, skip_failures_since: str | None = None) -> tuple[list[str], dict[str, dict[str, Any]], list[dict[str, Any]], int]:
    from app.adapters import AgentAdapterError
    from app.adapters.base import deduplicate_roots
    if not deduplicate_roots(roots):
        raise AgentAdapterError(host=host, code="root_not_granted", detail="None of the configured history roots exist or are authorized.")
    if host == "opencode":
        return _opencode_source_plan(roots, force, budget=budget, skip_sources=skip_sources,
                                     skip_failures_since=skip_failures_since)
    snapshots: dict[str, dict[str, Any]] = {}
    failures = []
    pending_sources = 0
    limit = max(1, int(getattr(budget, "max_sources", 16))) if budget else None
    candidates: list[tuple[tuple[Any, ...], str, dict[str, Any]]] = []
    antigravity_groups: dict[str, tuple[tuple[Any, ...], str]] = {}
    index_snapshots: dict[str, dict[str, Any]] = {}
    index_changed = False
    changed_count = 0
    opencode_db_changed = 0
    opencode_legacy_changed = False
    changed_paths: list[str] = []
    first_source_snapshot: dict[str, Any] | None = None
    skipped = skip_sources or set()

    def add_candidate(key: tuple[Any, ...], path: str, snapshot: dict[str, Any]) -> None:
        candidates.append((key, path, snapshot))
        candidates.sort(key=lambda item: item[0])
        if limit is not None and len(candidates) > limit:
            candidates.pop()

    with db() as conn:
        ensure_source_cursor_schema(conn)
        conn.commit()
        pending_commit_count = 0
        for source in _iter_source_files(host, roots):
            if budget:
                budget.checkpoint()
            if source.name.endswith(("-wal", "-shm")):
                continue
            try:
                canonical = str(source.resolve())
                old_row = conn.execute("SELECT * FROM source_sync_cursors WHERE host = ? AND source_path = ?", (host, canonical)).fetchone()
                old = dict(old_row) if old_row else None
                snapshot = _snapshot_source(host, source, roots, old)
            except (AgentAdapterError, OSError) as exc:
                failures.append({"path": str(source), "code": getattr(exc, "code", "permission_denied"), "detail": str(exc)})
                if old and old.get("status") == "pending":
                    _write_source_cursor(conn, host, {"source_path": canonical}, error=f"{getattr(exc, 'code', 'permission_denied')}: {str(exc)}")
                    pending_commit_count += 1
                continue
            path = snapshot["source_path"]
            if first_source_snapshot is None:
                first_source_snapshot = snapshot
            if path in skipped:
                continue
            changed = _source_changed(old, snapshot, force, skip_failures_since=skip_failures_since)
            if host == "codex" and Path(path).name == "session_index.jsonl":
                index_snapshots[path] = snapshot
                index_changed = index_changed or changed
                if budget and changed:
                    _write_source_pending(conn, host, snapshot)
                    pending_commit_count += 1
                    if pending_commit_count >= 64:
                        conn.commit()
                        pending_commit_count = 0
                continue
            if not changed:
                # Keep unchanged records only for unbudgeted planning, where the
                # complete cursor inventory is required to distinguish no-op sync.
                if not budget:
                    snapshots[path] = snapshot
                continue
            changed_count += 1
            if budget:
                # Persist only a compact receipt. This lets a later batch resume
                # without an in-memory set of every discovered source path.
                _write_source_pending(conn, host, snapshot)
                # Bound dirty SQLite pages while retaining durable work receipts.
                # Uncommitted paths are rediscovered from granted roots after a
                # cancellation, so this does not advance any source cursor.
                pending_commit_count += 1
                if pending_commit_count >= 64:
                    conn.commit()
                    pending_commit_count = 0
            if not budget:
                changed_paths.append(path)
            key = (bool(old and old.get("status") == "error"), str(old.get("updated_at") or "") if old else "", path)
            if budget and host == "opencode":
                if Path(path).name == "opencode.db":
                    opencode_db_changed += 1
                    add_candidate(key, path, snapshot)
                elif Path(path).suffix == ".json":
                    opencode_legacy_changed = True
                continue
            if not budget:
                snapshots[path] = snapshot
                continue
            if host == "antigravity":
                parent = str(Path(path).parent)
                current = antigravity_groups.get(parent)
                if current is None or key < current[0]:
                    antigravity_groups[parent] = (key, path)
                ranked = sorted(antigravity_groups.items(), key=lambda item: item[1][0])
                antigravity_groups = dict(ranked[:limit])
            else:
                add_candidate(key, path, snapshot)

    if not budget:
        # Title indexes are dependencies; if one changed, refresh every Codex
        # rollout from the authorized inventory in this unbudgeted pass.
        if host == "codex" and index_changed:
            snapshots.update(index_snapshots)
            selected = list(snapshots)
        elif host == "antigravity":
            parents = {str(Path(path).parent) for path in changed_paths}
            selected = [path for path in snapshots if str(Path(path).parent) in parents]
        elif host == "opencode" and changed_paths:
            selected = list(snapshots)
        else:
            selected = changed_paths[:]
            if host == "codex" and changed_paths:
                snapshots.update(index_snapshots)
                selected += [path for path in index_snapshots if path not in selected]
        return selected, snapshots, failures, 0

    if host == "opencode":
        if opencode_db_changed or opencode_legacy_changed:
            # Each SQLite database is one atomic source and may contain many
            # external session IDs. Legacy storage remains an atomic tree group.
            snapshots = {path: snap for _, path, snap in candidates}
            if opencode_legacy_changed:
                snapshots = {}
                for source in _iter_source_files(host, roots):
                    budget.checkpoint()
                    if source.suffix != ".json":
                        continue
                    try:
                        old = _cursor_for_source(host, str(source.resolve()))
                        snapshot = _snapshot_source(host, source, roots, old)
                        snapshots[snapshot["source_path"]] = snapshot
                    except (AgentAdapterError, OSError) as exc:
                        failures.append({"path": str(source), "code": getattr(exc, "code", "permission_denied"), "detail": str(exc)})
                # Retain selected independent databases alongside the legacy group.
                snapshots.update({path: snap for _, path, snap in candidates if Path(path).name == "opencode.db"})
            selected = list(snapshots)
            pending_sources = max(0, opencode_db_changed - sum(1 for path in snapshots if Path(path).name == "opencode.db"))
            return selected, snapshots, failures, pending_sources
        if first_source_snapshot is not None:
            # Retain one metadata row to let sync_connection distinguish an
            # unchanged grant from an empty source set without a full path set.
            return [], {first_source_snapshot["source_path"]: first_source_snapshot}, failures, 0
        return [], {}, failures, 0

    if host == "antigravity":
        chosen = set(antigravity_groups)
        # Grouped transcripts are atomic. Expand only the selected session
        # directories, so planner memory stays proportional to the batch.
        for parent, (_, representative) in antigravity_groups.items():
            directory = Path(parent)
            candidates_in_group = ([Path(representative)] if len(roots) == 1 and Path(roots[0]).is_file()
                                   else directory.glob("transcript*.jsonl"))
            for source in candidates_in_group:
                if not source.is_file():
                    continue
                budget.checkpoint()
                try:
                    canonical = str(source.resolve())
                    with db() as conn:
                        old_row = conn.execute("SELECT * FROM source_sync_cursors WHERE host = ? AND source_path = ?", (host, canonical)).fetchone()
                    old = dict(old_row) if old_row else None
                    snapshot = _snapshot_source(host, source, roots, old)
                    snapshots[snapshot["source_path"]] = snapshot
                except (AgentAdapterError, OSError) as exc:
                    failures.append({"path": str(source), "code": getattr(exc, "code", "permission_denied"), "detail": str(exc)})
                    with db() as conn:
                        _write_source_cursor(conn, host, {"source_path": canonical}, error=f"{getattr(exc, 'code', 'permission_denied')}: {str(exc)}")
        selected = list(snapshots)
        # Count durable pending directories in path order, so sibling transcripts
        # collapse to one group without building a set proportional to the tree.
        from app.adapters.base import validate_path
        total_pending_groups = 0
        previous_parent = None
        failed_path_set = {str(Path(f["path"]).resolve()) for f in failures if f.get("path")}
        with db() as conn:
            rows = conn.execute("SELECT source_path FROM source_sync_cursors WHERE host=? AND status='pending' ORDER BY source_path", (host,))
            for (pending_path,) in rows:
                if pending_path in failed_path_set:
                    continue
                try:
                    authorized = validate_path(pending_path, roots)
                except AgentAdapterError:
                    continue
                if not authorized.is_file():
                    continue
                parent = str(authorized.parent)
                if parent != previous_parent:
                    total_pending_groups += 1
                    previous_parent = parent
        pending_sources = max(0, total_pending_groups - len(chosen))
        if not selected and first_source_snapshot is not None:
            snapshots[first_source_snapshot["source_path"]] = first_source_snapshot
        return selected, snapshots, failures, pending_sources

    snapshots = {path: snap for _, path, snap in candidates}
    if host == "codex" and index_snapshots:
        snapshots.update(index_snapshots)
    selected = list(snapshots)
    pending_sources = max(0, changed_count - len(candidates))
    if not selected and first_source_snapshot is not None:
        snapshots[first_source_snapshot["source_path"]] = first_source_snapshot
    return selected, snapshots, failures, pending_sources


def _cursor_for_source(host: str, path: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM source_sync_cursors WHERE host = ? AND source_path = ?", (host, path)).fetchone()
        return dict(row) if row else None


def _write_source_cursor(conn, host: str, snapshot: dict[str, Any], error: str | None = None) -> None:
    if error:
        # A failed read remains an error cursor even though its last fingerprint
        # is recorded for change detection and within-invocation retry deferral.
        code = _source_error_code(error)
        # Keep the attempted fingerprint for all errors so one foreground call
        # can defer an unchanged failed source to the next sync invocation.
        # Status='error' still prevents any failed source from appearing complete.
        fingerprint = snapshot.get("fingerprint") or ""
        identity = (snapshot.get("file_identity") or "") if fingerprint else ""
        # Keep the scanned offset so an unchanged broken file is not mistaken for a short tail.
        offset = (int(snapshot.get("complete_offset") or 0)
                  if code in _TERMINAL_SOURCE_ERRORS or error == "usage_sync_pending" else 0)
        conn.execute("""INSERT INTO source_sync_cursors
            (host,source_path,fingerprint,file_identity,complete_offset,status,error,updated_at)
            VALUES(?,?,?,?,?,'error',?,?) ON CONFLICT(host,source_path) DO UPDATE SET
            fingerprint=excluded.fingerprint,file_identity=excluded.file_identity,
            complete_offset=excluded.complete_offset,status='error',error=excluded.error,updated_at=excluded.updated_at""",
            (host, snapshot["source_path"], fingerprint, identity, offset, error, now_iso()))
    else:
        conn.execute("""INSERT INTO source_sync_cursors
            (host,source_path,fingerprint,file_identity,complete_offset,status,error,updated_at)
            VALUES(?,?,?,?,?,'ready',NULL,?) ON CONFLICT(host,source_path) DO UPDATE SET
            fingerprint=excluded.fingerprint,file_identity=excluded.file_identity,
            complete_offset=excluded.complete_offset,status='ready',error=NULL,updated_at=excluded.updated_at""",
            (host, snapshot["source_path"], snapshot["fingerprint"], snapshot["file_identity"], snapshot["complete_offset"], now_iso()))


def _write_source_pending(conn, host: str, snapshot: dict[str, Any]) -> None:
    """Record a forced-sync work item without claiming that parsing completed."""
    conn.execute("""INSERT INTO source_sync_cursors
        (host,source_path,fingerprint,file_identity,complete_offset,status,error,updated_at)
        VALUES(?,?,?,?,?,'pending',NULL,?) ON CONFLICT(host,source_path) DO UPDATE SET
        fingerprint=excluded.fingerprint,file_identity=excluded.file_identity,
        complete_offset=excluded.complete_offset,status='pending',error=NULL,updated_at=excluded.updated_at""",
        (host, snapshot["source_path"], snapshot["fingerprint"], snapshot["file_identity"], snapshot["complete_offset"], now_iso()))


# Serialize each host's read/reconcile/commit so a slower scan cannot overwrite a
# newer cursor. Only this process owns the local lifecycle worker.
import threading
import time
_sync_locks: dict[str, Any] = {}
_sync_locks_guard = threading.Lock()
_CODEX_APP_SERVER_THROTTLE_SECONDS = 15.0
_codex_app_server_last_mono: dict[tuple[str, tuple[str, ...]], float] = {}


def _should_query_codex_app_server(force: bool, roots: list[Any]) -> bool:
    """Throttle automatic (force=False) Codex app-server spawns per DB and root set."""
    from app.config import settings
    key = (str(settings.db_path), tuple(str(r) for r in roots))
    now_mono = time.monotonic()
    if not force:
        last_mono = _codex_app_server_last_mono.get(key)
        if last_mono is not None and (now_mono - last_mono) < _CODEX_APP_SERVER_THROTTLE_SECONDS:
            return False
    _codex_app_server_last_mono[key] = now_mono
    return True


def sync_connection(connection_id: str, force: bool = True, *, allow_execution: bool = False, budget=None) -> dict[str, Any]:
    with _sync_locks_guard:
        lock = _sync_locks.setdefault(connection_id, threading.Lock())
    with lock:
        if budget is not None:
            return _sync_connection_impl(connection_id, force, allow_execution=allow_execution, budget=budget)
        # Foreground syncs also plan in bounded batches. The scheduler has no
        # background duty-cycle delay; it only caps retained path/snapshot state.
        from app.services.sync_budget import SyncBudget
        foreground = SyncBudget(threading.Event(), duty_cycle=1.0)
        foreground.max_sources = 128
        first_pass = True
        total_processed = 0
        sync_started_at = now_iso()
        result: dict[str, Any] = {}
        pass_count = 0
        max_passes = 1000
        last_pending = None
        while True:
            pass_count += 1
            result = _sync_connection_impl(
                connection_id, force if first_pass else False,
                allow_execution=allow_execution, budget=foreground,
                skip_failures_since=sync_started_at,
            )
            processed = int(result.get("processed_sources", 0))
            pending = int(result.get("pending_sources", 0))
            total_processed += processed
            first_pass = False
            if pending <= 0:
                break
            if processed == 0:
                break
            if last_pending is not None and pending >= last_pending and processed == 0:
                break
            last_pending = pending
            if pass_count >= max_passes:
                break
        result["processed_sources"] = total_processed
        return result


def _sync_connection_impl(connection_id: str, force: bool, *, allow_execution: bool, budget=None,
                          skip_sources: set[str] | None = None, skip_failures_since: str | None = None) -> dict[str, Any]:
    from app.adapters import AgentAdapterError, PartialHistoryError, read_sessions
    from app.services import conversation_service, project_service

    conn_data = get_connection(connection_id)
    if not conn_data.get("enabled"):
        raise HTTPException(status_code=400, detail="Cannot sync disabled connection")
    if conn_data.get("paused"):
        raise HTTPException(status_code=400, detail="Cannot sync paused connection")
    roots = conn_data.get("paths", [])
    connection_revision = conn_data.get("updated_at")
    now = now_iso()
    try:
        selected, snapshots, partial_failures, pending_sources = _source_plan(
            connection_id, roots, force, budget=budget, skip_sources=skip_sources,
            skip_failures_since=skip_failures_since)
    except AgentAdapterError as exc:
        message = f"{exc.code}: {exc.detail}"
        with db() as conn:
            conn.execute("UPDATE connections SET error = ?, updated_at = ? WHERE id = ?", (message, now, connection_id))
            log_audit(actor="user", action="connection.sync", target=connection_id, result="error", conn=conn)
        raise HTTPException(status_code=422, detail=message) from exc
    has_sources = bool(snapshots)
    has_rollout_sources = (
        any(Path(p).name != "session_index.jsonl" for p in snapshots)
        if connection_id == "codex"
        else has_sources
    )
    known_source_paths = set(snapshots)
    legacy_storage_paths = [path for path in selected
                            if connection_id == "opencode" and Path(path).is_dir()]

    def legacy_cursor_path(path: str) -> str:
        resolved = str(Path(path).resolve())
        for storage_path in legacy_storage_paths:
            try:
                if os.path.commonpath([resolved, storage_path]) == storage_path:
                    return storage_path
            except ValueError:
                continue
        return resolved

    waiting = [path for path, value in snapshots.items()
               if Path(path).suffix == ".jsonl" and value["complete_offset"] == 0]
    selected = [path for path in selected if path not in waiting]
    if budget:
        budget.checkpoint()
        # Planning already selected a bounded, dependency-complete batch.
        # Never advance a cursor for a deferred source, including on failure.
        snapshots = {path: value for path, value in snapshots.items() if path in selected or path in waiting}
    if has_sources and not selected and waiting and not partial_failures:
        with db() as conn:
            ensure_source_cursor_schema(conn)
            for path in waiting:
                _write_source_cursor(conn, connection_id, snapshots[path])
        return {"status": "waiting", "connection": conn_data, "reason": "incomplete_source"}
    should_query_codex_app_server = (
        connection_id == "codex"
        and allow_execution
        and not skip_sources
        and _should_query_codex_app_server(force, roots)
    )
    if not force and has_sources and not selected and not partial_failures and not should_query_codex_app_server:
        # Refresh the connection receipt so stable damaged sources read as
        # skipped, not as an unfinished whole-host repair.
        from app.adapters.base import validate_path
        retryable = 0
        skipped_terminal = 0
        skipped_codes: dict[str, int] = {}
        with db() as conn:
            ensure_source_cursor_schema(conn)
            for row in conn.execute(
                    "SELECT source_path,error,fingerprint,updated_at FROM source_sync_cursors WHERE host=? AND status='error' AND error!='usage_sync_pending'",
                    (connection_id,)):
                try:
                    validate_path(row["source_path"], roots)
                except AgentAdapterError:
                    continue
                code = _source_error_code(row["error"])
                this_cycle = bool(skip_failures_since and str(row["updated_at"] or "") >= skip_failures_since)
                if this_cycle and code not in _TERMINAL_SOURCE_ERRORS:
                    retryable += 1
                elif code in _TERMINAL_SOURCE_ERRORS and row["fingerprint"]:
                    skipped_terminal += 1
                    skipped_codes[code] = skipped_codes.get(code, 0) + 1
                elif code in _SETTLED_TRANSIENT_ERRORS and row["fingerprint"]:
                    continue
                else:
                    retryable += 1
            if retryable:
                note = f"partial_sync: {retryable} sources still require retry or repair"
                result = "partial"
            elif skipped_terminal:
                codes = ", ".join(f"{count} {code}" for code, count in sorted(skipped_codes.items()))
                note = (
                    f"skipped_damaged: {skipped_terminal} damaged sources skipped ({codes}); "
                    "readable history is current"
                )
                result = "ok"
            else:
                note = None
                result = "ok"
            conn.execute("UPDATE connections SET last_sync = ?, error = ?, updated_at = ? WHERE id = ?",
                         (now, note, now, connection_id))
            log_audit(actor="user", action="connection.sync", target=connection_id, result=result, conn=conn)
        updated = get_connection(connection_id)
        return {**updated, "status": "skipped", "connection": updated, "reason": "source_unchanged"}
    usage_failures = []
    usage_failed_paths = set()
    unstable: set[str] = set()

    def ensure_sync_authority() -> None:
        current = get_connection(connection_id)
        if (not current.get("enabled") or current.get("paused")
                or current.get("paths", []) != roots
                or current.get("updated_at") != connection_revision):
            from app.services.sync_budget import SyncCancelled
            raise SyncCancelled()

    def read_opencode_spooled():
        """Stage one parsed source batch on an unlinked temp file, then stream sessions."""
        if not selected:
            return
        from app.adapters.opencode import iter_opencode_sessions
        database_paths = [path for path in selected if Path(path).name == "opencode.db"]
        legacy_paths = [path for path in selected if Path(path).is_dir() or Path(path).suffix == ".json"]
        groups = [[path] for path in database_paths]
        if legacy_paths:
            groups.append(legacy_paths)
        if not groups:
            groups = [selected]
        for group in groups:
            spool = tempfile.SpooledTemporaryFile(max_size=64 * 1024, mode="w+t", encoding="utf-8")
            try:
                try:
                    for session in iter_opencode_sessions(roots, source_paths=group):
                        if budget:
                            budget.checkpoint()
                        ensure_sync_authority()
                        spool.write(json_module.dumps(session, ensure_ascii=False))
                        spool.write("\n")
                except PartialHistoryError as exc:
                    partial_failures.extend(exc.failures)
                except AgentAdapterError as exc:
                    partial_failures.append({"path": exc.path or group[0], "code": exc.code, "detail": exc.detail})
                changed = False
                for path in group:
                    before = snapshots.get(path)
                    if before is None:
                        continue
                    try:
                        if _snapshot_source(connection_id, Path(path), roots, before, budget=budget)["fingerprint"] != before["fingerprint"]:
                            raise OSError("Source changed during sync")
                    except (OSError, AgentAdapterError) as exc:
                        changed = True
                        unstable.add(path)
                        partial_failures.append({"path": path, "code": "file_changed", "detail": str(exc)})
                if changed:
                    continue
                spool.seek(0)
                for line in spool:
                    if budget:
                        budget.checkpoint()
                    ensure_sync_authority()
                    yield json_module.loads(line)
            finally:
                spool.close()

    def read_bounded_sources():
        # Parse one independent source/group at a time, then release its text.
        # A 16-file scheduling batch must not hold 16 full transcripts in memory.
        session_titles = None
        if connection_id == "codex" and selected:
            from app.adapters import codex
            wanted = {
                session_id for path in selected
                if Path(path).name != "session_index.jsonl"
                if (session_id := codex._rollout_session_id(Path(path), roots))
            }
            # One bounded, batch-level title projection avoids rescanning and
            # retaining the whole session index once per rollout.
            session_titles = codex._load_session_titles(roots, wanted)
        if connection_id == "opencode" or not selected:
            groups = [selected]
        elif connection_id == "antigravity":
            grouped_paths = {}
            for path in selected:
                grouped_paths.setdefault(str(Path(path).parent), []).append(path)
            groups = list(grouped_paths.values())
        else:
            indexes = [path for path in selected if Path(path).name == "session_index.jsonl"]
            groups = [[path, *indexes] for path in selected if path not in indexes]
        for group in groups:
            budget.checkpoint()
            # The pre-import recheck can already see a moved snapshot. Parsing it
            # would only produce a result the import loop is required to drop.
            if any(path in unstable for path in group):
                continue
            try:
                batch = read_sessions(connection_id, roots, source_paths=group if snapshots else None,
                                      allow_execution=allow_execution, session_titles=session_titles)
            except PartialHistoryError as exc:
                batch = exc.sessions
                partial_failures.extend(exc.failures)
            except AgentAdapterError as exc:
                if _is_legitimately_empty_error(connection_id, exc):
                    batch = []
                else:
                    if not group:
                        raise
                    failed = str(Path(exc.path).resolve()) if exc.path else None
                    affected = [failed] if failed in group else [path for path in group if Path(path).name != "session_index.jsonl"] or group
                    partial_failures.extend({"path": path, "code": exc.code, "detail": exc.detail} for path in affected)
                    continue
            changed = False
            for path in group:
                before = snapshots.get(path)
                if before is None:
                    continue
                try:
                    if _snapshot_source(connection_id, Path(path), roots, before)["fingerprint"] != before["fingerprint"]:
                        raise OSError("Source changed during sync")
                except (OSError, AgentAdapterError):
                    changed = True
            if changed:
                partial_failures.extend({"path": path, "code": "file_changed"} for path in group)
            else:
                yield from batch
            del batch
    def _supplement_codex_with_app_server(base_sessions):
        seen_ext_ids: set[str] = set()
        with db() as conn:
            for row in conn.execute(
                "SELECT external_id, source_path FROM conversations WHERE agent = 'codex' AND external_id IS NOT NULL"
            ).fetchall():
                ext_id = str(row["external_id"] or "")
                sp = str(row["source_path"] or "")
                if ext_id and sp and not sp.startswith("codex://") and sp in known_source_paths:
                    seen_ext_ids.add(ext_id)
        for item in base_sessions:
            ext_id = str(item.get("external_id") or "")
            if ext_id:
                seen_ext_ids.add(ext_id)
            yield item
        try:
            from app.transport import fetch_codex_sessions_sync
            extra_sessions = fetch_codex_sessions_sync([str(r) for r in roots], allow_execution=True)
        except Exception:
            extra_sessions = []
        for extra in extra_sessions or []:
            if not isinstance(extra, dict):
                continue
            ext_id = str(extra.get("external_id") or "")
            if ext_id and ext_id in seen_ext_ids:
                continue
            if ext_id:
                seen_ext_ids.add(ext_id)
            yield extra

    try:
        try:
            # No implicit app-server startup for passive history collection.
            if connection_id == "opencode":
                sessions = read_opencode_spooled()
            elif connection_id == "codex" and allow_execution and not has_rollout_sources:
                if should_query_codex_app_server:
                    sessions = read_sessions(connection_id, roots, source_paths=None, allow_execution=True)
                else:
                    sessions = iter(())
            elif budget:
                sessions = read_bounded_sources() if selected else iter(())
                if should_query_codex_app_server and has_rollout_sources:
                    sessions = _supplement_codex_with_app_server(sessions)
            elif not selected and partial_failures:
                sessions = iter(())
            else:
                sessions = read_sessions(connection_id, roots, source_paths=selected if snapshots else None, allow_execution=allow_execution)
                if should_query_codex_app_server and has_rollout_sources:
                    sessions = _supplement_codex_with_app_server(sessions)
        except PartialHistoryError as exc:
            sessions = exc.sessions
            partial_failures.extend(exc.failures)
        except AgentAdapterError as exc:
            if _is_legitimately_empty_error(connection_id, exc):
                sessions = iter(())
            else:
                raise
        failed_paths = {str(Path(f["path"]).resolve()) for f in partial_failures if f.get("path")}
        # Recheck BEFORE importing: an adapter must not commit a moving snapshot.
        for path, before in snapshots.items():
            if path in unstable:
                continue
            canonical_p = Path(path)
            try:
                current_snap = _snapshot_source(connection_id, canonical_p, roots, before, budget=budget)
                if current_snap["fingerprint"] != before["fingerprint"]:
                    # For SQLite databases (.db, .sqlite, .vscdb), SQLite's read transaction
                    # gives snapshot isolation. If the main DB file identity is stable, WAL growth/activity
                    # during or after read is normal concurrent behavior. We keep the valid consistent snapshot
                    # and record the updated fingerprint.
                    if canonical_p.suffix in {".db", ".sqlite", ".vscdb"}:
                        before_id = before.get("file_identity")
                        current_id = current_snap.get("file_identity")
                        if before_id and current_id and before_id == current_id:
                            snapshots[path] = current_snap
                            continue
                    raise OSError("Source changed during sync")
            except (OSError, AgentAdapterError) as exc:
                unstable.add(path)
                partial_failures.append({"path": path, "code": "file_changed", "detail": str(exc)})
        failed_paths |= unstable
        if connection_id == "codex" and any(Path(p).name == "session_index.jsonl" for p in snapshots):
            try:
                from app.adapters import codex
                titles = codex._load_session_titles(roots)
                if titles:
                    with db() as conn:
                        for sid, title in titles.items():
                            if sid and title:
                                conn.execute(
                                    "UPDATE conversations SET title = ? WHERE agent = 'codex' AND (external_id = ? OR external_id = ?) AND title != ?",
                                    (title, f"codex:{sid}", sid, title),
                                )
            except Exception:
                pass
        for session in sessions:
            if budget:
                budget.checkpoint()
            ensure_sync_authority()
            external_id = session.get("external_id")
            source_path = session.get("source_path")
            physical_source = source_path.rsplit("#", 1)[0] if connection_id == "opencode" and source_path else source_path
            if physical_source and str(Path(physical_source).resolve()) in unstable:
                continue
            title = session.get("title") or f"{connection_id} Session"
            cwd = session.get("cwd")
            tombstone_key = conversation_service.compute_source_identity_key(connection_id, source_path, title, external_id)
            with db() as conn:
                if conn.execute("SELECT id FROM tombstones WHERE entity_type = 'conversation' AND dedupe_key = ?", (tombstone_key,)).fetchone():
                    continue
            project_id = project_service.reconstruct_or_resolve_project(
                cwd, metadata={"name": session["project_name"]} if session.get("project_name") else None)
            if session.get("messages"):
                conversation_service.upsert_conversation(
                    agent=connection_id, title=title, external_id=external_id, project_id=project_id,
                    cwd=cwd, source_path=source_path, messages=session.get("messages", []),
                    process_summary=session.get("process_summary") or "", incremental=False,
                    started_at=session.get("started_at"), updated_at=session.get("updated_at"))
            if session.get("usage_observations"):
                try:
                    from app.services import usage_service
                    source_id = hashlib.sha256(f"{connection_id}:{external_id or physical_source}".encode()).hexdigest()
                    observations = usage_service.extract_usage_from_messages(
                        session["usage_observations"], host_id=connection_id, source_id=source_id,
                        project_id=project_id, started_at=session.get("started_at"))
                    batch_size = 100 if budget else 1000
                    for offset in range(0, len(observations), batch_size):
                        if budget:
                            budget.checkpoint()
                        ensure_sync_authority()
                        usage_service.ingest(observations[offset:offset + batch_size])
                except Exception:
                    from app.services.sync_budget import SyncCancelled
                    if budget and budget.stop.is_set():
                        raise SyncCancelled()
                    # Metadata import is separately retryable. Never downgrade a
                    # successfully parsed conversation because accounting is unavailable.
                    usage_failures.append(source_id if "source_id" in locals() else "usage")
                    if physical_source:
                        usage_failed_paths.add(legacy_cursor_path(physical_source))
        # Translate child-level legacy failures to their single atomic storage
        # cursor. This keeps retained path state bounded by the selected batch.
        for failure in partial_failures:
            if failure.get("path"):
                failed_path = str(Path(failure["path"]).resolve())
                failed_paths.add(legacy_cursor_path(failed_path))
        partial_error = None
        unique_failures = _unique_failures(partial_failures)
        if unique_failures:
            failed = ", ".join(str(f.get("path") or "unknown") for f in unique_failures)
            terminal_only = all(
                _source_error_code(str(f.get("code") or "")) in _TERMINAL_SOURCE_ERRORS
                for f in unique_failures
            )
            if terminal_only:
                details = "; ".join(
                    f"{_source_error_text(f)} at {f.get('path') or 'unknown'}" for f in unique_failures
                )
                partial_error = (
                    f"skipped_damaged: {len(unique_failures)} sources stay skipped until they change: {details}"
                )
            else:
                partial_error = (
                    f"partial_sync: readable sessions processed; {len(unique_failures)} "
                    f"unreadable sources require repair: {failed}"
                )
        if budget:
            budget.checkpoint()
        source_fingerprint = None
        if not partial_failures and not pending_sources:
            source_fingerprint = _source_fingerprint(connection_id, roots, budget=budget)
        with db() as conn:
            ensure_source_cursor_schema(conn)
            # Cursor advances only AFTER all conversation writes succeeded. A crash in
            # between replays idempotent upserts, never loses an uncommitted source.
            for path, snapshot in snapshots.items():
                _write_source_cursor(conn, connection_id, snapshot, "source_requires_retry" if path in failed_paths else ("usage_sync_pending" if path in usage_failed_paths else None))
            for failure in unique_failures:
                if failure.get("path"):
                    failed_path = str(Path(failure["path"]).resolve())
                    source_cursor_path = legacy_cursor_path(failed_path)
                    if source_cursor_path != failed_path:
                        continue
                    _write_source_cursor(
                        conn, connection_id,
                        snapshots.get(failed_path) or {"source_path": failed_path},
                        _source_error_text(failure),
                    )
            if source_fingerprint is not None and legacy_storage_paths and not failed_paths and not usage_failed_paths:
                # Remove legacy per-file cursors only after the directory source
                # has synced successfully, before computing outstanding errors.
                prefix_lengths = [(path.rstrip(os.sep) + os.sep, len(path.rstrip(os.sep) + os.sep)) for path in legacy_storage_paths]
                for prefix, prefix_length in prefix_lengths:
                    conn.execute("""DELETE FROM source_sync_cursors WHERE host='opencode'
                        AND length(source_path)>? AND substr(source_path,1,?)=?""",
                        (prefix_length, prefix_length, prefix))
            # Separate stable format failures from errors that can still succeed.
            from app.adapters.base import validate_path
            retryable = 0
            skipped_terminal = 0
            for row in conn.execute(
                    "SELECT source_path,error,fingerprint FROM source_sync_cursors WHERE host=? AND status='error' AND error!='usage_sync_pending'",
                    (connection_id,)):
                try:
                    validate_path(row["source_path"], roots)
                except AgentAdapterError:
                    continue
                code = _source_error_code(row["error"])
                if code in _TERMINAL_SOURCE_ERRORS and row["fingerprint"]:
                    skipped_terminal += 1
                else:
                    retryable += 1
            outstanding = retryable
            if not partial_error and not pending_sources:
                if retryable:
                    partial_error = f"partial_sync: {retryable} sources still require retry or repair"
                elif skipped_terminal:
                    # Damaged sources are remembered and skipped. This is not a
                    # whole-host sync failure and must not look like one.
                    partial_error = f"skipped_damaged: {skipped_terminal} sources stay skipped until they change"
            if outstanding and not partial_error:
                partial_error = f"partial_sync: {outstanding} sources still require retry or repair"
            if usage_failures:
                set_app_setting(f"usage_sync:{connection_id}", {"status": "partial", "failed_sources": usage_failures}, conn=conn)
                log_audit(actor="daemon", action="usage.native_sync", target=connection_id, result="partial", conn=conn)
            else:
                set_app_setting(f"usage_sync:{connection_id}", {"status": "ok", "failed_sources": []}, conn=conn)
            if source_fingerprint is not None:
                set_app_setting(f"sync_fingerprint:{connection_id}", source_fingerprint, conn=conn)
            sync_result = "partial" if (partial_error or "").startswith("partial_sync:") else "ok"
            conn.execute("UPDATE connections SET last_sync = ?, error = ?, updated_at = ? WHERE id = ?", (now, partial_error, now, connection_id))
            log_audit(actor="user", action="connection.sync", target=connection_id, result=sync_result, conn=conn)
    except Exception as exc:
        from app.services.sync_budget import SyncCancelled
        active_sessions = locals().get("sessions")
        close_sessions = getattr(active_sessions, "close", None)
        if callable(close_sessions):
            try:
                close_sessions()
            except Exception:
                pass
        if isinstance(exc, SyncCancelled):
            raise
        is_adapter = isinstance(exc, AgentAdapterError)
        message = f"{exc.code}: {exc.detail}" if is_adapter else f"SYNC_ERROR: {exc}"
        with db() as conn:
            ensure_source_cursor_schema(conn)
            error_path = str(Path(exc.path).resolve()) if is_adapter and exc.path else None
            for path, snapshot in snapshots.items():
                if path in selected and (error_path is None or path == error_path):
                    # A failed atomic import cannot move the source cursor.
                    # Keep the pending receipt as an error with no committed
                    # content fingerprint/offset so the next run replays it.
                    failed_snapshot = {**snapshot, "fingerprint": "", "file_identity": "", "complete_offset": 0}
                    _write_source_cursor(conn, connection_id, failed_snapshot, message)
            conn.execute("UPDATE connections SET error = ?, updated_at = ? WHERE id = ?", (message, now, connection_id))
            log_audit(actor="user", action="connection.sync", target=connection_id, result="error", conn=conn)
        raise HTTPException(status_code=422 if is_adapter else 500, detail=message) from exc
    result = get_connection(connection_id)
    if budget:
        budget.checkpoint()
        result["pending_sources"] = pending_sources
        result["processed_sources"] = len(selected)
    return result


def setup_connection(connection_id: str, base_url: str | None = None) -> dict[str, Any]:
    conn_data = get_connection(connection_id)
    from app.integrations.installer import generate_setup_bundle, _manifest_path

    trusted_base = resolve_trusted_base_url(base_url)
    token_preview = conn_data.get("token_preview")
    bundle = generate_setup_bundle(
        connection_id,
        token="<agent-token>",
        base_url=trusted_base,
    )
    manifest_p = str(_manifest_path(connection_id, Path.home()))
    from app.services.host_registry import HOST_DEFINITIONS
    from app.integrations.recovery import read_file
    installed_components = []
    installation_review = 'not_installed'
    try:
        manifest_raw, _ = read_file(Path(manifest_p), Path.home().resolve())
        if manifest_raw is not None:
            manifest = json.loads(manifest_raw)
            installed_components = manifest.get('components', ['mcp', 'hooks', 'skill'])
            if not isinstance(installed_components, list) or any(c not in ('mcp', 'hooks', 'skill') for c in installed_components):
                raise ValueError('Invalid component record')
            installation_review = 'recorded' if 'components' in manifest else 'legacy_review_required'
    except (OSError, ValueError, TypeError, AttributeError):
        installed_components, installation_review = [], 'review_required'
    from app.services.connection_plans import ConnectionPlans
    planner = ConnectionPlans()
    with planner.transactions.connect() as pending_conn:
        pending_plans = [dict(row) for row in pending_conn.execute(
            "SELECT id,status FROM management_plans WHERE status IN ('applying','recovery_required') AND json_extract(payload,'$.metadata.namespace')='connections' AND json_extract(payload,'$.metadata.host_id')=? ORDER BY created_at LIMIT 10", (connection_id,))]
    return {
        "id": connection_id,
        "label": conn_data["label"],
        "plan_supported": True,
        "surfaces": list(HOST_DEFINITIONS[connection_id].surfaces),
        "installed_components": installed_components,
        "installation_review": installation_review,
        "pending_plans": pending_plans,
        "token": None,
        "token_preview": "configured" if token_preview else None,
        "config_preview": bundle.mcp_config["content"],
        "target_paths": {
            "credential": bundle.credential_path,
            "mcp": bundle.mcp_config["target_path"],
            "hooks": bundle.hooks_config["target_path"],
            "skill": bundle.skill_or_command["target_path"],
            "manifest": manifest_p,
        },
        "operations": [
            {"action": "write_credential", "path": bundle.credential_path, "description": "写入 0600 权限的 Agent 访问令牌"},
            {"action": "configure_mcp", "path": bundle.mcp_config["target_path"], "description": "写入或合并 ContextCore MCP 服务配置"},
            {"action": "configure_hooks", "path": bundle.hooks_config["target_path"], "description": "配置会话采集 Hook / 扩展事件"},
            {"action": "install_skill", "path": bundle.skill_or_command["target_path"], "description": "安装 ContextCore 接棒命令或技能"},
            {"action": "write_manifest", "path": manifest_p, "description": "写入 ContextCore 托管所有权凭证 (Owner Manifest)"},
        ],
        "bundle": bundle.to_dict(),
        "notes": bundle.notes,
        "instructions": bundle.install_script,
    }

def verify_agent_token(token: str, *, purpose: str = "context_tools") -> str | None:
    if not token:
        return None
    token_h = hashlib.sha256(token.strip().encode("utf-8")).hexdigest()
    with db() as conn:
        row = conn.execute(
            "SELECT id, enabled, paused FROM connections WHERE token_hash = ?",
            (token_h,),
        ).fetchone()
        if not row:
            return None
        from app.services.connection_plans import read_grants, CAPABILITIES
        if purpose not in CAPABILITIES:
            return None
        grants = read_grants(conn, row['id'])
        if grants is not None:
            return row['id'] if grants.get(purpose) is True else None
        if not row["enabled"]:
            return None
        return row["id"]


def revoke_agent_token(connection_id: str) -> dict[str, Any]:
    with db() as conn:
        now = now_iso()
        conn.execute(
            """
            UPDATE connections SET
                token_hash = NULL,
                token_preview = NULL,
                config_preview = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (now, connection_id),
        )
        log_audit(actor="user", action="connection.token_revoke", target=connection_id, conn=conn)

    cred_path = Path.home() / ".contextcore" / "credentials" / f"{connection_id}.token"
    try:
        if cred_path.is_file():
            cred_path.unlink()
    except OSError:
        pass

    return get_connection(connection_id)
