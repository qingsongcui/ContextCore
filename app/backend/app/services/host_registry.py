"""Metadata-only host discovery. Never opens configuration, transcripts or credentials."""
from __future__ import annotations

import os
import plistlib
import re
import stat
from xml.parsers.expat import ExpatError
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.database import now_iso
from app.integrations.constants import HOST_LABELS, HOST_CAPABILITY_STATUS


@dataclass(frozen=True)
class HostDefinition:
    id: str
    roots: tuple[str, ...]
    history_roots: tuple[str, ...]
    binaries: tuple[str, ...]
    applications: tuple[str, ...] = ()
    home_env: str | None = None
    history_suffix: str = ""
    surfaces: tuple[str, ...] = ("cli",)


HOST_DEFINITIONS = {
    "codex": HostDefinition("codex", (".codex",), (".codex/sessions", ".codex/archived_sessions"), ("codex",), ("Codex.app",), "CODEX_HOME", "sessions", ("desktop", "cli")),
    "omp": HostDefinition("omp", (".omp",), (".omp/agent/sessions",), ("omp",)),
    "cursor": HostDefinition("cursor", (".cursor", "Library/Application Support/Cursor"), (".cursor/chats", "Library/Application Support/Cursor/User"), ("agent", "cursor-agent", "cursor"), ("Cursor.app",), surfaces=("ide", "cli")),
    "antigravity": HostDefinition("antigravity", (".gemini/antigravity", ".gemini/antigravity-cli", ".gemini/antigravity-ide"), (".gemini/antigravity/brain", ".gemini/antigravity-cli/brain", ".gemini/antigravity-ide/brain"), ("agy",), ("Antigravity.app",), surfaces=("desktop", "cli", "ide")),
    "claude-code": HostDefinition("claude-code", (".claude",), (".claude/projects",), ("claude",), home_env="CLAUDE_CONFIG_DIR", history_suffix="projects"),
    "opencode": HostDefinition("opencode", (".config/opencode", ".local/share/opencode"), (".local/share/opencode",), ("opencode",)),
}


def _observation(path: Path, kind: str) -> dict[str, Any]:
    """Do not resolve links: a link into another tree is not an implicit grant."""
    try:
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            if kind == "binary" and path.is_file() and os.access(path, os.X_OK):
                return {"kind": kind, "path": str(path), "status": "executable_link_present"}
            return {"kind": kind, "path": str(path), "status": "symlink_requires_authorization"}
        if path.exists():
            return {"kind": kind, "path": str(path), "status": "present"}
        return {"kind": kind, "path": str(path), "status": "missing"}
    except PermissionError:
        return {"kind": kind, "path": str(path), "status": "permission_denied"}
    except OSError:
        return {"kind": kind, "path": str(path), "status": "unavailable"}


def _application_version(application: Path) -> str | None:
    path = application / 'Contents/Info.plist'
    if any(p.is_symlink() for p in (path, *path.parents)):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > 65536:
                return None
            raw = os.read(fd, 65537)
        finally:
            os.close(fd)
        if len(raw) > 65536:
            return None
        metadata = plistlib.loads(raw)
        value = metadata.get('CFBundleShortVersionString') if isinstance(metadata, dict) else None
        return value if isinstance(value, str) and re.fullmatch(r'[0-9][A-Za-z0-9.+_-]{0,63}', value) else None
    except (OSError, ValueError, OverflowError, ExpatError, plistlib.InvalidFileException):
        return None


