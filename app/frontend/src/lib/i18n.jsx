import { createContext, createElement, useContext, useEffect, useSyncExternalStore } from 'react';
import { en } from './messages.en.js';

const STORAGE_KEY = 'contextcore.language';
const SUPPORTED = ['zh', 'en'];

let lang = detectInitial();
const listeners = new Set();
const missing = new Set();

function detectInitial() {
  if (typeof window === 'undefined') return 'zh';
  try {
    const override = new URLSearchParams(window.location.search).get('lang');
    if (SUPPORTED.includes(override)) return override;
  } catch {
    // ignore
  }
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (SUPPORTED.includes(stored)) return stored;
  } catch {
    // ignore
  }
  const list = navigator.languages?.length ? navigator.languages : [navigator.language || ''];
  const primary = (list[0] || '').toLowerCase();
  if (!primary) return 'zh';
  return primary.startsWith('zh') ? 'zh' : 'en';
}

export function getLang() {
  return lang;
}

export function locale() {
  return lang === 'en' ? 'en-US' : 'zh-CN';
}

export function t(key, vars) {
  const template = lang === 'en' && Object.prototype.hasOwnProperty.call(en, key) ? en[key] : key;
  if (lang === 'en' && !Object.prototype.hasOwnProperty.call(en, key) && !missing.has(key)) {
    missing.add(key);
    if (import.meta.env?.DEV) console.warn('[i18n] missing English copy:', key);
  }
  if (!vars) return template;
  return template.replace(/\{(\w+)\}/g, (whole, name) => (Object.prototype.hasOwnProperty.call(vars, name) ? String(vars[name]) : whole));
}

function applyDocument() {
  if (typeof document === 'undefined') return;
  document.documentElement.lang = lang === 'en' ? 'en' : 'zh-CN';
  document.title = t('Senrelay · 续境 — 项目工作台');
}

export function setLang(next) {
  if (!SUPPORTED.includes(next) || next === lang) return;
  lang = next;
  try {
    window.localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // ignore
  }
  try {
    const url = new URL(window.location.href);
    if (url.searchParams.has('lang')) {
      url.searchParams.delete('lang');
      window.history.replaceState(null, '', url);
    }
  } catch {
    // ignore
  }
  applyDocument();
  listeners.forEach(listener => listener());
}

export function subscribe(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

if (typeof document !== 'undefined') applyDocument();

const I18nCtx = createContext({ lang, setLang });

export function I18nProvider({ children }) {
  const current = useSyncExternalStore(subscribe, getLang, () => 'zh');
  useEffect(() => {
    applyDocument();
  }, [current]);
  return createElement(I18nCtx.Provider, { value: { lang: current, setLang } }, children);
}

export function useI18n() {
  return useContext(I18nCtx);
}

export function missingEnglishKeys() {
  return [...missing];
}

export function listenerCount() {
  return listeners.size;
}
