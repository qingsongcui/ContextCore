# ContextCore product specification

**Public requirements baseline · updated 2026-10-25.** This is a sanitized derivative of confirmed product decisions, not the private interview or a certificate of implementation. Direct user decisions take precedence over this document. Earlier draft PRD documents are superseded. Implementation locations, remaining gaps and acceptance evidence are separated in [release readiness](release-readiness.md) and [evidence](evidence.md).

## 1. Purpose and delivery boundary

ContextCore (public brand: **Senrelay · 续境**) is a local multi-Agent context, knowledge and long-term memory system. Its central scenario is switching to another Agent when the predecessor runs out of quota, then continuing the same task without asking the predecessor to summarize or repeatedly supplying the background (U001–U007, D007). This is an acceptance scenario, **not** a measured zero-loss guarantee.

The product is a browser workbench plus a macOS background service. On 2026-10-25 the user additionally requested a native macOS desktop App, superseding the no-native-shell portion of D045–D047; see `app/macos/README.md` for implementation and distribution limits. Closing the web page must not stop collection; an explicit service stop does. Autostart is desired by default and can be disabled, but installation and registration require the user's authorization. Important notices stay inside the web interface. The delivery includes the full confirmed scope (D050, D062), including optional semantic retrieval and source-backed relationships in the first release; optional means disabled until consent, not deferred implementation.

Success is assessed in order: successful handoff, correct memory, time saved (D049). There is no fabricated numerical zero-loss target, fixed delivery deadline or external paid-customer proof (D051–D053). Self-use, similar heavy users and commercialization are successive audiences (D048).

## 2. Cross-Agent continuation

- User-facing cross-Agent continuation happens inside the target Agent conversation (D003, D004, D005, D063):
  - **In-chat `/contextcore` conversation relay** (or saying “接着做” / “接力” / “continue” / “relay”, or invoking a companion Skill): Discovers and lists recent cross-Agent conversation candidates as short recognizable briefs (`title`, `brief_summary`, native session ID, directory, and `git_branch` / `git_head`, keeping sibling Git worktrees and distinct same-title sessions separate rather than merging or guessing when multiple plausible sessions exist).
    - **(a) Candidate selection only**: Replying with only a candidate number/session (or exact native/internal session ID) loads the relay context with `must_not_execute=true`. Candidate lists stay short briefs used only to recognize the right session and do not call a model. After a concrete selection, the service returns the conversation's bounded context (`context.mode == "full_transcript_relay"`) and `transcript_paging` without calling a background model or presenting a six-element summary/handoff card; when any turns are omitted, the receiving Agent sequentially pages `read_conversation_messages` / `GET /api/conversations/{conversation_id}/messages` from `offset=0` until `next_offset` is null to read and compact the entire conversation in its own context, then reports the compacted context and waits for the user's next instruction before modifying code.
    - **(b) Candidate selection with a concrete task instruction**: Attaching a concrete follow-up task alongside the `/contextcore` relay selection authorizes continuing directly using the loaded context and bound worktree directory (unless a matched `working_tasks` record is `discussion_only`).
  - **Underlying structured handoff contract (`hnd_...`)**: At the backend/MCP protocol layer (`start_handoff`, `read_handoff`, `confirm_handoff`, `assemble_context`), D005 defines six elements: **goal, progress, constraints, working scene, blockers, next steps** (目标、进度、约束、现场、阻塞、下一步). User-facing `/contextcore` conversation relay (`relay_conversation`) does not emit a six-element summary card; the web workbench focuses on browsing conversations, memories, working tasks, and materials rather than preparing standalone handoff cards, and **never confirms continuation on behalf of the target Agent**.
- Once confirmed or explicitly instructed during conversation relay, the target Agent follows `continuation.resume_from`, does not redo completed work, does not switch to unselected parallel tasks and excludes historical superseded decisions.
- Current scope is **cross-Agent** switching. Same-Agent new-session recovery is not added by the unified-management goal (D006).
- Quota exhaustion must not require a new summary from the unavailable predecessor (D007).
- Lifecycle hooks only collect/report events. They must not inject memory, warnings or cards, rewrite input or synthesize chat messages without user authorization (D063, superseding the earlier hook auto-injection behavior). Under the dual-tier working-memory closed loop (D064), Agents that have ContextCore MCP tools or companion Skills installed may call `bootstrap_working_memory` on substantive project work while keeping hooks silent and strictly honoring `must_not_execute` (`discussion_only` tasks, unresolved task ambiguity, and unconfirmed handoffs never authorize code edits).

