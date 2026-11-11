# Senrelay / 续境 website

Independent static Chinese/English product showcase. The recorded adopted public brand is **Senrelay · 续境**; technical identifiers and repository remain ContextCore. This site is not the local workbench, does not certify installation/host adoption and is not a model-calling or history-reading interface.

Requirements and implementation status are separate: [public specification](../docs/SPECIFICATION.md), [readiness/gaps](../docs/release-readiness.md), [historical evidence summary](../docs/evidence.md). Root PRD is superseded. Public descriptions must not claim six live-host passes, universal management parity, measured zero loss/time savings, paid users or a released installer without evidence.

## Local preview

Node.js 22+. No third-party runtime dependencies or npm install needed. **Commands start at the repository root.**

```bash
npm --prefix website run build
npm --prefix website run preview
```

Open `http://127.0.0.1:4318`; Ctrl+C stops this independent loopback preview. `PORT=4320 npm --prefix website run preview` selects another port. `website/dist/` is generated output; rebuild after source changes.

Default builds use `noindex, nofollow` and a deny-crawl robots file; no canonical/sitemap or invented deployment domain. Supply `SITE_URL` only when building for an owned deployment target.

## Deployment preparation, not authorization to publish

Only after confirming an owned domain, public repository/release contents and explicit publication authorization, supply the actual complete deployment URL. Example below is a placeholder, not an owned domain:

```bash
SITE_URL='https://your-confirmed-domain.example/project/' npm --prefix website run build
npm --prefix website run check
```

`SITE_URL` accepts a root/subpath URL and normalizes its trailing slash. The documented build rejects non-HTTP(S), userinfo, query and fragment; explicitly supplying a site URL switches indexing metadata. It does not upload anything. Publish the entire generated directory with correct HTML/CSS/JS/PNG/SVG MIME types. For subpath hosting the domain owner's root `robots.txt` must carry the sitemap policy; a subdirectory robots file does not replace it. Restore the default local/noindex build by rerunning build without `SITE_URL`.

## Brand and content

`brand.config.mjs` supplies identity and repository/deployment URLs; current product descriptions and JSON-LD come from `index.html`, with English in `assets/i18n.js`. The builder preserves this page-owned copy instead of replacing it with older brand-description text. The versioned PNG mother image is `../brand/assets/senrelay-logo-original-v1.png`; source/provenance limits are in the [brand README](../brand/README.md). From repository root, `node brand/scripts/sync-identity.mjs` copies identity assets to website/workbench; this is a file-mutating operation, not a required preview step or a fresh hash-verification result.

Content describes in-chat `/contextcore` cross-Agent continuation, six-item context coverage and confirmation discipline, real-time companion memory capture plus shared/versioned memory, independent deletion, per-assistant configuration management, and optional local semantic retrieval with honest host boundaries. D005's required items are goal/progress/constraints/working scene/blockers/next steps; current API decisions/evidence names do not prove coverage by themselves. The marketing page must not imitate a real task confirmation or connection probe.

The recorded reference evidence is **four live-host continuation loops** (Codex CLI, OMP CLI, Antigravity Desktop, OpenCode) and **two protocol loops** (Cursor, Claude Code). Codex CLI evidence does not certify Codex Desktop; bundled grades are reference, not local-install verification. Models/accounts/tools/Skills/instructions, project combinations, migration and usage are explained with per-assistant scope, not universal support. Official account eligibility, external calls, real host loading, wider-host differences and semantic quality remain separate evidence. Skills copy/shared-link modes are described as host-dependent, not a fresh live adoption claim. Optional semantic/relations remain first-release scope, disabled pending consent.

SEO metadata and static structure do not guarantee indexing, rich results, AI citation or ranking. `llms.txt` is only an optional reading aid. Do not fabricate ratings, traffic, customers, prices or domain ownership.

## Source and visual boundaries

Styles are sourced under `assets/css/` and bundled by `scripts/css-bundle.mjs`; edit source, not `dist`. The hero's Canvas surface is implemented in `assets/sphere.js`; semantic page content, FAQ and navigation must remain readable without it. Animation represents a flow illustration, **not real connection state**. Reduced motion, page visibility/offscreen pausing, keyboard focus and small-screen readability require current browser checks; simulated canvas tests cannot prove visual correctness.

Historical design records mention charcoal/warm-white/gray-green tokens, orbit/particle motion and the approved PNG identity. These are implementation/reference choices, not mandatory interview hex colors or a still-active model-exclusivity rule. D058 exclusivity was revoked 2026-10-25.

## Current consumer-page changes

- Daily jobs now include writing, research, planning and coding. “Agent” is explained as an AI assistant. The first CTA opens the on-page start steps rather than implying a download.
- `#assistants` starts with choosing one assistant, then covers models/accounts, tools/Skills, instructions, project combinations, local configuration import/migration and usage. The page distinguishes saved definitions, written files and actual assistant loading.
- `#workspace` keeps shared memory/document explanations and adds source-backed question/keyword search with optional enhancements behind a native disclosure. `#workflow` gives four beginner steps and states the source-setup prerequisite.
- `#connections` discloses the four-live/two-protocol reference split in an expandable explanation. Native FAQs cover permissions, shared-memory scope, independent permanent deletion, accounts, advanced tools/routing/context and current release boundaries.
- PNG identity, sphere implementation and its five behavioral checks are unchanged. New CSS is `assets/css/08-consumer-features.css`, following numbered bundle ordering. Native navigation and disclosures remain useful without JavaScript; language buttons only appear when JavaScript is available.
- Chinese/English page copy, description/social metadata, both JSON-LD descriptions and the generated reading aid reflect productivity use rather than developer-only positioning. Language switching preserves open disclosures; navigation labels follow both language and expanded state.

There is no live demo workspace, fake confirmation button, connection probe, installer or paid-account claim. The sphere and static workspace illustrations are explicitly illustrative, not live state.

## Checks and browser verification

From the repository root:

```bash
npm --prefix website run build
npm --prefix website run check
npm --prefix website run check:brand
npm --prefix website run check:i18n
npm --prefix website run check:interactions
npm --prefix website run check:sphere
```

Automated checks execute the interaction module against a DOM boundary (language/metadata/restoration, navigation/Escape/focus, and preference/storage/disclosure/visibility state), run the five sphere behavioral tests, validate semantic headings, six-entry handoff structure, native disclosure labels, control targets, links, assets, indexing rules (`noindex` preview vs `SITE_URL` deployment), brand rename consistency, and full Chinese/English coverage across `index.html` and `guide.html`.

Recommended manual browser walkthrough before public deployment: serve the built static site on a loopback preview port and check **375 / 768 / 1280 px** in Chinese and English, including navigation, `#workflow`, `#assistants`, `#workspace`, disclosures, `guide.html` language toggle, reduced-motion behavior, and keyboard focus. Automated DOM/style checks complement but do not replace visual and assistive-technology review.
