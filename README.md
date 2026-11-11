# ContextCore · Senrelay / 续境

A native macOS app, browser workbench, and local background service that lets multiple AI coding assistants accumulate source-linked project memories, relay conversations across agents in a single command, and manage providers, MCP tools, Skills, and instructions in one place.

[English](README.md) · [中文](README.zh-CN.md)

## Product and scope

The active product lives in `app/`. The [public specification](docs/SPECIFICATION.md) summarizes confirmed product requirements and boundaries.

- **Native macOS App + Browser Workbench + Local Daemon**: Built with a native AppKit/WKWebView macOS shell, a React/Vite workbench, and an independent FastAPI + SQLite loopback service. Closing the window or browser tab does not stop background capture.
- **Six Core Context Hosts**: Codex (Desktop and CLI), Oh My Pi (OMP), Cursor, Antigravity, Claude Code, and OpenCode. Capabilities are tracked independently per host and surface (Codex Desktop ≠ Codex CLI, Cursor IDE ≠ Cursor CLI, Antigravity Desktop ≠ agy, OMP ≠ Pi / OpenCode); see [docs/support-matrix.md](docs/support-matrix.md). Writing a configuration file does not prove a running host process has reloaded it, and local endpoint checks are distinct from live model inference.
- **In-Agent Conversation Relay**: Open any new Agent and type `/contextcore` (or say `"接着做"` / `"接力"` / `"continue"` / `"relay"`) to automatically scan and list recent cross-host conversation candidates with their number, source Agent, project, title, and summary. **Replying with only a candidate number** loads the relay context (`must_not_execute=true`), summarizes where the previous chat left off, and waits for your next instruction; **attaching a concrete task alongside your selection** continues execution directly. Lifecycle hooks report events silently and never force unrequested context into chat.
- **Companion Auto-Memory & Version Governance**: During everyday chats, the active Agent captures structured, source-quoted memories in real time via companion MCP tools and Skills (leaving a brief `💡` receipt). Ordinary memories take effect automatically, explicit user updates supersede older decisions, and ambiguous contradictions route to Pending Conflicts. Supports 📌 core-rule pinning, `AGENTS.md` handbook preview, version rollback, and independent permanent deletion of chats versus memories.
- **Unified Multi-Host Management**: Switch target hosts from the permanent top bar, run **One-Click Connect**, fill read-only provider presets and test endpoint latency, bind models and Keychain secrets, and manage MCP servers, Skills, system prompts, project bundles, per-host routing modes, and local configuration import/export. Changes save automatic rollback snapshots and open a Diff confirmation dialog whenever external manual file edits conflict.
- **Instant Lexical Search & Optional Local Semantics**: Search across all projects or the current project from the workbench header with local FTS5 indexing. Optional local vector semantic search and source-backed relationship graphs live under Settings → Advanced (disabled by default; searching never triggers automatic downloads or dependency installation).
- **Backups & Scope Boundaries**: Whole-database SQLite online snapshots (`0600` permissions under `<data_dir>/backups/`) are supported from Settings and `contextcore backup`. Automated cloud sync, Skill generation foundries, and unsolicited proactive insights are out of scope.

## Desktop app and current distribution

The native macOS bundle ships a self-contained Python runtime, backend service, and frontend workbench; see [desktop build and verification](app/macos/README.md). Current public Beta candidates target **Apple Silicon (`arm64`) on macOS 14.0+** and use ad-hoc signing (`Senrelay-macOS.zip` contains `Senrelay.app` only). First launch takes two steps:

1. Move `Senrelay.app` to `/Applications`;
2. Run `xattr -cr /Applications/Senrelay.app` in Terminal (or allow it under **System Settings → Privacy & Security → Open Anyway**; source checkouts also include `app/macos/install-macos.sh`).

Developer ID signing, Apple notarization, clean-Mac physical Gatekeeper acceptance, and live upgrade acceptance remain open boundaries documented in [release readiness](docs/release-readiness.md).

## Daily-use workflow

### Navigation and layout

- **Primary Left Navigation (3 fixed entries + conditional conflict badge)**:
  - **Projects** (`projects`): Top global search bar across projects, plus four focused project tabs: `Overview`, `Chats`, `Memory` (including tasks/todos and 📌 pinned rules), and `Materials`.
  - **Agent Management** (`agents`): Permanent top host switcher bar for One-Click Connect, providers & models, MCP tools, Skills, system prompts, and routing modes.
  - **Usage & Performance** (`usage`): Recorded requests, token consumption, latency, and cost estimates, with unobserved metrics marked unknown rather than `0`.
  - **Needs Decision** (`conflicts`, shown on demand): Appears in primary navigation with a count badge only when memory conflicts are pending.
- **Compact Sidebar Footer**: Live local service indicator, **Settings** (`settings`, containing appearance, language, LaunchAgent autostart, SQLite online backup, optional semantic search settings, and the embedded **Change Log** viewer), bilingual toggle (`中 / EN`), and theme switcher.

### 1. Create and organize projects

Projects organize work topics. Create a project associated with one or more local repository directories, or with **no directory at all** (directory-free topic projects). Rename, merge, split, or archive projects (archived projects remain searchable), or reconstruct projects from historical chat sessions.

### 2. Accumulate memories, materials, and sources

