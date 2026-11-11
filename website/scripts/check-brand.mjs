import assert from 'node:assert/strict';
import { mkdtemp, cp, readFile, writeFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawnSync } from 'node:child_process';
const root = fileURLToPath(new URL('../', import.meta.url));
const temp = await mkdtemp(path.join(os.tmpdir(), 'contextcore-site-brand-'));
try {
  for (const entry of ['index.html', 'guide.html', '404.html', 'assets', 'scripts', 'brand.config.mjs']) await cp(path.join(root, entry), path.join(temp, entry), { recursive: true });
  const config = (await readFile(path.join(temp, 'brand.config.mjs'), 'utf8'))
    .replace("name: 'Senrelay'", "name: 'Renamed Context'")
    .replace("chineseName: '续境'", "chineseName: '改名境'");
  await writeFile(path.join(temp, 'brand.config.mjs'), config);
  let result = spawnSync(process.execPath, [path.join(temp, 'scripts/build.mjs')], { env: { ...process.env, SITE_URL: '' }, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const html = await readFile(path.join(temp, 'dist/index.html'), 'utf8');
  assert.match(html, /<title>Renamed Context · 改名境 —/);
  assert.ok(html.includes('href="https://github.com/qingsongcui/ContextCore"'));
  assert.ok(html.includes('href="https://github.com/qingsongcui/ContextCore/blob/main/LICENSE"'));
  assert.ok(!html.includes('github.com/qingsongcui/Renamed'));
  assert.ok(!html.includes('>Senrelay<'));
  assert.ok(!html.includes('为什么 Senrelay'));

  const enHtml = await readFile(path.join(temp, 'dist/en/index.html'), 'utf8');
  assert.match(enHtml, /<title>Renamed Context —/);
  assert.ok(!enHtml.includes('>Senrelay<'));

  const guide = await readFile(path.join(temp, 'dist/guide.html'), 'utf8');
  assert.ok(guide.includes('Renamed Context'));
  assert.ok(guide.includes('改名境'));
  assert.ok(!guide.includes('Senrelay · 续境'));

  const enGuide = await readFile(path.join(temp, 'dist/en/guide.html'), 'utf8');
  assert.match(enGuide, /<title>Renamed Context — Local Setup &amp; Workflow Guide<\/title>/);
  assert.ok(!enGuide.includes('Senrelay —'));

  const notFound = await readFile(path.join(temp, 'dist/404.html'), 'utf8');
  assert.ok(notFound.includes('Renamed Context'));
  assert.ok(!notFound.includes('Senrelay · 续境'));

  const { pathToFileURL } = await import('node:url');
  const i18n = await import(pathToFileURL(path.join(temp, 'dist/assets/i18n.js')).href);
  assert.match(i18n.metaEn.title, /^Renamed Context —/);
  assert.ok(i18n.metaEn.ogTitle.includes('Renamed Context'));
  assert.ok(!i18n.metaEn.title.includes('Senrelay'), 'no stale brand in metaEn.title');
  assert.ok(i18n.htmlEn['intro.body'].includes('Renamed Context brings'), 'brand updated in htmlEn intro.body');
  assert.ok(!i18n.htmlEn['intro.body'].includes('Senrelay'), 'no stale brand in htmlEn intro.body');
  assert.ok(i18n.htmlEn.start.includes('./guide.html'), 'guide link in htmlEn.start');
  assert.equal(i18n.textEn['为什么 Renamed Context'], 'Why Renamed Context');
  assert.equal(i18n.attrEn['Renamed Context 改名境首页'], 'Renamed Context home');
  assert.equal(i18n.attrEn['向下了解 Renamed Context'], 'Learn more about Renamed Context');
  assert.ok(!i18n.textEn['为什么 Senrelay'], 'old brand key removed from textEn');

  // Meaningful runtime behavior: verify that every Chinese text node in the rebuilt HTML has a matching key in textEn
  const withoutScripts = html.replace(/<script[\s\S]*?<\/script>/g, '').replace(/<style[\s\S]*?<\/style>/g, '').replace(/<!--[\s\S]*?-->/g, '');
  const cjk = /[\u4e00-\u9fff]/;
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
  const texts = extractTextChunks(withoutScripts)
    .map(part => part.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ').trim())
    .filter(part => part && cjk.test(part));
  for (const text of new Set(texts)) {
    assert.ok(Object.prototype.hasOwnProperty.call(i18n.textEn, text), `runtime dictionary missing key in renamed build: ${text}`);
  }

  // Test custom github URL consistency across HTML and English dictionary
  const customConfig = (await readFile(path.join(temp, 'brand.config.mjs'), 'utf8'))
    .replace("github: 'https://github.com/qingsongcui/ContextCore'", "github: 'https://github.com/custom-team/custom-repo'");
  await writeFile(path.join(temp, 'brand.config.mjs'), customConfig);
  result = spawnSync(process.execPath, [path.join(temp, 'scripts/build.mjs')], { env: { ...process.env, SITE_URL: '' }, encoding: 'utf8' });
  assert.equal(result.status, 0, result.stderr);
  const customHtml = await readFile(path.join(temp, 'dist/index.html'), 'utf8');
  assert.ok(customHtml.includes('href="https://github.com/custom-team/custom-repo"'));
  const customI18n = await import(pathToFileURL(path.join(temp, 'dist/assets/i18n.js')).href + `?cachebust=${Date.now()}`);
  assert.ok(customI18n.htmlEn.start.includes('href="https://github.com/custom-team/custom-repo"'), 'custom github URL propagates to English start links');
  console.log('Brand rename check passed: name, metadata, English dictionaries, runtime key coverage, and custom GitHub URLs stay consistent.');
} finally { await rm(temp, { recursive: true, force: true }); }
