import { attrEn, htmlEn, metaEn, textEn } from './i18n.js';
import { mountSphere } from './sphere.js';
const root = document.documentElement;

const LANG_KEY = 'senrelay.website.language';
const originals = new WeakMap();
const htmlOriginals = new Map();
const attrOriginals = new WeakMap();
const originalTitle = document.title;
const metaOriginal = new WeakMap();
const ldNode = document.querySelector('script[type="application/ld+json"]');
const originalLd = ldNode ? ldNode.textContent : '';

function detectLanguage() {
  const override = new URLSearchParams(window.location.search).get('lang');
  if (override === 'zh' || override === 'en') return override;
  try {
    const stored = window.localStorage.getItem(LANG_KEY);
    if (stored === 'zh' || stored === 'en') return stored;
  } catch { /* Storage is optional. */ }
  const primary = ((navigator.languages && navigator.languages[0]) || navigator.language || '').toLowerCase();
  if (!primary) return 'zh';
  return primary.startsWith('zh') ? 'zh' : 'en';
}
let language = detectLanguage();

const skipSelector = 'script,style,[data-i18n-html],.language-switch';

function applyText(lang) {
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent || !node.nodeValue.trim()) return NodeFilter.FILTER_REJECT;
      if (parent.closest(skipSelector)) return NodeFilter.FILTER_REJECT;
      return NodeFilter.FILTER_ACCEPT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    if (!originals.has(node)) originals.set(node, node.nodeValue);
    const source = originals.get(node);
    const core = source.trim();
    if (lang === 'en') {
      if (Object.prototype.hasOwnProperty.call(textEn, core)) {
        const lead = source.match(/^\s*/)[0];
        const trail = source.match(/\s*$/)[0];
        node.nodeValue = lead + textEn[core] + trail;
      } else if (/[\u4e00-\u9fff]/.test(core)) {
        node.parentElement.dataset.i18nMissing = core;
      }
    } else {
      node.nodeValue = source;
    }
  }
  document.querySelectorAll('[data-i18n-html]').forEach(element => {
    const key = element.dataset.i18nHtml;
    if (!htmlOriginals.has(element)) htmlOriginals.set(element, element.innerHTML);
    const openDetails = [...element.querySelectorAll('details')].map(detail => detail.open);
    const source = htmlOriginals.get(element);
    element.innerHTML = lang === 'en' && htmlEn[key] ? htmlEn[key] : source;
    element.querySelectorAll('details').forEach((detail, index) => { detail.open = openDetails[index] ?? false; });
  });
  document.querySelectorAll('[aria-label],[title],[placeholder]').forEach(element => {
    if (!attrOriginals.has(element)) attrOriginals.set(element, {});
    const store = attrOriginals.get(element);
    for (const attr of ['aria-label', 'title', 'placeholder']) {
      if (!element.hasAttribute(attr)) continue;
      if (!(attr in store)) store[attr] = element.getAttribute(attr);
      const source = store[attr];
      element.setAttribute(attr, lang === 'en' && attrEn[source] ? attrEn[source] : source);
    }
  });
}

function applyMeta(lang) {
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  document.title = lang === 'en' ? metaEn.title : originalTitle;
  const entries = [
    ['meta[name="description"]', 'content', metaEn.description],
    ['meta[property="og:title"]', 'content', metaEn.ogTitle],
    ['meta[property="og:description"]', 'content', metaEn.ogDescription],
    ['meta[name="twitter:title"]', 'content', metaEn.ogTitle],
    ['meta[name="twitter:description"]', 'content', metaEn.twitterDescription],
    ['meta[property="og:locale"]', 'content', metaEn.locale],
  ];
  for (const [selector, attr, value] of entries) {
    const element = document.querySelector(selector);
    if (!element) continue;
    if (!metaOriginal.has(element)) metaOriginal.set(element, element.getAttribute(attr));
    element.setAttribute(attr, lang === 'en' ? value : metaOriginal.get(element));
  }
  if (ldNode) {
    try {
      const data = JSON.parse(originalLd);
      if (lang === 'en') {
        data['@graph'][0].inLanguage = 'en';
        data['@graph'][0].description = metaEn.siteDescription;
        data['@graph'][1].description = metaEn.jsonDescription;
      }
      ldNode.textContent = JSON.stringify(data);
    } catch { /* Keep the original structured data. */ }
  }
}