- **Companion Auto-Memory + Manual Curation**: Companion MCP tools and Skills capture source-quoted memories with a brief `💡` receipt during conversations, while the workbench supports manual notes and document imports (`MD`, `TXT`, `JSON`, `PDF`, `DOCX`, `DOC`).
- **Six Categories & Tiered Context**: Organize memories across **Preferences, Background, Constraints, Tasks, Lessons, and References**, pin 📌 core rules to the protected prompt tier, consolidate duplicates, and preview `AGENTS.md` handbooks.
- **Independent Permanent Deletion**: Trace any memory back to its exact dialogue turns or document source and roll back retained versions. Deleting a conversation never deletes extracted memories, and deleting a memory never deletes original conversations.

### 3. Cross-Agent conversation relay

When switching tools (for example after hitting quota or context limits), continue directly inside your receiving Agent without filling out forms in the web UI:

1. Open your target Agent (Claude Code, Codex, Cursor, Antigravity, OMP, or OpenCode) and type `/contextcore` (or say `"continue"` / `"relay"` / `"接着做"` / `"接力"`).
2. Senrelay syncs authorized histories and lists recent cross-host conversation candidates with their number, source Agent, project, title, and summary.
3. Choose how to proceed:
   - **Reply with just a number** (e.g. `1`): Loads the relay context (`must_not_execute=true`), reports a concise summary of goal, progress, constraints, and next steps, and waits for your next instruction before touching code.
   - **Reply with a number plus a concrete task** (e.g. `1, finish the remaining edge-case unit tests`): Loads the relay context and continues working right away.

### 4. Agent Management

- **Top Host Switcher & One-Click Connect**: Select any core or extended host from the top bar; click **One-Click Connect** to configure authorized history paths, MCP tools, `/contextcore` slash-command Skills, and silent hooks in one step.
- **One-Click Switch with Safe Snapshots**: Fill read-only provider presets, run live endpoint latency checks, and isolate API keys in macOS Keychain. Switching providers or toggling MCP / Skills / prompts saves automatic rollback snapshots with readback verification, opening a Diff review modal whenever external manual edits conflict.
- **Multi-Host Sync & Routing Modes**: Sync shared MCP tools, Skills, and prompts across selected hosts (queuing Diff previews if any host has conflicts), and configure per-host Direct, Local Proxy Routing (with ordered fallback chains), or Model Aggregation modes.
- **Skills Safety**: Defaults to independent file copies (`copy`), with managed per-file version symlinks (`symlink`) available in advanced mode; installing a Skill never executes scripts, and generic directory symlinks remain rejected.
- **Independent Permissions**: Reading chat history, modifying host configuration files, and downloading optional local semantic models are confirmed separately and never bundled.

## Local development quick start

Recommended **Python 3.12** (minimum 3.11 with SQLite `blobopen`) and **Node 24 LTS** (`>=24.15.0`; supported package engines `^22.22.2 || ^24.15.0 || >=26`). **Run all commands below from the repository root** using an isolated data directory:

```bash
python3.12 -m venv app/backend/.venv
app/backend/.venv/bin/python -m pip install -e 'app/backend[test]'
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --no-static
```

In a second terminal, start the isolated Vite dev server from the repository root (`15173` proxies to `18420`):

```bash
npm --prefix app/frontend ci --ignore-scripts
npm --prefix app/frontend run dev:isolated
```

### Built workbench

Stop the dev backend on the same data directory (`Ctrl+C`), then build and serve the staged frontend:

```bash
npm --prefix app/frontend run build
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --frontend-dir app/frontend/dist-staging
```

Open `http://127.0.0.1:18420`. Default source static discovery uses `app/frontend/dist-staging` (never falling back to stale `dist`); `--no-static` serves API/MCP only. See [app operations](app/README.md) for matched release preparation and activation.

## Service and configuration

```bash
app/backend/.venv/bin/contextcore status --json --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore backup --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore stop --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore generate-plist --data-dir "$HOME/.contextcore/dev-data"
```

`./app/scripts/contextcore-service` wraps the same CLI commands plus `prepare-release` and `activate-release`. `generate-plist` prints a LaunchAgent template without installing it; `install` / `uninstall` modify macOS LaunchAgents and require explicit authorization. Backend `.env` files are **not** auto-loaded (see [.env.example](.env.example)), and browser sessions use in-memory random session IDs without requiring a cookie-signing secret.

## Verification gates and boundaries

Automated verification gates currently pass across:

- **Backend suite (`pytest`)**: 1,484 passed;
- **Frontend workbench (`typecheck`, `lint`, `vitest`, `check:i18n`)**: 188 Vitest tests passed, 1,334 Chinese/English UI strings verified with zero unwrapped Chinese;
- **Website checks**: Preview and `SITE_URL` deployment checks, 112 index bilingual strings + bilingual `guide.html`, 3 interaction tests, and 5 sphere motion tests passed;
- **Packaging, Native App & Security**: Installed non-editable wheel 11-resource verification (`check_release_readiness.py`), native `Senrelay.app` (`arm64`, `minos <= 14.0`) bundle/archive and bilingual WKWebView verification, 6-host isolated hook verification, and full working-tree + Git history security scans (`verify_repository_security.py`).

See [release readiness](docs/release-readiness.md) and [evidence](docs/evidence.md) for detailed implementation mappings, historical reference records, and disclosed external boundaries (private GitHub auth, live account billing/OAuth eligibility, Apple notarization, and clean-Mac physical device acceptance).

## Security

Loopback only (`127.0.0.1`); never expose service ports over LAN, public interfaces, reverse proxies, or tunnels. Browser mutations enforce session-bound CSRF and Same-Site origin checks; Agent MCP/hooks and local model routing use distinct authorized credentials. Raw transcripts retain pasted tokens by design while audit logs and default exports strip managed secrets; application-level permanent deletion is not forensic disk erasure. Read [SECURITY](SECURITY.md) for details.

## License

[MIT](LICENSE)
