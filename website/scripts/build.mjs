import { readFile, mkdir, cp, writeFile, rm } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import brand from '../brand.config.mjs';
import { bundleCss } from './css-bundle.mjs';
const root = fileURLToPath(new URL('../', import.meta.url));
const out = path.join(root, 'dist');
function deploymentUrl(value) {
  if (!value) return null;
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) throw new Error('SITE_URL must be an http(s) URL without credentials, query, or hash.');
  if (!url.pathname.endsWith('/')) url.pathname += '/';
  return url;
}
const site = deploymentUrl(brand.siteUrl);
const escapeHtml = (value) => value.replaceAll('&', '&amp;').replaceAll('"', '&quot;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const escapeJson = (value) => JSON.stringify(value).replaceAll('<', '\\u003c');
let html = await readFile(path.join(root, 'index.html'), 'utf8');
html = html.replaceAll('https://github.com/qingsongcui/ContextCore', '__BRAND_GITHUB_URL__')
  .replaceAll('Senrelay', escapeHtml(brand.name))
  .replaceAll('SENRELAY', escapeHtml(brand.name.toUpperCase()))
  .replaceAll('续境', escapeHtml(brand.chineseName))
  .replaceAll('换个 Agent，思路接着走。', escapeHtml(brand.tagline))
  .replaceAll('__BRAND_GITHUB_URL__', escapeHtml(brand.github));
// Page copy owns the product description; brand configuration owns identity/URLs.
const description = html.match(/<meta name="description" content="([^"]+)">/)[1];
const schema = JSON.parse(html.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
for (const entry of schema['@graph']) {
  entry.name = `${brand.name} · ${brand.chineseName}`;
  if (site) entry.url = site.href;
  if (entry['@type'] === 'SoftwareApplication') {
    entry.sameAs = brand.github;
    entry.license = brand.github + '/blob/main/LICENSE';
    entry.downloadUrl = brand.github + '/releases/latest';
    entry.softwareVersion = '1.0.0-beta.1';
  }
}
html = html.replace(/<script type="application\/ld\+json">.*?<\/script>/s, `<script type="application/ld+json">${escapeJson(schema)}</script>`);
let metadata = '';
if (site) {
  html = html.replace('content="noindex, nofollow"', 'content="index, follow"');
  metadata = `<link rel="canonical" href="${escapeHtml(site.href)}">\n  <meta property="og:url" content="${escapeHtml(site.href)}">\n  <meta property="og:image" content="${escapeHtml(new URL('assets/social-original-v1.png', site).href)}">\n  <meta property="og:image:width" content="1536">\n  <meta property="og:image:height" content="1024">\n  <meta property="og:image:alt" content="${escapeHtml(brand.name)} — ${escapeHtml(brand.tagline)}">\n  <meta name="twitter:image" content="${escapeHtml(new URL('assets/social-original-v1.png', site).href)}">`;
}
html = html.replace('<!-- deployment-metadata -->', metadata);
await rm(out, { recursive: true, force: true });
await mkdir(out, { recursive: true });
// Authored modules in assets/css are source-only; dist ships one bundled stylesheet.
// Exclude unreachable duplicate social.png without deleting original source asset.
const cssSource = path.join(root, 'assets', 'css');
await cp(path.join(root, 'assets'), path.join(out, 'assets'), {
  recursive: true,
  filter: (source) => source !== cssSource && !source.startsWith(cssSource + path.sep) && path.basename(source) !== 'social.png',
});
await writeFile(path.join(out, 'assets', 'site.css'), await bundleCss());

// Transform dist/assets/i18n.js with configured brand tokens to keep English dictionaries and links consistent.
let i18n = await readFile(path.join(root, 'assets', 'i18n.js'), 'utf8');
i18n = i18n.replaceAll('https://github.com/qingsongcui/ContextCore', '__BRAND_GITHUB_URL__')
  .replaceAll('Senrelay', brand.name)
  .replaceAll('SENRELAY', brand.name.toUpperCase())
  .replaceAll('续境', brand.chineseName)
  .replaceAll('换个 Agent，思路接着走。', brand.tagline)
  .replaceAll('__BRAND_GITHUB_URL__', brand.github);
await writeFile(path.join(out, 'assets', 'i18n.js'), i18n);

await writeFile(path.join(out, 'index.html'), html);

const guideUrl = site ? new URL('guide.html', site).href : './guide.html';

// Build local guide page with brand tokens
let guide = await readFile(path.join(root, 'guide.html'), 'utf8');
guide = guide.replaceAll('https://github.com/qingsongcui/ContextCore', '__BRAND_GITHUB_URL__')
  .replaceAll('Senrelay', escapeHtml(brand.name))
  .replaceAll('SENRELAY', escapeHtml(brand.name.toUpperCase()))
  .replaceAll('续境', escapeHtml(brand.chineseName))
  .replaceAll('换个 Agent，思路接着走。', escapeHtml(brand.tagline))
  .replaceAll('__BRAND_GITHUB_URL__', escapeHtml(brand.github));
const guideDescription = guide.match(/<meta name="description" content="([^"]+)">/)[1];
let guideMetadata = '';
if (site) {
  guide = guide.replace('content="noindex, nofollow"', 'content="index, follow"');
  const socialImg = escapeHtml(new URL('assets/social-original-v1.png', site).href);
  guideMetadata = `<link rel="canonical" href="${escapeHtml(guideUrl)}">\n  <meta property="og:type" content="article">\n  <meta property="og:site_name" content="${escapeHtml(brand.name)} · ${escapeHtml(brand.chineseName)}">\n  <meta property="og:title" content="${escapeHtml(brand.name)} · ${escapeHtml(brand.chineseName)} — 本地运行与使用指南">\n  <meta property="og:description" content="${escapeHtml(guideDescription)}">\n  <meta property="og:url" content="${escapeHtml(guideUrl)}">\n  <meta property="og:image" content="${socialImg}">\n  <meta name="twitter:card" content="summary_large_image">\n  <meta name="twitter:image" content="${socialImg}">`;
}
guide = guide.replace('<!-- deployment-metadata -->', guideMetadata);
await writeFile(path.join(out, 'guide.html'), guide);

