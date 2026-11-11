# How to install and recover the Beta App

For the ad-hoc Apple Silicon Beta on macOS 14.0+. This is not a notarized installer; whole-database SQLite online backups can be created locally, while restoring a backup remains manual file replacement with the service stopped.

## Install or first-launch an ad-hoc build

1. Confirm the machine is Apple Silicon (`arm64`) running macOS 14.0 or later. Intel and older macOS versions are unclaimed.
2. Copy `Senrelay-macOS.zip` from the verified release or candidate output directory, not from `app/macos/dist/` on a development Mac that is actively running the daily service.
3. Check `SHA256SUMS` against the ZIP bytes before extracting (`Senrelay-macOS.zip` contains `Senrelay.app` only).
4. Extract `Senrelay.app` and follow the two-step first-launch procedure for ad-hoc macOS 14.0+ builds:
   1. Move `Senrelay.app` to `/Applications` (or to an isolated candidate folder such as `/tmp/senrelay-candidate/` when testing on a development Mac without replacing a daily install).
   2. Run `xattr -cr /Applications/Senrelay.app` in Terminal to clear quarantine attributes (or allow it in **System Settings → Privacy & Security → Open Anyway**). Source checkouts also provide `app/macos/install-macos.sh` for Terminal installation. Never disable Gatekeeper globally.
5. Open `Senrelay.app`. On first launch, the App starts its bundled service with `~/Library/Application Support/ContextCore` if nothing is already listening on loopback port `8420` with the bundled identity.

A development Mac that already runs a daily `8420` service should not treat an isolated candidate as a silent upgrade. Keep candidate builds on a separate path and data directory until you explicitly choose a cutover.

## First-run recovery

Use **Window → Reconnect** (`Cmd+R`) if the workspace page fails, or **Window → Close Window** (`Cmd+W`) to close the window while keeping the background service running. Startup errors offer a reconnect action, and an interrupted page process keeps project data on disk.

If the App reports a service version mismatch, use the explicit **Update Local Service & Reconnect** action (`Cmd+U`). That path stops the older local ContextCore service on port `8420` and starts the bundled one without deleting saved projects or memories. It does not silently upgrade on launch.

If port `8420` is held by an unrelated third-party process, stop that conflicting program first and click **Reconnect**.

## Data after a crash and backups

Closing the window does not stop collection. After a crash, reopen the App and reconnect. Chats and memories already committed to `~/Library/Application Support/ContextCore` remain intact. In-flight browser sessions are not a durable store.

Configuration changes write automatic rollback snapshots before modifying host files and surface diff confirmation when external file conflicts occur or when explicitly previewing a plan. Do not restore host files by copying older whole-file backups over a tree you have edited since.

Whole-database SQLite online backup is supported locally via `contextcore backup` and `POST /api/settings/backup` (saved with `0600` permissions under `<data_dir>/backups/`). Automated cloud backup or one-click snapshot restore is not included: restoring a database snapshot requires stopping the local service first and manually replacing the SQLite database file. Time Machine or a disk clone remains outside this guide.

## Uninstall

1. Quit the App.
2. Stop the background service from Settings (or `contextcore stop`) if it is still running.
3. Move `/Applications/Senrelay.app` to Trash.
4. User data stays in `~/Library/Application Support/ContextCore` until you delete it intentionally. Deleting the App does not delete projects, memories, or macOS Keychain items.

Removing a host integration is an explicit action in Agent management. It is not the same as deleting the App.
