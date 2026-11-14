# Senrelay · 续境 (ContextCore)

A native macOS app, browser workbench, and local background service that lets multiple AI coding assistants accumulate source-linked project memories, relay conversations across agents in a single command, and manage providers, MCP tools, Skills, and instructions in one place.

[English](README.md) · [中文](README.zh-CN.md) · [Official Website](https://senrelay.com/en/) · [Setup Guide](https://senrelay.com/en/guide.html) · [5-Way Comparison](https://senrelay.com/en/compare.html) · [中文对比页](https://senrelay.com/compare.html) · [Public Beta Release (`v1.0.0-beta.2`)](https://github.com/qingsongcui/ContextCore/releases/tag/v1.0.0-beta.2)

> **100% Free & Open Source (`$0`, [MIT License](LICENSE)) · No Account Required · Apple Silicon `macOS 14.0+` (`arm64`) Local-First**

## Product and scope

The active product lives in `app/`. The [public specification](docs/SPECIFICATION.md) summarizes confirmed product requirements and boundaries.

- **Native macOS App + Browser Workbench + Local Daemon**: Built with a native AppKit/WKWebView macOS shell, a React/Vite workbench, and an independent FastAPI + SQLite loopback service. Closing the window or browser tab does not stop background capture.
- **Six Core Context Hosts**: Codex (Desktop and CLI), Oh My Pi (OMP), Cursor, Antigravity, Claude Code, and OpenCode. Capabilities are tracked independently per host and surface (Codex Desktop ≠ Codex CLI, Cursor IDE ≠ Cursor CLI, Antigravity Desktop ≠ agy, OMP ≠ Pi / OpenCode); see [docs/support-matrix.md](docs/support-matrix.md). Writing a configuration file does not prove a running host process has reloaded it, and local endpoint checks are distinct from live model inference.
- **In-Agent Conversation Relay**: Open any new Agent and type `/contextcore` (or say `"接着做"` / `"接力"` / `"continue"` / `"relay"`) to list recent cross-host conversation candidates as short, recognizable briefs (`title`, `brief_summary`, source Agent, project or explicit unassigned label, native session ID, directory, and Git branch—keeping sibling Git worktrees and distinct same-title sessions separate without calling a background model). **Replying with only a candidate number** (or native/internal session ID) loads bounded context and `transcript_paging` (`must_not_execute=true`) without generating a summary card; when turns are omitted, the receiving Agent sequentially pages `read_conversation_messages` (`offset=0`, `limit=30`, or `GET /api/conversations/<id>/messages`), compacts the entire transcript in its own context, reports where the previous session left off, and waits for your next instruction. **Attaching a concrete task alongside your selection** authorizes continuing directly (unless a matching `working_tasks` entry is marked `discussion_only`). Lifecycle hooks report events silently and never inject unrequested context into chat.
- **Dual-Tier Companion Auto-Memory & Version Governance**: Combines durable, source-quoted project/global memories (`contents`: six classes with 📌 core-rule pinning, `AGENTS.md` handbook preview, explicit supersession, `outdated` archival, Pending Conflicts, and honest `saved`/`provided`/`cited` receipts, leaving a brief `💡` notice) with short-term `working_tasks` state (active task goal, completed steps, failed attempts, one-off constraints, and fail-closed `discussion_only` gates via `bootstrap_working_memory`, `upsert_working_task`, and source-backed `promote_task_lesson`). Supports version rollback and independent permanent deletion of chats versus memories.
- **Unified Multi-Host Management**: Switch target hosts from the permanent top bar, run **One-Click Connect**, fill read-only provider presets and test endpoint latency, bind models and Keychain secrets, and manage MCP servers, Skills, system prompts, project bundles, per-host routing modes, and local configuration import/export. Changes save automatic rollback snapshots and open a Diff confirmation dialog whenever external manual file edits conflict.
- **Instant Lexical Search & Optional Local Semantics**: Search across all projects or the current project from the workbench header with local FTS5 indexing. Optional local vector semantic search and source-backed relationship graphs live under Settings → Advanced (disabled by default; searching never triggers automatic downloads or dependency installation).
- **Backups & Scope Boundaries**: Whole-database SQLite online snapshots (`0600` permissions under `<data_dir>/backups/`) are supported from Settings and `contextcore backup`. Automated cloud sync, Skill generation foundries, and unsolicited proactive insights are out of scope.

## Desktop app, pricing, and when NOT to use

The native macOS bundle ships a self-contained Python runtime (`1.0.0b2`), backend service, and frontend workbench; see [desktop build and verification](app/macos/README.md). Current public Beta candidates (`v1.0.0-beta.2`) target **Apple Silicon (`arm64`) on macOS 14.0+** and use ad-hoc signing (`Senrelay-macOS.zip` contains `Senrelay.app` only).

- **Pricing & License**: **Free (`$0`, [MIT License](LICENSE))**, with no paid tiers, cloud account, or telemetry. You supply your own API keys stored locally in macOS Keychain.
- **When NOT to use Senrelay**:
  1. You use **Windows, Linux, or an Intel (`x86_64`) Mac** — current builds support **macOS 14.0+ Apple Silicon (`arm64`)** only.
  2. You need **multi-device cloud sync, hosted team memory, or remote web access** — Senrelay runs strictly on local loopback (`127.0.0.1:8420`, dev `18420`) on a single Mac.
  3. You expect **cross-host session capture or `/contextcore` relay for the 9 extended config hosts** (`Claude Desktop`, `Gemini CLI`, `Grok Build`, `Kimi Code`, `MiniMax Code`, `Pi`, `Hermes`, `OpenClaw`, `agy`) — those 9 hosts only support previewed configuration management.
  4. You require a **zero-prompt Apple-notarized installer** — Public Beta builds are ad-hoc signed, so first launch requires running `xattr -cr /Applications/Senrelay.app` once.

### Quick install via Homebrew Tap (macOS 14.0+ `arm64`)

```bash
brew install --cask qingsongcui/tap/senrelay
xattr -cr /Applications/Senrelay.app
```

### Manual ZIP install

1. Download `Senrelay-macOS.zip` from [`v1.0.0-beta.2`](https://github.com/qingsongcui/ContextCore/releases/tag/v1.0.0-beta.2), extract it, and move `Senrelay.app` to `/Applications`;
2. Run `xattr -cr /Applications/Senrelay.app` in Terminal (or allow it under **System Settings → Privacy & Security → Open Anyway**; source checkouts also include `app/macos/install-macos.sh`).

> **Package naming note**: The `contextcore` CLI ships inside `Senrelay.app` and this source repository (`qingsongcui/ContextCore`). Do **not** run `pip install contextcore` — the `contextcore` package on PyPI is an unrelated third-party project.

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

- **Companion Auto-Memory + Dual-Tier Working Memory**: Companion MCP tools and Skills capture source-quoted durable memories (`contents` with a brief `💡` receipt) and short-term active task state (`working_tasks` with completed steps, failed attempts, one-off restrictions, and `discussion_only` edit guards via `bootstrap_working_memory`, `upsert_working_task`, and source-backed `promote_task_lesson`), while the workbench supports manual notes and document imports (`MD`, `TXT`, `JSON`, `PDF`, `DOCX`, `DOC`).
- **Six Categories & Tiered Governance**: Organize memories across **Preferences, Background, Constraints, Tasks, Lessons, and References**, pin 📌 core rules to the protected prompt tier, archive obsolete rules as `outdated`, consolidate duplicates, and preview `AGENTS.md` handbooks.
- **Independent Permanent Deletion**: Trace any memory back to its exact dialogue turns or document source and roll back retained versions. Deleting a conversation never deletes extracted memories, and deleting a memory never deletes original conversations.

### 3. Cross-Agent conversation relay

When switching tools (for example after hitting quota or context limits), continue directly inside your receiving Agent without filling out forms in the web UI:

1. Open your target Agent (Claude Code, Codex, Cursor, Antigravity, OMP, or OpenCode) and type `/contextcore` (or say `"continue"` / `"relay"` / `"接着做"` / `"接力"`).
2. Senrelay syncs authorized histories and lists recent cross-host conversation candidates as short, recognizable briefs (`title`, `brief_summary`, source Agent, project or unassigned marker, native session ID, directory, and Git branch/head—keeping distinct same-title sessions and sibling Git worktrees separate without calling a background model).
3. Choose how to proceed:
   - **Reply with just a number** (or provide an exact native/internal session ID): Loads bounded context and `transcript_paging` (`must_not_execute=true`) without a handoff card; if any turns were omitted, the receiving Agent sequentially calls `read_conversation_messages` (`offset=0`, `limit=30`, or `GET /api/conversations/<id>/messages`), compacts the full transcript in context, summarizes progress, constraints, and next steps, and waits before modifying code.
   - **Reply with a number plus a concrete task** (e.g. `1, finish the remaining edge-case unit tests`): Loads the relay context and continues working right away (unless a matched `working_tasks` entry is `discussion_only`).

### 4. Agent Management

- **Top Host Switcher & One-Click Connect**: Select any core or extended host from the top bar; click **One-Click Connect** to configure authorized history paths, MCP tools, `/contextcore` slash-command Skills, and silent hooks in one step.
- **One-Click Switch with Safe Snapshots**: Fill read-only provider presets, run live endpoint latency checks, and isolate API keys in macOS Keychain. Switching providers or toggling MCP / Skills / prompts saves automatic rollback snapshots with readback verification, opening a Diff review modal whenever external manual edits conflict.
- **Multi-Host Sync & Routing Modes**: Sync shared MCP tools, Skills, and prompts across selected hosts (queuing Diff previews if any host has conflicts), and configure per-host Direct, Local Proxy Routing (with ordered fallback chains), or Model Aggregation modes.
- **Skills Safety**: Defaults to independent file copies (`copy`), with managed per-file version symlinks (`symlink`) available in advanced mode; installing a Skill never executes scripts, and generic directory symlinks remain rejected.
- **Independent Permissions**: Reading chat history, modifying host configuration files, and downloading optional local semantic models are confirmed separately and never bundled.

## Common scenarios (FAQ)

- **How do I continue a Claude Code session in Cursor, Codex, or Antigravity without re-explaining context?**
  Run **One-Click Connect** once for each host in `Agent Management`. When switching tools, type `/contextcore` (or `"continue"` / `"relay"` / `"接力"`) in the target Agent to list recent cross-host sessions as short briefs with distinct directory and Git branch/worktree labels. Reply with just a session number (or native session ID) to page and compact the full transcript via `read_conversation_messages` and wait before acting (`must_not_execute=true`), or reply with a number plus your next instruction to continue immediately.
- **How does multi-host MCP, Skills, and prompt sync avoid overwriting manual config edits?**
  Every configuration change records per-entry ownership hashes and saves an automatic rollback snapshot. If a host configuration file was edited externally, Senrelay pauses auto-apply and opens a Diff confirmation dialog rather than overwriting your manual changes.
- **Where are chat transcripts, project memories, and API keys stored?**
  All transcripts, FTS5 indexes, and versioned memories stay on your Mac in a local SQLite database (`0600` file permissions) served strictly over `127.0.0.1`, with online snapshot backups via `contextcore backup`. Managed provider API keys are isolated in macOS Keychain and excluded from configuration exports.
- **How is Senrelay (ContextCore) different from standalone config switchers or cloud memory services?**
  Standalone config switchers manage provider files without project context, while cloud memory plugins require uploading conversations to remote servers. Senrelay combines local-first cross-Agent session relay, turn-cited persistent project memory, and snapshot-guarded multi-host MCP / Skills / provider management in a single macOS runtime.

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

- **Backend suite (`pytest`)**: 1,648 passed, 1 opt-in 100k usage benchmark (`SENRELAY_USAGE_PERF=1`);
- **Frontend workbench (`typecheck`, `lint`, `vitest`, `check:i18n`)**: 205 Vitest tests passed across 11 files, 1,518 Chinese/English UI strings verified with zero unwrapped Chinese;
- **Website checks**: Preview and `SITE_URL` deployment checks across bilingual `index.html`, `guide.html`, and `compare.html` (`check`, `check:brand`, `check:i18n`, `check:interactions`, `check:sphere`);
- **Packaging, Native App & Security**: 29 `scripts/tests` security/version tests, installed non-editable wheel 11-resource verification (`check_release_readiness.py`), native `Senrelay.app` (`arm64`, `minos <= 14.0`, build 2) bundle/archive/lifecycle/WKWebView verification, 6-host isolated hook verification, and full working-tree + Git history security scans (`verify_repository_security.py`).

See [release readiness](docs/release-readiness.md) and [evidence](docs/evidence.md) for detailed implementation mappings, historical reference records, and disclosed external boundaries (private GitHub auth, live account billing/OAuth eligibility, Apple notarization, and clean-Mac physical device acceptance).

## Security

Loopback only (`127.0.0.1`); never expose service ports over LAN, public interfaces, reverse proxies, or tunnels. Browser mutations enforce session-bound CSRF and Same-Site origin checks; Agent MCP/hooks and local model routing use distinct authorized credentials. Raw transcripts retain pasted tokens by design while audit logs and default exports strip managed secrets; application-level permanent deletion is not forensic disk erasure. Read [SECURITY](SECURITY.md) for details.

## License

[MIT](LICENSE)
