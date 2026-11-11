"""
ContextCore Host Adapters Package.

Exports:
- discover(home: Path | None = None) -> list[dict]
- read_sessions(agent_id: str, roots: list[str]) -> list[dict]
- parse_hook(host: str, payload: dict, ...) -> list[dict]
- AgentAdapterError
- CAPABILITY_MATRIX
- HostId
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .antigravity import read_antigravity_sessions
from .base import CAPABILITY_MATRIX, AgentAdapterError, HostId, PartialHistoryError
from .claude_code import read_claude_code_sessions
from .opencode import read_opencode_sessions
from app.integrations.constants import HOST_LABELS
from .codex import read_codex_sessions
from .cursor import read_cursor_sessions
from .hooks import parse_hook
from .omp import read_omp_sessions

# Default candidate directories to probe for discovery (derived from official host documentation)
DISCOVERY_PROFILES: dict[str, dict[str, Any]] = {
    "codex": {
        "label": "Codex Desktop",
        "candidates": [
            (".codex", True),
            (".codex/sessions", True),
            (".codex/archived_sessions", True),
            (".codex/skills", True),
            ("Library/Application Support/Codex", True),
        ],
    },
    "omp": {
        "label": "Oh My Pi (OMP)",
        "candidates": [
            (".omp", True),
            (".omp/agent", True),
            (".omp/agent/sessions", True),
        ],
    },
    "cursor": {
        "label": "Cursor",
        "candidates": [
            (".cursor", True),
            (".cursor/chats", True),
            ("Library/Application Support/Cursor", True),
            ("Library/Application Support/Cursor/User/globalStorage", True),
        ],
    },
    "antigravity": {
        "label": "Antigravity",
        "candidates": [
            (".gemini/config", True),
            (".gemini/antigravity", True),
            (".gemini/antigravity/brain", True),
            (".gemini/antigravity-cli", True),
            (".gemini/antigravity-cli/brain", True),
            (".gemini/antigravity-ide", True),
            (".gemini/antigravity-ide/brain", True),
        ],
    },
    "claude-code": {
        "label": HOST_LABELS["claude-code"],
        "candidates": [
            (".claude", True),
            (".claude/projects", True),
        ],
    },
    "opencode": {
        "label": HOST_LABELS["opencode"],
        "candidates": [
            (".config/opencode", True),
            (".local/share/opencode", True),
        ],
    },
}


def discover(home: Path | None = None) -> list[dict[str, Any]]:
    """
    Discover installed host agents by safely probing candidate directory existence.
    Strictly read-only: does not inspect file contents, private configurations, or credentials.
    """
    base_home = home.resolve() if home else Path.home().resolve()
    connections: list[dict[str, Any]] = []

    for host_id, profile in DISCOVERY_PROFILES.items():
        found_paths: list[str] = []
        for rel_path, is_dir_check in profile["candidates"]:
            target = base_home / rel_path
            # Check existence safely without reading content
            try:
                if target.exists():
                    found_paths.append(str(target.resolve()))
            except (PermissionError, OSError):
                continue

        discovered = len(found_paths) > 0
        caps = CAPABILITY_MATRIX.get(host_id, {})

        connections.append({
            "id": host_id,
            "label": profile["label"],
            "discovered": discovered,
            "enabled": False,
            "paused": False,
            "paths": found_paths,
            "capabilities": caps,
            "last_sync": None,
            "error": None,
            "config_preview": None,
        })

    return connections


HISTORY_READERS: dict[str, Any] = {
    "codex": lambda roots, allow_execution=False, source_paths=None, session_titles=None: read_codex_sessions(roots, allow_execution=allow_execution, source_paths=source_paths, session_titles=session_titles),
    "omp": lambda roots, allow_execution=False, source_paths=None, session_titles=None: read_omp_sessions(roots, source_paths=source_paths),
    "cursor": lambda roots, allow_execution=False, source_paths=None, session_titles=None: read_cursor_sessions(roots, source_paths=source_paths),
    "antigravity": lambda roots, allow_execution=False, source_paths=None, session_titles=None: read_antigravity_sessions(roots, source_paths=source_paths),
    "claude-code": lambda roots, allow_execution=False, source_paths=None, session_titles=None: read_claude_code_sessions(roots, source_paths=source_paths),
    "opencode": lambda roots, allow_execution=False, source_paths=None, session_titles=None: read_opencode_sessions(roots, source_paths=source_paths),
}


def read_sessions(
    agent_id: str,
    roots: list[str],
    *,
    allow_execution: bool = False,
    source_paths: list[str] | None = None,
    session_titles: dict[str, str] | None = None,
    **kwargs: Any,
) -> list[dict[str, Any]]:
    """
    Read conversation sessions from authorized roots for the specified agent host.
    Strictly enforces path containment and prevents symlink escape.
    Unsupported hosts or formats raise AgentAdapterError; never return false empty success.
    allow_execution is explicitly forwarded for hosts requiring subprocess execution (e.g. Codex app-server).
    """
    host_key = agent_id.strip().lower()
    reader = HISTORY_READERS.get(host_key)
    if reader is None:
        from app.integrations.constants import SUPPORTED_HOSTS
        raise AgentAdapterError(
            host=agent_id,
            code="not_installed",
            detail=f"Unknown or unsupported host agent: '{agent_id}'. Must be one of {', '.join(SUPPORTED_HOSTS)}.",
        )
    return reader(roots, allow_execution=allow_execution, source_paths=source_paths, session_titles=session_titles)


__all__ = [
    "AgentAdapterError",
    "PartialHistoryError",
    "CAPABILITY_MATRIX",
    "HostId",
    "discover",
    "parse_hook",
    "read_sessions",
]
