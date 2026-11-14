# Public evidence register

Source package version: `1.0.0-beta.2` (Python package `1.0.0b2`, native macOS short version `1.0.0` plus numeric build number). Surface limits are documented in [support-matrix](support-matrix.md). Requirements: [specification](SPECIFICATION.md). Delivery boundaries: [release readiness](release-readiness.md) and [known issues](known-issues.md).

The sections below summarize **verified automated release gates** and **sanitized historical reference records** without publishing private transcripts, credentials, or raw machine dumps.

## Current automated verification gates

| Verification gate | Scope & result | Boundary |
|---|---|---|
| Backend suite (`pytest`) | 1,648 passed, 1 opt-in 100k benchmark (`SENRELAY_USAGE_PERF=1`) | Isolated synthetic fixtures and protocol/adapter tests (including dual-tier `working_tasks`, `read_conversation_messages` transcript paging, and Git worktree disambiguation); does not replace live external provider billing or OAuth sessions. |
| Frontend workbench (`vitest`, `typecheck`, `lint`, `check:i18n`) | 205 tests passed (11 files), 0 type/lint errors, 1,518 Chinese/English keys verified | JSDOM/component and build checks; complements live browser inspection. |
| Website (`build`, `check`, `check:i18n`, `check:brand`, `check:interactions`, `check:sphere`) | Static/SEO checks, bilingual `index.html`, `guide.html`, and `compare.html`, interaction tests, and sphere tests | Automated DOM and static bundle verification across preview and `SITE_URL` modes. |
| Packaging & release readiness (`check_release_readiness.py`, `verify_bundle.py`, `verify_archive.py`, `verify_hook_distribution.py`, `verify_data_lifecycle.py`, `verify_native.py`) | Installed non-editable wheel 11-resource check, default static serving, `Senrelay.app` / `Senrelay-macOS.zip` (`arm64`, `minos <= 14.0`, build 2), 6-host isolated hook execution, restart persistence & deletion safety, bilingual WKWebView native recovery checks | Isolated local verification on Apple Silicon; does not replace clean-Mac Gatekeeper/notarization acceptance. |
| Repository security (`scripts/tests` + `verify_repository_security.py`) | 29 security/version unit tests + tracked source tree and Git object history scanned with 0 credential/path leaks and 0 dependency advisories | Automated static/history gate, not a third-party security audit. |

## Provenance and limits

- The bundled [host reference JSON](../app/backend/app/integrations/host_capability_evidence.json) contains host versions, cell grades and testbed identifiers. It is reference acceptance, **not verification of the reader's local installation**. Runtime discovery and local observations must establish that separately.
- Historical implementation checkpoints referenced filenames (`summary.json`, screenshots, traces, release receipts) that are intentionally excluded from the public repository to protect local paths and session privacy.
- Private raw interviews, user histories, credentials, recovery snapshots and host-config dumps remain outside public artifacts. Decision paraphrases in the specification do not create independent authority.
- `code_present`, `synthetic_tested`, `live_protocol`, `live_host`, unsupported and unverified describe different evidence levels, not interchangeable passes. A live protocol request can succeed without the named Agent executing a full interactive task, and a model distillation run is distinct from a cross-Agent handoff.

## Recorded host continuation

The [readiness matrix](release-readiness.md#reference-host-matrix-not-local-install-acceptance) reproduces the bundled per-cell grades. The recorded confirmed-continuation results are:

| Host/surface | Grade | Handoff identifier | Limitation |
|---|---|---|---|
| Codex CLI 0.158.0 | live_host | `hnd_99b1c7457870` | CLI result, not all Desktop behavior. |
| OMP CLI 18.10.4 | live_host | `hnd_4d79d7f8af27` | Not every OMP surface/version. |
| Antigravity Desktop | live_host | `hnd_325c33198431` | Does not establish agy CLI equivalence. |
| OpenCode 1.18.35 | live_host | `hnd_6697a2e18540` | Version-bound synthetic task. |
| Cursor Desktop | live_protocol | `hnd_7c3754ee0469` | Real readback recorded, but confirmed continuation used protocol; CLI billing prerequisite. |
| Claude Code 2.1.284 | live_protocol | `hnd_86dff0436650` | Hooks/MCP connectivity recorded; model completion returned provider 402. |

These used controlled synthetic acceptance tasks. Four live-host loops plus two protocol loops are **not six live-host passes**, all-host memory-write proof, D007 exhaustion coverage, or long-term memory-quality proof. All six `memory_write` cells are `synthetic_tested` with live invocation unverified. All history-reader cells are `live_protocol`; Cursor capture specifically does not claim hook consumption.

## Three real OMP distillations from synthetic inputs

The latest timeline reports three explicitly authorized real OMP model executions against **synthetic conversations**, not three mock model runs and not three private-history quality studies:

| Distillation / usage event | Reported tokens | Reported output |
|---|---:|---|
| `dist_d5fa53fe54f6` / `use_026a87b25039` | 6,570 | Four active memories; one ambiguous conflict routed for review. |
| `dist_278cbca12708` / `use_97a66f91318b` | 6,319 | Three active memories. |
| `dist_909c5dca5696` / `use_c3eafe1e2150` | 4,813 | Two active memories; explicit superseding retired an old decision. |

The record notes that compute was restored to `compute_authorized=false` afterward. Neither earlier failed experiments nor these bounded synthetic runs prove unconstrained future compute quality on arbitrary private histories; background compute remains opt-in and disabled by default.

## Remaining external prerequisites

Real interactive OAuth/Keychain lifecycles, external MCP/registry/private GitHub behavior, optional local semantic runtime/model downloads and retrieval quality, live host process reload/model activation, all-host live memory writes, 24-hour clean-device soak runs, and Mac sleep/reboot autostart behavior remain separate external or physical-device prerequisites. Refer to [release readiness](release-readiness.md) for category-specific boundaries.
