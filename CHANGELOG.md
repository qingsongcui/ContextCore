# Changelog

All notable changes to ContextCore will be documented in this file.

## [Unreleased]

- Keep `/contextcore` candidate lists as short recognizable briefs without calling a model; upon concrete selection, avoid handoff-card clutter and return `transcript_paging` so the receiving Agent reads the full transcript sequentially via `read_conversation_messages` (bounded per page to stay within MCP response budgets) and compacts the entire conversation directly in context.
- Preserve native session IDs in conversation APIs, search, and relay candidates; resolve bare native UUIDs without guessing when multiple sessions match.
- Keep distinct same-title sessions and explicitly labeled unassigned sessions visible in newest-first, counted relay pages. Read indexed metadata for discovery instead of decoding all stored transcripts.
- Refresh a selected authorized native source independently of unrelated tasks and history backlogs. Selection-only reads do not create or confirm handoffs; stale, unavailable, or changing sources are reported explicitly.
- Pack long selected conversations against the serialized response budget, preserving the newest user request and timestamped latest-answer excerpts with explicit coverage. Keep protected constraints intact and defer execution confirmation until the payload fits.
- Follow Antigravity's native transcript companions and committed dialogue positions, prioritize recently changed sources, and wake background reconciliation for authorized hidden/generated history directories.
- Stop silently refreshing installed host skills during service startup or local configuration import. Explicit host installation, update, and model-management operations remain available.
- Add dual-tier working memory (`working_tasks` + governed long-term `contents` with `outdated` lifecycle, supersession version guards, Gate-0 admission checks, and `saved`/`provided`/`cited` usage receipts), project merge task migration, and frozen numbered relay selection snapshots while preserving manually created projects across startup and repair.
- Reject expired, missing, deleted, or other-consumer numbered relay selections instead of loading a different conversation. Exact native/internal IDs remain usable without a list.
- Preserve complete safety rules and task restrictions in bootstrap responses; insufficient budgets fail closed without emitting a provided-memory receipt.
- Require same-project source dialogue and a matching, relevant quote before promoting lessons. Agent promotions are labeled source-backed, not independently verified; conflict results do not complete tasks.
- Page active, historical, and combined task lists with exact totals; deep links locate the correct page beyond 200 records, and shrinking lists recover their last valid page.

## [1.0.0-beta.1] - 2026-11-11

### Public Beta (`v1.0.0-beta.1` · macOS 14.0+ `arm64`)

- Unified in-chat `/contextcore` cross-host conversation relay inside the Agent (selection-only loads context and summarizes with `must_not_execute=true`; attaching a task continues immediately) and streamlined the workbench around three primary navigation entries (`projects`, `agents`, `usage`, plus conditional `conflicts`) with global project search and Settings-embedded change log & semantic indexing controls.
- Added whole-database SQLite online backup via `contextcore backup` and `POST /api/settings/backup` (`0600` permissions under `<data_dir>/backups/`).
- Hardened loopback security, cross-port `Same-Site` origin checks, SSRF / DNS-rebinding guards across provider/routing/OAuth/Skill endpoints, Antigravity & Cursor transcript adapters, Codex `app-server` incremental sync, and memory quote/conflict/tombstone governance.
- Added full Chinese/English parity and language switcher (`中 / EN`) to `website/guide.html`, native macOS `macOS 14.0` (`arm64`) `minos` enforcement, atomic `/Applications` installer staging, and multi-conflict host sync previews.

- Keep optional semantic retrieval and source relations disabled until explicit consent; revocation during indexing or search does not commit derived results.
- Carry indexed source quotes through automatic memory writes and reject unquoted or acknowledgement-only claims as user decisions.
- Write supported OMP, Cursor, Antigravity, and Codex Desktop Skills/prompts through preview, confirmation, and readback. Cursor and Antigravity provider changes stay in their own settings.
- Reserve the source service port before opening the database, and refuse a damaged native update before stopping the running service.

