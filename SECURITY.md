# Security Policy

ContextCore is a single-user browser workspace backed by a macOS local background service. The operating-system user account constitutes the primary trust boundary; there is no separate multi-tenant application lock.

## Network & Boundary Protections

- **Strict Loopback Binding**: The service binds exclusively to loopback (`127.0.0.1`). Never expose service ports to local area networks (LAN), reverse proxies, tunnels, or public interfaces.
- **Header & Origin Validation**: Validates peer address, Host header, and browser Origin. Cross-site browser requests are rejected.
- **CSRF Defense**: Browser mutations require an unexpired opaque session-ID cookie and that exact session's matching CSRF token. Sessions live in the process's in-memory store; a restart invalidates them. `CONTEXTCORE_SESSION_SECRET` is not used to sign these cookies and is not a required setup step.
- **Agent Credential Store**: MCP servers and host hooks require an authorized Agent credential. Credential-store failures must deny access (fail-closed), never falling back to anonymous access.
- **Credential Scope**: A hook token is strictly bound to its assigned Agent; it cannot impersonate another host. Browser administration endpoints do not accept Agent tokens in lieu of CSRF tokens.
- **Single-User OS Scope**: These controls defend against network-adjacent and cross-site threats; they do not isolate against malicious processes running under the same local OS user account.

## Data Governance & Privacy Boundaries

- **Shared Memory Scope**: Authorized Agents share project knowledge and memory with uniform access. Ordinary memory writes do not require per-item approval.
- **Opt-in Handoff Confirmation**: Explicit handoff confirmation is required before task continuation. Ambiguous content conflicts require a user resolution in the review inbox.
- **Pause & Revocation**: Pausing collection suspends background ingestion; revoking an Agent disables its credential. Context already provided to an Agent's active context window cannot be retracted.
- **Permanent Deletion & Tombstones**: Conversation and memory deletions are independent and permanent at the application level, not a promise of forensic disk erasure. Migration, ingestion, extraction, version restoration and derived handoff/index copies must respect the dedicated `tombstones` table and current canonical state. A separately surviving memory can identify a deleted source; it must not revive the deleted record.
- **Retained Data & Credentials**: Original session records may contain tokens or keys as part of development chat transcripts; these are retained as written by explicit product design. Audit records and operational logs must **never** duplicate source bodies, raw tool outputs, passwords, or bearer tokens.
- **External Provider & Routing Disclosure**: Routing requests or executing an authorized provider check sends the relevant request payload to the selected model provider and may consume quota. History access, host configuration writes, routing use, and optional semantic model downloads are separate consents; backend compute endpoints remain disabled by default.
- **Credential Permissions**: Generated agent tokens must be stored with owner-only permissions (`chmod 0600`); never transmit them in URL query parameters, public setup previews, or error logs.

## Host & System Operations

- **Explicit Authorization**: Installing or removing host integrations and macOS LaunchAgents (`com.contextcore.service.plist`) requires explicit user authorization. Development runs and automated checks must use isolated temporary directories and synthetic records.
- **Distillation Safety**: Conversation inputs are untrusted data, not executable instructions. Unmetered or unconstrained tool-enabled agents must not be spawned to summarize history. Unsupported or unsafe compute paths must fail explicitly.
- **Process Termination Safety**: Stopping the service requires verified process identity (matching executable and PID). A stale PID file must not trigger termination of an unrelated process. Forced termination (`--force`) requires explicit flags and process identity confirmation.

## Verification Status

Security behavior is governed by integrated verification. Unit tests and synthetic protocol checks do not establish real-host compatibility, safe launchd installation, or complete production readiness. Do not treat documentation as a formal security certification.

Bundled host evidence records a reference testbed, not security or compatibility verification of a fresh installation. Private historical execution attachments are not published in the [public summary](docs/evidence.md).

## Reporting Security Issues

To report potential security vulnerabilities or boundary violations:

1. Use the ordinary bug template for non-security product issues. For a potential vulnerability, do not post exploit details, credentials, private chats, databases, or account exports in a public issue.
2. Use GitHub Private Vulnerability Reporting only if the repository visibly offers it. Otherwise ask a maintainer for a private reporting channel using a non-sensitive description; wait for that channel before sharing sensitive findings. This repository does not currently document a verified private contact address. Do not assume Security Advisories or private reporting are enabled.
3. Include the affected version, macOS version, interface (Web UI, CLI, or MCP), minimal synthetic reproduction steps, expected behavior, and observed boundary failure.
