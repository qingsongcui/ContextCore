# Senrelay macOS app

Senrelay uses an AppKit window and WKWebView for the current workbench. The bundle contains a frozen Python runtime, the backend, frontend and required resources. Running the app does not require a source checkout, Node or a separate Python installation.

For the local personal-workspace test build, run the following inside the locked backend build environment. The resulting app has its own bundle identifier, loopback port 19426, service data in `~/Library/Application Support/SenrelayLocalTest`, and a separate service home beneath that directory. First launch opens an empty workspace. Use synthetic projects or explicitly choose material to import.

```sh
app/backend/.venv/bin/python app/macos/build.py --from-source --local-workbench-test --build-number 5 --output-dir ../local-workbench-test
```

For this branch’s redesigned workbench, select its own test identity and output directory.

```sh
app/backend/.venv/bin/python app/macos/build.py --from-source --workspace-design-test --build-number 7 --output-dir ../workspace-design-test
```

This build runs at `127.0.0.1:19436` and keeps its data and service home in `~/Library/Application Support/SenrelayWorkspaceDesignTest`. Its workspace, personal Agent and management sections share navigation with the local browser UI. Project data belongs to the local service and persists when the window closes. The native shell supports Chinese and English, initially follows the system/app preference, and follows language changes in the workbench. Startup errors provide a reconnect action; interrupted page processes and failed downloads have visible recovery messages.

The optional `使用本机Agent配置.command` launcher opens the same local web workspace against the current user's actual Agent configuration roots. Run it next to the app, or open the copy inside `Senrelay.app/Contents/Resources` in the ZIP distribution. Type START, then open the printed local URL in a browser. The foreground process uses port 19446 and its own state directory `~/Library/Application Support/SenrelayHostWorkspaceTest`. The regular test workspace uses its separate test home. Export and import selected definitions to transfer them between the two workspaces. Preview target paths before applying changes and stop the optional service with Ctrl-C.

Services on this build share an OS-user lock when applying or recovering host configuration, even across separate databases. If a write is in progress, re-preview and retry after it finishes. Before writing to actual Agent paths during an upgrade or test, exit older Senrelay services that manage those same paths. Older services and third-party editors do not participate in this lock.

## Menu bar and keyboard navigation

Open the Senrelay menu bar icon to see favorite and recent agents. Each agent submenu reads the current provider, model and configuration file status from the workbench service. Choose the agent’s models, change history or usage to open that screen. Account quotas shows saved readings and their observation times. Open the full quota screen to authorize a refresh.

The **Go** menu opens the global resource library and Agent card and provides ⌘0 for the workbench home, ⌘1 for projects, ⌘2 for all agents, ⌘3 for the usage overview and ⌥⌘3 for account quotas. These shortcuts work while the app is active. Navigation keeps the existing workbench document and supports Back.

## Build a candidate from source

On macOS, install Command Line Tools, the locked backend build extra and the frontend dependencies. Commands start at the repository root:

```sh
uv sync --project app/backend --frozen --extra test --extra build
npm --prefix app/frontend ci --registry=https://registry.npmjs.org
app/backend/.venv/bin/python app/macos/build.py --from-source --build-number 4
app/backend/.venv/bin/python app/macos/verify_bundle.py
app/backend/.venv/bin/python app/macos/verify_native.py
```

`--from-source` builds the current frontend and prepares a matched immutable backend/frontend snapshot under `app/macos/build/`. It does not use or activate the daily service, register a LaunchAgent, read history or change host configuration. This makes a clean CI build possible. CI installs both locked test and build extras, builds the actual native bundle and runs its bundled-service smoke verification.

Outputs in `app/macos/dist/`:

- `Senrelay.app` and `Senrelay-macOS.zip` (`Senrelay-macOS.zip` contains `Senrelay.app` only).
- `BUILD.json`: release ID, backend/frontend hashes, package version, build number, architecture, `minimum_macos` (`14.0`), parent UI selection and signing status.
- `SHA256SUMS`: checksum of the ZIP.

