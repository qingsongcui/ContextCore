# ContextCore backend

FastAPI/SQLite REST and Streamable HTTP MCP service, browser session boundary, background capture and management CLI. Product platform: macOS; isolated tests/CI on another OS do not establish supported end-user lifecycle there. Python >=3.11 with SQLite `blobopen`; Python 3.12 recommended.

## Install and run

**All commands below start at the repository root**, not this directory. Canonical editable setup (also used by the [root guide](../../README.md)):

```bash
python3.12 -m venv app/backend/.venv
app/backend/.venv/bin/python -m pip install -e 'app/backend[test]'
app/backend/.venv/bin/contextcore serve --port 18420 --data-dir "$HOME/.contextcore/dev-data" --no-static
```

The editable package registers `contextcore`; `pip install -r app/backend/requirements.txt` alone installs dependencies only and does not register the console command. No global installation is needed. uv alternative: `uv sync --project app/backend --frozen --extra test`. Runtime-only sync can omit `--extra test`; packaging tools are the optional `build` extra, not a native desktop product.

`uv build --wheel app/backend` produces the wheel in `app/backend/dist/`; install that actual wheel into a clean environment for distribution checks, then execute it **outside the checkout** (automated via `app/scripts/check_release_readiness.py`). Wheel resources and a built web frontend are separate: supply `--frontend-dir` or explicitly choose `--no-static`.

```bash
app/backend/.venv/bin/python -m pytest app/backend/tests -q
app/backend/.venv/bin/contextcore status --json --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore stop --data-dir "$HOME/.contextcore/dev-data"
app/backend/.venv/bin/contextcore generate-plist --data-dir "$HOME/.contextcore/dev-data"
```

`generate-plist` prints a template. LaunchAgent `install/uninstall`, production stop/activation, host setup, real histories/accounts, model calls/downloads and publication require separate authorization.

## Static/release serving

- Source CLI default static discovery: `app/frontend/dist-staging`; no automatic fallback to stale `dist`.
- `--frontend-dir app/frontend/dist-staging` explicitly selects a built frontend; invalid explicit paths fail.
- `--no-static` runs only API/MCP. Health 200 does not prove `/` serves a workbench.
- Static files are pinned into `<data-dir>/frontend-releases/<sha256>`; prepared matching releases additionally pin backend files and an external verifier.
- Release preparation uses `app/scripts/prepare_release.py --backend ... --frontend ... --data-dir ... [--activate-release]`. The helper commands are `prepare-release` / `activate-release`, not `upgrade`. Activation requires a stopped service; it does not register LaunchAgent or restart it.

See [application operations](../README.md) for complete root-relative commands and [readiness](../../docs/release-readiness.md) for gaps.

## Environment and security

The backend does not automatically load `.env`; [.env.example](.env.example) documents process environment and hook-only variables. CLI `--data-dir` sets its database path; direct `config.py` imports without CLI/environment use a source-directory fallback instead of the CLI macOS platform default. `VITE_API_URL` does not configure the frontend proxy.

Browser cookies carry random opaque session IDs held in memory, not signed cookies configured with `CONTEXTCORE_SESSION_SECRET`. Agent MCP/hooks and model routing require their own credentials; do not paste secrets into environment examples or shell history. Networking is loopback-only. See [SECURITY](../../SECURITY.md).

## Non-Python runtime resources

`pyproject.toml` declares package data for integration JSON/TypeScript, six `integrations/skills/*.md` files, and service JSON/TXT resources including semantic runtime requirements/wheel manifests. Those declarations require clean installed-wheel verification; their presence in source is not proof of artifact completeness. Bundled host capability evidence describes a reference testbed, not current-install verification. Optional semantic preparation/download/inference is consented separately and defaults off.
