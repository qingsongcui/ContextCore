import assert from 'node:assert/strict';
import { readFile, access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = fileURLToPath(new URL('../dist/', import.meta.url));
const html = await readFile(path.join(root, 'index.html'), 'utf8');
const enHtml = await readFile(path.join(root, 'en/index.html'), 'utf8');
const guide = await readFile(path.join(root, 'guide.html'), 'utf8');
const enGuide = await readFile(path.join(root, 'en/guide.html'), 'utf8');
const notFound = await readFile(path.join(root, '404.html'), 'utf8');
const llms = await readFile(path.join(root, 'llms.txt'), 'utf8');
const llmsFull = await readFile(path.join(root, 'llms-full.txt'), 'utf8');

assert.equal((html.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in index.html');
assert.equal((enHtml.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in en/index.html');
assert.equal((guide.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in guide.html');
assert.equal((enGuide.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in en/guide.html');
assert.equal((notFound.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in 404.html');

assert.match(html, /<html lang="zh-CN">/);
assert.match(enHtml, /<html lang="en" data-static-locale="en">/);
assert.match(guide, /<html lang="zh-CN">/);
assert.match(enGuide, /<html lang="en">/);
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

for (const [label, doc] of [['index.html', html], ['en/index.html', enHtml]]) {
  const ids = [...doc.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
  assert.equal(ids.length, new Set(ids).size, `IDs unique in ${label}`);
  for (const [, target] of doc.matchAll(/href="#([^"\s]+)"/g)) {
    assert.ok(ids.includes(target), `anchor ${target} exists in ${label}`);
  }
  for (const [, controls] of doc.matchAll(/aria-controls="([^"]+)"/g)) {
    for (const id of controls.split(/\s+/)) assert.ok(ids.includes(id), `controlled element ${id} exists in ${label}`);
  }
}

for (const [, asset] of html.matchAll(/(?:href|src)="\.\/([^"\s]+)"/g)) {
  await access(path.join(root, asset));
}
for (const [, asset] of enHtml.matchAll(/(?:href|src)="(\.\.?\/[^"\s]+)"/g)) {
  await access(path.resolve(root, 'en', asset));
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

const cjk = /[\u4e00-\u9fff]/;
const stripSwitchAndScripts = (doc) =>
  doc
    .replace(/<script[\s\S]*?<\/script>/g, '')
    .replace(/<style[\s\S]*?<\/style>/g, '')
    .replace(/<div class="language-switch"[^>]*>[\s\S]*?<\/div>/g, '')
    .replace(/\bdata-(?:zh|aria-zh)="[^"]*"/g, '');
assert.ok(!cjk.test(stripSwitchAndScripts(enHtml)), 'no untranslated CJK in static en/index.html');
assert.ok(!cjk.test(stripSwitchAndScripts(enGuide)), 'no untranslated CJK in static en/guide.html');

assert.ok(!html.includes('/blob/main/app/README.md'), 'no link to superseded public app/README.md');
assert.ok(html.includes('href="./guide.html"'), 'links to verified local guide.html');
assert.ok(enHtml.includes('href="./guide.html"'), 'English homepage links to en/guide.html');
for (const doc of [html, enHtml, guide, enGuide, notFound]) {
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
  ['WebSite', 'SoftwareApplication', 'Organization', 'FAQPage'],
);
assert.equal(graph['@graph'][3].mainEntity.length, 6, 'six FAQ entities in zh JSON-LD');

const enGraph = JSON.parse(enHtml.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(
  enGraph['@graph'].map((n) => n['@type']),
  ['WebSite', 'SoftwareApplication', 'Organization', 'FAQPage'],
);
assert.equal(enGraph['@graph'][0].inLanguage, 'en');
assert.equal(enGraph['@graph'][3].mainEntity.length, 6, 'six FAQ entities in en JSON-LD');

const guideGraph = JSON.parse(guide.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(guideGraph['@graph'].map((n) => n['@type']), ['TechArticle', 'BreadcrumbList']);
const enGuideGraph = JSON.parse(enGuide.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(enGuideGraph['@graph'].map((n) => n['@type']), ['TechArticle', 'BreadcrumbList']);

assert.match(guide, /class="language-switch"/);
assert.match(enGuide, /class="language-switch"/);

// GEO llms.txt & llms-full.txt checks
const llmsSummaryLine = llms.split('\n').find((line) => line.startsWith('> '));
assert.ok(llmsSummaryLine && !cjk.test(llmsSummaryLine), 'llms.txt blockquote summary is pure English');
assert.match(llms, /brew install --cask qingsongcui\/tap\/senrelay/);
assert.match(llms, /pip install contextcore/);
assert.match(llmsFull, /must_not_execute=true/);
assert.match(llmsFull, /Extended Config Host/);

const production = html.includes('content="index, follow"');
if (production) {
  for (const doc of [html, enHtml, guide, enGuide]) {
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
  assert.equal((sitemap.match(/<url>/g) || []).length, 4, 'four canonical URLs in sitemap.xml');
  assert.equal((sitemap.match(/<lastmod>\d{4}-\d{2}-\d{2}<\/lastmod>/g) || []).length, 4, 'valid lastmod dates');
  assert.match(sitemap, /xhtml:link rel="alternate" hreflang="en"/);
  const robots = await readFile(path.join(root, 'robots.txt'), 'utf8');
  for (const bot of ['Googlebot', 'OAI-SearchBot', 'GPTBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended', 'Applebot-Extended', 'Bytespider']) {
    assert.ok(robots.includes(`User-agent: ${bot}`), `robots.txt explicitly declares ${bot}`);
  }
  assert.match(robots, /Sitemap: /);
} else {
  for (const doc of [html, enHtml, guide, enGuide]) {
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
