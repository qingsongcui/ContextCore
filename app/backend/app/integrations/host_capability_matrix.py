"""Two independent host support tables. Live results never transfer across hosts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.integrations.constants import SUPPORTED_HOSTS

CONTEXT_CAPABILITIES = (
    "history_read",
    "new_capture",
    "tool_load",
    "memory_read",
    "memory_write",
    "handoff_readback",
    "confirm_then_continue",
)
CONFIG_CAPABILITIES = (
    "model_switch",
    "third_party_mcp",
    "skills_prompts",
    "uninstall_restore",
)
EVIDENCE_GRADES = (
    "unverified",
    "code_present",
    "synthetic_tested",
    "live_protocol",
    "live_host",
)
EVIDENCE_PATH = Path(__file__).with_name("host_capability_evidence.json")


def load_evidence() -> dict[str, Any]:
    return json.loads(EVIDENCE_PATH.read_text(encoding="utf-8"))


def _cell(table: dict[str, Any], host: str, capability: str) -> dict[str, Any]:
    host_row = table.get(host)
    if not isinstance(host_row, dict) or capability not in host_row:
        raise KeyError(f"Missing {host}.{capability}")
    cell = host_row[capability]
    grade = cell.get("grade")
    if grade not in EVIDENCE_GRADES:
        raise ValueError(f"{host}.{capability} has unknown grade {grade!r}")
    if cell.get("verified") is True and grade not in {"live_host", "live_protocol"}:
        raise ValueError(f"{host}.{capability} cannot be verified at grade {grade}")
    return cell


def context_matrix(evidence: dict[str, Any] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    data = evidence or load_evidence()
    table = data["context_handoff"]
    return {host: {cap: _cell(table, host, cap) for cap in CONTEXT_CAPABILITIES} for host in SUPPORTED_HOSTS}


def config_matrix(evidence: dict[str, Any] | None = None) -> dict[str, dict[str, dict[str, Any]]]:
    data = evidence or load_evidence()
    table = data["config_tools"]
    return {host: {cap: _cell(table, host, cap) for cap in CONFIG_CAPABILITIES} for host in SUPPORTED_HOSTS}


def inherited_live_passes(evidence: dict[str, Any] | None = None) -> list[str]:
    """Claude Code and OpenCode must not copy another host's live_host pass."""
    context = context_matrix(evidence)
    problems = []
    donors = [
        json.dumps(context[host], sort_keys=True, ensure_ascii=False)
        for host in ("codex", "cursor", "omp", "antigravity")
    ]
    donor_evidence = {
        cell.get("evidence")
        for host in ("codex", "cursor", "omp", "antigravity")
        for cell in context[host].values()
        if cell.get("evidence")
    }
    for host in ("claude-code", "opencode"):
        blob = json.dumps(context[host], sort_keys=True, ensure_ascii=False)
        if blob in donors:
            problems.append(f"{host} context matrix is identical to another host")
        for cap, cell in context[host].items():
            if cell.get("grade") == "live_host":
                if not cell.get("host_version"):
                    problems.append(f"{host}.{cap} live_host requires its own host_version")
                if cell.get("evidence") in donor_evidence:
                    problems.append(f"{host}.{cap} live_host cannot copy another host's evidence")
    return problems
