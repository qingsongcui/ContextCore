// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
const state = vi.hoisted(() => ({ offline: false }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), request: vi.fn(async path => {
  if (path === '/api/status') {
    if (state.offline) throw new Error('Connection unavailable');
    return { ok: true, service: { running: true } };
  }
  if (path === '/api/projects') return [];
  return {};
}) }));
vi.mock('../src/pages/ProjectsPage.jsx', () => ({ ProjectsPage: () => <h1>Projects</h1> }));
import App from '../src/App.jsx';
let container, root, source;
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  state.offline = false;
  window.history.replaceState(null, '', '/?page=projects&lang=en');
  vi.stubGlobal('matchMedia', () => ({ matches: false, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal('EventSource', class {
    constructor() { source = this; }
    addEventListener() {}
    close() {}
  });
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
it('rechecks backend on lost events and recovers status even while the page is hidden', async () => {
  vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
  await act(async () => root.render(<App />));
  await act(async () => source.onopen());
  expect(container.querySelector('.local-status .online')).not.toBeNull();
  state.offline = true;
  await act(async () => source.onerror());
  expect(container.querySelector('.local-status .online')).toBeNull();
  expect(container.querySelector('.service-banner')).not.toBeNull();
  state.offline = false;
  await act(async () => source.onopen());
  expect(container.querySelector('.local-status .online')).not.toBeNull();
  expect(container.querySelector('.service-banner')).toBeNull();
  vi.restoreAllMocks();
});
it('keeps a confirmed healthy backend online when only its event stream is reconnecting', async () => {
  await act(async () => root.render(<App />));
  await act(async () => source.onopen());
  await act(async () => source.onerror());
  expect(container.querySelector('.local-status .online')).not.toBeNull();
  expect(container.querySelector('.service-banner')).toBeNull();
  expect(container.querySelector('.connection-note')).not.toBeNull();
});
