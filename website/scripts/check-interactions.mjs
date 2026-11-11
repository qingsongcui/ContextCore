import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { runInNewContext } from 'node:vm';
import { attrEn, htmlEn, metaEn, textEn } from '../assets/i18n.js';

// Execute the shipped interaction module against a small DOM boundary. Browser
// layout, native disclosure keyboard behavior, and visual motion belong to QA.
const source = (await readFile(new URL('../assets/site.js', import.meta.url), 'utf8')).replace(/^import .*;\n/gm, '');
const html = await readFile(new URL('../index.html', import.meta.url), 'utf8');
function fixture({ search = '?lang=zh', stored = null, blockedStorage = false } = {}) {
  let focused = null;
  const storage = new Map(stored ? [['senrelay.website.language', stored]] : []);
  function element(attrs = {}) {
    const attributes = new Map(Object.entries(attrs));
    const listeners = new Map();
    return {
      dataset: {}, hidden: false, open: false, classList: { add() {}, toggle() {} },
      getAttribute: key => attributes.get(key) ?? null,
      hasAttribute: key => attributes.has(key),
      setAttribute: (key, value) => attributes.set(key, String(value)),
      removeAttribute: key => attributes.delete(key),
      addEventListener: (name, fn) => listeners.set(name, fn),
      dispatch(name, event = {}) { listeners.get(name)?.(event); },
      closest: () => null,
      querySelectorAll: () => [],
      focus() { focused = this; },
    };
  }
  const root = element();
  const toggle = element({ 'aria-expanded': 'false', 'aria-label': '展开导航' });
  const nav = element(), group = element({ 'aria-label': '语言' });
  const zh = element({ 'aria-pressed': 'true' }), en = element({ 'aria-pressed': 'false' });
  zh.dataset.lang = 'zh'; en.dataset.lang = 'en';
  const target = element(), link = element();
  link.hash = '#assistants'; link.closest = selector => selector === 'a' ? link : null;
  nav.querySelectorAll = () => [link];
  const textKey = Object.keys(textEn).find(key => textEn[key] && key !== textEn[key]);
  const text = { nodeValue: textKey, parentElement: element() };
  const block = element(); block.dataset.i18nHtml = 'faq';
  let blockHtml = html.match(/<div class="faq-list" data-i18n-html="faq">([\s\S]*?)<\/div><\/section>/)[1];
  let details = [];
  Object.defineProperty(block, 'innerHTML', {
    get: () => blockHtml,
    set: value => { blockHtml = value; details = [...value.matchAll(/<details\b/g)].map(() => element()); },
  });
  block.innerHTML = blockHtml;
  block.querySelectorAll = selector => selector === 'details' ? details : [];
  const ld = { textContent: html.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1] };
  const metas = new Map([...html.matchAll(/<meta (name|property)="([^"]+)" content="([^"]*)">/g)]
    .map(([, kind, key, value]) => [`meta[${kind}="${key}"]`, element({ content: value })]));
  const originalTitle = html.match(/<title>(.*?)<\/title>/)[1];
  const document = Object.assign(element(), {
    documentElement: root, title: originalTitle, body: element(), hidden: false,
    createTreeWalker(_, __, { acceptNode }) {
      let visited = false;
      return { currentNode: null, nextNode() {
        if (visited) return false;
        visited = true;
        if (acceptNode(text) !== 1) return false;
        this.currentNode = text; return true;
      } };
    },
    querySelector(selector) {
      return metas.get(selector) || ({
        'script[type="application/ld+json"]': ld,
        '.language-switch': group,
        '[data-nav-toggle]': toggle,
        '#site-navigation': nav,
        '#assistants': target,
      })[selector] || null;
    },
    querySelectorAll(selector) {
      return ({
        '[data-i18n-html]': [block],
        '[aria-label],[title],[placeholder]': [toggle, group],
        '[data-lang]': [zh, en],
      })[selector] || [];
    },
  });
  const media = element();
  let href = `https://preview.invalid/${search}`;
  const window = {
    location: { search, get href() { return href; } },
    localStorage: {
      getItem(key) { if (blockedStorage) throw new Error('denied'); return storage.get(key) ?? null; },
      setItem(key, value) { if (blockedStorage) throw new Error('denied'); storage.set(key, value); },
    },
    history: { replaceState(_, __, url) { href = url.href; } },
    matchMedia: () => media,
  };
  runInNewContext(source, { window, document, navigator: { languages: ['en-US'] }, URL, URLSearchParams,
    NodeFilter: { SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 }, attrEn, htmlEn, metaEn, textEn });
  return { root, toggle, nav, zh, en, link, target, block, text, textKey, ld, metas,
    document, storage, media, originalTitle, get focused() { return focused; }, get href() { return href; } };
}

