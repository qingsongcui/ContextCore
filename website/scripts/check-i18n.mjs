import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
let i18nModule;
try {
  i18nModule = await import(pathToFileURL(path.join(root, 'dist/assets/i18n.js')).href);
} catch {
  i18nModule = await import('../assets/i18n.js');
}
const { attrEn, htmlEn, metaEn, textEn } = i18nModule;
const html = await readFile(path.join(root, 'dist/index.html'), 'utf8');
const cjk = /[\u4e00-\u9fff]/;

assert.match(html, /class="language-switch"/, 'language switch exists');
assert.match(html, /data-lang="zh"/);
assert.match(html, /data-lang="en"/);
assert.match(html, /data-i18n-html="hero.title"/);

const withoutScripts = html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '').replace(/<!--[\s\S]*?-->/g, '');
// Track element nesting: translated blocks can contain more than one nested div.
const voidTags = new Set(['area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr']);
function extractTextChunks(source) {
  const stack = [], chunks = [];
  let cursor = 0;
  for (const match of source.matchAll(/<\/?([a-zA-Z][\w:-]*)\b[^>]*>/g)) {
    const skipping = stack.some(entry => entry.skip);
    if (!skipping) chunks.push(source.slice(cursor, match.index));
    const tag = match[1].toLowerCase();
    if (match[0].startsWith('</')) {
      const index = stack.findLastIndex(entry => entry.tag === tag);
      if (index !== -1) stack.splice(index);
    } else if (!voidTags.has(tag) && !match[0].endsWith('/>')) {
      stack.push({ tag, skip: skipping || /\bdata-i18n-html=/.test(match[0]) || /\bclass="language-switch"/.test(match[0]) });
    }
    cursor = match.index + match[0].length;
  }
  if (!stack.some(entry => entry.skip)) chunks.push(source.slice(cursor));
  return chunks;
}

// Regression at the extraction seam: retain separate text nodes around nested
// overrides and void tags. No page wording is pinned by this synthetic fixture.
assert.deepEqual(
  extractTextChunks('<main><p>alpha<br>beta</p><div data-i18n-html="sample"><div>skip<div>skip</div></div>skip</div><div class="language-switch"><button>skip</button></div><p>gamma<span>delta</span></p></main>').filter(Boolean),
  ['alpha', 'beta', 'gamma', 'delta'],
  'extract text nodes independently without leaking nested translation overrides',
);
const texts = extractTextChunks(withoutScripts)
  .map(part => part.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ').trim())
  .filter(part => part && cjk.test(part));

const attrPairs = [];
for (const match of withoutScripts.matchAll(/([\w-]+)="([^"]*[\u4e00-\u9fff][^"]*)"/g)) {
  if (match[1] === 'content') continue;
  const pair = `${match[1]}=${match[2]}`;
  if (!attrPairs.includes(pair)) attrPairs.push(pair);
}

const missingText = [...new Set(texts)].filter(text => !Object.prototype.hasOwnProperty.call(textEn, text));
const missingAttr = attrPairs.filter(pair => !Object.prototype.hasOwnProperty.call(attrEn, pair.split('=').slice(1).join('=')));
const chineseValues = Object.entries(textEn).filter(([, value]) => cjk.test(value) && value !== '');
const htmlKeys = [...html.matchAll(/data-i18n-html="([^"]+)"/g)].map(match => match[1]);
const missingHtml = [...new Set(htmlKeys)].filter(key => !htmlEn[key]);
const missingMeta = ['title', 'description', 'ogTitle', 'ogDescription', 'twitterDescription', 'locale', 'siteDescription', 'jsonDescription'].filter(key => !metaEn[key]);

assert.equal(missingText.length, 0, `missing English for static text: ${missingText.join(' | ')}`);
assert.equal(missingAttr.length, 0, `missing English for attributes: ${missingAttr.join(' | ')}`);
assert.equal(missingHtml.length, 0, `missing English for HTML blocks: ${missingHtml.join(' | ')}`);
assert.equal(missingMeta.length, 0, `missing English metadata: ${missingMeta.join(' | ')}`);
assert.equal(chineseValues.length, 0, `Chinese left in English values: ${chineseValues.map(([key]) => key).join(' | ')}`);
for (const [key, value] of Object.entries(htmlEn)) {
  assert.ok(!cjk.test(value), `Chinese left in English HTML block: ${key}`);
}
for (const [key, value] of Object.entries(metaEn)) {
  assert.ok(!cjk.test(value), `Chinese left in English metadata: ${key}`);
}
assert.ok(!htmlEn.start.includes('/blob/main/app/README.md'), 'no link to superseded public app/README.md in English start block');
assert.ok(htmlEn.start.includes('./guide.html'), 'English start block links to local guide');

console.log(`Website i18n passed: ${new Set(texts).size} text nodes, ${attrPairs.length} attributes, ${new Set(htmlKeys).size} HTML blocks, ${Object.keys(textEn).length} English strings.`);
