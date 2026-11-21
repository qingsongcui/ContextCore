// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { GlobalAssetEditor } from '../src/components/agent-management/GlobalAssetEditor.jsx';
import { setLang } from '../src/lib/i18n.jsx';
import { request, mutate } from '../src/lib/api.js';

vi.mock('../src/lib/api.js', async original => ({ ...await original(), request: vi.fn(), mutate: vi.fn() }));

let root, host, saved;
const endpoint = '/api/manage/assets';
const summary = { id: 'a', name: 'List row', kind: 'prompt', _summary: true, version: 2 };
const original = { id: 'a', name: 'Full row', kind: 'prompt', version: 5, content: 'Original', agent_ids: ['codex'], preserved: { metadata: 'yes' } };
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement('div');
  document.body.appendChild(host);
  root = createRoot(host);
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  setLang('zh');
  vi.resetAllMocks();
  saved = vi.fn();
  request.mockResolvedValue(original);
  mutate.mockResolvedValue({ ...original, version: 6 });
});
afterEach(async () => { await act(() => root.unmount()); host.remove(); });
const render = async (kind, item = null) => act(() => root.render(<GlobalAssetEditor kind={kind} item={item} onSaved={saved} onClose={() => {}} />));
const button = name => [...host.querySelectorAll('button')].find(node => node.textContent === name);
const click = async name => act(() => button(name).click());
async function fill(el, value) {
  await act(() => {
    Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value').set.call(el, value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
  });
}

it('loads the full definition before editing, preserves extension fields, and uses its current version', async () => {
  await render('prompt', summary);
  expect(request.mock.calls[0][0]).toBe(endpoint + '/a');
  expect(host.querySelector('textarea').value).toBe('Original');
  await fill(host.querySelector('textarea'),'Edited');
  await click('保存共享定义');
  expect(mutate).toHaveBeenCalledWith(endpoint + '/a','PATCH',{ name: 'Full row', kind:'prompt',content:'Edited',preserved:{ metadata:'yes' },agent_ids:['codex'],expected_version:5 });
  expect(saved).toHaveBeenCalledWith(expect.objectContaining({ version: 6 }));
});

it('creates an unassigned global rule without a host write and rejects incomplete raw JSON', async () => {
  await render('prompt');
  await click('高级：编辑完整定义');
  await fill(host.querySelector('textarea'),'{');
  expect(button('保存共享定义').disabled).toBe(true);
  await fill(host.querySelector('textarea'),JSON.stringify({ name:'Rule', content:'Brief', kind:'prompt', agent_ids:['codex'] }));
  await click('保存共享定义');
  expect(mutate).toHaveBeenCalledTimes(1);
  expect(mutate).toHaveBeenCalledWith(endpoint,'POST',{ name:'Rule',content:'Brief',kind:'prompt',agent_ids:[] });
});

it('includes MCP bindings and explicit native review when saving a tool that needs review', async () => {
  const tool = { id:'a',kind:'mcp', version:6, name:'Tool', config:{command:'local-tool',args:['--safe']}, native_review_required:true, agent_ids:[] };
  request.mockResolvedValue(tool);
  await render('mcp',{ ...summary, kind:'mcp' });
  const review = [...host.querySelectorAll('label')].find(el => el.textContent.includes('已核对工具连接'));
  expect(review.querySelector('input').checked).toBe(false);
  await act(() => review.querySelector('input').click());
  await click('保存共享定义');
  expect(mutate).toHaveBeenCalledWith(endpoint+'/a','PATCH',expect.objectContaining({ mcp_reviewed:true,config:tool.config,expected_version:6 }));
});

it('requires deletion confirmation and retains the dialog on a stale version', async () => {
  mutate.mockRejectedValueOnce(Object.assign(new Error('Changed'),{ status:409 }));
  await render('prompt',summary);
  await click('删除共享定义');
  expect(mutate).not.toHaveBeenCalled();
  await click('取消');
  expect(host.querySelector('textarea').value).toBe('Original');
  await click('删除共享定义');
  await click('确认删除定义');
  expect(mutate).toHaveBeenCalledWith(endpoint+'/a?expected_version=5','DELETE');
  expect(saved).not.toHaveBeenCalled();
  expect(host.querySelector('[role=alert]')).toBeTruthy();
  request.mockResolvedValue({ ...original, version:7, content:'Latest definition' });
  await click('放弃本次编辑并读取最新版');
  expect(host.querySelector('textarea').value).toBe('Latest definition');
  await click('删除共享定义');
  await click('确认删除定义');
  expect(mutate).toHaveBeenCalledWith(endpoint+'/a?expected_version=7','DELETE');
  expect(saved).toHaveBeenCalledWith(null);
});

it('rejects a mismatched detail response before exposing edit or delete actions', async () => {
  request.mockResolvedValue({ ...original, id:'unexpected' });
  await render('prompt', summary);
  expect(host.querySelector('[role=alert]').textContent).toContain('资料详情与当前选择不一致');
  expect(button('保存共享定义')).toBeUndefined();
});