**Current API vocabulary, not a replacement decision:** `handoff_service.py` exposes `goal`, `progress`, `decisions`, `evidence`, `blockers`, `next_steps`. Those field names alone do not prove D005's constraints and working-scene coverage. That semantic coverage and actual host continuation must be verified separately. Creating a card or confirming server state is not task execution.

## 3. Knowledge, projects and memory

- Ordinary memories accumulate and become effective automatically under the authorized collection/compute policy, without daily per-item approval (D001). Knowledge is shared across all projects and all authorized Agents (D002, D041); project filters are not permission walls.
- Readable existing history and subsequent new conversations are both in scope (D008). Keep raw records and extracted memory separately; memory supports everyday use, raw evidence supports checking (D009). Retain until explicit deletion, without scheduled automatic cleanup (D010).
- Deleting a conversation must not delete its existing memories; deleting a memory must not delete its conversation (D011). Deletion is permanent, with no recycle bin (D032). Imports, stale cards, derived indexes, in-flight extraction and version restore must not resurrect the deleted entity. Independent surviving memories may identify their source as deleted. This is application-level deletion, not a claim of forensic disk erasure.
- Automatically form the six memory classes **habits, background, constraints, todos, lessons and reference material** (D012). Engineering storage labels are not new user decisions.
- Web editing and Agent conversation maintain the same memories (D013). Explicit changes supersede the old decision and retain it as history; unresolved ambiguity requires user resolution (D014). Preserve content, time and actor for versions and allow rollback of retained items (D015), without reviving permanently deleted items.
- The default view is a project workbench (D016) with **overview, chats, memories (including tasks), materials** (D018). The primary navigation consists of three fixed entries—**Projects** (`projects`), **Agent management** (`agents`), and **Usage and performance** (`usage`)—plus conditional **Needs your decision** (`conflicts`) shown when unresolved conflicts exist. **Settings** (`settings`, which houses the embedded **Change log** `audit` and optional semantic indexing controls), language, and theme controls remain in the footer. A project is a conceptual work topic with multiple directories or **no directory at all** (optional/directory-free project, D019). Reconstruct historical projects from identifiers and content rather than requiring manual re-entry (D020).
- Accept manual text and MD/TXT/JSON/PDF/Word imports, not a crawler or OCR service (D017). Web search defaults to the current project with an all-project switch; Agent retrieval remains globally shared (D021).
- Support project rename, directory changes, merge, split and searchable archive (D022); complete memory detail/provenance/links and keyword/combined filters (D023); batch delete, retag and deliberate item merge (D031).
- Ambiguous conflicts appear in both Agent dialogue and the web pending list, with synchronized resolution (D033). Mutation audits cover writes, changes, deletes and configuration, not a large read log (D039).

## 4. Hosts, compute and security

The six context hosts are **Codex (Desktop and CLI), Oh My Pi (OMP), Cursor, Google Antigravity, Claude Code and OpenCode** (D059, expanding D025). OMP is not Pi or OpenCode. History formats, new capture, MCP loading, memory read/write, protocol readback / full-transcript relay (`read_conversation_messages`), and confirm-then-continue require separate per-host and per-surface evidence. Discovery, configuration present, protocol reachable and actual host consumption are different states (D026, D029, D053).

D037 prefers reusing the active connected Agent rather than requiring an independent model API: in normal use, the connected Agent captures memories in real time via companion MCP tools and Skills during conversation, while underlying backend distillation endpoints remain disabled by default and require separate explicit authorization. Do not infer model-call rights from a provider/model listing, and no silent external-provider fallback is permitted. D038 imposes no extra financial cap; show observable usage and mark missing usage/cost unknown, not zero. Context token budgets and queue limits are not financial caps. Separate permission boundaries are strictly maintained: reading past/new conversations, modifying host configuration files, and downloading optional semantic models/runtimes require distinct authorizations and are never bundled.

Authorized Agents have uniform shared-memory access (D041). Raw transcript credentials are retained as written, with explicit disclosure of storage and possible model-provider disclosure risks (D042). This does **not** authorize copying secrets into audit logs, public artifacts, management exports or new credential stores. The macOS user account is the primary boundary; no separate app lock is required (D043). The implementation uses loopback networking, browser session/CSRF checks, Agent credentials and independent routing credentials; those are engineering controls, not claims that the source decisions specified every mechanism. See [security policy](../SECURITY.md).

## 5. Model and tool management (D060, D061)

The confirmed management scope covers multi-host configuration and resource management while retaining the browser/macOS topology. It includes:

