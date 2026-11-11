// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
const api = vi.hoisted(() => ({ request: vi.fn(), mutate: vi.fn() }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), ...api }));
import { ConnectionPlanDialog } from '../src/components/ConnectionPlanDialog.jsx';
import { setLang } from '../src/lib/i18n.jsx';
let root, container;
const setup = { id: 'omp', label: 'OMP', data: { plan_supported: true, surfaces: ['cli'], installed_components: ['mcp', 'skill'] } };
const plan = { id: 'plan_test', version: 1, status: 'planned', operation: 'install', capabilities: ['context_tools'], surface: 'cli', files: [{ path: '/isolated/.omp/agent/mcp.json', before_hash: null }], readback: 'not_run', dependencies: [] };
const changed = vi.fn();
const button = label => [...container.querySelectorAll('button')].find(b => b.textContent.trim() === label);
const checkbox = () => container.querySelector('input[type=checkbox]');
async function click(node) { expect(node).toBeTruthy(); await act(async () => node.click()); }
async function mount(data = setup) { await act(async () => root.render(<ConnectionPlanDialog setup={data} onClose={vi.fn()} onChanged={changed} />)); }
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  setLang('zh'); api.request.mockReset(); api.mutate.mockReset(); changed.mockReset();
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
  api.mutate.mockResolvedValue(plan);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

it('requires an explicit capability and separates preview from apply', async () => {
  await mount();
  expect(button('预览所选改动').disabled).toBe(true);
  expect([...container.querySelectorAll('input[type=checkbox]')].every(c => !c.checked)).toBe(true);
  await click(checkbox()); await click(button('预览所选改动'));
  expect(api.mutate).toHaveBeenCalledWith('/api/connections/omp/plans', 'POST', {operation:'install', capabilities:['context_tools'], surface:'cli'});
  expect(button('确认执行计划').disabled).toBe(true);
  expect(api.mutate).toHaveBeenCalledTimes(1);
});

it('requires a target entry when there is more than one surface', async () => {
  await mount({...setup, data:{...setup.data, surfaces:['desktop','cli']}});
  await click(checkbox());
  expect(button('预览所选改动').disabled).toBe(true);
  expect(api.mutate).not.toHaveBeenCalled();
});

it('accepts success only after a verified status readback', async () => {
  await mount(); await click(checkbox()); await click(button('预览所选改动'));
  await click(checkbox());
  api.mutate.mockResolvedValue({...plan, status:'applied', readback:'verified'});
  api.request.mockResolvedValue({...plan, status:'applied', readback:'verified'});
  await click(button('确认执行计划'));
  expect(api.request).toHaveBeenCalledWith('/api/connections/omp/plans/plan_test');
  expect(container.textContent).toContain('助手实际加载与真实调用仍待验证');
  expect(changed).toHaveBeenCalledTimes(1);
});

it('blocks resubmission when both submission and readback have unknown results', async () => {
  await mount(); await click(checkbox()); await click(button('预览所选改动')); await click(checkbox());
  api.mutate.mockRejectedValue(new Error('Network unavailable')); api.request.mockRejectedValue(new Error('Network unavailable'));
  await click(button('确认执行计划'));
  expect(container.textContent).toContain('操作结果尚不确定');
  expect(button('确认执行计划')).toBeUndefined();
  expect(button('重新选择并预览')).toBeUndefined();
  expect(changed).not.toHaveBeenCalled();
  api.request.mockResolvedValue({...plan, status:'applied', readback:'verified'});
  await click(button('重新读取结果'));
  expect(changed).toHaveBeenCalledTimes(1);
});

it('shows interrupted plans and requires fresh consent for recovery', async () => {
  await mount(); await click(checkbox()); await click(button('预览所选改动')); await click(checkbox());
  api.mutate.mockRejectedValue(new Error('Interrupted'));
  api.request.mockResolvedValue({...plan, status:'recovery_required'});
  await click(button('确认执行计划'));
  expect(container.textContent).toContain('配置待恢复');
  expect(button('恢复本次改动').disabled).toBe(true);
  await click(checkbox());
  api.mutate.mockResolvedValue({...plan, status:'recovered'});
  api.request.mockResolvedValue({...plan, status:'recovered'});
  await click(button('恢复本次改动'));
  expect(api.mutate).toHaveBeenLastCalledWith('/api/connections/omp/plans/plan_test/recover', 'POST', {version:1,confirmed:true});
  expect(container.textContent).toContain('已恢复操作前的配置');
});

it('submits once on rapid repeated confirmation clicks', async () => {
  await mount(); await click(checkbox()); await click(button('预览所选改动')); await click(checkbox());
  let resolve;
  api.mutate.mockImplementation(() => new Promise(done => { resolve = done; }));
  api.request.mockResolvedValue({...plan,status:'applied',readback:'verified'});
  const confirm = button('确认执行计划');
  await act(async () => { confirm.click(); confirm.click(); });
  expect(api.mutate).toHaveBeenCalledTimes(2); // one preview and one application
  await act(async () => resolve({...plan,status:'applied'}));
});

it('renders the permission selection in English', async () => {
  setLang('en'); await mount();
  expect(container.textContent).toContain('Context tools (MCP)');
  expect(container.textContent).not.toMatch(/[\u4e00-\u9fff]/);
});

it('refreshes installed capabilities before preparing a follow-up operation', async () => {
  await mount({...setup, data:{...setup.data, installed_components:[]}});
  await click(checkbox()); await click(button('预览所选改动')); await click(checkbox());
  api.request.mockResolvedValue({...plan,status:'applied',readback:'verified'});
  await click(button('确认执行计划'));
  api.request.mockResolvedValue({...setup.data, installed_components:['mcp']});
  await click(button('重新选择并预览'));
  expect(api.request).toHaveBeenLastCalledWith('/api/connections/omp/setup');
  const operation = container.querySelectorAll('select')[1];
  await act(async () => { operation.value = 'uninstall'; operation.dispatchEvent(new Event('change', {bubbles:true})); });
  expect(checkbox().disabled).toBe(false);
  expect(checkbox().checked).toBe(false);
  expect(container.querySelectorAll('input[type=checkbox]')[1].disabled).toBe(true);
});

it.each(['zh', 'en'])('explains selected component effects and risks before consent (%s)', async lang => {
  setLang(lang);
  api.mutate.mockResolvedValue({ ...plan, capabilities: ['context_tools', 'continuation', 'lifecycle'], dependencies: ['context_tools'] });
  await mount();
  await click(checkbox());
  await click(button(lang === 'zh' ? '预览所选改动' : 'Preview selected changes'));
  const text = container.textContent;
  expect(text).toContain(lang === 'zh' ? '本次改动的影响' : 'Effect of this change');
  expect(text).toContain(lang === 'zh' ? '本机队列' : 'enqueue selected events locally');
  expect(text).toContain(lang === 'zh' ? '可能产生费用' : 'may incur costs');
  expect(text).toContain(lang === 'zh' ? '不会自动发送消息' : 'does not send messages automatically');
  expect(container.querySelector('.consent input').checked).toBe(false);
  expect(api.mutate).toHaveBeenCalledTimes(1);
});
