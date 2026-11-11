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
let guideMetadata = '';
if (site) {
  guide = guide.replace('content="noindex, nofollow"', 'content="index, follow"');
  guideMetadata = `<link rel="canonical" href="${escapeHtml(guideUrl)}">\n  <meta property="og:url" content="${escapeHtml(guideUrl)}">`;
}
guide = guide.replace('<!-- deployment-metadata -->', guideMetadata);
await writeFile(path.join(out, 'guide.html'), guide);

await writeFile(path.join(out, 'robots.txt'), site ? `User-agent: *\nAllow: ${site.pathname}\nSitemap: ${new URL('sitemap.xml', site).href}\n` : 'User-agent: *\nDisallow: /\n');
if (site) await writeFile(path.join(out, 'sitemap.xml'), `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>${escapeHtml(site.href)}</loc></url><url><loc>${escapeHtml(guideUrl)}</loc></url></urlset>\n`);

await writeFile(path.join(out, 'llms.txt'), `# ${brand.name} · ${brand.chineseName}\n\n> ${description}\n\nLocal macOS service and browser workbench for multi-agent workflows. Unifies multi-host configuration management (permanent top-bar host switching across Claude Code, Codex, Cursor, Antigravity, OMP, OpenCode and extended hosts, One-Click Connect, built-in provider presets, latency testing, MCP/Skills/prompt sync with automatic rollback snapshots, and existing local configuration import), companion auto-memory capture during conversations (with source links and a brief 💡 receipt), and cross-agent conversation relay.\n\nType \`/contextcore\` or say "Relay" in a new agent to automatically list recent conversation candidates across supported core hosts (Codex, Claude Code, Cursor, Antigravity, OMP, OpenCode) with their index, source agent, project, title, and summary. Reply with just a number to load the relay context and receive a brief status report while waiting for your next instruction, or include a specific task to continue working immediately.\n\n- [Local setup & workflow guide](${guideUrl}): local service setup and core workflow guide.\n- [Source repository](${brand.github}): source repository.\n- [License](${brand.github}/blob/main/LICENSE): MIT.\n`);
console.log(`Built ${out}\nMode: ${site ? `deployment-ready (${site.href})` : 'local preview (noindex; no canonical or sitemap)'}`);