| Category | Required capability |
|---|---|
| Providers and models | Presets/custom APIs, endpoints and credentials, protocol/model mapping, ordered/copyable definitions, host selection/coexistence, model catalogs and checks. Distinguish reachability, authentication and potentially paid inference checks. |
| Routing and failover | Opt-in host direct/routed modes, streaming, explicit model mapping/protocol differences, authorized fallback/aggregation/circuit state and recovery to a usable direct configuration. Do not discard unsupported tools, images or reasoning semantics silently. |
| Usage and performance | Traceable requests and attempts, input/output/cache semantics, prices/estimates/unknowns, quota/balance where observable, filters/trends, TTFT, total time, output speed and sample coverage. Log and router observations must not double-count a request. |
| MCP | Discover/import/create/edit third-party servers; per-host enable/disable/sync/remove owned configuration; connection and tools discovery separate from host loading. |
| Skills | Local/GitHub/ZIP/registry sources, discovery/install/distribution, **independent copy mode (default) and managed version symlink mode (advanced, implemented and locally backend-tested)**, versions/updates/uninstall and original-file recovery. Installing a Skill never authorizes running its scripts. Generic directory symlinks remain rejected for safety. |
| Prompts | Import/edit/version/diff and per-host activation with conflict checks, preserving unrelated rules and the handoff confirmation gate. |
| Accounts | Multiple accounts, legitimate official authorization, state/refresh/logout, provider binding and observable quota. API keys and OAuth are different. Third-party client identities or private login caches cannot substitute for authorized official flows. |
| Project bundles | Project-linked model/MCP/Skills/prompt combinations with whole-plan preview, switching and recovery. Applying a bundle does not authorize executing the project or confirming a handoff. |
| Sessions and applications | Existing session browsing/search, host-specific recovery commands where verified, version/path/capability discovery, and separately confirmed application install/upgrade/reload. Same-Agent native recovery does not become D006 cross-Agent acceptance. |
| Migration and export | Selected-source compatible local configuration preflight, provider/MCP/Skill/prompt import/export, compatible file/link sharing, explicit credential selection, unsupported-field reporting. Share exports omit secrets by default. |

Configuration changes create automatic rollback snapshots before writing host files, preserve unknown/external fields and originals, and surface diff confirmation when external file conflicts occur or when explicitly previewing a plan. Multi-file filesystem and database updates are not an absolute cross-resource atomic transaction: partial failure, recovery and host reload/request status must be explicit.

### Agent management model

- **Permanent top host switcher with App-controlled URL**: Navigation targets an individual assistant via a permanent top host switcher bar and App-controlled URL parameters (`page=agents`, `agent=<id>`, `section=<tab>`), plus **One-Click Connect** (`一键接入续境`) for context hosts. Configuration changes never default to all hosts.
- **Dedicated forms**: Clear categories for models & accounts (`providers`), MCP tools (`mcp`), Skills (`skill`), instructions/prompts (`prompt`), account authorization (`accounts`), project bundles (`bundles`), change plans (`plans`), connection permissions (`connections`), and advanced migration (`advanced`).
- **Read-only presets**: Known provider presets are metadata templates only; catalog presence does not assume model access or account entitlements.
- **Save definition vs. Apply**: Saving a shared definition stores it in the local shared library without modifying host configuration files. Applying or toggling configuration for a selected assistant creates automatic rollback snapshots, writes and reads back host files immediately when clean, and surfaces a diff confirmation modal on external file conflicts or explicit plan preview. Writing a configuration file to disk does not prove that the host process has loaded it.
- **Scope switch invalidation**: Switching between assistants or sections immediately clears open editors, unapplied plans, and execution receipts to prevent cross-host contamination.
- **Transient drafts**: Non-secret edits during the App session are held in memory and can be continued or discarded. Drafts are not persisted across browser refresh, and passwords, API keys, and raw imports are never cached.
- **Skills installation**: Defaults to an independent copy (`copy`). Managed version symlinks (`symlink`) are an advanced option that is implemented and locally backend-tested (not an unresolved feature); installing a Skill never executes scripts. Generic directory symlinks remain rejected for safety.
- **Connection modes and advanced operations**: Direct, local routing and model aggregation use readable per-assistant forms. The independent-client routing API and migration remain in an explicit advanced scope. Provider catalog refresh is manual and does not trigger inference; Gemini catalog/checks are unsupported manual entries with previewed host compatibility. Advanced JSON schemas, protocols, IDs, and receipts are preserved for technical users.
- **Usage and cost accuracy**: Unknown usage counts and costs are labeled as unknown, never reported as zero.