function applyLanguage(lang) {
  applyText(lang);
  applyMeta(lang);
  document.querySelectorAll('[data-lang]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.lang === lang)));
  const group = document.querySelector('.language-switch');
  if (group) group.setAttribute('aria-label', lang === 'en' ? 'Language' : '语言');
  syncNavigationLabel();
}

function setLanguage(next) {
  if ((next !== 'zh' && next !== 'en') || next === language) return;
  language = next;
  try { window.localStorage.setItem(LANG_KEY, next); } catch { /* Storage is optional. */ }
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.has('lang')) {
      url.searchParams.delete('lang');
      window.history.replaceState(null, '', url);
    }
  } catch { /* ignore */ }
  applyLanguage(next);
}

root.classList.add('has-js');
function syncPageVisibility() {
  root.dataset.pageVisible = String(!document.hidden);
}
document.addEventListener('visibilitychange', syncPageVisibility);
syncPageVisibility();
const heroVisual = document.querySelector('.hero-visual');
if (heroVisual && 'ResizeObserver' in window) mountSphere(heroVisual);

const navToggle = document.querySelector('[data-nav-toggle]');
const nav = document.querySelector('#site-navigation');
navToggle.hidden = false;
function syncNavigationLabel() {
  const expanded = navToggle.getAttribute('aria-expanded') === 'true';
  const label = expanded ? '收起导航' : '展开导航';
  navToggle.setAttribute('aria-label', language === 'en' ? attrEn[label] : label);
}
function closeNav(returnFocus = false) {
  nav.dataset.open = 'false';
  navToggle.setAttribute('aria-expanded', 'false');
  syncNavigationLabel();
  if (returnFocus) navToggle.focus();
}
navToggle.addEventListener('click', () => {
  const open = navToggle.getAttribute('aria-expanded') !== 'true';
  nav.dataset.open = String(open);
  navToggle.setAttribute('aria-expanded', String(open));
  syncNavigationLabel();
});
nav.addEventListener('click', event => { const link = event.target.closest('a'); if (!link) return; closeNav(); const target = document.querySelector(link.hash); if (target) { target.setAttribute('tabindex', '-1'); target.focus({ preventScroll: true }); } });
document.addEventListener('keydown', event => { if (event.key === 'Escape' && nav.dataset.open === 'true') closeNav(true); });
document.addEventListener('click', event => { if (nav.dataset.open === 'true' && !event.target.closest('.site-header')) closeNav(); });
window.matchMedia('(min-width: 901px)').addEventListener('change', () => closeNav());

if ('IntersectionObserver' in window) {
  const motionObserver = new IntersectionObserver(entries => {
    for (const entry of entries) entry.target.dataset.inView = String(entry.isIntersecting);
  }, { threshold: 0.05 });
  document.querySelectorAll('[data-motion-region]').forEach(element => { element.dataset.inView = 'false'; motionObserver.observe(element); });
  const revealObserver = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      entry.target.classList.add('has-entered');
      revealObserver.unobserve(entry.target);
    }
  }, { threshold: 0.12 });
  document.querySelectorAll('[data-reveal]').forEach(element => revealObserver.observe(element));
  const headerObserver = new IntersectionObserver(entries => {
    document.querySelector('.site-header').classList.toggle('is-scrolled', !entries[0].isIntersecting);
  });
  headerObserver.observe(document.querySelector('.header-sentinel'));
  const sectionObserver = new IntersectionObserver(entries => {
    for (const entry of entries) if (entry.isIntersecting) {
      nav.querySelectorAll('a').forEach(link => {
        if (link.hash === `#${entry.target.id}`) link.setAttribute('aria-current', 'location');
        else link.removeAttribute('aria-current');
      });
    }
  }, { rootMargin: '-15% 0px -60% 0px', threshold: 0 });
  nav.querySelectorAll('a').forEach(link => { const section = document.querySelector(link.hash); if (section) sectionObserver.observe(section); });
}

document.querySelectorAll('[data-lang]').forEach(button => button.addEventListener('click', () => setLanguage(button.dataset.lang)));
applyLanguage(language);