test('language switching changes rendered content and both metadata descriptions, then restores originals', () => {
  const f = fixture();
  const originalLd = JSON.parse(f.ld.textContent);
  const originalDescription = f.metas.get('meta[name="description"]').getAttribute('content');
  f.en.dispatch('click');
  assert.equal(f.root.lang, 'en');
  assert.equal(f.text.nodeValue, textEn[f.textKey]);
  assert.equal(f.block.innerHTML, htmlEn.faq);
  assert.equal(f.document.title, metaEn.title);
  assert.equal(f.metas.get('meta[name="description"]').getAttribute('content'), metaEn.description);
  const englishLd = JSON.parse(f.ld.textContent)['@graph'];
  assert.equal(englishLd[0].description, metaEn.siteDescription);
  assert.equal(englishLd[1].description, metaEn.jsonDescription);
  assert.equal(f.en.getAttribute('aria-pressed'), 'true');
  assert.equal(f.storage.get('senrelay.website.language'), 'en');
  assert.equal(new URL(f.href).searchParams.has('lang'), false);
  f.zh.dispatch('click');
  assert.equal(f.root.lang, 'zh-CN');
  assert.equal(f.text.nodeValue, f.textKey);
  assert.equal(f.document.title, f.originalTitle);
  assert.equal(f.metas.get('meta[name="description"]').getAttribute('content'), originalDescription);
  assert.deepEqual(JSON.parse(f.ld.textContent), originalLd);
});

test('navigation opens, localizes its current state, escapes with focus return, and focuses linked sections', () => {
  const f = fixture();
  assert.equal(f.toggle.hidden, false);
  f.toggle.dispatch('click');
  assert.equal(f.nav.dataset.open, 'true');
  assert.equal(f.toggle.getAttribute('aria-expanded'), 'true');
  f.en.dispatch('click');
  assert.equal(f.toggle.getAttribute('aria-label'), attrEn['收起导航']);
  f.document.dispatch('keydown', { key: 'Escape' });
  assert.equal(f.nav.dataset.open, 'false');
  assert.equal(f.toggle.getAttribute('aria-label'), attrEn['展开导航']);
  assert.equal(f.focused, f.toggle);
  f.toggle.dispatch('click');
  f.nav.dispatch('click', { target: f.link });
  assert.equal(f.nav.dataset.open, 'false');
  assert.equal(f.target.getAttribute('tabindex'), '-1');
  assert.equal(f.focused, f.target);
  f.toggle.dispatch('click');
  f.document.dispatch('click', { target: { closest: () => null } });
  assert.equal(f.toggle.getAttribute('aria-expanded'), 'false');
});

test('URL language wins over storage; denied storage, open disclosures, and visibility changes remain usable', () => {
  const f = fixture({ stored: 'en' });
  assert.equal(f.root.lang, 'zh-CN');
  f.block.querySelectorAll('details')[0].open = true;
  f.en.dispatch('click');
  assert.equal(f.block.querySelectorAll('details')[0].open, true);
  f.zh.dispatch('click');
  assert.equal(f.block.querySelectorAll('details')[0].open, true);
  f.document.hidden = true;
  f.document.dispatch('visibilitychange');
  assert.equal(f.root.dataset.pageVisible, 'false');
  f.document.hidden = false;
  f.document.dispatch('visibilitychange');
  assert.equal(f.root.dataset.pageVisible, 'true');
  const denied = fixture({ search: '', blockedStorage: true });
  assert.equal(denied.root.lang, 'en');
  denied.zh.dispatch('click');
  assert.equal(denied.root.lang, 'zh-CN');
});
