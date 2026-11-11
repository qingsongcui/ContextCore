# Privacy

Beta reference for Senrelay · 续境. Product rules come from [SPECIFICATION](SPECIFICATION.md) D042, D043, D010, D011, D032, and D040. Operational controls are in [SECURITY](../SECURITY.md). This page is disclosure, not a certification.

## What the product stores on this Mac

The local service keeps project data, chats, memories, configuration, and audit metadata under the macOS user's application-support directory. Closing the window does not stop that service. An explicit stop does.

Authorized assistants share project knowledge and memory. Project filters organize the workbench. They are not permission walls.

Raw chats may retain tokens or keys you pasted, by product design (D042). Audit logs, management exports, and public artifacts must not copy those secrets. Default configuration export excludes managed keys. Free text inside prompts, skills, and materials is not scanned automatically. Review it before sharing.

API keys for providers live in the macOS Keychain when that path is used. The macOS user account is the trust boundary. There is no separate app lock (D043).

## What can leave this Mac

Loopback service traffic stays on `127.0.0.1`. Routing a request or making an authorized provider call sends the relevant content to that provider and uses quota. History reading, host configuration writes, routing, and optional semantic model downloads are separate consents. Ordinary search does not download models.

Unobserved usage and cost stay unknown. They are not recorded as zero.

## Deletion

Conversation deletion and memory deletion are independent and permanent at the application level. There is no recycle bin. This is not forensic disk erasure. A surviving independent memory may still name a deleted source. It must not revive the deleted record.

Whole-database SQLite online backup is supported locally via `contextcore backup` and `POST /api/settings/backup` (stored with `0600` permissions under `<data_dir>/backups/`), while automated cloud backup and one-click snapshot restore remain out of scope (D040; restoring a backup is a manual file replacement while the service is stopped). Configuration originals and rollback are separate from whole-database backups and are not a Time Machine substitute.

## Beta packaging

The first public Beta is an ad-hoc signed, unnotarized Apple Silicon (`arm64`) app with a macOS 14.0+ minimum. Ad-hoc signing does not add Apple notarization privacy review. Gatekeeper warnings are expected on quarantined downloads; move `Senrelay.app` to `/Applications` and run `xattr -cr /Applications/Senrelay.app` (or allow via **System Settings → Privacy & Security → Open Anyway**). Do not disable Gatekeeper globally to install it.

Private acceptance evidence, live history, and credentials stay outside the public repository.