Use `--output-dir /absolute/candidate/directory` to keep a candidate separate from a delivered app. Choose an increasing numeric `--build-number` for distribution (up to 7 digits in the primary component). The current build targets the build machine's architecture. The candidate compiler target, PyInstaller deployment target (`MACOSX_DEPLOYMENT_TARGET=14.0`), and bundle minimum are macOS 14.0+; older macOS compatibility is not claimed. Clean-system acceptance on Apple Silicon remains required before publication. The approved first public Beta uses ad-hoc signing without notarization: on first launch, (1) move `Senrelay.app` to `/Applications`, and (2) run `xattr -cr /Applications/Senrelay.app` in Terminal (or allow in `System Settings → Privacy & Security → Open Anyway`; source checkouts also provide `app/macos/install-macos.sh`). Intel, Developer ID signing and notarization are outside this Beta gate.

Without `--from-source`, the builder packages the installed immutable `current-release.json`, or `--release-manifest <path>`. This mode checks every release/hash against `selected-release.json` and refuses a different selection. The historical user-selected version 35 remains provenance for subsequent candidates. A new candidate is not automatically a user-installed release.

## Verify the actual artifact

```sh
app/backend/.venv/bin/python app/macos/verify_bundle.py --bundle /absolute/path/Senrelay.app
app/backend/.venv/bin/python app/macos/verify_native.py --bundle /absolute/path/Senrelay.app --output-dir /absolute/evidence/directory
```

The bundle smoke runs from an unrelated temporary directory with a clean environment and synthetic data. It checks health, matching release identity, built assets, no embedded projects, initially disabled capture, duplicate-start refusal and graceful shutdown.

Supply `--previous-bundle /path/to/previous/Senrelay.app` to run the upgrade scenario against the earlier frozen service and check its synthetic project, memory, saved profile and provider after upgrade.

Native verification starts the bundled service with a temporary data/home directory and renders the app's own WKWebView in a hidden verification mode. Chinese and English runs check the ten sidebar destinations, label bounds, the compact navigation drawer and native menu language. Evidence includes JSON and full native workspace snapshots. The export fixture exercises the three UI download actions, saves their WKDownload output to an isolated directory and compares the files with the selected profile and private markers. Its nonpersistent WebKit store does not reuse the user's browsing/session state. Extended verification also covers window sizes, themes, page headings, startup failure, wrong-port recovery and controlled service upgrade. The standard verifier injects WebView failure callbacks. A separate hidden `soak_native.py --terminate-webkit` scenario verifies actual, uniquely attributed WebContent termination and reconnection. These checks do not establish all-page usability or clean-Mac distribution acceptance.

## Lifecycle and data

- The daily build connects only to `127.0.0.1:8420`, requiring API contract 2 and exactly the bundled release/frontend identity. Another service or version produces a recoverable error. The side-by-side builds use the independent ports listed above. Only hidden verification can override a build’s port.
- If no service listens, the app starts its bundled service with `~/Library/Application Support/ContextCore`. The package embeds no runtime database, projects, accounts or history.
- Closing the window or quitting the app keeps the background service running. Explicit service stop remains in Settings. Existing LaunchAgent configuration is preserved; packaging does not install one.
- App-started service logs: `~/Library/Logs/Senrelay/service.log`.
- Uploads use the native file chooser; exports use a Save panel. External HTTPS links open in the default browser. The only native message handler synchronizes language from the app's own main frame; there is no filesystem or command execution bridge.
- Use Window → Reconnect / 窗口 → 重新连接 to retry. Standard editing shortcuts and reopening from the Dock are supported.

## Distribution status

Current candidates are ad-hoc signed. They are not Developer ID signed, Apple notarized or Mac App Store releases. Public release still requires the owner's signing identity, notarization and actual clean-Mac download/install acceptance. Automatic updating and clean-device upgrade acceptance remain open. On a recognized service version mismatch, the explicit “Update Local Service and Reconnect” action uses the owned-data service stop/start path and the bundled service; it does not silently upgrade on launch. Controlled synthetic upgrade and data retention passed, but this does not establish an upgrade from a real older user installation. Do not disable Gatekeeper globally.

The release workflow now includes native sources and a checked architecture-labelled ZIP beside the source/UI candidate, with version/commit metadata and checksums. A workflow definition does not prove that a remote CI run or public release occurred. See [release readiness](../../docs/release-readiness.md).