def discover_hosts(*, home: Path | None = None, env: Mapping[str, str] | None = None,
                   application_dirs: list[Path] | None = None) -> list[dict[str, Any]]:
    """Path/execute-bit and bounded bundle-version metadata, never health/auth probes.

    Explicit home isolates application and PATH lookup unless an env is supplied.
    SENRELAY_<HOST>_ROOTS (pathsep-separated) overrides history candidates only.
    """
    base = Path(home) if home is not None else Path.home()
    values = dict(env) if env is not None else (dict(os.environ) if home is None else {})
    app_dirs = application_dirs if application_dirs is not None else (
        [base / "Applications"] if home is not None else [Path("/Applications"), base / "Applications"]
    )
    path_dirs = [Path(p) for p in values.get("PATH", "").split(os.pathsep) if p and Path(p).is_absolute()]
    # Finder/launchd often has a minimal PATH. These are metadata-only fallbacks.
    path_dirs += [base / ".local/bin", base / ".cargo/bin", base / ".npm-global/bin"]
    if home is None:
        path_dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    checked_at = now_iso()
    results = []
    for host, definition in HOST_DEFINITIONS.items():
        roots = [base / p for p in definition.roots]
        history = [base / p for p in definition.history_roots]
        if definition.home_env and values.get(definition.home_env):
            override = Path(values[definition.home_env]).expanduser()
            if override.is_absolute():
                extra_history = override / definition.history_suffix if definition.history_suffix else override
                if host == "codex":
                    # CODEX_HOME is the CLI home. Desktop still reads ~/.codex.
                    roots = list(dict.fromkeys([*roots, override]))
                    history = list(dict.fromkeys([*history, extra_history]))
                else:
                    roots = [override]
                    history = [extra_history]
        override_name = "SENRELAY_" + host.upper().replace("-", "_") + "_ROOTS"
        if values.get(override_name):
            history = [Path(p).expanduser() for p in values[override_name].split(os.pathsep) if p and Path(p).expanduser().is_absolute()]
        if host == "codex" and not values.get(override_name):
            sqlite_pattern = re.compile(r"^(?:state|thread_history)_\d+\.sqlite$")
            for root_dir in roots:
                try:
                    if any(p.is_symlink() for p in (root_dir, *root_dir.parents)) or not root_dir.is_dir():
                        continue
                    with os.scandir(root_dir) as it:
                        sqlite_candidates = sorted(
                            (
                                Path(entry.path)
                                for entry in it
                                if sqlite_pattern.match(entry.name) and entry.is_file(follow_symlinks=False)
                            ),
                            key=lambda p: p.name,
                        )
                    if sqlite_candidates:
                        history = list(dict.fromkeys([*history, *sqlite_candidates]))
                except OSError:
                    continue
        if host == "opencode":
            if values.get("XDG_CONFIG_HOME") and Path(values["XDG_CONFIG_HOME"]).is_absolute():
                roots[0] = Path(values["XDG_CONFIG_HOME"]) / "opencode"
            if values.get("XDG_DATA_HOME") and Path(values["XDG_DATA_HOME"]).is_absolute():
                roots[-1] = Path(values["XDG_DATA_HOME"]) / "opencode"
                if not values.get(override_name):
                    history = [roots[-1]]
        evidence = [_observation(p, "config_root") for p in roots]
        history_evidence = [_observation(p, "history_root") for p in history]
        evidence.extend(history_evidence)
        applications = [_observation(d / name, "application") for d in app_dirs for name in definition.applications]
        for application in applications:
            if application['status'] == 'present':
                application['version'] = _application_version(Path(application['path']))
        evidence.extend(applications)
        binaries = []
        for binary in definition.binaries:
            for directory in dict.fromkeys(path_dirs):
                candidate = directory / binary
                observation = _observation(candidate, "binary")
                if observation["status"] == "present":
                    observation["status"] = "executable_present" if candidate.is_file() and os.access(candidate, os.X_OK) else "not_executable"
                    binaries.append(observation)
                elif observation["status"] != "missing":
                    binaries.append(observation)
        evidence.extend(binaries)
        present_history = list(dict.fromkeys(e["path"] for e in history_evidence if e["status"] == "present"))
        has_binary = any(e["status"] in {"executable_present", "executable_link_present"} for e in binaries)
        has_application = any(e["status"] == "present" for e in applications)
        surfaces = []
        app_versions = {entry['version'] for entry in applications if entry.get('version')}
        for surface in definition.surfaces:
            is_cli = surface == "cli"
            # CLI configuration never proves that the IDE/desktop is installed.
            installed = has_binary if is_cli else has_application
            surface_caps = {name: {"implementation": info.get("status", "unsupported"), "status": "not_probed", "verified": False}
                            for name, info in HOST_CAPABILITY_STATUS.get(host, {}).items()}
            cli_history_present = any(Path(p).name == "chats" for p in present_history)
            history_supported = not (host == "cursor" and is_cli and not cli_history_present)
            surface_caps["history"] = {"implementation": "partial" if host in {"cursor", "antigravity"} else "available",
                                       "status": ("candidate" if present_history else "not_detected") if history_supported else "unsupported",
                                       "verified": False}
            surface_caps["resume"] = {"implementation": "out_of_scope", "status": "out_of_scope", "verified": False}
            surfaces.append({"id": surface, "installation": "detected" if installed else "not_detected",
                             "version": next(iter(app_versions)) if not is_cli and len(app_versions) == 1 else None,
                             "version_source": "bundle_metadata" if not is_cli and len(app_versions) == 1 else "unknown",
                             "protocol": "not_probed", "authentication": "unknown",
                             "runtime": "unknown", "verified": False,
                             "capabilities": surface_caps})
        results.append({"id": host, "label": HOST_LABELS[host],
                        "discovered": has_binary or has_application or bool(present_history) or any(e["status"] == "present" for e in evidence),
                        "paths": present_history,
                        "discovery": {"mode": "metadata", "checked_at": checked_at, "execution_performed": False,
                                      "history_read": False, "credentials_read": False, "surfaces": surfaces,
                                      "evidence": evidence, "suggested_paths": present_history,
                                      "authorization": "required", "health": "not_probed"}})
    return results


