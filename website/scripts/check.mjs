import assert from 'node:assert/strict';
import { readFile, access } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import brand from '../brand.config.mjs';

const releaseDownloadUrl = `${brand.github}/releases/download/v${brand.releaseVersion}/Senrelay-macOS.zip`;

const root = fileURLToPath(new URL('../dist/', import.meta.url));
const enHtml = await readFile(path.join(root, 'index.html'), 'utf8');
const html = await readFile(path.join(root, 'zh/index.html'), 'utf8');
const enGuide = await readFile(path.join(root, 'guide.html'), 'utf8');
const guide = await readFile(path.join(root, 'zh/guide.html'), 'utf8');
const enCompare = await readFile(path.join(root, 'compare.html'), 'utf8');
const compare = await readFile(path.join(root, 'zh/compare.html'), 'utf8');
const enHosts = await readFile(path.join(root, 'hosts.html'), 'utf8');
const hosts = await readFile(path.join(root, 'zh/hosts.html'), 'utf8');
const enRelay = await readFile(path.join(root, 'relay-workflow.html'), 'utf8');
const relay = await readFile(path.join(root, 'zh/relay-workflow.html'), 'utf8');
const legacyEnHome = await readFile(path.join(root, 'en/index.html'), 'utf8');
const legacyEnGuide = await readFile(path.join(root, 'en/guide.html'), 'utf8');
const legacyEnCompare = await readFile(path.join(root, 'en/compare.html'), 'utf8');
const legacyEnHosts = await readFile(path.join(root, 'en/hosts.html'), 'utf8');
const legacyEnRelay = await readFile(path.join(root, 'en/relay-workflow.html'), 'utf8');
const notFound = await readFile(path.join(root, '404.html'), 'utf8');
const llms = await readFile(path.join(root, 'llms.txt'), 'utf8');
const llmsFull = await readFile(path.join(root, 'llms-full.txt'), 'utf8');
const pricingMd = await readFile(path.join(root, 'pricing.md'), 'utf8');
const indexMd = await readFile(path.join(root, 'index.md'), 'utf8');
const zhIndexMd = await readFile(path.join(root, 'zh/index.md'), 'utf8');
const guideMd = await readFile(path.join(root, 'guide.md'), 'utf8');
const compareMd = await readFile(path.join(root, 'compare.md'), 'utf8');
const hostsMd = await readFile(path.join(root, 'hosts.md'), 'utf8');
const relayMd = await readFile(path.join(root, 'relay-workflow.md'), 'utf8');

for (const [label, doc, expectedLangAttr] of [
  ['index.html', enHtml, '<html lang="en" data-static-locale="en">'],
  ['zh/index.html', html, '<html lang="zh-CN" data-static-locale="zh">'],
  ['guide.html', enGuide, '<html lang="en">'],
  ['zh/guide.html', guide, '<html lang="zh-CN">'],
  ['compare.html', enCompare, '<html lang="en">'],
  ['zh/compare.html', compare, '<html lang="zh-CN">'],
  ['hosts.html', enHosts, '<html lang="en">'],
  ['zh/hosts.html', hosts, '<html lang="zh-CN">'],
  ['relay-workflow.html', enRelay, '<html lang="en">'],
  ['zh/relay-workflow.html', relay, '<html lang="zh-CN">'],
]) {
  assert.equal((doc.match(/<h1\b/g) || []).length, 1, `exactly one h1 in ${label}`);
  assert.ok(doc.includes(expectedLangAttr), `expected lang attribute in ${label}`);
}
assert.equal((notFound.match(/<h1\b/g) || []).length, 1, 'exactly one h1 in 404.html');
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
  ['index.html', enHtml],
  ['zh/index.html', html],
  ['guide.html', enGuide],
  ['zh/guide.html', guide],
  ['compare.html', enCompare],
  ['zh/compare.html', compare],
  ['hosts.html', enHosts],
  ['zh/hosts.html', hosts],
  ['relay-workflow.html', enRelay],
  ['zh/relay-workflow.html', relay],
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

