// @vitest-environment jsdom
import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { setLang } from '../src/lib/i18n.jsx';
import { SharedBatchHistory, SharedBatchReceipt } from '../src/components/agent-management/SharedBatchReceipt.jsx';

const api = vi.hoisted(() => ({ mutate: vi.fn(), request: vi.fn() }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), ...api }));
const base = '/api/manage/shared-batches';
const example = {
  id: 'batch-1', version: 4, status: 'partial', operation: 'install',
  targets: [
    { host_id: 'a', status: 'applied', plan_id: 'one', can_apply: false, asset_names: { tool: 'My tool' }, asset_versions: { tool: 1 }, file_readback: 'verified', host_reload: 'not_run' },
    { host_id: 'b', status: 'failed', can_repreview: true, error_code: 'apply_conflict' },
    { host_id: 'c', status: 'recovery_required', plan_id: 'three', error_code: 'recovery_required' },
  ],
};
const planned = {
  ...example, version: 5, targets: example.targets.map(target => target.host_id === 'b' ? {
    ...target, error_code: null, status: 'planned', can_apply: true, can_repreview: false,
    files: [{ path: 'example.cfg', diff: '+tool = true' }],
  } : target),
};
const hosts = ['a', 'b', 'c'].map(id => ({ id, name: 'Agent ' + id }));
const recovery = vi.fn();
function Screen({ initial }) {
  const [batch, setBatch] = useState(initial);
  return <SharedBatchReceipt hosts={hosts} batch={batch} onUpdate={setBatch} onClose={() => {}} onRecovery={recovery} />;
}
let root;
beforeEach(() => {
  document.body.replaceChildren(document.createElement('main'));
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  root = createRoot(document.querySelector('main'));
  setLang('zh');
  vi.resetAllMocks();
});
afterEach(() => act(() => root.unmount()));
const render = node => act(async () => root.render(node));
const button = prefix => [...document.querySelectorAll('button')].find(el => el.textContent.startsWith(prefix));
const confirmBox = () => [...document.querySelectorAll('label')].find(el => el.textContent.includes('我已核对'))?.querySelector('input');
const click = node => act(async () => { expect(node).toBeTruthy(); node.click(); });

it('repreviews only unsuccessful eligible targets, then requires a new confirmation to apply', async () => {
  api.mutate.mockResolvedValueOnce(planned).mockResolvedValueOnce({ ...planned, version: 6 });
  await render(<Screen initial={example} />);
  expect(document.body.textContent).toContain('My tool');
  expect(document.body.textContent).toContain('文件或配置已变更');
  await click([...document.querySelectorAll('.matrix-receipt')].at(-1).querySelector('button'));
  expect(recovery).toHaveBeenCalledWith('c', 'three');
  await click(button('重新预览失败目标'));
  expect(api.mutate).toHaveBeenCalledWith(base + '/batch-1/preview', 'POST', { expected_version: 4, host_ids: ['b'] });
  expect(document.body.textContent).toContain('+tool = true');
  expect(button('应用已预览').disabled).toBe(true);
  await click(confirmBox());
  await click(button('应用已预览'));
  expect(api.mutate).toHaveBeenLastCalledWith(base + '/batch-1/apply', 'POST', { expected_version: 5, host_ids: ['b'], confirmed: true });
  expect(button('应用已预览').disabled).toBe(true);
});

it('locks writes after an indeterminate result and reads back instead of resending', async () => {
  api.mutate.mockRejectedValueOnce(new Error('timeout'));
  api.request.mockRejectedValueOnce(new Error('disconnected')).mockResolvedValueOnce({
    ...planned, version: 6, targets: [{ host_id: 'b', status: 'applied', plan_id: 'two', file_readback: 'verified' }],
  });
  await render(<Screen initial={planned} />);
  await click(confirmBox());
  await click(button('应用已预览'));
  expect(document.body.textContent).toContain('暂时无法确认本次结果');
  expect(confirmBox().disabled).toBe(true);
  await click(button('刷新回执'));
  expect(button('应用已预览')).toBeUndefined();
  expect(document.body.textContent).toContain('已验证');
  expect(api.mutate).toHaveBeenCalledTimes(1);
});

it('reloads a changed revision and clears confirmation before any further apply', async () => {
  api.mutate.mockRejectedValueOnce(Object.assign(new Error('changed'), { status: 409 }));
  api.request.mockResolvedValueOnce({ ...planned, version: 6 });
  await render(<Screen initial={planned} />);
  await click(confirmBox());
  await click(button('应用已预览'));
  expect(button('应用已预览').disabled).toBe(true);
  expect(document.body.textContent).toContain('已重新读取回执');
});

it('reads full detail when opening a persistent history entry', async () => {
  const open = vi.fn();
  api.request.mockResolvedValueOnce({ items: [{ ...example, created_at: '2026-05-06T09:00:00Z', counts: { applied: 1, failed: 2 } }], total: 1 })
    .mockResolvedValueOnce(example);
  await render(<SharedBatchHistory revision={0} onOpen={open} />);
  expect(api.request.mock.calls[0][0]).toBe(base + '?offset=0&limit=10');
  await click(document.querySelector('tbody button'));
  expect(api.request.mock.calls[1][0]).toBe(base + '/batch-1');
  expect(open).toHaveBeenCalledWith(example);
});