Maintain **two matrices**: six-host context support and broader configuration/tool management support. D060 also tracks wider multi-host configuration differences including Claude Desktop, Gemini CLI, Grok Build, OpenClaw, Hermes, Pi, MiniMax Code and Kimi Code, with Antigravity CLI (`agy`) distinct from desktop. Config-format support must not inherit context support, and only supporting the original six cannot establish full multi-host configuration parity. Platform/companion differences (Windows/Linux, tray, cloud sync, cloud backup) remain disclosed, not silently claimed equivalent.
## 6. Unified context and optional first-release enhancements (D061, D062)

Unify traceable lexical retrieval, evidence paging and version checks, relevance/context pack assembly, protected token zones, compression/checkpoints and recovery for content ContextCore controls. Unknown total host context usage must remain unknown. Active tasks, user authorization, pending actions, tool pairings and exact critical parameters must not be replaced by an unsafe summary. An insufficient protected budget must be explicit.

Optional local semantic retrieval and source-backed relations are **included in the first-release scope**. They default to off: no automatic dependency installation, model download, inference or relationship extraction. Plans disclose dependencies and downloads before consent. The initial approved model is a pinned `intfloat/multilingual-e5-small`; candidates cannot automatically become user decisions. Derived results must recheck current canonical authorization, versions and tombstones. SQLite relationships in the current source use `retrieval_relations`, not `entity_relations`.

The present implementation uses React/Vite, native CSS tokens, FastAPI/SQLite/FTS5 and an isolated offline semantic worker with pinned resources. These are implementation choices, not additional user requirements or evidence of installed model quality.

## 7. Design and exclusions

D054 authorizes a complete UI redesign; D055 requires a clean, spacious knowledge workbench with light/dark/system themes and a complete design specification. D056 provides black/white and particle references, not a 1:1 copy. D057 requires fluid, Apple-native-like interaction; exact colors, font sizes, damping, viewport targets and frame-rate thresholds are engineering choices subject to visible validation, not invented interview decisions. **D058's frontend-model exclusivity was revoked on 2026-10-25.**

Excluded: Skill foundry/generation and proactive insights (D036), and automated cloud/snapshot restore orchestration (D040; local whole-database SQLite online backup is supported via `contextcore backup` and `POST /api/settings/backup`, while restoring a backup remains manual file replacement with the service stopped), whereas the former native-shell exclusion (D045) was superseded by the 2026-10-25 desktop App request. Safe configuration originals/rollback remain separate from whole-database backups. Do not expand “backup excluded” into removing approved migration/export. Full management scope remains required; currently unsupported items stay visible in readiness.

## 8. Exact decision index

These concise paraphrases preserve identifiers without publishing private interview quotations, personal paths or raw history. They describe requirements, not implementation acceptance.

