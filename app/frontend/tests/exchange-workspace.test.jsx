// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import ExchangePage from '../src/pages/ExchangePage.jsx';
import { setLang } from '../src/lib/i18n.jsx';
import { downloadText } from '../src/lib/downloadText.js';
import { request } from '../src/lib/api.js';
import { useResource } from '../src/lib/useResource.js';

vi.mock('../src/lib/downloadText.js', () => ({ downloadText: vi.fn() }));
vi.mock('../src/lib/api.js', () => ({ request: vi.fn() }));
vi.mock('../src/lib/useResource.js', () => ({ useResource: vi.fn() }));
const path = '/api/workbench/profile/export';
const snapshot = {
  revision: 7, name: 'Plan Studio', bio: 'other bio',
  assets: [{ title: 'Data audit', excerpt: 'Check totals', notes: 'private' }, { title: 'Other', excerpt: 'Not chosen' }],
};
let root, container, reload, navigate, store, result;
beforeEach(() => {
  vi.resetAllMocks();
  setLang('zh');
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  reload = vi.fn();
  navigate = vi.fn();
  store = { current: null };
  result = { data: snapshot, loading: false, error: null, reload };
  useResource.mockImplementation(() => result);
  request.mockResolvedValue(snapshot);
});
afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
});

const render = () => act(() => root.render(<ExchangePage navigate={navigate} revision={1} draftStore={store} />));
const button = name => [...container.querySelectorAll('button')].find(item => item.textContent === name);
async function choose() {
  const select = container.querySelector('select');
  await act(() => {
    select.value = '0';
    select.dispatchEvent(new Event('change', { bubbles: true }));
  });
}
async function write(index, text) {
  const input = container.querySelectorAll('textarea')[index];
  await act(() => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value').set.call(input, text);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('exports only the single previewed offer and explicit brief fields after a fresh export-snapshot check', async () => {
  await render();
  const exportButton = button('导出协作说明');
  expect(exportButton.disabled).toBe(true);
  await choose();
  expect(exportButton.disabled).toBe(true);
  await write(0, 'One audit report');
  await write(1, 'Customer supplies anonymized tables');
  expect(exportButton.disabled).toBe(false);
  await act(() => exportButton.click());
  expect(request).toHaveBeenCalledWith(path, expect.objectContaining({ cache: 'no-store' }));
  expect(useResource.mock.calls.every(([url]) => url === path)).toBe(true);
  const [name, body] = downloadText.mock.calls[0];
  expect(name).toBe('Senrelay-collaboration.txt');
  expect(body).toContain('Plan Studio');
  expect(body).toContain('Data audit');
  expect(body).toContain('Check totals');
  expect(body).toContain('One audit report');
  expect(body).toContain('Customer supplies anonymized tables');
  expect(body).not.toMatch(/private|other bio|Other|Not chosen/);
});

it('retains the author’s terms but clears the selection if a card changes before export', async () => {
  await render();
  await choose();
  await write(0, 'Deliver report');
  request.mockResolvedValue({ ...snapshot, revision: 8, assets: [snapshot.assets[1]] });
  await act(() => button('导出协作说明').click());
  expect(downloadText).not.toHaveBeenCalled();
  expect(reload).toHaveBeenCalledTimes(1);
  expect(button('导出协作说明').disabled).toBe(true);
  expect(store.current.selection).toBeNull();
  expect(store.current.deliverable).toBe('Deliver report');
  expect(container.querySelector('[role=status]').textContent).toContain('名片已更新');
});

it('rechecks a draft against the latest card when returning and keeps typed content', async () => {
  await render();
  await choose();
  await write(0, 'Draft');
  await act(() => root.render(null));
  result.data = { ...snapshot, revision: 8, assets: [snapshot.assets[1], snapshot.assets[0]] };
  await render();
  expect(container.querySelector('select').value).toBe('');
  expect(container.querySelector('textarea').value).toBe('Draft');
  expect(button('导出协作说明').disabled).toBe(true);
  await choose();
  expect(button('导出协作说明').disabled).toBe(false);
  expect(container.querySelector('.exchange-paper h2').textContent).toBe('Other');
});

it('routes empty cards to real editors and cancels an outstanding download when leaving', async () => {
  result.data = { ...snapshot, assets: [] };
  await render();
  await act(() => button('编辑 Agent 名片').click());
  expect(navigate).toHaveBeenCalledWith({ page: 'personal', section: 'profile' });
  await act(() => button('整理能力与作品').click());
  expect(navigate).toHaveBeenCalledWith({ page: 'personal', section: 'assets' });
  result.data = snapshot;
  await render();
  await choose();
  await write(0, 'Only export while still here');
  let resolveRequest;
  request.mockImplementation(() => new Promise(resolve => { resolveRequest = resolve; }));
  await act(() => button('导出协作说明').click());
  await act(() => root.render(null));
  await act(() => resolveRequest(snapshot));
  expect(downloadText).not.toHaveBeenCalled();
});

it('blocks a delayed export response when a newer card arrived in the meantime', async () => {
  await render();
  await choose();
  await write(0, 'Deliver report');
  let resolveRequest;
  request.mockImplementation(() => new Promise(resolve => { resolveRequest = resolve; }));
  await act(() => button('导出协作说明').click());
  result.data = { ...snapshot, revision: 9, assets: [] };
  await render();
  await act(() => resolveRequest(snapshot));
  expect(downloadText).not.toHaveBeenCalled();
  expect(store.current.selection).toBeNull();
  expect(store.current.deliverable).toBe('Deliver report');
});
