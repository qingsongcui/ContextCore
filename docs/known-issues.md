# Known issues

Beta reference. Items here are current limits or incomplete acceptance, not a promise that they will stay. Requirements remain in [SPECIFICATION](SPECIFICATION.md). Open work remains in [release-readiness](release-readiness.md).

## Distribution

- The App is ad-hoc signed and not notarized (`Senrelay-macOS.zip` packages `Senrelay.app` only). macOS Gatekeeper blocks quarantined ad-hoc bundles by default; for first launch on macOS 14.0+: (1) move `Senrelay.app` to `/Applications`, and (2) run `xattr -cr /Applications/Senrelay.app` in Terminal (or allow in **System Settings → Privacy & Security → Open Anyway**). Source checkouts also include `app/macos/install-macos.sh`. Developer ID signing and notarization are outside this Beta gate.
- The supported deployment target is Apple Silicon (`arm64`) on macOS 14.0+. Intel, older macOS, and other platforms are unclaimed.
- There is no public auto-update. A version mismatch against the bundled service shows a recoverable error. Updating the local service is an explicit action.
- GitHub Pages and a public download URL are not published from this checkout. Do not treat the marketing site preview as a live installer.

## Native App and service

- Closing the window leaves the background service running. Stop it from Settings or the service command.
- Actual WebKit process death and service crash have hidden-verification coverage. Visible-window sleep/wake/reboot acceptance on a clean Mac is still open.
- Resource soaks on older candidates do not transfer to a new freeze. A new candidate needs its own 24-hour run.

## Assistants and accounts

- Saved definitions, written host files, actual assistant loading, and a real model call are different states. The workbench can show a successful write while the assistant has not loaded it.
- Cursor and Antigravity Desktop do not expose a supported custom-provider file API. Keys and model selection stay in those products' own settings. OMP and Codex Desktop file writes affect subsequent configuration reads; they do not prove a running conversation adopted them. Codex Desktop shares files with Codex CLI.
- Official OAuth, Keychain lock/recovery, and paid provider calls need a real account and operator consent. Code paths alone are not that evidence.
- Host support is per product and per surface. Codex CLI evidence does not certify Codex Desktop. Cursor new capture is not a claim that hooks ran.

## Data and retrieval

- Optional semantic search and source-backed relations are in first-release scope and off by default. Quality is unverified until a consented local model run exists.
- Permanent deletion is application-level. Disk remnants and independent surviving memories are documented in [privacy](privacy.md).
- Whole-database SQLite online backups can be created locally via `contextcore backup` or `POST /api/settings/backup` (saved with `0600` permissions under `<data_dir>/backups/`); automated cloud backup scheduling and one-click snapshot restore are not included (restoring a backup remains manual file replacement while the service is stopped).

## Website and docs

- Default local website builds emit `noindex, nofollow` preview metadata until built with an explicit `SITE_URL` for a confirmed deployment domain.
- Public downloads are distributed through explicit GitHub Releases attachments (`Senrelay-macOS.zip` + `SHA256SUMS`), not directly from the static website preview.

## Packaging and verification boundaries

- Public package version is `1.0.0-beta.1` → Python wheel `1.0.0b1` → native short version `1.0.0` plus numeric build number (`app/scripts/release_versions.py`).
- Per-surface limits are listed in [support-matrix](support-matrix.md). Historical live-host grades in [evidence](evidence.md) describe recorded reference runs and do not certify an untested user environment.
- Clean-Mac Gatekeeper install, upgrade from older user installations, sleep/reboot autostart, and uninstall-with-data-kept require physical clean-device acceptance (`app/scripts/check_macos_install_lifecycle.py`).
- Live external sessions (paid provider calls, official interactive OAuth, live Keychain lock/recovery, and all-host live memory writes) require separate user-consented account verification beyond automated pytest and Vitest suites.
