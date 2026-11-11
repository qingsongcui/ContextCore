// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
const h = vi.hoisted(() => ({ mutate: vi.fn() }));
vi.mock('../src/lib/api.js', () => ({ mutate: h.mutate }));
vi.mock('../src/lib/useResource.js', () => ({ useResource(path) { return { loading: false, error: null, reload() {}, data: path?.endsWith('/manifest') ? { revision: 'synthetic', download_bytes: 24 } : path?.endsWith('/status') ? { settings: { lexical_enabled: true, semantic_enabled: false, relation_enabled: false, max_results: 15, context_budget_tokens: 2000 }, semantic: { enabled: false }, relations: { enabled: false, count: 0 } } : null }; } }));
import { RetrievalSettings } from '../src/components/RetrievalSettings.jsx';
import { setLang } from '../src/lib/i18n.jsx';
let container, root;
const button = text => [...document.querySelectorAll('button')].find(item => item.textContent === text);
async function click(text) { expect(button(text)).toBeTruthy(); await act(async () => button(text).click()); }
beforeEach(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  setLang('zh'); h.mutate.mockReset();
  window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  await act(async () => root.render(<RetrievalSettings />));
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });
it('closing the model preview starts no download', async () => {
  await click('查看可选模型下载');
  await click('关闭');
  expect(h.mutate).not.toHaveBeenCalled();
  expect(document.querySelector('dialog')).toBeNull();
});
it('keeps cancellation pending until the download returns its terminal cleanup receipt', async () => {
  let finish;
  h.mutate.mockImplementation((path, method, body) => {
    if (path.endsWith('/cancel')) return Promise.resolve({ status: 'running', cancellation_requested: true, download_id: body.download_id });
    return new Promise(resolve => { finish = resolve; });
  });
  await click('查看可选模型下载'); await click('确认下载固定模型包');
  const key = h.mutate.mock.calls[0][2].download_id;
  expect(key).toMatch(/^[a-f0-9-]{36}$/);
  expect(button('关闭').disabled).toBe(true);
  await click('取消下载');
  expect(h.mutate.mock.calls[1]).toEqual(['/api/retrieval/semantic/download/cancel', 'POST', { download_id: key }]);
  expect(document.body.textContent).toContain('已请求取消，正在等待当前网络读取结束');
  expect(document.body.textContent).not.toContain('下载已取消，未完成的文件已清理。');
  expect(button('取消下载').disabled).toBe(true);
  await act(async () => finish({ status: 'cancelled', download_id: key }));
  expect(document.body.textContent).toContain('下载已取消，未完成的文件已清理。');
  expect(button('关闭').disabled).toBe(false);
  await click('关闭');
  expect(document.querySelector('dialog')).toBeNull();
});
it('user consent transition: toggle -> preview must not mutate; cancel discards; reopen shows original; only confirmation mutates', async () => {
  h.mutate.mockImplementation((path, method, body) => Promise.resolve(body));
  // 1. Open settings
  await click('调整设置');
  expect(document.querySelector('dialog')).toBeTruthy();
  const getBoxes = () => [...document.querySelectorAll('input[type="checkbox"]')];
  expect(getBoxes()[2].checked).toBe(false);

  // 2. Toggle relation consent
  await act(async () => getBoxes()[2].click());
  expect(getBoxes()[2].checked).toBe(true);

  // 3. Preview: must not mutate
  await click('预览差异');
  expect(document.body.textContent).toContain('变更前后');
  expect(h.mutate).not.toHaveBeenCalled();

  // 4. Return to edit: still no mutation
  await click('返回编辑');
  expect(document.body.textContent).not.toContain('变更前后');
  expect(h.mutate).not.toHaveBeenCalled();

  // 5. Cancel discards unconfirmed draft
  await click('取消');
  expect(document.querySelector('dialog')).toBeNull();
  expect(h.mutate).not.toHaveBeenCalled();

  // 6. Reopen shows original state (discarded draft is not retained)
  await click('调整设置');
  expect(getBoxes()[2].checked).toBe(false);

  // 7. Toggle, preview, and confirm: only now does mutation execute
  await act(async () => getBoxes()[2].click());
  expect(getBoxes()[2].checked).toBe(true);
  await click('预览差异');
  expect(h.mutate).not.toHaveBeenCalled();
  await click('确认应用设置');
  expect(h.mutate).toHaveBeenCalledTimes(1);
  expect(h.mutate).toHaveBeenCalledWith(
    '/api/retrieval/settings',
    'PATCH',
    expect.objectContaining({ relation_enabled: true })
  );
  expect(document.querySelector('dialog')).toBeNull();
});