| ID | Confirmed meaning |
|---|---|
| U001 | Local multi-Agent context relay without repeatedly supplying context. |
| U002 | User concern about old product positioning; not a performance metric. |
| U003 | Shared context, knowledge and memory across local Agents. |
| U004 | Same-project quota exhaustion and simple-command switch. |
| U005 | Memory formed from multiple projects' Agent conversations, visualized. |
| U006 | Human/Agent co-maintained knowledge, memory and context; manual addition. |
| U007 | Long-term user/project understanding across local Agents. |
| D001 | Automatic memory accumulation and effectiveness, no daily item approval. |
| D002 | All-project knowledge/memory shared with all connected Agents. |
| D003 | Simple receiving-Agent continuation command locates recent task. |
| D004 | Read back and wait for user confirmation before code execution. |
| D005 | Six elements: goal, progress, constraints, scene, blockers, next steps. |
| D006 | Cross-Agent switching; same-Agent new session outside this handoff scope. |
| D007 | Exhausted predecessor, no predecessor summary or repeated background. |
| D008 | Readable history and newly added conversations collected. |
| D009 | Raw records plus extracted memories, independently preserved. |
| D010 | Retain until user deletes; no timed automatic purge. |
| D011 | Conversation/memory deletion independent, never cascading. |
| D012 | Habits, background, constraints, todos, lessons, materials. |
| D013 | Web and Agent conversation maintain the same memory. |
| D014 | Explicit superseding updates; ambiguous conflicts ask user. |
| D015 | Content/time/actor version history and rollback. |
| D016 | Project workbench primary view. |
| D017 | Manual text and MD/TXT/JSON/PDF/Word; no crawler/OCR. |
| D018 | Overview, progress, chats, memories, materials. |
| D019 | Conceptual work topic; multiple/no directories permitted. |
| D020 | Automatic reconstruction of historical projects from records. |
| D021 | Web current-project default/all-project option; Agent globally shared. |
| D022 | Rename, directory changes, merge/split, searchable archive. |
| D023 | Complete memory detail/links, keyword and combined filters. |
| D024 | M01 module closure; not a new audit or schema requirement. |
| D025 | Initially named Codex/OMP/Cursor/Antigravity; expanded by D059. |
| D026 | Discover installed Agents, integrate after user confirmation. |
| D027 | Background continues after window closes; explicit exit and visible state. |
| D028 | Earlier automatic-use preference, dialogue/key-summary capture, natural/short command, Codex Desktop. Automatic injection superseded by D063. |
| D029 | Multidimensional connection status; disconnect stops sync, retains data. |
| D030 | M02 module closure; not a new protocol claim. |
| D031 | Batch delete, retag and deliberate item merge. |
| D032 | Permanent delete/no trash; no mandated forensic method. |
| D033 | Ambiguous conflicts synchronized between Agent and web. |
| D034 | M03 module closure. |
| D035 | Superseded historical decision, not active authority. |
| D036 | Remove Skill foundry and proactive insights; remain material base. |
| D037 | Prefer connected Agent compute; independent API not mandatory. |
| D038 | No extra call cap; observable usage shown, unobservable cost unknown. |
| D039 | Mutation/configuration audit, not a large read log. |
| D040 | No automated cloud/snapshot restore product this round (local SQLite online backup is supported; restore remains manual file replacement). |
| D041 | Flat shared-memory access for authorized Agents. |
| D042 | Preserve credentials in raw records and disclose risk. |
| D043 | OS account protection; no independent app lock. |
| D044 | M05 module closure. |
| D045 | Browser product, no Tauri desktop client. |
| D046 | Browser UI plus macOS communication/storage daemon. |
| D047 | macOS first; default autostart can be disabled; web-only notices. |
| D048 | Self-use → similar heavy users → commercialization. |
| D049 | Handoff success > memory correctness > time saved. |
| D050 | Full confirmed PRD delivery, not reduced scope. |
| D051 | Acceptance milestones, no invented hard date. |
| D052 | No external paid evidence yet. |
| D053 | Failure assumptions, guardrails and explicit unknowns. |
| D054 | Complete UI/design reconstruction. |
| D055 | Clean/spacious workbench; dual/system theme; complete design spec. |
| D056 | Black/white and dynamic particle reference, not literal copying. |
| D057 | Fluid interaction; motion acceptance still needs evidence. |
| D058 | Frontend model exclusivity revoked 2026-10-25. |
| D059 | Six context hosts, adding Claude Code and OpenCode. |
| D060 | Full model/API/account/usage/routing/MCP/Skills/prompt management. |
| D061 | Unified local multi-Agent management and low-friction context experience. |
| D062 | Approved full implementation, first-release optional semantics/relations. |
| D063 | Explicit opt-in context; silent lifecycle hooks, no forced injection. |
| D064 | Dual-tier memory (durable long-term `contents` + short-term `working_tasks` with `discussion_only` fail-closed gate, honest `saved`/`provided`/`cited` receipts, and MCP `bootstrap_working_memory` without hook prompt injection). |

## 9. Acceptance discipline

Verify history, new capture, shared-memory reads/writes, readback/wait and actual confirmed continuation per host/surface. Verify D007 under predecessor exhaustion, memory correctness/superseding/deletion under source changes, management operations with external-field preservation, and web/service lifecycle independently. An installed configuration, a parser, bundled reference grades or an old passing suite does not prove a fresh installation works.

Automated release gates verify backend (`pytest`), frontend (`typecheck`, `lint`, `check:i18n` across 1,518 keys, and `vitest` across 205 tests), website static/brand/i18n/interaction/sphere checks (`check`, `check:brand`, `check:i18n`, `check:interactions`, `check:sphere`), `scripts/tests`, installed-wheel resource completeness, native macOS bundle/hook/archive/lifecycle/WKWebView verification, and repository security scanning. Managed version symlinks for Skills are implemented and locally backend-tested (copy default, symlink advanced). External account eligibility/billing, official OAuth qualification, live Keychain/reload behavior, authorized downloads/inference, and clean-Mac reboot/autostart evidence remain distinct prerequisites. Neither a missing authorization nor a code gap may be disguised as success or silently removed from scope.
