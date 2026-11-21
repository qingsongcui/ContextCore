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
const briefsPath = '/api/workbench/briefs';
let root, container, reload, navigate, store, result, library;
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
  library = { data: { items: [], total: 0 }, loading: false, error: null, reload: vi.fn() };
  useResource.mockImplementation(url => url === path ? result : library);
  request.mockImplementation(async (url, options) => {
    if (url !== briefsPath + '/export') throw Error('Unexpected request');
    const input = JSON.parse(options.body);
    return { profile_revision: snapshot.revision, provider: snapshot.name, title: snapshot.assets[0].title,
      excerpt: snapshot.assets[0].excerpt, deliverable: input.deliverable, terms: input.terms };
  });
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
  expect(request).toHaveBeenCalledWith(briefsPath + '/export', expect.objectContaining({ method: 'POST' }));
  expect(JSON.parse(request.mock.calls[0][1].body)).toEqual({revision: 0, profile_revision: 7, asset_index: 0,
    deliverable: 'One audit report', terms: 'Customer supplies anonymized tables'});
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

it('saves and reopens a local draft, protects edits when switching, and checks the version before delete', async () => {
  let saved;
  request.mockImplementation(async (url, options) => {
    if (options.method === 'PUT') {
      const payload = JSON.parse(options.body);
      expect(payload).toMatchObject({revision: 0, asset_index: 0, profile_revision: 7});
      saved = {id: url.split('/').at(-1), revision: 1, deliverable: payload.deliverable, terms: payload.terms,
        selection: store.current.selection, updated_at: '2026-11-20T12:30:00Z'};
      library.data = {items:[saved],total:1};
      return saved;
    }
    if (options.method === 'DELETE') {
      expect(url).toBe(briefsPath + '/' + saved.id + '?revision=1');
      library.data = {items:[],total:0};
      return null;
    }
    expect(url).toBe(briefsPath + '/' + saved.id);
    return saved;
  });
  await render();
  await choose();
  await write(0, 'A saved brief');
  await act(() => button('保存协作草稿').click());
  expect(store.current).toMatchObject({revision: 1, dirty: false});
  expect(container.querySelector('.exchange-draft-open').textContent).toContain('A saved brief');
  await act(() => button('新草稿').click());
  expect(container.querySelector('textarea').value).toBe('');
  await write(0, 'Unsaved edits');
  await act(() => container.querySelector('.exchange-draft-open').click());
  expect(container.querySelector('textarea').value).toBe('Unsaved edits');
  await act(() => button('放弃修改').click());
  expect(container.querySelector('textarea').value).toBe('A saved brief');
  await act(() => button('删除草稿').click());
  await act(() => button('确认删除').click());
  expect(container.querySelector('.exchange-draft-open')).toBeNull();
  expect(container.querySelector('textarea').value).toBe('');
});

it('keeps stale-write text and offers a copy on a saved version conflict', async () => {
  store.current = {id:'saved', revision:1, selection:null, deliverable:'Local changes', terms:'', dirty:true};
  request.mockRejectedValue(Object.assign(new Error('Draft changed'), {status:409}));
  await render();
  await act(() => button('保存协作草稿').click());
  expect(store.current.deliverable).toBe('Local changes');
  await act(() => button('将当前文字另存为新草稿').click());
  expect(store.current.id).not.toBe('saved');
  expect(store.current.revision).toBe(0);
  expect(store.current.deliverable).toBe('Local changes');
});