for (const [pageLabel, pageDoc] of [['index.html', enHtml], ['guide.html', enGuide], ['compare.html', enCompare], ['hosts.html', enHosts], ['relay-workflow.html', enRelay]]) {
  for (const [, asset] of pageDoc.matchAll(/(?:href|src)="\.\/([^"\s]*)"/g)) {
    const [relPath, hash] = asset.split('#');
    let targetPath = path.join(root, relPath || 'index.html');
    if (relPath.endsWith('/')) targetPath = path.join(targetPath, 'index.html');
    await access(targetPath);
    if (hash && targetPath.endsWith('.html')) {
      const targetDoc = await readFile(targetPath, 'utf8');
      assert.ok(targetDoc.includes(`id="${hash}"`), `cross-page anchor #${hash} from ${pageLabel} exists in ${targetPath}`);
    }
  }
}
for (const [pageLabel, pageDoc] of [['zh/index.html', html], ['zh/guide.html', guide], ['zh/compare.html', compare], ['zh/hosts.html', hosts], ['zh/relay-workflow.html', relay]]) {
  for (const [, asset] of pageDoc.matchAll(/(?:href|src)="(\.\.?\/[^"\s]*)"/g)) {
    const [relPath, hash] = asset.split('#');
    let resolved = path.resolve(root, 'zh', relPath || 'index.html');
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

for (const [stubLabel, stubDoc, expectedRelTarget] of [
  ['en/index.html', legacyEnHome, '../'],
  ['en/guide.html', legacyEnGuide, '../guide.html'],
  ['en/compare.html', legacyEnCompare, '../compare.html'],
  ['en/hosts.html', legacyEnHosts, '../hosts.html'],
  ['en/relay-workflow.html', legacyEnRelay, '../relay-workflow.html'],
]) {
  assert.match(stubDoc, /http-equiv="refresh"/, `${stubLabel} includes meta refresh`);
  assert.ok(stubDoc.includes('window.location.replace') && stubDoc.includes('window.location.hash'), `${stubLabel} preserves query & hash on redirect`);
  assert.ok(stubDoc.includes(`href="${expectedRelTarget}"`), `${stubLabel} links to ${expectedRelTarget}`);
}

for (const [mdLabel, mdDoc] of [
  ['pricing.md', pricingMd],
  ['index.md', indexMd],
  ['guide.md', guideMd],
  ['compare.md', compareMd],
  ['hosts.md', hostsMd],
  ['relay-workflow.md', relayMd],
]) {
  assert.ok(mdDoc.includes('$0') && mdDoc.includes('MIT'), `${mdLabel} includes $0 MIT pricing`);
  assert.ok(mdDoc.includes(brand.releaseVersion), `${mdLabel} includes current version`);
  assert.ok(!/<(?:div|table|section|script|svg)\b/i.test(mdDoc), `${mdLabel} contains clean Markdown without raw HTML containers`);
}

await access(path.join(root, 'favicon.ico'));
await access(path.join(root, 'assets/icon-192.png'));
await access(path.join(root, 'assets/apple-touch-icon.png'));
await access(path.join(root, 'assets/senrelay-logo-display-v1.png'));
await access(path.join(root, 'assets/icon-192.png'));
await access(path.join(root, 'assets/senrelay-logo-original-v1.png'));

let socialPngExists = true;
try {
  await access(path.join(root, 'assets/social.png'));
} catch {
  socialPngExists = false;
}
assert.equal(socialPngExists, false, 'unreachable duplicate social.png excluded from dist');

await access(path.join(root, 'guide.html'));
await access(path.join(root, 'zh/guide.html'));
await access(path.join(root, 'en/guide.html'));
await access(path.join(root, 'compare.html'));
await access(path.join(root, 'zh/compare.html'));
await access(path.join(root, 'en/compare.html'));
await access(path.join(root, 'hosts.html'));
await access(path.join(root, 'zh/hosts.html'));
await access(path.join(root, 'en/hosts.html'));
await access(path.join(root, 'relay-workflow.html'));
await access(path.join(root, 'zh/relay-workflow.html'));
await access(path.join(root, 'en/relay-workflow.html'));
await access(path.join(root, 'pricing.md'));
await access(path.join(root, 'index.md'));
await access(path.join(root, 'guide.md'));
await access(path.join(root, 'compare.md'));
await access(path.join(root, 'hosts.md'));
await access(path.join(root, 'relay-workflow.md'));
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

for (const [label, zhDoc, enDoc] of [
  ['guide', guide, enGuide],
  ['compare', compare, enCompare],
  ['hosts', hosts, enHosts],
  ['relay-workflow', relay, enRelay],
]) {
  assert.ok(!/<div\b[^>]*\bdata-guide-section="en"/.test(zhDoc), `Chinese ${label} strips hidden English section`);
  assert.ok(!/<hr\b[^>]*\bclass="lang-divider"/.test(zhDoc), `Chinese ${label} strips lang-divider`);
  assert.ok(!/<div\b[^>]*\bdata-guide-section="zh"/.test(enDoc), `English ${label} strips hidden Chinese section`);
  assert.ok(!/<hr\b[^>]*\bclass="lang-divider"/.test(enDoc), `English ${label} strips lang-divider`);
}

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
assert.ok(!cjk.test(stripSwitchAndScripts(enHosts)), 'no untranslated CJK in static en/hosts.html');
assert.ok(!cjk.test(stripSwitchAndScripts(enRelay)), 'no untranslated CJK in static en/relay-workflow.html');

assert.ok(!html.includes('/blob/main/app/README.md'), 'no link to superseded public app/README.md');
for (const targetHref of ['href="./guide.html"', 'href="./compare.html"', 'href="./hosts.html"', 'href="./relay-workflow.html"']) {
  assert.ok(html.includes(targetHref), `Chinese homepage links to ${targetHref}`);
  assert.ok(enHtml.includes(targetHref), `English homepage links to ${targetHref}`);
}
for (const doc of [html, enHtml, guide, enGuide, compare, enCompare, hosts, enHosts, relay, enRelay, notFound]) {
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
assert.ok(Array.isArray(graph['@graph'][0].keywords) && graph['@graph'][0].keywords.length >= 4, 'zh WebSite includes keywords');
assert.ok(graph['@graph'][1].disambiguatingDescription?.includes('context-core.dev'), 'zh SoftwareApplication disambiguates context-core.dev');
assert.equal(graph['@graph'][3].url, 'https://github.com/qingsongcui');
assert.ok(graph['@graph'][3].sameAs.includes('https://x.com/George_ONair'));
assert.equal(graph['@graph'][4].mainEntity.length, 9, 'nine FAQ entities in zh JSON-LD');

const enGraph = JSON.parse(enHtml.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
assert.deepEqual(
  enGraph['@graph'].map((n) => n['@type']),
  ['WebSite', 'SoftwareApplication', 'Organization', 'Person', 'FAQPage'],
);
assert.equal(enGraph['@graph'][0].inLanguage, 'en');
assert.ok(Array.isArray(enGraph['@graph'][0].keywords) && enGraph['@graph'][0].keywords.length >= 4, 'en WebSite includes keywords');
assert.ok(enGraph['@graph'][1].disambiguatingDescription?.includes('context-core.dev'), 'en SoftwareApplication disambiguates context-core.dev');
assert.equal(enGraph['@graph'][2].name, enGraph['@graph'][1].name, 'en Organization name uses un-suffixed English brand');
assert.equal(enGraph['@graph'][4].mainEntity.length, 9, 'nine FAQ entities in en JSON-LD');

for (const [label, doc, faqCount] of [
  ['guide.html', guide, 3],
  ['en/guide.html', enGuide, 3],
  ['hosts.html', hosts, 4],
  ['en/hosts.html', enHosts, 4],
  ['relay-workflow.html', relay, 5],
  ['en/relay-workflow.html', enRelay, 5],
]) {
  const docGraph = JSON.parse(doc.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
  assert.deepEqual(docGraph['@graph'].map((n) => n['@type']), ['TechArticle', 'FAQPage', 'BreadcrumbList', 'Person']);
  assert.equal(docGraph['@graph'][1].mainEntity.length, faqCount, `${faqCount} FAQ entities in ${label} JSON-LD`);
  assert.ok(docGraph['@graph'][0].dateModified >= '2026-11-15', `${label} dateModified is updated to >= 2026-11-15`);
}

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

// SEO / GEO Baseline & Anti-Regression Veto Guardrails (Titles <=60 chars, Meta descriptions tight, version aligned to the published release, 4-Element Hero, Plain-Text $0 Pricing, Dated Fact Sheet, Real Limitations, Creator E-E-A-T, Internal Links & Content Floor)
for (const [label, doc] of [
  ['index.html', enHtml],
  ['zh/index.html', html],
  ['guide.html', enGuide],
  ['zh/guide.html', guide],
  ['compare.html', enCompare],
  ['zh/compare.html', compare],
  ['hosts.html', enHosts],
  ['zh/hosts.html', hosts],
  ['relay-workflow.html', enRelay],
  ['zh/relay-workflow.html', relay],
]) {
  const titleText = doc.match(/<title>(.*?)<\/title>/s)[1].replace(/&amp;/g, '&').trim();
  assert.ok(titleText.length >= 20 && titleText.length <= 60, `title length (${titleText.length}) must be 20..60 chars in ${label}: "${titleText}"`);
  const descText = doc.match(/<meta name="description" content="([^"]*)"/)[1];
  if (!label.startsWith('zh/')) {
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
  assert.ok(doc.includes(brand.releaseVersion), `current version ${brand.releaseVersion} appears in ${label}`);
  assert.ok(doc.includes('127.0.0.1:8420/mcp/') && doc.includes('contextcore'), `MCP endpoint & key contextcore documented in ${label}`);
  const bodyInternalLinks = [...doc.matchAll(/<a\b[^>]*class="inline-link"[^>]*href="(?:#|\.?\/(?:guide|compare|hosts|relay-workflow)\.html)[^"]*"/g)];
  assert.ok(bodyInternalLinks.length >= 6, `at least 6 descriptive inline internal links in ${label} (found ${bodyInternalLinks.length})`);
}

for (const [label, doc] of [['guide.html', guide], ['en/guide.html', enGuide], ['hosts.html', hosts], ['en/hosts.html', enHosts], ['relay-workflow.html', relay], ['en/relay-workflow.html', enRelay]]) {
  assert.ok(doc.includes(brand.releaseVersion), `current version ${brand.releaseVersion} appears in ${label}`);
  assert.ok(doc.includes('127.0.0.1:8420/mcp/') && doc.includes('contextcore'), `MCP endpoint & key contextcore documented in ${label}`);
  for (const host of ['Claude Code', 'Codex', 'Cursor', 'Antigravity', 'OMP', 'OpenCode']) {
    assert.ok(doc.includes(host), `core host ${host} appears in ${label}`);
  }
}

for (const [label, doc] of [
  ['index.html', enHtml], ['zh/index.html', html],
  ['guide.html', enGuide], ['zh/guide.html', guide],
  ['compare.html', enCompare], ['zh/compare.html', compare],
  ['hosts.html', enHosts], ['zh/hosts.html', hosts],
  ['relay-workflow.html', enRelay], ['zh/relay-workflow.html', relay],
]) {
  const downloads = [...doc.matchAll(/<a\b[^>]*\bdata-macos-download\b[^>]*>/g)];
  assert.ok(downloads.length > 0, `visible macOS download link in ${label}`);
  for (const [anchor] of downloads) {
    assert.equal(anchor.match(/\bhref="([^"]+)"/)?.[1], releaseDownloadUrl, `current archive link in ${label}`);
  }
  for (const [, url] of doc.matchAll(/\bhref="([^"]+\/releases\/download\/[^/]+\/Senrelay[^"]*\.zip)"/g)) {
    assert.equal(url, releaseDownloadUrl, `no stale installer link in ${label}`);
  }
}
for (const [label, schema] of [['Chinese', graph], ['English', enGraph]]) {
  const software = schema['@graph'].find(node => node['@type'] === 'SoftwareApplication');
  assert.equal(software.softwareVersion, brand.releaseVersion, `${label} schema version`);
  assert.equal(software.downloadUrl, releaseDownloadUrl, `${label} schema download target`);
}

assert.ok(hosts.includes('~/.claude.json') && hosts.includes('~/.openclaw/openclaw.json') && hosts.includes('~/.config/opencode/opencode.json'), 'hosts.html verifies concrete config file paths');
assert.ok(enHosts.includes('~/.claude.json') && enHosts.includes('~/.openclaw/openclaw.json') && enHosts.includes('~/.config/opencode/opencode.json'), 'en/hosts.html verifies concrete config file paths');
assert.ok(relay.includes('must_not_execute=true') && relay.includes('read_conversation_messages'), 'relay-workflow.html covers relay tool contract');
assert.ok(enRelay.includes('must_not_execute=true') && enRelay.includes('read_conversation_messages'), 'en/relay-workflow.html covers relay tool contract');
for (const [homepage, workflow] of [[html, relay], [enHtml, enRelay]]) {
  assert.match(homepage, /relay-workflow\.html#relay-evidence/);
  assert.match(workflow, /id="relay-evidence"/);
  assert.match(workflow, /docs\/evidence\.md#recorded-host-continuation/);
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
assert.match(hosts, /class="language-switch"/);
assert.match(enHosts, /class="language-switch"/);
assert.match(relay, /class="language-switch"/);
assert.match(enRelay, /class="language-switch"/);

// GEO llms.txt & llms-full.txt checks
const llmsSummaryLine = llms.split('\n').find((line) => line.startsWith('> '));
assert.ok(llmsSummaryLine && !cjk.test(llmsSummaryLine), 'llms.txt blockquote summary is pure English');
assert.match(llms, /context-core\.dev/);
assert.match(llms, /pricing\.md/);
assert.match(llms, /hosts\.md/);
assert.match(llms, /hosts\.html/);
assert.match(llms, /relay-workflow\.html/);
assert.ok(llms.includes(releaseDownloadUrl), 'llms.txt links directly to the published macOS archive');
assert.match(llms, /pip install contextcore/);
assert.match(llms, /http:\/\/127\.0\.0\.1:8420\/mcp\//);
assert.match(llms, /senrelay-mcp/);
assert.ok(llms.includes(brand.releaseVersion), `llms.txt reflects v${brand.releaseVersion}`);
assert.ok(llmsFull.includes(releaseDownloadUrl), 'llms-full.txt links directly to the published macOS archive');
assert.ok(!llms.includes('💡') && !llms.includes('📌'), 'no decorative emojis in llms.txt');
assert.match(llmsFull, /must_not_execute=true/);
assert.match(llmsFull, /read_conversation_messages/);
assert.match(llmsFull, /Extended Config Host/);
assert.match(llmsFull, /5-Way Comparison/);
assert.match(llmsFull, /中文核心摘要/);
assert.ok(!llmsFull.includes('1.0.0-beta.1'), 'llms-full.txt has no stale beta.1');
assert.ok(!llmsFull.includes('💡') && !llmsFull.includes('📌'), 'no decorative emojis in llms-full.txt');
assert.ok(!llmsFull.includes('6-point'), 'no stale 6-point handoff card reference in llms-full.txt');

const production = enHtml.includes('content="index, follow"');
assert.equal(zhIndexMd.split('\n')[0], '> 原文：' + (production ? 'https://senrelay.com/zh/' : './zh/'));
assert.match(zhIndexMd, /^# .*[\u4e00-\u9fff]/m, 'Chinese heading in zh/index.md');
if (production) {
  assert.ok(html.includes('type="text/markdown" href="https://senrelay.com/zh/index.md" title="Markdown Mirror"'), 'Chinese homepage mirror linked from zh/index.html');
}
assert.match(llms, /zh\/index\.md/, 'llms.txt exposes the Chinese homepage mirror');
assert.match(llmsFull, /zh\/index\.md/, 'llms-full.txt exposes the Chinese homepage mirror');
for (const [name, doc] of [['guide', guide], ['compare', compare], ['hosts', hosts], ['relay-workflow', relay]]) {
  const mirror = await readFile(path.join(root, 'zh', name + '.md'), 'utf8');
  const zhOrigin = production ? 'https://senrelay.com/zh/' : './zh/';
  assert.equal(mirror.split('\n')[0], '> 原文：' + zhOrigin + name + '.html');
  assert.match(mirror, /^# .*[\u4e00-\u9fff]/m, `Chinese article heading in ${name}.md`);
  if (production) {
    assert.ok(doc.includes(`type="text/markdown" href="${zhOrigin}${name}.md" title="Markdown Mirror"`), `Chinese mirror linked from ${name}.html`);
  }
}
const expectedClarityId = (process.env.CLARITY_ID || '').trim();
const addedDocs = [];
const featureGuides = ['agent-management.html', 'ai-coding-usage-tracker.html'];
for (const fileName of ['claude-code-memory.html', 'mcp-json-vs-toml-config-guide.html', ...featureGuides]) {
  for (const prefix of ['', 'zh/']) {
    const doc = await readFile(path.join(root, prefix, fileName), 'utf8');
    assert.equal((doc.match(/<h1\b/g) || []).length, 1, `${prefix}${fileName} has one primary heading`);
    assert.match(doc, /href="\.\/guide.html"/, 'new guides link to installation');
    assert.match(doc, prefix ? /<html lang="zh-CN"/ : /<html lang="en"/);
    if (featureGuides.includes(fileName)) {
      const jsonLd = JSON.parse(doc.match(/<script type="application\/ld\+json">([^<]+)<\/script>/)[1]);
      const article = jsonLd['@graph'].find(node => node['@type'] === 'TechArticle');
      assert.equal(article.datePublished, '2026-11-19');
      assert.equal(article.inLanguage, prefix ? 'zh-CN' : 'en');
      assert.equal(jsonLd['@graph'].find(node => node['@type'] === 'FAQPage').mainEntity.length, 3);
      const mdFile = fileName.replace('.html', '.md');
      const mirror = await readFile(path.join(root, prefix, mdFile), 'utf8');
      assert.match(mirror, prefix ? /[\u4e00-\u9fff]/ : /Machine-Readable Markdown Mirror/);
      assert.ok(llms.includes(prefix + mdFile), 'new language mirrors are discoverable in llms.txt');
      if (production) {
        const canonical = 'https://senrelay.com/' + prefix + fileName;
        assert.ok(doc.includes('<link rel="canonical" href="' + canonical + '"'), 'guide points to its own canonical');
        assert.ok(doc.includes('type="text/markdown" href="https://senrelay.com/' + prefix + mdFile + '"'), 'guide exposes a same-language mirror');
      }
    }
    addedDocs.push(doc);
  }
}
const deployedDocs = [enHtml, html, enGuide, guide, enCompare, compare, enHosts, hosts, enRelay, relay, ...addedDocs];
const legacyRedirects = [legacyEnHome, legacyEnGuide, legacyEnCompare, legacyEnHosts, legacyEnRelay];
if (production) {
  for (const doc of deployedDocs) {
    assert.match(doc, /content="index, follow"/);
    assert.match(doc, /rel="canonical"/);
    assert.match(doc, /hreflang="zh-CN"/);
    assert.match(doc, /hreflang="zh"/);
    assert.match(doc, /hreflang="en"/);
    assert.match(doc, /hreflang="x-default"/);
    assert.match(doc, /type="text\/markdown"/);
    assert.match(doc, /property="og:image"/);
    assert.match(doc, /property="og:image:width" content="192"/);
    if (expectedClarityId) {
      assert.ok(
        doc.includes('https://www.clarity.ms/tag/') && doc.includes(`"${expectedClarityId}"`),
        `Clarity script tag with ID ${expectedClarityId} injected in production`,
      );
    } else {
      assert.ok(!doc.includes('https://www.clarity.ms/tag/'), 'no Clarity tag injected when CLARITY_ID unset');
    }
  }
  for (const stub of legacyRedirects) {
    assert.match(stub, /content="noindex, follow"/);
    assert.match(stub, /rel="canonical"/);
  }
  const sitemap = await readFile(path.join(root, 'sitemap.xml'), 'utf8');
  assert.match(sitemap, /xmlns:xhtml="http:\/\/www\.w3\.org\/1999\/xhtml"/);
  assert.equal((sitemap.match(/<url>/g) || []).length, deployedDocs.length, 'one canonical URL per built page');
  const lastmods = [...sitemap.matchAll(/<lastmod>(\d{4}-\d{2}-\d{2})<\/lastmod>/g)].map((m) => m[1]);
  assert.equal(lastmods.length, deployedDocs.length, 'valid lastmod dates');
  assert.ok(lastmods.every((d) => d >= '2026-11-15'), 'sitemap lastmod dates >= 2026-11-15');
  assert.ok(sitemap.includes('/hosts.html') && sitemap.includes('/zh/hosts.html') && sitemap.includes('/relay-workflow.html'), 'sitemap includes English root and /zh/ URLs');
  for (const fileName of featureGuides) {
    assert.ok(sitemap.includes('<loc>https://senrelay.com/' + fileName + '</loc>'));
    assert.ok(sitemap.includes('<loc>https://senrelay.com/zh/' + fileName + '</loc>'));
    assert.ok(enHtml.includes('href="./' + fileName + '"') && html.includes('href="./' + fileName + '"'), 'both homepages link to the feature guide');
  }
  assert.ok(!sitemap.includes('/en/'), 'sitemap excludes legacy /en/ redirect URLs');
  assert.match(sitemap, /xhtml:link rel="alternate" hreflang="en"/);
  const robots = await readFile(path.join(root, 'robots.txt'), 'utf8');
  for (const bot of ['Googlebot', 'Bravebot', 'DuckAssistBot', 'OAI-SearchBot', 'GPTBot', 'ClaudeBot', 'PerplexityBot', 'Google-Extended', 'Applebot-Extended', 'Bytespider', 'Baiduspider']) {
    assert.ok(robots.includes(`User-agent: ${bot}`), `robots.txt explicitly declares ${bot}`);
  }
  assert.match(robots, /Sitemap: /);
} else {
  for (const doc of deployedDocs) {
    assert.match(doc, /content="noindex, nofollow"/);
    assert.ok(!doc.includes('rel="canonical"'));
    assert.ok(!doc.includes('https://www.clarity.ms/tag/'), 'no analytics in preview builds');
  }
  for (const stub of legacyRedirects) {
    assert.match(stub, /content="noindex, nofollow"/);
    assert.ok(!stub.includes('rel="canonical"'));
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