The one-off screenshot-repair delivery script was archived in private recovery evidence. It is not a general installer or upgrade entry point.

## Read-only QA resource samples

For already-running, explicitly selected isolated QA processes, run:

```sh
python3 app/macos/sample_resources.py --app-pid APP_PID --service-pid SERVICE_PID --seconds 60 --output /absolute/evidence/resources.json
```

Replace the two PID placeholders with the actual QA process IDs. This tool does not launch, stop or elevate any process. It records current RSS and physical footprint, stops on process exit/PID reuse, and leaves missing or partial WebKit measurements unknown. System-managed WebKit attribution and whole-product totals are not provided by descendant-only sampling. For an independently hidden-launched LaunchServices QA App, the explicit `--isolated-launchservices` mode can measure matching WebKit coalition members; never use it for a direct CLI launch. A short observation is not a soak test.

## Frozen-backend stability observation

`soak_backend.py` exercises an explicitly chosen frozen bundle with synthetic data and a fresh output directory. It does not activate the daily service. For a bounded smoke run:

```sh
python3 app/macos/soak_backend.py --bundle /absolute/path/Senrelay.app --output /absolute/new-evidence-directory --seconds 30 --interval 0.1
```

The default duration is 24 hours at 60-second intervals. Observe the existing process/checkpoint before resuming a long run; never replace a live run based only on an output timeout. A successful short run does not certify 24 hours, and backend stability does not certify App/WebView stability.

### Hidden native sustained observation

`soak_native.py` launches an exact candidate through LaunchServices with a hidden
verification window, its own frozen service, fresh HOME/data and 100 synthetic
records. It rotates seven pages (`home`, `projects`, `personal`, `exchange`, `agents`, `usage`, `settings`) and records independently attributed WebKit
resources. A new output directory is required:

```bash
python3 app/macos/soak_native.py --bundle /absolute/path/Senrelay.app --output /absolute/new-evidence-directory --seconds 25 --interval 1
```

Defaults are 24 hours and 60-second page intervals, with resources sampled every
30 seconds. Long resource ledgers stream to JSONL; the JSON checkpoint retains
only 100 recent samples. Check live process handles before restarting anything.
This never activates the daily App. Timed completion is not a memory-growth
verdict or sleep/wake acceptance.

### Isolated 24-hour growth soak

`soak_beta.py` wraps the hidden native observer, refuses daily App paths,
`app/macos/dist/`, and any output directory inside the repository checkout, and
fails with exit code 2 if App or backend RSS stays more than 128 MiB above
baseline for ten samples, or if either process stays at or above 80% CPU for ten
samples. Output must be a new external directory outside the repository:

```sh
python3 app/macos/soak_beta.py \
  --bundle /tmp/senrelay-candidate/Senrelay.app \
  --output /tmp/senrelay-beta-soak \
  --seconds 86400 --interval 60
```

`--seconds 10` is the minimum for a harness smoke. A short run does not certify
24 hours. Do not point `--bundle` at `/Applications`, `~/Applications`, the
daily Application Support tree, `app/macos/dist`, or a binary that is already
running.

### Release candidate verification

Native CI runs on GitHub's Apple Silicon `xcode-27` runner image while compiling
and packaging with a `macOS 14.0+` (`arm64`) deployment minimum
(`LSMinimumSystemVersion = 14.0`, `MACOSX_DEPLOYMENT_TARGET=14.0`, and
`-target arm64-apple-macosx14.0`, verified via `vtool -show-build` `minos <= 14.0`).
GitHub documents the runner image in its
[2026-10-15 runner announcement](https://github.blog/changelog/2026-10-15-xcode-27-runner-image-generally-available/).
This is a runner selection, not evidence of a successful remote CI execution or
clean-Mac acceptance across older macOS releases.

`verify_hook_distribution.py --bundle <Senrelay.app> --output-dir <evidence-dir>`
installs components through the packaged API in a fresh HOME, executes generated
Codex/Cursor/Antigravity/Claude Code commands and installed OMP/OpenCode modules,
checks consumption and revocation, and terminates only its own service. Node 24+
is needed by the module verifier and represents the host runtime; it is not
shipped inside Senrelay. These synthetic callbacks do not prove actual host
loading, real sessions, or clean-device installation.
