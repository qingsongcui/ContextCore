"""Constants and capability definitions for host integrations.

Based strictly on HostAdapterRecon findings and official vendor documentation:
- Codex: MCP TOML in ~/.codex/config.toml, hooks in ~/.codex/hooks.json, skill in ~/.codex/skills/contextcore/.
- OMP: MCP in ~/.omp/agent/mcp.json, extension in ~/.omp/agent/hooks/pre/contextcore.ts, /contextcore command.
- Cursor: MCP in ~/.cursor/mcp.json, hooks in ~/.cursor/hooks.json, skill in ~/.cursor/skills/contextcore/.
- Antigravity: MCP in ~/.gemini/config/mcp_config.json, hooks in ~/.gemini/config/hooks.json, skill in ~/.gemini/config/skills/contextcore/.
"""

from typing import Literal

HostId = Literal["codex", "omp", "cursor", "antigravity", "claude-code", "opencode"]

SUPPORTED_HOSTS: tuple[HostId, ...] = (
    "codex",
    "omp",
    "cursor",
    "antigravity",
    "claude-code",
    "opencode",
)

# Single source for user-facing host labels. Discovery, database seeding and tests read this.
HOST_LABELS: dict[HostId, str] = {
    "codex": "Codex Desktop",
    "omp": "Oh My Pi",
    "cursor": "Cursor",
    "antigravity": "Google Antigravity",
    "claude-code": "Claude Code",
    "opencode": "OpenCode",
}

DEFAULT_BASE_URL: str = "http://127.0.0.1:8420"
DEFAULT_MCP_ENDPOINT: str = "/mcp/"
DEFAULT_HOOK_ENDPOINT_TEMPLATE: str = "/api/connections/{agent_id}/hook"
# Host MCP clients wait this long for a local loopback server. A down backend
# must fail open instead of stalling chat. Seconds, matching Codex/Antigravity fields.
MCP_STARTUP_TIMEOUT_SEC = 3
MCP_TOOL_TIMEOUT_SEC = 8
HOOK_COMMAND_TIMEOUT_SEC = 2
COMMAND_HOOK_HOSTS: tuple[HostId, ...] = ("codex", "cursor", "antigravity", "claude-code")

# CoreBackend MCP tools contract (do not invent names)
MCP_TOOLS: tuple[str, ...] = (
    "search_memory",
    "assemble_context",
    "ack_context",
    "get_project_brief",
    "start_handoff",
    "confirm_handoff",
    "read_handoff",
    "read_memory",
    "edit_memory",
    "save_memory",
    "update_task_progress",
    "resolve_conflict",
    "delete_memory",
    "import_note",
    "get_system_status",
    "read_evidence",
    "read_conversation_messages",
    "discover_relay_conversations",
    "list_relay_conversations",
    "relay_conversation",
    "list_relay_projects",
    "relay_project",
    "auto_summarize_memory",
)

