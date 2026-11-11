import assert from 'node:assert/strict';
import { readFile, access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
const root = fileURLToPath(new URL('../dist/', import.meta.url));
const html = await readFile(path.join(root, 'index.html'), 'utf8');
assert.equal((html.match(/<h1\b/g) || []).length, 1, 'exactly one h1');
assert.match(html, /<html lang="zh-CN">/);
assert.match(html, /name="description"/);
assert.match(html, /name="twitter:card"/);
assert.match(html, /class="handoff-checklist"/);

const css = await readFile(path.join(root, 'assets/site.css'), 'utf8');
assert.match(css, /prefers-reduced-motion/);
assert.match(css, /focus-visible/);
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]);
assert.equal(ids.length, new Set(ids).size, 'IDs unique');
for (const [, target] of html.matchAll(/href="#([^"\s]+)"/g)) assert.ok(ids.includes(target), `anchor ${target} exists`);
for (const [, asset] of html.matchAll(/(?:href|src)="\.\/([^"\s]+)"/g)) await access(path.join(root, asset));
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
const guide = await readFile(path.join(root, 'guide.html'), 'utf8');
// Run-guide commands must name scripts that actually exist in this checkout.
// This catches stale instructions such as a removed staging-build alias.
const repository = fileURLToPath(new URL('../../', import.meta.url));
for (const [, prefix, script] of guide.matchAll(/npm --prefix ([\w/-]+) run ([\w:-]+)/g)) {
  const manifest = JSON.parse(await readFile(path.join(repository, prefix, 'package.json'), 'utf8'));
  assert.ok(Object.hasOwn(manifest.scripts || {}, script), `guide command exists: ${prefix} run ${script}`);
}
assert.equal((guide.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in guide');
assert.match(guide, /<html\b/);
assert.ok(!html.includes('/blob/main/app/README.md'), 'no link to superseded public app/README.md');
assert.ok(html.includes('href="./guide.html"'), 'links to verified local guide.html');
assert.ok(!html.includes('brand-mark.svg'));
assert.ok(!html.includes('favicon.svg'));
const graph = JSON.parse(html.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.equal(graph['@graph'].length, 2);
assert.ok(!html.includes('aggregateRating'));
assert.ok(!html.includes('<!-- deployment-metadata -->'));
assert.ok(!guide.includes('<!-- deployment-metadata -->'));
assert.match(guide, /class="language-switch"/);
const production = html.includes('content="index, follow"');
if (production) {
  assert.match(html, /rel="canonical"/);
  assert.match(html, /property="og:image"/);
  assert.match(guide, /content="index, follow"/);
  assert.match(guide, /rel="canonical"/);
  await access(path.join(root, 'sitemap.xml'));
} else {
  assert.match(html, /content="noindex, nofollow"/);
  assert.ok(!html.includes('rel="canonical"'));
  assert.match(guide, /content="noindex, nofollow"/);
  assert.ok(!guide.includes('rel="canonical"'));
  assert.equal((await readFile(path.join(root, 'robots.txt'), 'utf8')).trim(), 'User-agent: *\nDisallow: /');
}
const checklist = html.match(/<ol class="handoff-checklist">([\s\S]*?)<\/ol>/)[1];
assert.equal((checklist.match(/<li>/g) || []).length, 6, 'six semantic handoff entries');
assert.equal((checklist.match(/<h4>/g) || []).length, 6, 'each handoff entry has a heading');
for (const [, body] of html.matchAll(/<details\b[^>]*>([\s\S]*?)<\/details>/g)) {
  assert.match(body, /^\s*<summary\b[^>]*>[\s\S]+?<\/summary>/, 'disclosure starts with a labeled native summary');
}
for (const [, controls] of html.matchAll(/aria-controls="([^"]+)"/g)) {
  for (const id of controls.split(/\s+/)) assert.ok(ids.includes(id), `controlled element ${id} exists`);
}
for (const [, tag] of html.matchAll(/(<a\b[^>]*target="_blank"[^>]*>)/g)) {
  assert.match(tag, /rel="[^"]*\bnoopener\b[^"]*"/, 'external-window links prevent opener access');
}
for (const [, tag] of html.matchAll(/(<div\b[^>]*\baria-label="[^"]*"[^>]*>)/g)) {
  assert.match(tag, /\brole="(?:img|group|navigation)"/, 'named containers use an appropriate explicit role');
}
console.log(`Static checks passed: semantic headings, disclosures and handoff structure, internal anchors, controlled elements, assets, JSON-LD and metadata (${production ? 'deployment' : 'preview'}).`);
