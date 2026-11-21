// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const h = vi.hoisted(() => ({ request: vi.fn(), mutate: vi.fn(), data: null }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), mutate: h.mutate, request: h.request }));
vi.mock('../src/lib/useResource.js', () => ({ useResource: () => ({ data: h.data, loading: false, reload() {} }) }));
import { SharedResourceMatrix, ConfigurationBackup } from '../src/components/agent-management/SharedResources.jsx';
import { setLang } from '../src/lib/i18n.jsx';

let root;
const hosts = ['codex', 'claude-code'].map(id => ({ id, name: id, capabilities: { mcp: true } }));
const byText = label => [...document.querySelectorAll('button')].find(node => node.textContent.includes(label));
const click = async element => act(async () => { expect(element).toBeTruthy(); element.click(); });
const batch = (targets, version = 1) => ({ id: 'batch-a', operation: 'install', version,
  status: targets.every(t => t.status === 'applied') ? 'applied' : 'planned', targets });
const target = (id, status = 'planned') => ({ host_id: id, status, plan_id: 'plan-' + id,
  can_apply: status === 'planned', can_repreview: status === 'failed', files: [{ path: '/' + id, diff: '+tool' }],
  asset_versions: { a: 3 }, asset_names: { a: 'Tool' },
});
async function mountMatrix() {
  await act(async () => root.render(<SharedResourceMatrix hostId="codex" allHosts={hosts} revision={0} />));
  await click(document.querySelector('input[aria-label^="Tool · Codex"]'));
  await click(document.querySelector('input[aria-label^="Tool · Claude Code"]'));
}
beforeEach(() => {
  document.body.innerHTML = '<div id="root"></div>';
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  root = createRoot(document.getElementById('root'));
  setLang('en'); h.mutate.mockReset(); h.request.mockReset();
  h.data = { items: [{ id: 'a', name: 'Tool', version: 3, cells: {
    codex: { status: 'verified' }, 'claude-code': { status: 'not_managed' }
  } }], limit: 15, total: 1, summary: true, offset: 0 };
});
afterEach(async () => { await act(async () => root.unmount()); });

it('persists one versioned preview and applies only the confirmed targets', async () => {
  await mountMatrix();
  const ids = ['codex','claude-code'];
  h.mutate.mockResolvedValueOnce(batch(ids.map(id => target(id)))).mockResolvedValueOnce(batch(ids.map(id => target(id, 'applied')), 2));
  await click(byText('Preview selected changes (2)'));
  expect(h.mutate).toHaveBeenCalledWith('/api/manage/shared-batches','POST', {
    operation: 'install', targets: ids.map(host_id => ({ host_id, asset_versions: { a: 3 } })),
  });
  const modal = document.querySelector('dialog[open]');
  const confirm = modal.querySelector('.matrix-confirm button');
  expect(confirm.disabled).toBe(true);
  await click(modal.querySelector('.matrix-confirm input'));
  await click(confirm);
  expect(h.mutate).toHaveBeenLastCalledWith('/api/manage/shared-batches/batch-a/apply','POST', {
    expected_version: 1, host_ids: ids, confirmed: true,
  });
  expect(modal.textContent).toContain('Applied');
});

it('keeps blocked shared-file targets visible and excludes them from application', async () => {
  await mountMatrix();
  const blocked = { ...target('claude-code','failed'), plan_id: null, error_code: 'shared_target', files: [] };
  h.mutate.mockResolvedValueOnce(batch([target('codex'), blocked])).mockResolvedValueOnce(batch([target('codex','applied'), blocked], 2));
  await click(byText('Preview selected changes (2)'));
  const modal = document.querySelector('dialog[open]');
  expect(modal.querySelectorAll('.matrix-receipt')).toHaveLength(2);
  expect(modal.textContent).toContain('Failed');
  await click(modal.querySelector('.matrix-confirm input'));
  await click(modal.querySelector('.matrix-confirm button'));
  expect(h.mutate).toHaveBeenLastCalledWith('/api/manage/shared-batches/batch-a/apply','POST', {
    confirmed: true, expected_version: 1, host_ids: ['codex'],
  });
});

it('requires the current reviewed preview for restoring a backup', async () => {
  await act(async () => root.render(<ConfigurationBackup />));
  await click(byText('Restore configuration backup'));
  const input = document.querySelector('input[type=file]');
  const data = { format: 'senrelay-management', version: 1, objects: [] };
  Object.defineProperty(input, 'files', { value: [{ name: 'test.json', size: 30, text: async () => JSON.stringify(data) }] });
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })));
  h.mutate.mockResolvedValueOnce({ status: 'preview', preview_id: 'p1', changed: 1, items: [{ id: 'a', action: 'create', name: 'Saved tool' }] });
  await click(byText('Preview restore changes'));
  expect(document.body.textContent).toContain('Saved tool');
  expect(h.mutate.mock.calls[0][0]).toBe('/api/manage/configuration-backup/preview');
  h.mutate.mockResolvedValueOnce({ status: 'restored', preview_id: 'p1', changed: 1 });
  await click(byText('Restore definitions'));
  expect(h.mutate.mock.calls[1][2]).toEqual({ data, replace_existing: false, preview_id: 'p1', confirmed: true });
  expect(document.body.textContent).toContain('Restored 1 definitions');
});

it('requires review of omitted MCP credentials and displays the selected local account', async () => {
  const { McpAccountFields } = await import('../src/components/agent-management/McpAccountFields.jsx');
  const { AssetTools } = await import('../src/components/AssetTools.jsx');
  h.data = { items: [
    { id: 'account_local', name: 'Local key', kind: 'api_key', has_credential: true },
    { id: 'account_empty', name: 'Missing key', kind: 'api_key', has_credential: false },
  ] };
  let current;
  function Editor() {
    const [draft, setDraft] = React.useState({ id: 'asset_tool', kind: 'mcp', name: 'Tools', version: 1,
      credential_reattach_required: true, config: { transport: 'http', url: 'https://example.test/mcp' },
      mcp_fields_to_review: [{ location: 'headers', name: 'Authorization', bearer: true }] });
    current = draft;
    return <><McpAccountFields draft={draft} update={patch => setDraft({ ...draft, ...patch })} /><AssetTools item={draft} onChanged={() => {}} /></>;
  }
  await act(async () => root.render(<Editor />));
  expect([...document.querySelectorAll('button')].find(b => b.textContent === 'Verify tool discovery').disabled).toBe(true);
  await click(byText('Authorization'));
  const choice = document.querySelector('select');
  expect([...choice.options].map(o => o.value)).toEqual(['', 'account_local']);
  await act(async () => {
    choice.value = 'account_local'; choice.dispatchEvent(new Event('change', { bubbles: true }));
  });
  await click(byText('Add field binding'));
  expect(current.mcp_account_bindings).toEqual([{ name: 'Authorization', location: 'headers', account_id: 'account_local', bearer: true }]);
  expect(document.body.textContent).toContain('Local key');
  expect(current.mcp_reviewed).toBe(false);
  await click(document.querySelector('input[type=checkbox]'));
  expect(current.mcp_reviewed).toBe(true);
  expect(h.mutate).not.toHaveBeenCalled();
});