# Honest capability matrix based on official vendor evidence
# status: 'available' | 'partial' | 'unsupported' | 'unverified'
HOST_CAPABILITY_STATUS: dict[HostId, dict[str, dict[str, str]]] = {
    "codex": {
        "history": {
            "status": "available",
            "detail": "Supported via official app-server JSON-RPC and local rollout JSONL parser (session_meta, item_completed, response_item).",
        },
        "capture": {
            "status": "available",
            "detail": "Supported via UserPromptSubmit (prompt), Stop (last_assistant_message), PostToolUse, and SessionEnd hooks.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and the /contextcore skill. Lifecycle hooks stay silent.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP save_memory tool. This does not reconstruct undocumented transcripts; live host write invocation unverified.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported when the user asks to continue or invokes the skill; MCP start_handoff/read_handoff/confirm_handoff. Hooks do not inject the card.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "Codex CLI lacks documented no-tools or strict data boundary flags to prevent read access to private files/MCP; compute is blocked until supported isolation policy is proven.",
        },
    },
    "omp": {
        "history": {
            "status": "available",
            "detail": "Supported via strict parsing of OMP session JSONL v3 (title slot, tree leaf traversal, reset_boundary, compaction).",
        },
        "capture": {
            "status": "available",
            "detail": "Supported via in-process extension events (input, before_agent_start, session_stop).",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP and the /contextcore command. Extension auto events stay silent and do not rewrite the user prompt.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP save_memory tool after the connection is explicitly enabled; live host write invocation unverified.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via the /contextcore command and MCP tools. Auto input events do not inject a card. Do not override built-in /handoff.",
        },
        "compute": {
            "status": "available",
            "detail": "Supported via omp -p --mode json with verified official flags (--no-tools, --no-extensions, --no-skills, --no-rules, --no-lsp, --no-session, --no-title, and --config YAML overlay disabling discovery sources via disabledProviders) while preserving HOME auth.",
        },
    },
    "cursor": {
        "history": {
            "status": "partial",
            "detail": "Supported via local SQLite state.vscdb (composerHeaders and cursorDiskKV bubbleId) and Cursor CLI store.db (meta + protobuf/JSON blobs under ~/.cursor/chats).",
        },
        "capture": {
            "status": "partial",
            "detail": "Incremental conversation capture supported via local SQLite state.vscdb ingestion. Real-time hook execution and consumption in Cursor is unverified; hook consumption not claimed.",
        },
        "memory_read": {
            "status": "partial",
            "detail": "Opt-in via MCP assemble_context and /contextcore. beforeSubmitPrompt cannot inject; sessionStart additional_context is unused after silent-hooks.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP save_memory tool. Cursor cannot inject memory before every prompt; live host write invocation unverified.",
        },
        "handoff": {
            "status": "partial",
            "detail": "beforeSubmitPrompt cannot inject context; handoff must be triggered explicitly via /contextcore skill or rules.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "Cursor CLI (agent -p) lacks documented read-only sandbox or --no-tools flags; unconstrained tool execution is disabled for safety.",
        },
    },
    "antigravity": {
        "history": {
            "status": "partial",
            "detail": "Supported via local transcript.jsonl and transcript_full.jsonl across 2.0, IDE, and CLI brain directories (<USER_REQUEST> extraction and PLANNER_RESPONSE); internal database formats remain unverified.",
        },
        "capture": {
            "status": "partial",
            "detail": "Supported via SessionStart, PreInvocation, PostToolUse, and Stop hooks. Note: hook payload does not contain user prompt text.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and the handoff skill. Lifecycle hooks stay silent.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP save_memory tool. Hook payloads do not provide a structured transcript writer; live host write invocation unverified.",
        },
        "handoff": {
            "status": "partial",
            "detail": "Natural language prompt interception is impossible because hook payload lacks prompt text; requires explicit /contextcore skill.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "Antigravity CLI (agy -p) lacks documented read-only sandbox or --no-tools flags; unconstrained tool execution is disabled for safety.",
        },
    },
    "claude-code": {
        "history": {
            "status": "available",
            "detail": "Read-only JSONL parser for ~/.claude/projects/<encoded-cwd>/<session>.jsonl. Rebuilds the parentUuid leaf chain so rewound branches stay out; sidechain and meta records are excluded; the filename is kept as the session identity authority.",
        },
        "capture": {
            "status": "partial",
            "detail": "User prompts are captured by UserPromptSubmit. The Stop payload carries no reply text, so assistant turns become complete only after transcript sync.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and /contextcore. SessionStart and UserPromptSubmit hooks stay silent.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP save_memory tool. This is not a transcript writer; live host write invocation unverified.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via the /contextcore skill and MCP tools. Hooks do not intercept the prompt or inject a card.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "No documented no-tools or read-only data boundary for Claude Code CLI; compute stays OMP-only.",
        },
    },
    "opencode": {
        "history": {
            "status": "available",
            "detail": "Read-only SQLite reader for opencode.db (session/message/part, XDG_DATA_HOME aware). Subagent sessions are excluded and legacy storage/{session,message,part} JSON is used only when no database exists.",
        },
        "capture": {
            "status": "partial",
            "detail": "The plugin captures every submitted prompt. Assistant text becomes complete only after SQLite sync; session.idle sends a lifecycle boundary, not reply bodies.",
        },
        "memory_read": {
            "status": "available",
            "detail": "Opt-in via MCP assemble_context and the /contextcore command. The plugin captures prompts but does not insert synthetic chat parts.",
        },
        "memory_write": {
            "status": "available",
            "detail": "Shared memory writes use the authorized MCP remote server with the installed bearer; live host write invocation unverified.",
        },
        "handoff": {
            "status": "available",
            "detail": "Supported via the /contextcore command and MCP tools. chat.message is capture-only.",
        },
        "compute": {
            "status": "unavailable",
            "detail": "No documented no-tools isolation for the OpenCode CLI under this policy; compute stays OMP-only.",
        },
    },
}

# Standard paths for host configurations
HOST_CONFIG_PATHS: dict[HostId, dict[str, str]] = {
    "codex": {
        "mcp": "~/.codex/config.toml",
        "hooks": "~/.codex/hooks.json",
        "skill": "~/.codex/skills/contextcore/SKILL.md",
    },
    "omp": {
        "mcp": "~/.omp/agent/mcp.json",
        "hooks": "~/.omp/agent/hooks/pre/contextcore.ts",
        "command": "~/.omp/agent/commands/contextcore.md",
    },
    "cursor": {
        "mcp": "~/.cursor/mcp.json",
        "hooks": "~/.cursor/hooks.json",
        "skill": "~/.cursor/skills/contextcore/SKILL.md",
    },
    "antigravity": {
        "mcp": "~/.gemini/config/mcp_config.json",
        "hooks": "~/.gemini/config/hooks.json",
        "skill": "~/.gemini/config/skills/contextcore/SKILL.md",
    },
    "claude-code": {
        "mcp": "~/.claude.json",
        "hooks": "~/.claude/settings.json",
        "skill": "~/.claude/skills/contextcore/SKILL.md",
    },
    "opencode": {
        "mcp": "~/.config/opencode/opencode.json",
        "hooks": "~/.config/opencode/plugins/contextcore.ts",
        "command": "~/.config/opencode/commands/contextcore.md",
    },
}

# Legacy owned skill locations accepted during upgrade/uninstall migration
LEGACY_HOST_SKILL_PATHS: dict[HostId, tuple[str, ...]] = {
    "codex": (
        "~/.agents/skills/contextcore-handoff/SKILL.md",
        "~/.codex/skills/contextcore-handoff/SKILL.md",
    ),
    "cursor": ("~/.cursor/skills/contextcore-handoff/SKILL.md",),
    "antigravity": ("~/.gemini/config/skills/contextcore-handoff/SKILL.md",),
    "claude-code": ("~/.claude/skills/contextcore-handoff/SKILL.md",),
}

# JSON MCP config container path per host. Codex uses TOML and is intentionally absent.
HOST_MCP_CONTAINER: dict[HostId, tuple[str, ...]] = {
    "omp": ("mcpServers",),
    "cursor": ("mcpServers",),
    "antigravity": ("mcpServers",),
    "claude-code": ("mcpServers",),
    "opencode": ("mcp",),
}

# Four hosts use MCP + Skill + hook config + runner + credential + manifest.
MAX_CONNECTION_FILES = 6
