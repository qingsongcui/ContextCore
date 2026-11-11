from __future__ import annotations

"""Template rendering for MCP configs, hooks, extensions, and skills."""

import json
import os
from pathlib import Path
import shlex
import sys
from typing import Any
from app.integrations.constants import (
    DEFAULT_BASE_URL,
    DEFAULT_MCP_ENDPOINT,
    HOOK_COMMAND_TIMEOUT_SEC,
    HOST_CONFIG_PATHS,
    HostId,
    MCP_STARTUP_TIMEOUT_SEC,
    MCP_TOOL_TIMEOUT_SEC,
)

SKILLS_DIR = Path(__file__).parent / "skills"
OMP_EXTENSION_PATH = Path(__file__).parent / "omp_extension.ts"
OPENCODE_PLUGIN_PATH = Path(__file__).parent / "opencode_plugin.ts"
CC_HOOK_PATH = Path(__file__).parent / "cc_hook.py"


def render_mcp_config(
    agent_id: HostId,
    base_url: str = DEFAULT_BASE_URL,
    token: str = "<agent-token>",
) -> dict[str, Any]:
    """Render the exact MCP configuration structure for the given host."""
    clean_base = base_url.rstrip("/")
    mcp_url = f"{clean_base}{DEFAULT_MCP_ENDPOINT}"
    target_path = HOST_CONFIG_PATHS[agent_id]["mcp"]

    if agent_id == "codex":
        # Official Codex MCP format in ~/.codex/config.toml
        content = (
            f"[mcp_servers.contextcore]\n"
            f'url = "{mcp_url}"\n'
            f'http_headers = {{ Authorization = "Bearer {token}" }}\n'
            f"startup_timeout_sec = {MCP_STARTUP_TIMEOUT_SEC}\n"
            f"tool_timeout_sec = {MCP_TOOL_TIMEOUT_SEC}\n"
            f"enabled = true\n"
        )
        return {
            "target_path": target_path,
            "format": "toml",
            "content": content,
            "data": {
                "mcp_servers": {
                    "contextcore": {
                        "url": mcp_url,
                        "http_headers": {"Authorization": f"Bearer {token}"},
                        "startup_timeout_sec": MCP_STARTUP_TIMEOUT_SEC,
                        "tool_timeout_sec": MCP_TOOL_TIMEOUT_SEC,
                        "enabled": True,
                    }
                }
            },
        }

    elif agent_id == "omp":
        # Official Oh My Pi format in ~/.omp/agent/mcp.json
        data = {
            "mcpServers": {
                "contextcore": {
                    "type": "http",
                    "url": mcp_url,
                    "headers": {
                        "Authorization": f"Bearer {token}",
                    },
                    "timeout": MCP_TOOL_TIMEOUT_SEC,
                }
            }
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
        }

    elif agent_id == "cursor":
        # Official Cursor format in ~/.cursor/mcp.json
        data = {
            "mcpServers": {
                "contextcore": {
                    "url": mcp_url,
                    "headers": {
                        "Authorization": f"Bearer {token}",
                    },
                    "timeout": MCP_TOOL_TIMEOUT_SEC,
                }
            }
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
        }

    elif agent_id == "antigravity":
        # Official Google Antigravity format in ~/.gemini/config/mcp_config.json
        data = {
            "mcpServers": {
                "contextcore": {
                    "serverUrl": mcp_url,
                    "headers": {
                        "Authorization": f"Bearer {token}",
                    },
                    "timeoutSeconds": MCP_TOOL_TIMEOUT_SEC,
                }
            }
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
        }

    elif agent_id == "claude-code":
        # Official Claude Code user MCP config in ~/.claude.json
        data = {
            "mcpServers": {
                "contextcore": {
                    "type": "http",
                    "url": mcp_url,
                    "headers": {
                        "Authorization": f"Bearer {token}",
                    },
                    "timeout": MCP_TOOL_TIMEOUT_SEC,
                }
            }
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
        }

    elif agent_id == "opencode":
        # Official OpenCode remote MCP format in ~/.config/opencode/opencode.json
        data = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {
                "contextcore": {
                    "type": "remote",
                    "url": mcp_url,
                    "headers": {
                        "Authorization": f"Bearer {token}",
                    },
                    "timeout": MCP_TOOL_TIMEOUT_SEC,
                    "enabled": True,
                }
            },
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
        }

    raise ValueError(f"Unsupported agent_id: {agent_id}")


