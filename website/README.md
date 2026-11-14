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

## Content & Architecture Boundaries

Content describes cross-agent `/contextcore` continuation across six core context hosts (Claude Code, Codex, Cursor, Antigravity, OMP, OpenCode) using short-brief discovery, bounded sequential transcript paging (`read_conversation_messages`), in-context compaction (`must_not_execute=true` on number-only selection), dual-tier working memory (`working_tasks` + versioned long-term memories with provenance), multi-host config sync (6 core + 9 extended hosts under the `contextcore` MCP key on `127.0.0.1:8420/mcp/`), and optional local semantic retrieval. The marketing pages (`index.html`, `guide.html`, `compare.html`, and static `/en/*` equivalents) disclose real limits (`$0` MIT, `macOS 14.0+ arm64` only, single-machine loopback, first-launch `xattr -cr`) and must not fabricate usage metrics, installers, or live host telemetry.

The recorded verification baseline in the repository covers four live-host continuation loops (Codex CLI, OMP CLI, Antigravity Desktop, OpenCode) and two protocol loops (Cursor, Claude Code), while config schema serialization and snapshots are tested across declared hosts. Models/accounts/tools/Skills/instructions, project combinations, and migration behavior remain scoped per host format rather than universal parity.

SEO metadata, JSON-LD graphs, and `llms.txt`/`llms-full.txt` follow structured clarity without guaranteeing indexing, citation, or ranking. Keep copy factual and concise.

## Source and visual boundaries

Styles are sourced under `assets/css/` and bundled by `scripts/css-bundle.mjs`; edit source, not `dist`. The hero's Canvas surface is implemented in `assets/sphere.js`; semantic page content, FAQ and navigation must remain readable without it. Animation represents a flow illustration, **not real connection state**. Reduced motion, page visibility/offscreen pausing, keyboard focus and small-screen readability require current browser checks; simulated canvas tests cannot prove visual correctness.

## Pages and Bilingual Coverage

- `index.html` (`en/index.html` via `assets/i18n.js`): Product showcase, 6-step full-transcript relay architecture, dual-tier memory workspace, 6+9 host matrix, official dated Fact Sheet (`#fact-sheet`), and 9-item native `<details>` FAQ.
- `guide.html` (`en/guide.html`): Apple Silicon macOS install (`brew install --cask qingsongcui/tap/senrelay` / `v1.0.0-beta.2` zip / `xattr -cr`), source dev mode, MCP key (`contextcore` on `/mcp/`) vs `/contextcore` skill, security boundaries, and 3-item troubleshooting FAQ (`FAQPage` schema).
- `compare.html` (`en/compare.html`): Honest 5-option comparison (`AGENTS.md`, Cloud/Docker Memory MCP, Senrelay ranked #3, Dotfiles, Single IDE) with interactive setup picker, `#option-1`..`#option-5` ItemList schema, and 6-item decision FAQ.
- `llms.txt` & `llms-full.txt`: Pure-English summary header plus full-transcript relay specification, 6+9 host matrix, 5-way comparison table, and Chinese core summary (`续境` / `ContextCore`).

There is no live demo workspace, fake confirmation button, or server-side tracking. The sphere and static workspace previews are explicitly illustrative, not live state.

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

Automated checks execute the interaction module against a DOM boundary (language/metadata/restoration, navigation/Escape/focus, and preference/storage/disclosure/visibility state), run the five sphere behavioral tests, validate semantic headings, six-entry handoff structure, native disclosure labels, control targets, links, assets, indexing rules (`noindex` preview vs `SITE_URL` deployment), brand rename consistency, and full Chinese/English coverage across `index.html`, `guide.html`, and `compare.html`.

Recommended manual browser walkthrough before public deployment: serve the built static site on a loopback preview port and check **375 / 768 / 1280 px** in Chinese and English, including navigation, `#workflow`, `#assistants`, `#workspace`, disclosures, `guide.html` & `compare.html` language toggles, reduced-motion behavior, and keyboard focus. Automated DOM/style checks complement but do not replace visual and assistive-technology review.