def ensure_observation_schema(conn=None) -> None:
    from app.database import db
    if conn is None:
        with db() as local:
            ensure_observation_schema(local)
        return
    conn.execute("""CREATE TABLE IF NOT EXISTS host_capability_observations (
        host_id TEXT NOT NULL, surface TEXT NOT NULL, capability TEXT NOT NULL,
        evidence_kind TEXT NOT NULL, metadata TEXT NOT NULL DEFAULT '{}',
        first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY(host_id,surface,capability,evidence_kind)
    )""")


def record_observation(host_id: str, capability: str, surface: str = "unknown",
                       detail: Mapping[str, Any] | None = None, *, evidence_kind: str = "server_observed",
                       conn=None) -> dict[str, Any]:
    """Record authenticated server receipt, never assert GUI consumption or login.

    Callers own authentication. Only enumerated metadata keys are accepted; no
    prompt, message, path, token, payload or tool result body is persisted.
    """
    from app.database import db, encode
    if host_id not in HOST_DEFINITIONS:
        raise ValueError("Unsupported host")
    if surface not in {*HOST_DEFINITIONS[host_id].surfaces, "unknown"}:
        raise ValueError("Unsupported host surface")
    if capability not in {*HOST_CAPABILITY_STATUS.get(host_id, {}), "history", "configuration"}:
        raise ValueError("Unsupported capability")
    if evidence_kind not in {"server_observed", "readback_verified"}:
        raise ValueError("Unsupported evidence kind")
    allowed = {"event_name", "tool_name", "transport", "result", "schema_version"}
    metadata = {k: str(v)[:100] for k, v in (detail or {}).items() if k in allowed and isinstance(v, (str, int, bool))}
    at = now_iso()
    if conn is None:
        with db() as local:
            return record_observation(host_id, capability, surface, metadata, evidence_kind=evidence_kind, conn=local)
    ensure_observation_schema(conn)
    conn.execute("""INSERT INTO host_capability_observations
        (host_id,surface,capability,evidence_kind,metadata,first_seen_at,last_seen_at,count)
        VALUES(?,?,?,?,?,?,?,1) ON CONFLICT(host_id,surface,capability,evidence_kind)
        DO UPDATE SET metadata=excluded.metadata,last_seen_at=excluded.last_seen_at,count=count+1""",
        (host_id, surface, capability, evidence_kind, encode(metadata), at, at))
    return {"host_id": host_id, "surface": surface, "capability": capability,
            "evidence_kind": evidence_kind, "metadata": metadata, "last_seen_at": at,
            "user_interface_consumption": "unknown"}


def get_observations(host_id: str, *, conn=None) -> list[dict[str, Any]]:
    from app.database import db, decode
    if conn is None:
        with db() as local:
            return get_observations(host_id, conn=local)
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='host_capability_observations'").fetchone():
        return []
    rows = conn.execute("SELECT * FROM host_capability_observations WHERE host_id=? ORDER BY surface,capability,evidence_kind", (host_id,)).fetchall()
    return [{**dict(row), "metadata": decode(row["metadata"], {}), "user_interface_consumption": "unknown"} for row in rows]

def get_reference_evidence(host_id: str) -> dict[str, Any] | None:
    """Return bundled reference acceptance evidence for a host.

    This records reference vendor/testbed verification, NOT the current
    installation's local verification. Local verification is tracked solely via
    get_observations() or live probes.
    """
    from app.integrations.host_capability_matrix import load_evidence
    try:
        data = load_evidence()
        return {
            "context_handoff": data.get("context_handoff", {}).get(host_id, {}),
            "config_tools": data.get("config_tools", {}).get(host_id, {}),
            "is_reference_baseline": True,
            "local_installation_verified": False,
        }
    except Exception:
        return None