def render_hooks_config(
    agent_id: HostId,
    hook_runner_path: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, Any]:
    """Render host-specific hooks configuration or extension code."""
    raw_runner = hook_runner_path or str(CC_HOOK_PATH.resolve())
    clean_base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if getattr(sys, "frozen", False):
        runner = f"{shlex.quote(sys.executable)} --hook"
    elif raw_runner.endswith(".py"):
        runner = f"{shlex.quote(sys.executable)} {shlex.quote(raw_runner)}"
    else:
        runner = shlex.quote(raw_runner)
    if clean_base != DEFAULT_BASE_URL.rstrip("/"):
        runner = f"CONTEXTCORE_BASE_URL={shlex.quote(clean_base)} {runner}"
    target_path = HOST_CONFIG_PATHS[agent_id].get("hooks", "")

    if agent_id == "codex":
        # Official Codex hooks schema in ~/.codex/hooks.json
        data = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume|clear|compact|fork",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{runner} codex session-start",
                                "additionalContextLimit": 4000,
                            }
                        ],
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{runner} codex prompt",
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{runner} codex stop",
                                "timeout": HOOK_COMMAND_TIMEOUT_SEC,
                            }
                        ]
                    }
                ],
            }
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
            "trust_note": "Requires explicit trust in Codex CLI via /hooks or desktop settings. Hooks will not execute until trusted.",
        }

    elif agent_id == "omp":
        # Official OMP extension written in TypeScript
        if not OMP_EXTENSION_PATH.is_file():
            raise FileNotFoundError(f"Missing packaged OMP extension template: {OMP_EXTENSION_PATH}")
        content = OMP_EXTENSION_PATH.read_text(encoding="utf-8")
        if not content.strip():
            raise RuntimeError(f"Packaged OMP extension template is empty: {OMP_EXTENSION_PATH}")
        if clean_base != DEFAULT_BASE_URL.rstrip("/"):
            content = content.replace(f'const BASE_URL = "{DEFAULT_BASE_URL}";', f'const BASE_URL = "{clean_base}";')
        return {
            "target_path": target_path,
            "format": "ts",
            "content": content,
            "data": None,
            "trust_note": "OMP loads pre-hooks from ~/.omp/agent/hooks/pre/*.ts. Fully deterministic in-process event interception.",
        }
    elif agent_id == "cursor":
        # Official Cursor hooks schema in ~/.cursor/hooks.json
        data = {
            "version": 1,
            "hooks": {
                "sessionStart": [{"command": f"{runner} cursor session-start"}],
                "beforeSubmitPrompt": [{"command": f"{runner} cursor prompt"}],
                "afterAgentResponse": [{"command": f"{runner} cursor assistant"}],
                "postToolUse": [{"command": f"{runner} cursor tool"}],
                "stop": [{"command": f"{runner} cursor stop"}],
                "sessionEnd": [{"command": f"{runner} cursor end"}],
            },
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
            "trust_note": "Cursor hooks operate on trusted workspaces. Notice: beforeSubmitPrompt cannot inject context into ongoing prompts; context injection is fire-and-forget on sessionStart.",
        }

    elif agent_id == "antigravity":
        # Official Antigravity hooks schema in ~/.gemini/config/hooks.json
        data = {
            "contextcore-session": {
                "SessionStart": [{"type": "command", "command": f"{runner} antigravity session-start"}]
            },
            "contextcore-invoke": {
                "PreInvocation": [{"type": "command", "command": f"{runner} antigravity pre-invoke"}]
            },
            "contextcore-tool": {
                "PostToolUse": [{"matcher": "*", "hooks": [{"command": f"{runner} antigravity tool"}]}]
            },
            "contextcore-stop": {
                "Stop": [{"type": "command", "command": f"{runner} antigravity stop"}]
            },
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
            "trust_note": "Antigravity hooks support PreInvocation injectSteps. Notice: hook payloads do not contain prompt text, so natural-language prompt interception is not possible.",
        }

    elif agent_id == "claude-code":
        # Official Claude Code user hooks in ~/.claude/settings.json
        data = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|resume|clear|compact|fork",
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{runner} claude-code session-start",
                                "timeout": HOOK_COMMAND_TIMEOUT_SEC,
                            }
                        ],
                    }
                ],
                "UserPromptSubmit": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{runner} claude-code prompt",
                                "timeout": HOOK_COMMAND_TIMEOUT_SEC,
                            }
                        ]
                    }
                ],
                "Stop": [
                    {
                        "hooks": [
                            {
                                "type": "command",
                                "command": f"{runner} claude-code stop",
                                "timeout": HOOK_COMMAND_TIMEOUT_SEC,
                            }
                        ]
                    }
                ],
            }
        }
        return {
            "target_path": target_path,
            "format": "json",
            "content": json.dumps(data, indent=2, ensure_ascii=False),
            "data": data,
            "kind": "json_hooks",
            "trust_note": "Claude Code loads user hooks from ~/.claude/settings.json. Review them in /hooks after installation. Injected context is capped below the 10,000-character limit.",
        }

    elif agent_id == "opencode":
        if not OPENCODE_PLUGIN_PATH.is_file():
            raise FileNotFoundError(f"Missing packaged OpenCode plugin template: {OPENCODE_PLUGIN_PATH}")
        content = OPENCODE_PLUGIN_PATH.read_text(encoding="utf-8")
        if not content.strip():
            raise RuntimeError(f"Packaged OpenCode plugin template is empty: {OPENCODE_PLUGIN_PATH}")
        if clean_base != DEFAULT_BASE_URL.rstrip("/"):
            content = content.replace(f'const BASE_URL = "{DEFAULT_BASE_URL}";', f'const BASE_URL = "{clean_base}";')
        return {
            "target_path": target_path,
            "format": "ts",
            "content": content,
            "data": None,
            "kind": "ts_plugin",
            "trust_note": "OpenCode loads ~/.config/opencode/plugins/*.ts at startup; restart OpenCode after installation. The plugin fails open within 2.5 seconds and never logs the bearer.",
        }
    raise ValueError(f"Unsupported agent_id: {agent_id}")