- Fixed default source startup to serve the current `dist-staging` build rather than a stale `dist` directory.
- Include all required host evidence, TypeScript extensions, six Skill templates and semantic runtime plans in wheels; missing templates now fail explicitly.
- Align pinned runtime requirements with `pyproject.toml` and `uv.lock`; remediate reported frontend and Python dependency advisories without blanket exceptions.
- Add fail-closed source/history, dependency and artifact gates. Scanner failures and incomplete/shallow history are unknown failures, not successful clean reports.
- Add reproducible candidate workflows and portable source/frontend artifacts; remote CI, production activation and public releases remain separate operations.
- Verify real non-editable wheel installation and the public console entrypoint from outside the source checkout. Fix console PID recognition, unrelated launchd registration isolation, exited-zombie handling and concurrent PID-record preservation.
- Implement managed per-file Skill links alongside copy installation, including versioned targets, previews, transactional ownership, rollback/recovery, mode changes and uninstall. Generic configuration symlink rejection remains intact.
- Carry current constraints and the selected working scene in the six-field handoff with versioned provenance; preserve task ambiguity, exclude parallel/historical contamination and fail explicitly when protected context cannot fit.
- Preserve independent deletion and source invalidation; validate supersession domains and migrate FTS mutation triggers only when availability changes.
- Add a directory-free project creation flow and actual browser acceptance on isolated synthetic data.
- Include project/reference license notices in Python artifacts and collect installed browser runtime license texts with the frontend build.
- Restructured workbench navigation hierarchy: primary navigation for Projects (`projects`), Agent Management (`agents`), and Usage & Performance (`usage`), plus conditional Pending Conflicts (`conflicts`) when conflicts exist, with Settings (`settings`, housing embedded Change Log `audit` and optional semantic search settings), language, and theme controls in the compact footer.
- Aligned plain daily-use consumer contracts across documentation: directory-free project creation (optional directory), companion source-quoted memory capture + manual/document management, and in-Agent `/contextcore` cross-host conversation relay.
- Enforced cross-Agent continuation safeguards: selecting a conversation candidate alone loads context (`must_not_execute=true`), summarizes, and waits for the user's next instruction, while attaching an explicit task continues directly.
- Documented "Agent Management" model: URL-scoped per-assistant and per-section navigation with permanent top host switcher bar and One-Click Connect; provider presets as read-only metadata without entitlement inference; one-click switch/toggle and multi-host sync with automatic rollback snapshots and conflict Diff review; file writes do not prove host process loading; scope switches invalidate open editors, plans, and receipts.
- Added transient in-memory drafts during App session lifetime with continue/discard capability, without refresh persistence or password/credential/import caching.
- Clarified Skills installation capabilities: independent file copy is the default mode (`copy`), while managed per-file version symlink (`symlink`) is an advanced option that is implemented and locally backend-tested (no script execution authorized; generic configuration directory symlinks remain rejected for safety).
- Enforced independent permission boundaries across conversation history reading, host configuration writing, and optional local semantic model/runtime downloads.
- Preserved technical interfaces: advanced JSON schemas, protocol configurations, entity IDs, and execution receipts remain accessible; unmeasured usage counts and costs are labeled as unknown rather than zero.
- Updated developer Node.js requirements to recommended Node 24 LTS (>=24.15.0; supported engines `^22.22.2 || ^24.15.0 || >=26` due to dev-only `jsdom@30.1.3`; CI Node 24), preserved Python 3.12 recommended / min 3.11 with SQLite `blobopen`, and cautioned against routine re-creation of running project virtualenvs.
### Prior implementation and acceptance records

These historical entries are not cumulative current behavior. The public specification and implementation status identify superseded scope and verification limits.

- Activated the continuation-contract and opt-in MCP release (`71ca1283f442e784-8b1a18da9775f041`) on the production `127.0.0.1:8420` LaunchAgent service.
- Completed real end-to-end `confirm_then_continue` verification across all six supported hosts: four verified as `live_host` (`antigravity`, `codex`, `omp`, `opencode`) and two verified as `live_protocol` on production `8420` (`cursor`, `claude-code`, with `claude-code` also verified as `live_host` for `new_capture` and `tool_load`).
- Fixed OMP `--no-tools` distillation overlay configuration (`app/transport.py:_resolve_omp_default_model_from_config`) so disabling `retry.modelFallback` preserves the user's configured `modelRoles` (`default` -> `smol` -> `task` -> `commit`) from `~/.omp/agent/config.yml`, and verified controlled real OMP memory distillation (including atomic decision supersession to `historical` and ambiguous conflict queueing).
- Isolated HTTP/MCP now covers the same confirm-then-continue contract as the in-process gold cases: ambiguous open tasks refuse auto-pick, only the target agent may confirm, assemble_context returns a ready continuation, completed work and historical decisions stay out of resume text, and host hook formatters remain silent.
- Closed the core handoff loop in the server contract: after confirmation, successors receive a continuation cursor (selected task, resume_from, parallel tasks, do-not-redo, active vs historical decisions). Parallel open tasks are no longer auto-selected or mixed into next_steps. Completed tasks stay out of resume targets.
- Memory quality gold cases now cover idea-vs-decision, explicit replacement, cross-project supersede refusal, duplicate capture, and edit/delete retrieval without calling a model. Live auto-distillation remains blocked while compute is off.
- Host support is now two independent evidence tables (context handoff vs config/tools). Claude Code and OpenCode cannot inherit another host’s live pass. Capability copy matches silent hooks: context is opt-in via MCP/skill.
- OpenCode’s plugin no longer inserts synthetic chat parts. MCP instructions and the six host skills describe the same trigger: the user asks to continue; hooks stay silent.

