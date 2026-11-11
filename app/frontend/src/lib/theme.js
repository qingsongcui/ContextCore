import { createContext, createElement, useContext, useEffect, useState } from 'react';

const STORAGE_KEY = 'contextcore.theme';

function getInitialTheme() {
  if (typeof window === 'undefined') return 'system';
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    if (
      stored === 'light' ||
      stored === 'dark' ||
      stored === 'system'
    ) {
      return stored;
    }
  } catch {
    // ignore
  }
  return 'system';
}

function resolveTheme(theme) {
  if (theme === 'system') {
    if (typeof window === 'undefined') return 'light';
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  }
  return theme;
}

const ThemeCtx = createContext({
  theme: 'system',
  resolved: 'light',
  setTheme: () => {},
});

export function ThemeProvider({ children }) {
  const [theme, setThemeState] = useState(getInitialTheme);
  const [systemTheme, setSystemTheme] = useState(() => resolveTheme('system'));
  const resolved = theme === 'system' ? systemTheme : theme;

  useEffect(() => {
    const root = window.document.documentElement;
    root.classList.remove('light', 'dark');
    root.classList.add(resolved);
    try {
      window.localStorage.setItem(STORAGE_KEY, theme);
    } catch {
      // ignore
    }
  }, [theme, resolved]);

  useEffect(() => {
    if (theme !== 'system') return;
    const mq = window.matchMedia('(prefers-color-scheme: dark)');
    const handler = () => setSystemTheme(resolveTheme('system'));
    handler();
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, [theme]);

  const setTheme = (next) => {
    if (
      next === 'light' ||
      next === 'dark' ||
      next === 'system'
    ) {
      setThemeState(next);
    }
  };

  return createElement(
    ThemeCtx.Provider,
    { value: { theme, resolved, setTheme } },
    children,
  );
}

export function useTheme() {
  return useContext(ThemeCtx);
}