def _read_skill_template(path: Path, agent_id: HostId) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Missing packaged skill or command template for {agent_id}: {path}")
    content = path.read_text(encoding="utf-8")
    if not content.strip():
        raise RuntimeError(f"Packaged skill or command template is empty for {agent_id}: {path}")
    return content


def render_skill_or_command(agent_id: HostId) -> dict[str, Any]:
    """Render the skill markdown or command definition file for the host."""
    if agent_id == "codex":
        path = SKILLS_DIR / "codex_skill.md"
        content = _read_skill_template(path, agent_id)
        return {
            "target_path": HOST_CONFIG_PATHS["codex"]["skill"],
            "filename": "SKILL.md",
            "content": content,
            "invocation": "$contextcore-handoff",
        }
    elif agent_id == "omp":
        path = SKILLS_DIR / "omp_command.md"
        content = _read_skill_template(path, agent_id)
        return {
            "target_path": HOST_CONFIG_PATHS["omp"]["command"],
            "filename": "contextcore.md",
            "content": content,
            "invocation": "/contextcore",
        }
    elif agent_id == "cursor":
        path = SKILLS_DIR / "cursor_skill.md"
        content = _read_skill_template(path, agent_id)
        return {
            "target_path": HOST_CONFIG_PATHS["cursor"]["skill"],
            "filename": "SKILL.md",
            "content": content,
            "invocation": "/contextcore-handoff",
        }
    elif agent_id == "antigravity":
        path = SKILLS_DIR / "antigravity_skill.md"
        content = _read_skill_template(path, agent_id)
        return {
            "target_path": HOST_CONFIG_PATHS["antigravity"]["skill"],
            "filename": "SKILL.md",
            "content": content,
            "invocation": "/contextcore-handoff",
        }
    elif agent_id == "claude-code":
        path = SKILLS_DIR / "claude_code_skill.md"
        content = _read_skill_template(path, agent_id)
        return {
            "target_path": HOST_CONFIG_PATHS["claude-code"]["skill"],
            "filename": "SKILL.md",
            "content": content,
            "invocation": "/contextcore-handoff",
        }
    elif agent_id == "opencode":
        path = SKILLS_DIR / "opencode_command.md"
        content = _read_skill_template(path, agent_id)
        return {
            "target_path": HOST_CONFIG_PATHS["opencode"]["command"],
            "filename": "contextcore.md",
            "content": content,
            "invocation": "/contextcore",
        }
    raise ValueError(f"Unsupported agent_id: {agent_id}")


def render_setup_preview(
    agent_id: HostId,
    base_url: str = DEFAULT_BASE_URL,
    token: str = "<agent-token>",
    hook_runner_path: str | None = None,
) -> dict[str, Any]:
    """Assemble a comprehensive preview of all configuration files for an agent."""
    mcp = render_mcp_config(agent_id, base_url=base_url, token=token)
    hooks = render_hooks_config(agent_id, hook_runner_path=hook_runner_path, base_url=base_url)
    skill = render_skill_or_command(agent_id)

    return {
        "agent_id": agent_id,
        "base_url": base_url,
        "mcp_config": mcp,
        "hooks_config": hooks,
        "skill_or_command": skill,
    }