- Simplified the website context sphere: six stable host labels, a restrained rotating point cloud, sequential relay traces, and a correctly sized central brand tile.

- Refined the workbench with macOS-inspired design tokens, translucent materials, inset grouped lists, sheet dialogs, and animated segmented controls.
- Added a responsive 3D shared-context sphere to the website, with animated positions for all six supported hosts and reduced-motion support.
- Split website CSS into numbered source modules and bundle them into one stylesheet at build time.

- Background sync now computes a stat-only fingerprint for each host source set and skips unchanged histories. Manual sync still forces a full pass, so idle CPU no longer scales with total history size.
- Added Claude Code and OpenCode as supported hosts: read-only history adapters (Claude project JSONL with parentUuid leaf chains; OpenCode SQLite with legacy JSON fallback), MCP templates, Claude hooks and the OpenCode plugin, installer merge/ownership rules, and per-host capability reporting. Compute remains OMP-only; real-host acceptance is still pending.
- Updated the workbench and website to a six-tool story. The website lists both new hosts, and its four-node hero is explicitly labeled as showing only some tools.
- Added a persisted Chinese/English switch to the workbench and the website. Product copy is keyed by its Chinese source with 512 English strings; the website ships 178 English strings plus translated metadata. Missing English falls back to Chinese and fails the new `check:i18n` guard; the switch itself is covered by runtime tests in both surfaces.
- Rewrote user-facing Chinese copy across the workbench and marketing website: removed leaked engineering terms (host, compute attribution, observable metrics, metadata, synthetic), split stacked disclaimers into single sentences, and unified the six memory category names. Copy-only change; APIs, authorization boundaries, data paths and stored values are unchanged.
- Replaced the desktop shell, Skill foundry, per-item approval inbox, and backup flow with a browser workspace plus macOS local service.
- Shared project memory is written directly by authorized Agents. Only ambiguous conflicts require review.
- Cross-agent handoff requires a six-part readback and explicit confirmation.
- Conversations and memories delete independently and permanently.
- Local history readers now handle Codex rollout files, Cursor SQLite composer messages, OMP JSONL, and Antigravity brain transcripts; unknown formats fail explicitly. Unsafe compute paths remain blocked.
- Earlier unreleased notes about Skill drafts, emergence reports, and the 12-tool approval surface are superseded and are not current behavior.
- Conversation ingestion and distillation queueing share a transaction; same-session OMP hooks preserve incremental turns.
- Conflict resolution, content tombstone checks, and copied-handoff invalidation are transactional. MCP edits require current versions and use authenticated host identity.
- Setup preview is read-only; writing and removing host configuration require separate explicit confirmation. Date-only end filters include the selected day.
- OMP distillation parses completed assistant messages from the actual JSONL event stream; malformed output no longer becomes an empty successful result. Unknown usage remains unknown. Temporary execution settings disable implicit retry/model fallback.
- Hook requests now return project memory and cross-agent continuation context, including six-field cards awaiting explicit confirmation; paused collection retains shared-context access and empty lifecycle hooks do not create conversations.
- Distillation receives indexed conversation messages and current decision versions. Explicit user-evidenced replacements preserve historical decisions; ambiguous, stale or unsupported replacements become conflicts instead of silently retiring records.
- Project reconstruction no longer treats conversation titles as project identities; repeated explicit metadata reuses the project. Project merge respects permanent-deletion tombstones.
- Added separate reusable-compute consent and host selection. Cursor/Codex/Antigravity sources can use an explicitly authorized OMP compute connection; missing/revoked consent keeps work pending, preserves source provenance and meters the actual compute host.
- Preserved source dialogue chronology across import and processing. Codex replayed parent headers no longer overwrite owning thread identity, and Cursor threads sharing a SQLite file remain separate.
- Valid Antigravity companion transcripts recover malformed primary logs; genuinely damaged source sessions remain explicit partial-sync failures while valid sessions import.
- Handoff cards snapshot source revisions and reject stale confirmations/reads, honor the explicitly selected task, exclude successor self-turns from predecessor evidence, and audit the authenticated confirming Agent.

## 1.0.0-alpha

- Initial local-first ContextCore app:
  - FastAPI + SQLite backend.
  - React/Vite desktop UI.
  - Tauri v2 shell with backend sidecar.
  - Streamable HTTP MCP and stdio MCP entrypoints.
  - Approval inbox, audit log, backup/restore, permissions, Skills, and optional semantic search.

[Unreleased]: https://github.com/qingsongcui/ContextCore/compare/v1.0.0-beta.1...HEAD
[1.0.0-beta.1]: https://github.com/qingsongcui/ContextCore/releases/tag/v1.0.0-beta.1
