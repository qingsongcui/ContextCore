# Contributing to ContextCore

Active product: `app/`. Requirements: [public specification](docs/SPECIFICATION.md). Scope gaps: [release readiness](docs/release-readiness.md).

## Setup (repository-root commands)

Python 3.12 recommended (minimum 3.11 with SQLite `blobopen`); Node.js recommended Node 24 LTS (>=24.15.0; supported engines `^22.22.2 || ^24.15.0 || >=26` due to dev-only `jsdom@30.1.3`; CI uses Node 24). Canonical editable setup installs the **package**, not just its dependencies:

```bash
python3.12 -m venv app/backend/.venv
app/backend/.venv/bin/python -m pip install -e 'app/backend[test]'
npm --prefix app/frontend ci --ignore-scripts
```

Equivalent uv setup: `uv sync --project app/backend --frozen --extra test`. Use that project environment consistently; no global `pip` or `contextcore` is required. Existing running project virtualenvs must not be recreated as routine validation; point to a separate virtualenv and data directory when testing fresh installation or isolation. Requirements-only installs do not register the console command. `.env` is not automatically loaded by the backend; prefer CLI `--data-dir` and review [.env.example](.env.example).
Run isolated development in separate terminals, each from the repository root:

```bash
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --no-static
```

```bash
npm --prefix app/frontend run dev:isolated
```

The latter serves 15173 and proxies 18420. Ordinary `dev` targets 8420 and must not be used accidentally against production. Build output is `app/frontend/dist-staging`; old `dist` is not the CLI's default fallback. Wheel users must provide a built frontend or run `--no-static`.

## Checks
Run the verification commands below from the repository root before submitting changes. Reference boundaries and historical acceptance records are documented in [release readiness](docs/release-readiness.md) and [evidence](docs/evidence.md).

From the repository root after setup:
```bash
app/backend/.venv/bin/python -m pytest app/backend/tests -q
npm --prefix app/frontend run typecheck
npm --prefix app/frontend run lint
npm --prefix app/frontend run test
npm --prefix app/frontend run check:i18n
npm --prefix app/frontend run build
```

`test` is the current aggregate `vitest run` script. `test:management` and `test:regressions` remain available for focused work. With uv, the backend equivalent is `uv run --project app/backend --frozen --extra test pytest app/backend/tests -q`.

If changing the independent static website (no third-party runtime install required):

```bash
npm --prefix website run build
npm --prefix website run check
npm --prefix website run check:brand
npm --prefix website run check:i18n
npm --prefix website run check:interactions
npm --prefix website run check:sphere
```

For dependency audits use explicit official registry flags rather than changing machine-wide settings, e.g. `npm --prefix app/frontend audit --registry=https://registry.npmjs.org`. Advisory output must be reported with the command/version, not inferred from old audit counts. Source suites alone are insufficient for release acceptance: also verify clean wheel installation and resource loading, default static serving, and isolated runtime behavior (`app/scripts/check_release_readiness.py`).

## Boundaries and review

- **Consumer daily workflows and contracts**:
  - **Projects**: support directory-free project creation (optional directory), global cross-project search, and four project tabs (`Overview`, `Chats`, `Memory`, `Materials`).
  - **Memories, materials, and sources**: companion MCP/Skills real-time source-quoted auto-memory capture (`💡` receipt) plus manual notes/document imports across six memory classes (habits, background, constraints, todos, lessons, reference materials), 📌 core-rule pinning, `AGENTS.md` handbook preview, versioned rollback, and independent permanent deletion without cascading or recycle bins.
  - **Cross-Agent relay**: triggered inside the receiving Agent via `/contextcore` (or `"continue"` / `"relay"` / `"接着做"` / `"接力"`), listing numbered cross-host conversation candidates; selecting a candidate alone loads context (`must_not_execute=true`), summarizes where the prior chat left off, and waits for the next instruction; attaching a concrete task continues directly. Do not add Web UI handoff dialogs/buttons.
  - **Navigation structure**: primary navigation comprises only three fixed entries—Projects (`projects`), Agent management (`agents`), and Usage and performance (`usage`)—plus conditional Needs your decision (`conflicts`) when pending conflicts exist. Do not add standalone retrieval or audit pages to primary navigation; Settings (`settings`, containing embedded Change log `audit` and advanced retrieval settings), language, and theme controls remain in the footer.
  - **Agent management**: URL-scoped per-assistant and per-section navigation (`page=agents`, `agent=<id>`, `section=<tab>`) with permanent top host switcher bar and One-Click Connect; known provider presets are read-only metadata without entitlement inference; one-click switch/toggle and multi-host sync save automatic rollback snapshots and open Diff confirmation modals on external file conflicts or explicit preview (disk writes do not prove host process loading); scope switches invalidate open editors, plans, and receipts; transient in-memory drafts (App session lifetime) can be continued or discarded without refresh persistence or password/credential/import caching; Skills default to independent file copy (`copy`), with managed per-file version symlinks (`symlink`) as an advanced, implemented, and locally backend-tested option (never execute scripts; generic config directory symlinks remain rejected); unknown usage counts and costs remain unknown, never zero; independent permissions (conversation reading, host configuration writing, and optional semantic model/runtime downloads) are strictly maintained.
- **Decision traceability**: Cite exact historical decision IDs (U001–U007, D001–D063). Do not invent or fabricate decision IDs; current usability requests do not create new D-numbers. Keep requirements, implementation locations, historical observations, and current checks strictly distinct.
- **Environment isolation**: Use temporary/isolated HOME and data plus synthetic inputs for behavior checks. No live histories, real account login, paid model call, model download, host modification, launchd operation or publication without specific user authorization.
- **Preserve confirmed scope**: Unsupported code paths stay visible as gaps; external billing/consent prerequisites are not implementation passes.
- **Behavioral test discipline**: Add behavioral tests at meaningful boundaries, not source-string/default-copy/wiring tests or prose snapshots. Do not remove a failure just to claim green.
- **Privacy and hygiene**: Update relevant public operational docs when changing interfaces. Do not publish private draft paths, interview quotations, personal histories, credential files, or raw recovery dumps.
- **Truthful reporting**: In PRs and reports, state exact commands actually executed, results, and limitations. Bundled host reference evidence does not verify the current machine. Do not claim all-host parity from parser/config presence, and distinguish automated test coverage from live host, external account, and clean-Mac acceptance.
- Do not commit runtime databases/WAL/SHM, virtualenvs, node_modules, build outputs, generated acceptance artifacts, or secrets. Keep unrelated shared-checkout work intact.
