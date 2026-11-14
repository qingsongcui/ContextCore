import assert from 'node:assert/strict';
import { readFile, access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../dist/', import.meta.url));
const html = await readFile(path.join(root, 'index.html'), 'utf8');
const enHtml = await readFile(path.join(root, 'en/index.html'), 'utf8');
const guide = await readFile(path.join(root, 'guide.html'), 'utf8');
const enGuide = await readFile(path.join(root, 'en/guide.html'), 'utf8');
const compare = await readFile(path.join(root, 'compare.html'), 'utf8');
const enCompare = await readFile(path.join(root, 'en/compare.html'), 'utf8');
const notFound = await readFile(path.join(root, '404.html'), 'utf8');
const llms = await readFile(path.join(root, 'llms.txt'), 'utf8');
const llmsFull = await readFile(path.join(root, 'llms-full.txt'), 'utf8');

assert.equal((html.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in index.html');
assert.equal((enHtml.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in en/index.html');
assert.equal((guide.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in guide.html');
assert.equal((enGuide.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in en/guide.html');
assert.equal((compare.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in compare.html');
assert.equal((enCompare.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in en/compare.html');
assert.equal((notFound.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in 404.html');

assert.match(html, /<html lang="zh-CN">/);
assert.match(enHtml, /<html lang="en" data-static-locale="en">/);
assert.match(guide, /<html lang="zh-CN">/);
assert.match(enGuide, /<html lang="en">/);
assert.match(compare, /<html lang="zh-CN">/);
assert.match(enCompare, /<html lang="en">/);
assert.match(html, /<meta name="theme-color" content="#121412">/);
assert.match(notFound, /<meta name="theme-color" content="#121412">/);
assert.match(notFound, /<meta name="robots" content="noindex">/);
assert.match(html, /name="description"/);
assert.match(html, /name="twitter:card"/);
assert.match(html, /class="handoff-checklist"/);
assert.match(html, /class="host-matrix-table"/);
assert.match(enHtml, /class="host-matrix-table"/);

const css = await readFile(path.join(root, 'assets/site.css'), 'utf8');
assert.match(css, /prefers-reduced-motion/);
assert.match(css, /focus-visible/);

for (const [label, doc] of [
  ['index.html', html],
  ['en/index.html', enHtml],
  ['guide.html', guide],
  ['en/guide.html', enGuide],
  ['compare.html', compare],
  ['en/compare.html', enCompare],
]) {
  const ids = [...doc.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(ids.length, new Set(ids).size, `IDs unique in ${label}`);
  for (const [, target] of doc.matchAll(/href="#([^"\s]+)"/g)) {
    assert.ok(ids.includes(target), `anchor ${target} exists in ${label}`);
  }
  for (const [, controls] of doc.matchAll(/aria-controls="([^"]+)"/g)) {
    for (const id of controls.split(/\s+/)) assert.ok(ids.includes(id), `controlled element ${id} exists in ${label}`);
  }
}

for (const [pageLabel, pageDoc] of [['index.html', html], ['guide.html', guide], ['compare.html', compare]]) {
  for (const [, asset] of pageDoc.matchAll(/(?:href|src)="\.\/([^"\s]*)"/g)) {
    const [relPath, hash] = asset.split('#');
    const targetPath = path.join(root, relPath || 'index.html');
    await access(targetPath);
    if (hash && targetPath.endsWith('.html')) {
      const targetDoc = await readFile(targetPath, 'utf8');
      assert.ok(targetDoc.includes(`id="${hash}"`), `cross-page anchor #${hash} from ${pageLabel} exists in ${targetPath}`);
    }
  }
}
for (const [pageLabel, pageDoc] of [['en/index.html', enHtml], ['en/guide.html', enGuide], ['en/compare.html', enCompare]]) {
  for (const [, asset] of pageDoc.matchAll(/(?:href|src)="(\.\.?\/[^"\s]*)"/g)) {
    const [relPath, hash] = asset.split('#');
    let resolved = path.resolve(root, 'en', relPath || 'index.html');
    if (relPath === './' || relPath === '../' || relPath.endsWith('/')) {
      resolved = path.join(resolved, 'index.html');
    }
    await access(resolved);
    if (hash && resolved.endsWith('.html')) {
      const targetDoc = await readFile(resolved, 'utf8');
      assert.ok(targetDoc.includes(`id="${hash}"`), `cross-page anchor #${hash} from ${pageLabel} exists in ${resolved}`);
    }
  }
}

await access(path.join(root, 'favicon.ico'));
await access(path.join(root, 'assets/icon-192.png'));
await access(path.join(root, 'assets/apple-touch-icon.png'));
await access(path.join(root, 'assets/senrelay-logo-display-v1.png'));
await access(path.join(root, 'assets/social-original-v1.png'));
await access(path.join(root, 'assets/senrelay-logo-original-v1.png'));

let socialPngExists = true;
try {
  await access(path.join(root, 'assets/social.png'));
} catch {
  socialPngExists = false;
}
assert.equal(socialPngExists, false, 'unreachable duplicate social.png excluded from dist');

await access(path.join(root, 'guide.html'));
await access(path.join(root, 'en/guide.html'));
await access(path.join(root, 'compare.html'));
await access(path.join(root, 'en/compare.html'));
await access(path.join(root, '404.html'));
await access(path.join(root, 'llms.txt'));
await access(path.join(root, 'llms-full.txt'));

// Run-guide commands must name scripts that actually exist in this checkout.
const repository = fileURLToPath(new URL('../../', import.meta.url));
for (const doc of [guide, enGuide]) {
  for (const [, prefix, script] of doc.matchAll(/npm --prefix ([\w/-]+) run ([\w:-]+)/g)) {
    const manifest = JSON.parse(await readFile(path.join(repository, prefix, 'package.json'), 'utf8'));
    assert.ok(Object.hasOwn(manifest.scripts || {}, script), `guide command exists: ${prefix} run ${script}`);
  }
}

assert.ok(!/<div\b[^>]*\bdata-guide-section="en"/.test(guide), 'Chinese guide strips hidden English section');
assert.ok(!/<hr\b[^>]*\bclass="lang-divider"/.test(guide), 'Chinese guide strips lang-divider');
assert.ok(!/<div\b[^>]*\bdata-guide-section="zh"/.test(enGuide), 'English guide strips hidden Chinese section');
assert.ok(!/<hr\b[^>]*\bclass="lang-divider"/.test(enGuide), 'English guide strips lang-divider');
assert.ok(!/<div\b[^>]*\bdata-guide-section="en"/.test(compare), 'Chinese compare strips hidden English section');
assert.ok(!/<hr\b[^>]*\bclass="lang-divider"/.test(compare), 'Chinese compare strips lang-divider');
assert.ok(!/<div\b[^>]*\bdata-guide-section="zh"/.test(enCompare), 'English compare strips hidden Chinese section');
assert.ok(!/<hr\b[^>]*\bclass="lang-divider"/.test(enCompare), 'English compare strips lang-divider');

const cjk = /[\u4e00-\u9fff]/;
const stripSwitchAndScripts = (doc) =>
  doc
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<div class="language-switch"[^>]*>[\s\S]*?<\/div>/g, '')
    .replace(/\bdata-(?:zh|aria-zh)="[^"]*"/g, '');
assert.ok(!cjk.test(stripSwitchAndScripts(enHtml)), 'no untranslated CJK in static en/index.html');
assert.ok(!cjk.test(stripSwitchAndScripts(enGuide)), 'no untranslated CJK in static en/guide.html');
assert.ok(!cjk.test(stripSwitchAndScripts(enCompare)), 'no untranslated CJK in static en/compare.html');

assert.ok(!html.includes('/blob/main/app/README.md'), 'no link to superseded public app/README.md');
assert.ok(html.includes('href="./guide.html"'), 'links to verified local guide.html');
assert.ok(enHtml.includes('href="./guide.html"'), 'English homepage links to en/guide.html');
assert.ok(html.includes('href="./compare.html"'), 'links to verified local compare.html');
assert.ok(enHtml.includes('href="./compare.html"'), 'English homepage links to en/compare.html');
for (const doc of [html, enHtml, guide, enGuide, compare, enCompare, notFound]) {
  assert.ok(!doc.includes('brand-mark.svg'));
  assert.ok(!doc.includes('favicon.svg'));
  assert.ok(!doc.includes('src="./assets/senrelay-logo-original-v1.png"'), 'pages use lightweight display logo');
  assert.ok(!doc.includes('src="../assets/senrelay-logo-original-v1.png"'), 'pages use lightweight display logo');
  assert.ok(!doc.includes('aggregateRating'));
  assert.ok(!doc.includes('<!-- deployment-metadata -->'));
}

const graph = JSON.parse(html.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(
  graph['@graph'].map((n) => n['@type']),
  ['WebSite', 'SoftwareApplication', 'Organization', 'Person', 'FAQPage'],
);
assert.equal(graph['@graph'][2].name, `${graph['@graph'][0].name}`, 'zh Organization name matches zh display name');
assert.equal(graph['@graph'][3].url, 'https://github.com/qingsongcui');
assert.ok(graph['@graph'][3].sameAs.includes('https://x.com/George_ONair'));
assert.equal(graph['@graph'][4].mainEntity.length, 9, 'nine FAQ entities in zh JSON-LD');

const enGraph = JSON.parse(enHtml.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(
  enGraph['@graph'].map((n) => n['@type']),
  ['WebSite', 'SoftwareApplication', 'Organization', 'Person', 'FAQPage'],
);
assert.equal(enGraph['@graph'][0].inLanguage, 'en');
assert.equal(enGraph['@graph'][2].name, enGraph['@graph'][1].name, 'en Organization name uses un-suffixed English brand');
assert.equal(enGraph['@graph'][4].mainEntity.length, 9, 'nine FAQ entities in en JSON-LD');

const guideGraph = JSON.parse(guide.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(guideGraph['@graph'].map((n) => n['@type']), ['TechArticle', 'FAQPage', 'BreadcrumbList', 'Person']);
assert.equal(guideGraph['@graph'][1].mainEntity.length, 3, 'three troubleshooting FAQ entities in zh guide JSON-LD');
assert.ok(guideGraph['@graph'][0].dateModified >= '2026-11-15', 'guide dateModified is updated to >= 2026-11-15');
const enGuideGraph = JSON.parse(enGuide.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(enGuideGraph['@graph'].map((n) => n['@type']), ['TechArticle', 'FAQPage', 'BreadcrumbList', 'Person']);
assert.equal(enGuideGraph['@graph'][1].mainEntity.length, 3, 'three troubleshooting FAQ entities in en guide JSON-LD');

const compareGraph = JSON.parse(compare.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(compareGraph['@graph'].map((n) => n['@type']), ['TechArticle', 'ItemList', 'FAQPage', 'BreadcrumbList', 'Person']);
assert.equal(compareGraph['@graph'][1].numberOfItems, 5, 'five compared options in zh ItemList');
assert.ok(compareGraph['@graph'][1].itemListElement[2].name.includes('ContextCore'), 'own product ranked #3 (not #1) in zh ItemList');
assert.ok(compareGraph['@graph'][1].itemListElement[2].url.includes('#option-3'), 'option #3 ListItem has #option-3 fragment URL');
assert.ok(compareGraph['@graph'][1].itemListElement[2].item, 'option #3 ListItem references SoftwareApplication node');
assert.ok(!compareGraph['@graph'][1].itemListElement[0].name.includes('ContextCore'), 'own product NOT ranked #1 in zh ItemList');
assert.equal(compareGraph['@graph'][2].mainEntity.length, 6, 'six FAQ entities in zh compare JSON-LD');
assert.ok(compareGraph['@graph'][0].dateModified >= '2026-11-15', 'compare dateModified is updated to >= 2026-11-15');

const enCompareGraph = JSON.parse(enCompare.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(enCompareGraph['@graph'].map((n) => n['@type']), ['TechArticle', 'ItemList', 'FAQPage', 'BreadcrumbList', 'Person']);
assert.equal(enCompareGraph['@graph'][1].numberOfItems, 5, 'five compared options in en ItemList');
assert.ok(enCompareGraph['@graph'][1].itemListElement[2].name.includes('ContextCore'), 'own product ranked #3 (not #1) in en ItemList');
assert.ok(enCompareGraph['@graph'][1].itemListElement[2].url.includes('#option-3'), 'option #3 ListItem has #option-3 fragment URL');
assert.ok(!enCompareGraph['@graph'][1].itemListElement[0].name.includes('ContextCore'), 'own product NOT ranked #1 in en ItemList');
assert.equal(enCompareGraph['@graph'][2].mainEntity.length, 6, 'six FAQ entities in en compare JSON-LD');

// SEO / GEO Baseline & Anti-Regression Veto Guardrails (Titles <=60 chars, Meta descriptions tight, Version aligned to v1.0.0-beta.2, 4-Element Hero, Plain-Text $0 Pricing, Dated Fact Sheet, Real Limitations, Creator E-E-A-T, Internal Links & Content Floor)
for (const [label, doc] of [
  ['index.html', html],
  ['en/index.html', enHtml],
  ['guide.html', guide],
  ['en/guide.html', enGuide],
  ['compare.html', compare],
  ['en/compare.html', enCompare],
]) {
  const titleText = doc.match(/<title>(.*?)<\/title>/s)[1].replace(/&amp;/g, '&').trim();
  assert.ok(titleText.length >= 20 && titleText.length <= 60, `title length (${titleText.length}) must be 20..60 chars in ${label}: "${titleText}"`);
  const descText = doc.match(/<meta name="description" content="([^"]*)"/)[1];
  if (label.startsWith('en/')) {
    assert.ok(descText.length >= 80 && descText.length <= 155, `EN meta description length (${descText.length}) must be 80..155 chars in ${label}`);
  } else {
    assert.ok(descText.length >= 45 && descText.length <= 90, `ZH meta description length (${descText.length}) must be 45..90 chars in ${label}: "${descText}"`);
  }
  assert.ok(!doc.includes('1.0.0-beta.1') && !doc.includes('1.0.0b1'), `no stale beta.1 version string in ${label}`);
  assert.ok(!doc.includes('💡') && !doc.includes('📌'), `no decorative emojis in ${label}`);
  const visibleOnly = doc.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();
  assert.ok(visibleOnly.includes('$0'), `plain-text $0 pricing must appear in visible HTML of ${label}`);
  assert.ok(visibleOnly.includes('MIT'), `MIT license must appear in visible HTML of ${label}`);
  assert.ok(visibleOnly.includes('2026-11-15'), `fact-checked date 2026-11-15 must appear in visible HTML of ${label}`);
  assert.ok(visibleOnly.includes('Windows') && visibleOnly.includes('Linux'), `honest OS limitations must appear in visible HTML of ${label}`);
  assert.ok(visibleOnly.includes('xattr -cr'), `first-launch xattr -cr note must appear in visible HTML of ${label}`);
  assert.ok(visibleOnly.includes('Qingsong Cui') && visibleOnly.includes('George_ONair'), `creator attribution must appear in visible HTML of ${label}`);
  const minChars = label.includes('guide') ? 2200 : label.includes('compare') ? 3500 : 3200;
  assert.ok(visibleOnly.length >= minChars, `anti-shrink content floor (${visibleOnly.length} >= ${minChars}) in ${label}`);
}

for (const [label, doc] of [['index.html', html], ['en/index.html', enHtml]]) {
  assert.match(doc, /id="fact-sheet"/, `Official Fact Sheet table exists in ${label}`);
  assert.match(doc, /id="about-privacy"/, `About & Privacy box exists in ${label}`);
  assert.ok(doc.includes('1.0.0-beta.2'), `current version 1.0.0-beta.2 appears in ${label}`);
  assert.ok(doc.includes('127.0.0.1:8420/mcp/') && doc.includes('contextcore'), `MCP endpoint & key contextcore documented in ${label}`);
  const bodyInternalLinks = [...doc.matchAll(/<a\b[^>]*class="inline-link"[^>]*href="(?:#|\.?\/(?:guide|compare)\.html)[^"]*"/g)];
  assert.ok(bodyInternalLinks.length >= 5, `at least 5 descriptive inline internal links in ${label} (found ${bodyInternalLinks.length})`);
}

for (const [label, doc] of [['guide.html', guide], ['en/guide.html', enGuide]]) {
  assert.ok(doc.includes('1.0.0-beta.2'), `current version 1.0.0-beta.2 appears in ${label}`);
  assert.ok(doc.includes('127.0.0.1:8420/mcp/') && doc.includes('contextcore'), `MCP endpoint & key contextcore documented in ${label}`);
  for (const host of ['Claude Code', 'Codex', 'Cursor', 'Antigravity', 'OMP', 'OpenCode']) {
    assert.ok(doc.includes(host), `core host ${host} appears in ${label}`);
  }
}

for (const [label, doc, pickerAttr] of [['compare.html', compare, 'zh'], ['en/compare.html', enCompare, 'en']]) {
  assert.match(doc, new RegExp(`data-picker="${pickerAttr}"`), `interactive stack picker exists in ${label}`);
  assert.equal((doc.match(/<article class="option-card\b/g) || []).length, 5, `5 compared option cards in ${label}`);
  for (let i = 1; i <= 5; i += 1) {
    assert.ok(doc.includes(`id="option-${i}"`), `option anchor #option-${i} exists in ${label}`);
  }
  const compareInternalLinks = [...doc.matchAll(/<a\b[^>]*class="inline-link"[^>]*href="\.\/(?:#|guide\.html)[^"]*"/g)];
  assert.ok(compareInternalLinks.length >= 5, `at least 5 descriptive inline internal links in ${label} (found ${compareInternalLinks.length})`);
}
assert.match(enCompare, /How can I safely sync MCP servers between Claude Code and Codex\?[\s\S]*?Keychain/, 'en/compare.html MCP sync FAQ preserves Keychain & tool attribution');

assert.match(guide, /class="language-switch"/);
assert.match(enGuide, /class="language-switch"/);
assert.match(compare, /class="language-switch"/);
assert.match(enCompare, /class="language-switch"/);

// GEO llms.txt & llms-full.txt checks
const llmsSummaryLine = llms.split('\n').find((line) => line.startsWith('> '));
assert.ok(llmsSummaryLine && !cjk.test(llmsSummaryLine), 'llms.txt blockquote summary is pure English');
assert.match(llms, /brew install --cask qingsongcui\/tap\/senrelay/);
assert.match(llms, /pip install contextcore/);
assert.match(llms, /http:\/\/127\.0\.0\.1:8420\/mcp\//);
assert.match(llms, /senrelay-mcp/);
assert.ok(llms.includes('1.0.0-beta.2') && !llms.includes('1.0.0-beta.1'), 'llms.txt reflects v1.0.0-beta.2');
assert.ok(!llms.includes('💡') && !llms.includes('📌'), 'no decorative emojis in llms.txt');
assert.match(llmsFull, /must_not_execute=true/);
assert.match(llmsFull, /read_conversation_messages/);
assert.match(llmsFull, /Extended Config Host/);
assert.match(llmsFull, /5-Way Comparison/);
assert.match(llmsFull, /中文核心摘要/);
assert.ok(!llmsFull.includes('1.0.0-beta.1'), 'llms-full.txt has no stale beta.1');
assert.ok(!llmsFull.includes('💡') && !llmsFull.includes('📌'), 'no decorative emojis in llms-full.txt');
assert.ok(!llmsFull.includes('6-point'), 'no stale 6-point handoff card reference in llms-full.txt');

const production = html.includes('content="index, follow"');
if (production) {
  for (const doc of [html, enHtml, guide, enGuide, compare, enCompare]) {
    assert.match(doc, /content="index, follow"/);
    assert.match(doc, /rel="canonical"/);
    assert.match(doc, /hreflang="zh-CN"/);
    assert.match(doc, /hreflang="zh"/);
    assert.match(doc, /hreflang="en"/);
    assert.match(doc, /hreflang="x-default"/);
    assert.match(doc, /property="og:image"/);
    assert.match(doc, /property="og:image:width" content="1536"/);
  }
  const sitemap = await readFile(path.join(root, 'sitemap.xml'), 'utf8');
  assert.match(sitemap, /xmlns:xhtml="http:\/\/www\.w3\.org\/1999\/xhtml"/);
  assert.equal((sitemap.match(/<url>/g) || []).length, 6, 'six canonical URLs in sitemap.xml');
  assert.equal((sitemap.match(/<lastmod>\d{4}-\d{2}-\d{2}<\/lastmod>/g) || []).length, 6, 'valid lastmod dates');
  assert.ok(sitemap.includes('<lastmod>2026-11-15</lastmod>'), 'sitemap lastmod aligned to 2026-11-15');
  assert.match(sitemap, /xhtml:link rel="alternate" hreflang="en"/);
  const robots = await readFile(path.join(root, 'robots.txt'), 'utf8');
  for (const bot of ['Googlebot', 'OAI-SearchBot', 'GPTBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended', 'Applebot-Extended', 'Bytespider', 'Baiduspider']) {
    assert.ok(robots.includes(`User-agent: ${bot}`), `robots.txt explicitly declares ${bot}`);
  }
  assert.match(robots, /Sitemap: /);
} else {
  for (const doc of [html, enHtml, guide, enGuide, compare, enCompare]) {
    assert.match(doc, /content="noindex, nofollow"/);
    assert.ok(!doc.includes('rel="canonical"'));
  }
  assert.equal((await readFile(path.join(root, 'robots.txt'), 'utf8')).trim(), 'User-agent: *\nDisallow: /');
}

for (const doc of [html, enHtml]) {
  const checklist = doc.match(/<ol class="handoff-checklist">([\s\S]*?)<\/ol>/)[1];
  assert.equal((checklist.match(/<li>/g) || []).length, 6, 'six semantic handoff entries');
  assert.equal((checklist.match(/<h4>/g) || []).length, 6, 'each handoff entry has a heading');
  for (const [, body] of doc.matchAll(/<details\b[^>]*>([\s\S]*?)<\/details>/g)) {
    assert.match(body, /^\s*<summary\b[^>]*>[\s\S]+?<\/summary>/, 'disclosure starts with a labeled native summary');
  }
  for (const [, tag] of doc.matchAll(/(<a\b[^>]*target="_blank"[^>]*>)/g)) {
    assert.match(tag, /rel="[^"]*\bnoopener\b[^"]*"/, 'external-window links prevent opener access');
  }
  for (const [, tag] of doc.matchAll(/(<div\b[^>]*\baria-label="[^"]*"[^>]*>)/g)) {
    assert.match(tag, /\brole="(?:img|group|navigation)"/, 'named containers use an appropriate explicit role');
  }
}

console.log(
  `Static checks passed: zh + static /en/ pages, 404.html, raster favicons, 4-node JSON-LD, hreflang, AI robots.txt, and llms.txt (${
    production ? 'deployment' : 'preview'
  }).`,
);