await writeFile(path.join(out, 'robots.txt'), site ? `User-agent: *\nAllow: ${site.pathname}\nSitemap: ${new URL('sitemap.xml', site).href}\n` : 'User-agent: *\nDisallow: /\n');
if (site) await writeFile(path.join(out, 'sitemap.xml'), `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>${escapeHtml(site.href)}</loc></url><url><loc>${escapeHtml(guideUrl)}</loc></url></urlset>\n`);

const llmsFullUrl = site ? new URL('llms-full.txt', site).href : './llms-full.txt';
const llmsContent = `# ${brand.name} · ${brand.chineseName} (ContextCore)

> ${description}

${brand.name} (${brand.chineseName}, repository \`ContextCore\`) is an MIT-licensed, local-first native macOS desktop app, browser workbench, and background service (\`127.0.0.1:8420\`) for developers working across multiple AI coding assistants.

## Core Capabilities

- **Cross-Agent Conversation Relay (\`/contextcore\`)**: Type \`/contextcore\` (or say "接着做" / "接力" / "continue" / "relay") inside Claude Code, Codex, Cursor, Antigravity, Oh My Pi (OMP), or OpenCode to list recent cross-host conversation candidates with index, source agent, project, title, and summary. Replying with only a candidate number loads relay context (\`must_not_execute=true\`) and reports progress while awaiting instructions; replying with a number plus a task continues execution directly.
- **Companion Source-Linked Auto-Memory**: Active agents capture structured memories across six categories (Preferences, Background, Constraints, Tasks, Lessons, References) via companion MCP tools and Skills with exact conversation-turn or document provenance and a brief \`💡\` receipt. Supports core-rule pinning (\`📌\`), conflict review, version rollback, \`AGENTS.md\` preview, and independent permanent deletion of chats versus memories.
- **Unified Multi-Host Configuration Management**: Switch across core and extended hosts from a permanent top bar, run One-Click Connect, fill provider presets, check endpoint latency, isolate API keys in macOS Keychain, and sync MCP servers, Skills, and system prompts with automatic rollback snapshots and Diff review when external manual edits conflict.

## System Requirements & Privacy Boundaries

- **Platform**: macOS 14.0 (Sonoma) or later on Apple Silicon (\`arm64\`); developer source mode also runs via Python 3.11+ and Node 24 LTS.
- **Privacy & Network Boundary**: Strictly binds to loopback (\`127.0.0.1\`) backed by local SQLite + FTS5. No mandatory cloud account or automatic cloud upload.

## Resources

- [Product Homepage](${site ? site.href : './index.html'}): Bilingual product overview and interactive walkthrough.
- [Setup & Workflow Guide](${guideUrl}): Installation, first launch (\`xattr -cr /Applications/Senrelay.app\`), and daily workflow guide.
- [Extended LLM Reference](${llmsFullUrl}): Detailed FAQ, architecture boundaries, and host support summary.
- [GitHub Repository](${brand.github}): Source code, specifications, and verification gates.
- [macOS Release Downloads](${brand.github}/releases/latest): Prebuilt \`Senrelay-macOS.zip\` (Public Beta, ad-hoc signed).
- [License](${brand.github}/blob/main/LICENSE): MIT License.
`;

const llmsFullContent = `${llmsContent}
## Common Scenarios & FAQ (EN / 中文)

### How do I hand off a coding session from Claude Code to Cursor or Codex? / 如何在不同 AI 编程助手间无缝接力对话？
Connect your hosts once via **Agent Management → One-Click Connect**. When switching assistants, type \`/contextcore\` (or "接力" / "continue") in the target agent. It queries local authorized histories and lists numbered recent sessions. Reply \`1\` to load context and get a status summary without modifying files, or \`1, finish the unit tests\` to continue immediately.

### How does multi-host MCP and Skills sync avoid overwriting manual config edits? / 跨宿主同步 MCP 与 Skills 时如何防止覆盖手改配置？
Every configuration write records ownership hashes and an automatic rollback snapshot. If a target host file (\`~/.claude.json\`, \`~/.codex/config.toml\`, Cursor MCP config, etc.) was modified externally, ${brand.name} pauses auto-apply and opens a side-by-side Diff confirmation modal instead of silently overwriting user changes.

### Where are conversations, memories, and API keys stored? / 数据与密钥存在哪里？
Conversations and versioned memories live in a local SQLite database (\`0600\` permissions) on your Mac with online snapshot backup (\`contextcore backup\`). Managed API credentials are stored in macOS Keychain rather than plaintext exports.
`;

await writeFile(path.join(out, 'llms.txt'), llmsContent);
await writeFile(path.join(out, 'llms-full.txt'), llmsFullContent);
console.log(`Built ${out}\nMode: ${site ? `deployment-ready (${site.href})` : 'local preview (noindex; no canonical or sitemap)'}`);
