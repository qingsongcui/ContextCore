# Coding Agent guide

## Product and sources

The active ContextCore product is in `app/`: `backend/` (FastAPI/SQLite, REST/MCP, tests), `frontend/` (React/Vite workbench), `scripts/` (explicit service/release operations).

Read [public requirements](docs/SPECIFICATION.md), [release gaps](docs/release-readiness.md), [evidence limits](docs/evidence.md), [app operations](app/README.md) and [contributor commands](CONTRIBUTING.md). Public docs derive from user decisions; direct user statements and current source outrank prior summaries. Preserve private raw interview/history outside public artifacts.

## Confirmed boundaries

- Browser workbench plus macOS local daemon. The user explicitly requested a native macOS App on 2026-10-25, superseding the no-desktop-shell portion of D045–D047; `app/macos/` contains the AppKit/WKWebView bundle. Closed page does not stop service; desired autostart does not prove installation.
- Ordinary memories automatically accumulate/become effective under authorization (D001); all projects/authorized Agents share memory (D002, D041). No per-item routine approval and no project permission wall.
- Explicit cross-Agent continuation (D003, D006, D063): triggered inside the receiving Agent via `/contextcore` (or `"接着做"` / `"接力"` / `"continue"` / `"relay"`): (a) selecting a conversation relay candidate alone loads context (`must_not_execute=true`), reports a summary, and waits for the next instruction; (b) attaching a concrete task instruction alongside candidate selection continues directly. Backend handoff card protocols (`hnd_...`) remain supported at the API/MCP layer for backward compatibility, while user-facing continuation stays 100% inside the Agent (no Web UI handoff button/dialog). Primary left navigation has 3 fixed items (`projects`, `agents`, `usage`, plus conditional `conflicts`), with no standalone `retrieval` page or standalone background-distillation Settings panel. D007 requires quota-exhausted predecessor without predecessor summary/repeated background.
- D005's six requirements are goal/progress/**constraints/scene**/blockers/next steps. Current fields use decisions/evidence; do not pretend field presence proves semantic coverage.
- Hooks stay silent: no unsolicited memory/cards/warnings, input rewrite or synthetic context messages (D063 supersedes old automatic injection).
- Raw records and memories are distinct (D009). Conversation/memory deletion is independent (D011), permanent/no trash (D032), and must not resurrect via imports, old cards, versions, jobs or indexes. No forensic-erasure claim.
- Explicit superseding retires old decisions; ambiguity asks the user (D014, D033); retained-item versions support rollback (D015). Mutation/config audit is D039, not D024.
- Six context hosts (D059): Codex Desktop/CLI, OMP, Cursor, Antigravity, Claude Code, OpenCode. Separate surfaces and history/capture/read/write/readback/continuation cells. OMP ≠ Pi/OpenCode. Bundled reference acceptance **never** verifies a fresh local installation.
- Full first-release scope (D050, D062) includes optional local semantics and source-backed SQLite relations (`retrieval_relations`), disabled until consent, not deferred. No automatic download/install/extraction from searching.
- D060/D061 include full provider/model/account/usage/routing/MCP/Skills/prompt/project-bundle/migration/export management and wider-host differences. Copy-only Skills implementation does not remove required symlink mode. Unsupported operations stay explicit; configuration format ≠ actual loading.
- Compute prefers connected Agents (D037), separately consented and disabled by default. Do not silently fall back to another provider; unknown usage/cost remains unknown (D038). Listing a model does not prove account eligibility.
- Raw transcript secrets retained by design (D042); do not copy them into public artifacts, audit or exports. OS-user protection is D043; flat Agent access is D041.
- Skill foundry/proactive insights excluded by D036; automated cloud/snapshot restore orchestration excluded by D040, while whole-database SQLite online backup (`contextcore backup` / `POST /api/settings/backup`) is supported locally and snapshot restore remains manual file replacement while the service is stopped. Configuration safety rollback remains separate from whole-database backups. No arbitrary scope reduction.
- D054–D057 describe design direction/quality, not mandatory exact hex colors or fabricated FPS. D058 frontend model exclusivity **revoked 2026-10-25**.

## Commands and authorization

All commands in [CONTRIBUTING](CONTRIBUTING.md) start at the repository root. Canonical setup:

```bash
python3.12 -m venv app/backend/.venv
app/backend/.venv/bin/python -m pip install -e 'app/backend[test]'
npm --prefix app/frontend ci --ignore-scripts
```

Canonical backend check: `app/backend/.venv/bin/python -m pytest app/backend/tests -q`. Frontend scripts: `typecheck`, `lint`, aggregate `test`, `check:i18n`, `build`. Run the full verification suite from the repository root when validating changes.

Isolated backend: `app/backend/.venv/bin/contextcore serve --port 18420 --data-dir "$HOME/.contextcore/dev-data" --no-static`; frontend: `npm --prefix app/frontend run dev:isolated` (15173 → 18420). Default `dev` points to 8420. Built assets go to `dist-staging`; no stale `dist` fallback. Backend `.env` files do not autoload.

No private-history inspection, model calls/downloads, real account flows, host config changes, production stop/activation, LaunchAgent install/uninstall, push or publish without specific authorization. `prepare-release` and `activate-release` are helper commands, **not** `upgrade`; see app operations for exact arguments. Documented commands are not permission to execute consequential operations.

## Shared-checkout hygiene

Preserve unrelated changes and respect file ownership. Do not branch, worktree, commit or publish when the assignment forbids it. Report only observed results; do not promote peer completion prose or bundled reference grades into local acceptance. Keep runtime databases, credentials, virtualenvs, node_modules, build outputs and private acceptance artifacts out of commits.
