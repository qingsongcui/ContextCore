# Senrelay / 续境 identity

The recorded adopted identity uses the transparent PNG extracted from the approved concept image, not a later code-redrawn SVG approximation. Public brand: **Senrelay · 续境**. Technical protocols, API/MCP, data paths, LaunchAgent identifiers and GitHub repository remain ContextCore.

## Assets and provenance limits

- `assets/senrelay-logo-original-v1.png`: 1145×1374 RGBA mother image with transparent margins (`SHA-256: fd8984696ed71ff630b88695f937cfb2bdb03e2cf46a21d58df5237b68c84162`).

The repository PNG is the canonical distributed brand mark; no external generation path is needed to use it.

From the repository root, `node brand/scripts/sync-identity.mjs` copies the mother image into website/workbench assets and checks the configured hashes. Preserve aspect ratio and pixels rather than tracing, recoloring, or replacing the mark. The light-theme treatment uses a dark CSS backing for the same PNG. A CSS display size is not a new logo design. No vector logo is claimed.

## Naming and domain

Tagline: **换个 Agent，思路接着走。 / New agent. Same thread.** Senrelay is a coined brand name; the record's suggested pronunciation is SEN-relay, not a claim of established English etymology.

The last record says the brand was adopted, but domain registration and trademark clearance were not complete. Earlier RDAP availability observations are historical, not present availability, purchase rights, trademark clearance or a guarantee of price. Do not link an unowned domain or claim deployment/registration without authorization and evidence. GitHub remains `https://github.com/qingsongcui/ContextCore`.

## Local preview and verification

From the repository root:

```bash
node brand/scripts/serve.mjs
```

Preview: `http://127.0.0.1:4319`. The separate website preview normally uses 4318; product service normally uses 8420, neither is started by this command. Preview is local-only, not a domain purchase, backend restart or public deployment.

Historical identity and verification notes are summarized in [evidence](../docs/evidence.md). Brand adoption does not certify host integration or product completeness. D058's earlier model-exclusive frontend restriction was revoked on 2026-10-25.
