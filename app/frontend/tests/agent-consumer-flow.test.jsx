// @vitest-environment jsdom
import React, { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const h = vi.hoisted(() => ({ data: {}, paths: [], mutate: vi.fn(), request: vi.fn() }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), mutate: h.mutate, request: h.request }));
vi.mock('../src/lib/useResource.js', async original => ({ ...await original(), useResource(path) { h.paths.push(path); return { data: h.data[path] ?? h.data[path?.split('?')[0]] ?? null, loading: false, error: null, reload() {} }; } }));
import { AgentManagementPage } from '../src/pages/AgentManagementPage.jsx';
import { AssistantConnectionModes } from '../src/components/agent-management/AssistantConnectionModes.jsx';
import { AccountOperation } from '../src/components/agent-management/AccountOperation.jsx';
import { AgentPlanDialog } from '../src/components/agent-management/AgentPlanDialog.jsx';
import { ProviderChecks } from '../src/components/ProviderChecks.jsx';
import { AssetTools } from '../src/components/AssetTools.jsx';
import { BundleAssetSelection, CatalogChoice, SharedLibrary, catalogPath } from '../src/components/agent-management/CatalogControls.jsx';
import { setLang } from '../src/lib/i18n.jsx';
const host = { id: 'codex', name: 'Codex CLI', configuration_status: 'supported_format', verification_status: 'not_verified', capabilities: { mcp: true, prompts: true, skills: true, providers: true, native_activation: true } };
const provider = { agent_ids: ['codex'], id: 'provider_fixture', name: 'Local synthetic provider', protocol: 'openai', base_url: 'http://127.0.0.1:18999/v1', version: 2, enabled: true, model_map: {} };
let root, container;
async function mount(section = 'providers', draftStore) { await act(async () => { root.render(<AgentManagementPage agentId="codex" section={section} draftStore={draftStore} onAgentChange={() => {}} projects={[]} revision={0} onChanged={() => {}} />); }); }
function dialog() { return document.querySelector('dialog[open]'); }
function field(label, parent = dialog()) { const item = [...parent.querySelectorAll('label.field')].find(node => node.querySelector('span')?.textContent === label); if (!item) throw new Error(`Missing field ${label}`); return item.querySelector('input,select,textarea'); }
function button(text, parent = document) { const item = [...parent.querySelectorAll('button')].find(node => node.textContent === text); if (!item) throw new Error(`Missing button ${text}`); return item; }
async function click(text, parent) { await act(async () => { button(text, parent).click(); }); }
async function change(node, value) { await act(async () => { Object.getOwnPropertyDescriptor(node.tagName === 'SELECT' ? HTMLSelectElement.prototype : node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set.call(node, value); node.dispatchEvent(new Event(node.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); }); }
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  setLang('zh');
  window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  h.data = { '/api/manage/hosts': [host], '/api/manage/providers': [provider], '/api/manage/assets': [], '/api/manage/models': [], '/api/manage/accounts': [], '/api/manage/bindings': [], '/api/manage/provider-presets': { presets: [{ id: 'official-fixture', name: 'Official preset fixture', base_url: 'https://provider.example/v1', protocol: 'anthropic', source_url: 'https://provider.example/docs' }], network_access: false } };
  h.paths = []; h.mutate.mockReset(); h.request.mockReset();
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); });

describe('ordinary assistant configuration forms', () => {
  it('shows a repair action for an unproven legacy model selection without choosing an account or model automatically', async () => {
    h.data['/api/manage/hosts/codex/state'] = { selection: { status: 'not_checked', receipt_unknown: true }, resources: {}, coverage: 'complete_owned_entries' };
    await mount();
    expect(container.querySelector('.agent-current-configuration').textContent).toContain('旧版模型选择记录不完整，请先选择并启用当前模型，再继续管理连接。');
    expect(h.mutate).not.toHaveBeenCalled(); expect(h.request).not.toHaveBeenCalled();
  });

  it('reviews human resource names and the selected model while keeping native paths in collapsed advanced details', async () => {
    const plan = { status: 'planned', payload: { metadata: { host_id: 'codex', operation: 'install', configuration_changes: [{ name: 'My research tool', kind: 'mcp', operation: 'install' }, { name: 'My writing skill', kind: 'skill', operation: 'install' }], model_selection: { provider_name: 'My service', native_model: 'everyday-model' } }, files: [{ path: '/synthetic/.codex/config.toml', diff: { entries: [] } }] } };
    await act(async () => root.render(<AgentPlanDialog plan={plan} agent="Codex CLI" category="项目配置组合" valid onClose={() => {}} onApply={() => {}} />));
    expect(document.querySelector('.agent-resource-changes').textContent).toContain('My research tool');
    expect(document.querySelector('.agent-resource-changes').textContent).toContain('My writing skill');
    expect(document.querySelector('.agent-change-summary').textContent).toContain('My service');
    expect(document.querySelector('.agent-change-summary').textContent).toContain('everyday-model');
    const technical = document.querySelector('.agent-file-details');
    expect(technical.open).toBe(false);
    expect(technical.textContent).not.toContain('/synthetic/.codex/config.toml');
    await act(async () => { technical.open = true; technical.dispatchEvent(new Event('toggle')); });
    expect(technical.textContent).toContain('/synthetic/.codex/config.toml');
  });

  it('reads the current selection back in the result and keeps an unread host state unlabeled', async () => {
    const plan = { status: 'applied', payload: { metadata: { host_id: 'codex', operation: 'uninstall', provider_id: 'provider_fixture', entity_versions: { provider_fixture: 2 } }, readback: 'verified', host_reload: 'not_run', real_request: 'not_run', files: [] } };
    const hostReadback = { state: { host_id: 'codex', selection: { provider_name: 'Local synthetic provider', model_id: 'current-native', status: 'verified' }, effects: { selection: 'recorded', configuration: 'verified', host_loaded: 'not_verified', reachability: 'not_checked', authentication: 'not_checked', invocation: 'not_checked' }, coverage: 'complete_owned_entries' }, resources: [{ id: 'provider_fixture', name: 'Local synthetic provider', status: 'removed' }] };
    await act(async () => root.render(<AgentPlanDialog plan={plan} result={plan} hostReadback={hostReadback} agent="Codex CLI" category="模型与账号" valid onClose={() => {}} onApply={() => {}} />));
    const readback = document.querySelector('.agent-host-readback');
    expect(readback.textContent).toContain('Local synthetic provider');
    expect(readback.textContent).toContain('current-native');
    expect(readback.textContent).toContain('已从助手移除');
    expect(readback.textContent).toContain('尚未检查。写入配置不能说明网络可达。');
    await act(async () => root.render(<AgentPlanDialog plan={plan} result={plan} agent="Codex CLI" category="模型与账号" valid onClose={() => {}} onApply={() => {}} />));
    expect(dialog().textContent).toContain('应用后的本机配置还没有读回');
    expect(dialog().textContent).not.toContain('已从助手移除');
    setLang('en');
    await act(async () => root.render(<AgentPlanDialog plan={plan} result={plan} hostReadback={hostReadback} agent="Codex CLI" category="模型与账号" valid onClose={() => {}} onApply={() => {}} />));
    expect(document.querySelector('.agent-host-readback').textContent).toContain('Configuration read back after applying');
    expect(document.querySelector('.agent-host-readback').textContent).toContain('Removed from this assistant');
  });

  it('stops polling an abandoned authorization at its deadline without retrying the official flow', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-26T04:00:00Z'));
    try {
      const deadline = Date.now() / 1000 + 3;
      h.mutate.mockResolvedValue({ account_id: 'pending_account', status: 'authorizing', expires_at: deadline, authorization_url: 'https://official.example/authorize' });
      h.request.mockResolvedValue({ id: 'pending_account', kind: 'oauth', status: 'authorizing', authorization_expires_at: deadline });
      await act(async () => root.render(<AccountOperation action="authorize" agent="Codex CLI" item={{ id: 'pending_account', kind: 'oauth' }} onChanged={() => {}} onClose={() => {}} />));
      await click('确认操作', dialog());
      expect(dialog().textContent).toContain('等待官方授权');
      await act(async () => { await vi.advanceTimersByTimeAsync(2500); });
      expect(h.request).toHaveBeenCalledTimes(1);
      await act(async () => { await vi.advanceTimersByTimeAsync(31000); });
      expect(dialog().textContent).toContain('授权已过期');
      expect(dialog().querySelector('a')).toBeNull();
      expect(h.request).toHaveBeenCalledTimes(1); expect(h.mutate).toHaveBeenCalledTimes(1);
      await act(async () => root.render(null)); expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });

  it('shows an OAuth account expiring while the page is open without a credential read or refresh request', async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date('2026-10-26T04:00:00Z'));
    try {
      h.data['/api/manage/accounts'] = [{ id: 'account_expiring', name: 'Expiring official account', agent_ids: ['codex'], kind: 'oauth', status: 'authorized', expires_at: Date.now() / 1000 + 2 }];
      await mount('accounts');
      const card = container.querySelector('.management-card');
      expect(card.textContent).toContain('已授权');
      await act(async () => { await vi.advanceTimersByTimeAsync(2200); });
      expect(card.textContent).toContain('授权已过期');
      expect(card.textContent).not.toContain('已授权');
      expect(h.mutate).not.toHaveBeenCalled(); expect(h.request).not.toHaveBeenCalled();
      await act(async () => root.render(null));
      expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); }
  });

  it('explicitly chooses account credentials without silently deleting a separately saved provider key', async () => {
    h.data['/api/manage/providers'] = [{ ...provider, has_credential: true }];
    h.data['/api/manage/accounts'] = [{ id: 'account_fixture', name: 'Saved account', kind: 'api_key', provider_id: provider.id, has_credential: true }];
    await mount(); await click('编辑');
    expect(field('凭据来源').value).toBe('provider');
    await change(field('绑定授权账号'), 'account_fixture');
    expect(field('凭据来源').value).toBe('account');
    await click('预览保存', dialog()); h.mutate.mockResolvedValue({ ...provider, version: 3 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers/provider_fixture', 'PATCH', expect.objectContaining({ account_id: 'account_fixture', credential_source: 'account', expected_version: 2 }));
    expect(h.mutate.mock.calls.some(([path]) => path.endsWith('/credential') || path.includes('/plans'))).toBe(false);
  });

  it('requires an explicit model capacity in the ordinary Kimi setup form', async () => {
    const kimi = { ...host, id: 'kimi-code', name: 'Kimi Code CLI', provider_protocols: ['openai', 'anthropic'] };
    h.data['/api/manage/hosts'] = [kimi]; h.data['/api/manage/providers'] = [];
    await act(async () => root.render(<AgentManagementPage agentId="kimi-code" section="providers" projects={[]} revision={0} onChanged={() => {}} />));
    await click('添加服务商'); await change(field('名称'), 'Kimi native service'); await change(field('服务地址'), 'https://fixture.example/v1'); await change(field('默认请求模型'), 'kimi-fixture');
    expect(field('上下文窗口').required).toBe(true);
    expect(button('保存连接与模型', dialog()).disabled).toBe(true);
    await change(field('上下文窗口'), '0'); expect(button('保存连接与模型', dialog()).disabled).toBe(true);
    await change(field('上下文窗口'), '32768');
    h.mutate
      .mockResolvedValueOnce({ provider: { ...provider, id: 'kimi_provider' }, model: { id: 'kimi_model' } })
      .mockResolvedValueOnce({ id: 'plan_kimi', version: 1, status: 'planned', payload: { metadata: { host_id: 'kimi-code' }, files: [] } });
    await click('保存连接与模型', dialog());
    expect(h.mutate.mock.calls[0]).toEqual(['/api/manage/providers/setup', 'POST', expect.objectContaining({ host_id: 'kimi-code', context_window: 32768, model_id: 'kimi-fixture' })]);
  });

  it('preserves the actually applied selection when its dependent account needs an update', async () => {
    h.data['/api/manage/hosts/codex/state'] = { selection: { provider_id: provider.id, provider_name: provider.name, model_id: 'previously-applied-model', status: 'update_available' }, resources: { [provider.id]: { status: 'update_available', paths: 1 } } };
    await mount();
    const current = document.querySelector('.agent-current-configuration');
    expect(current.textContent).toContain(provider.name);
    expect(current.textContent).toContain('previously-applied-model');
    expect(current.textContent).toContain('定义已更新，待应用');
  });

  it('copies a provider without reusing its bound account or pretending a copied key is present', async () => {
    h.data['/api/manage/providers'] = [{ ...provider, has_credential: true, account_id: 'original_account', credential_source: 'account' }];
    await mount();
    await act(async () => container.querySelector('button[aria-label*="复制定义"]').click());
    expect(field('绑定授权账号').value).toBe('');
    expect(field('凭据来源').value).toBe('provider');
    await click('预览保存', dialog()); h.mutate.mockResolvedValue({ id: 'copy', version: 1 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers', 'POST', expect.objectContaining({ credential_source: 'provider', credential_reattach_required: true }));
    expect(h.mutate.mock.calls[0][2]).not.toHaveProperty('account_id');
    expect(h.data['/api/manage/providers'][0].account_id).toBe('original_account');
  });

  it('retains an unknown legacy API until edited and clears obsolete declarations when protocol changes', async () => {
    await mount(); await click('编辑');
    expect(field('OpenAI 接口格式').value).toBe('');
    expect(h.mutate).not.toHaveBeenCalled();
    await change(field('OpenAI 接口格式'), 'responses');
    await change(field('协议'), 'anthropic');
    await click('预览保存', dialog()); h.mutate.mockResolvedValue({ ...provider, version: 3 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers/provider_fixture', 'PATCH', expect.objectContaining({ protocol: 'anthropic', wire_api: null, supported_endpoints: ['messages'], expected_version: 2 }));
  });

  it('keeps preset API support but narrows the declaration after editing the API address', async () => {
    h.data['/api/manage/provider-presets'].presets = [{ id: 'dual-api', name: 'Dual API fixture', protocol: 'openai', base_url: 'https://fixture.example/v1', supported_endpoints: ['chat/completions', 'responses'] }];
    await mount(); await click('添加服务商');
    await change(field('选择服务商'), 'dual-api');
    expect(field('OpenAI 接口格式').value).toBe('responses');
    const both = [...dialog().querySelectorAll('label')].find(node => node.textContent.includes('服务商文档明确支持两种 OpenAI 接口')).querySelector('input');
    expect(both.checked).toBe(true);
    await change(field('服务地址'), 'https://custom.example/v1');
    expect(both.checked).toBe(false);
    await change(field('默认请求模型'), 'fixture-model');
    h.mutate
      .mockResolvedValueOnce({ provider, model: { id: 'm' } })
      .mockResolvedValueOnce({ id: 'plan_dual', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } });
    await click('保存连接与模型', dialog());
    expect(h.mutate.mock.calls[0]).toEqual(['/api/manage/providers/setup', 'POST', expect.objectContaining({ provider: { name: 'Dual API fixture', base_url: 'https://custom.example/v1', protocol: 'openai', wire_api: 'responses', supported_endpoints: ['responses'] } })]);
  });

  it('explains a known API mismatch before allowing native activation', async () => {
    h.data['/api/manage/providers'] = [{ ...provider, wire_api: 'chat', supported_endpoints: ['chat/completions'] }];
    await mount();
    expect(button('选择并启用模型').disabled).toBe(true);
    expect(document.body.textContent).toContain('接口与 Codex CLI 的直连要求不同');
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('uses registry metadata to save a provider without JSON and without applying it', async () => {
    await mount(); await click('新建');
    await change(field('选择服务商'), 'official-fixture');
    expect(field('服务地址').value).toBe('https://provider.example/v1');
    await click('预览保存', dialog()); h.mutate.mockResolvedValue({ id: 'created', version: 1 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers', 'POST', expect.objectContaining({ name: 'Official preset fixture', base_url: 'https://provider.example/v1', protocol: 'anthropic', model_map: {} }));
    expect(h.mutate.mock.calls.some(([path]) => path.includes('/plans') || path.includes('/credential'))).toBe(false);
  });

  it('edits a tool URL while preserving credential references and external configuration without probing', async () => {
    const asset = { agent_ids: ['codex'], id: 'asset_mcp', version: 3, name: 'Team tool', kind: 'mcp', source: { type: 'manual' }, config: { url: 'https://tool.example/mcp', transport: 'http', headers: { Authorization: '{{credential:fixture-reference}}' }, custom_setting: { keep: true } }, outside_field: 'preserved' };
    h.data['/api/manage/assets'] = [asset];
    await mount('mcp'); await click('编辑'); await change(field('服务网址'), 'https://tool.example/updated');
    await click('预览保存', dialog()); h.mutate.mockResolvedValue({ ...asset, version: 4 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/asset_mcp', 'PATCH', expect.objectContaining({ expected_version: 3, outside_field: 'preserved', config: { ...asset.config, url: 'https://tool.example/updated' } }));
    expect(h.mutate.mock.calls.some(([path]) => path.endsWith('/probe'))).toBe(false);
  });

  it('creates an HTTP tool with the actual transport contract and retains edits across connection choices', async () => {
    await mount('mcp'); await click('新建'); await change(field('名称'), 'My HTTP tool');
    expect(field('工具连接方式').value).toBe('url');
    await change(field('工具连接方式'), 'command');
    await change(field('程序名称或路径'), 'fixture-command');
    await change(field('启动参数（每行一项）'), '--fixture\nread-only');
    await change(field('工具连接方式'), 'url'); await change(field('服务网址'), 'https://tool.example/mcp');
    await change(field('工具连接方式'), 'command');
    expect(field('程序名称或路径').value).toBe('fixture-command');
    expect(field('启动参数（每行一项）').value).toBe('--fixture\nread-only');
    await change(field('工具连接方式'), 'url'); expect(field('服务网址').value).toBe('https://tool.example/mcp');
    await click('预览保存', dialog()); h.mutate.mockResolvedValue({ id: 'asset_new', version: 1 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets', 'POST', expect.objectContaining({ config: { url: 'https://tool.example/mcp', transport: 'http' } }));
  });

  it('requires explicit catalog access and retains model input when refresh fails', async () => {
    h.data['/api/manage/providers/provider_fixture/models/catalog'] = { status: 'fresh', items: [{ id: 'cached-model', name: 'Cached model' }] };
    await mount(); await click('添加模型'); await change(field('供应商'), provider.id);
    await change(field('名称'), 'My model'); await change(field('请求模型'), 'cached-model');
    expect(dialog().querySelector('datalist option').value).toBe('cached-model'); expect(h.mutate).not.toHaveBeenCalled();
    await click('更新可选模型', dialog()); expect(h.mutate).not.toHaveBeenCalled();
    h.mutate.mockRejectedValueOnce(new Error('synthetic catalog unavailable'));
    await click('确认访问模型目录', dialog());
    expect(dialog().textContent).toContain('synthetic catalog unavailable'); expect(field('请求模型').value).toBe('cached-model');
    h.mutate.mockResolvedValueOnce({ id: 'saved_model', version: 1 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenLastCalledWith('/api/manage/models', 'POST', expect.objectContaining({ name: 'My model', provider_id: provider.id, model_id: 'cached-model', context_window: null }));
  });

  it('explains an empty fresh catalog and still saves a typed model without refresh or inference', async () => {
    h.data['/api/manage/providers/provider_fixture/models/catalog'] = { status: 'fresh', items: [] };
    await mount(); await click('添加模型'); await change(field('供应商'), provider.id);
    expect(dialog().textContent).toContain('已读取的模型目录是空的');
    expect(dialog().querySelectorAll('datalist option')).toHaveLength(0);
    expect(field('请求模型').disabled).toBe(false);
    await change(field('名称'), 'Manual empty catalog'); await change(field('请求模型'), 'provider-documented-model');
    expect(h.mutate).not.toHaveBeenCalled(); expect(h.request).not.toHaveBeenCalled();
    h.mutate.mockResolvedValueOnce({ id: 'saved_model', version: 1 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/models', 'POST', expect.objectContaining({ name: 'Manual empty catalog', provider_id: provider.id, model_id: 'provider-documented-model', context_window: null }));
    expect(h.mutate.mock.calls.some(([path]) => String(path).includes('/catalog') || String(path).includes('inference'))).toBe(false);
  });

  it('distinguishes providers that share a display name', async () => {
    h.data['/api/manage/providers'] = [provider, { ...provider, id: 'provider_other', base_url: 'https://other.example/v1' }];
    await mount(); await click('添加模型');
    const labels = [...field('供应商').options].map(option => option.textContent);
    expect(labels).toContain('Local synthetic provider · http://127.0.0.1:18999/v1');
    expect(labels).toContain('Local synthetic provider · https://other.example/v1');
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('does not describe an unread catalog as empty', async () => {
    h.data['/api/manage/providers/provider_fixture/models/catalog'] = { status: 'not_fetched', items: [] };
    await mount(); await click('添加模型'); await change(field('供应商'), provider.id);
    expect(dialog().textContent).not.toContain('已读取的模型目录是空的');
    expect(dialog().textContent).toContain('尚未获取');
    expect(field('请求模型').disabled).toBe(false);
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('keeps unsupported catalog protocols manual without attempting discovery', async () => {
    h.data['/api/manage/providers'] = [{ ...provider, protocol: 'gemini' }];
    await mount(); await click('添加模型'); await change(field('供应商'), provider.id);
    expect(dialog().textContent).toContain('暂不支持读取模型目录');
    expect([...dialog().querySelectorAll('button')].some(node => node.textContent === '更新可选模型')).toBe(false);
    expect(h.paths).not.toContain('/api/manage/providers/provider_fixture/models/catalog');
    await change(field('名称'), 'Manual Gemini model'); await change(field('请求模型'), 'provider-documented-model');
    h.mutate.mockResolvedValue({ id: 'model', version: 1 }); await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/models', 'POST', expect.objectContaining({ model_id: 'provider-documented-model' }));
  });

  it('does not revive unsupported catalog or inference actions through advanced provider checks', async () => {
    await act(async () => { root.render(<ProviderChecks provider={{ ...provider, protocol: 'gemini' }} contextTitle="Codex CLI" />); });
    await click('目录与独立检查');
    expect(button('明确刷新远程目录', dialog()).disabled).toBe(true);
    expect(button('确认执行此检查', dialog()).disabled).toBe(true);
    await change(field('检查范围'), 'inference'); await change(field('请求模型'), 'documented-model');
    await act(async () => { dialog().querySelector('input[type=checkbox]').click(); });
    expect(button('确认执行此检查', dialog()).disabled).toBe(true);
    expect(h.paths).not.toContain('/api/manage/providers/provider_fixture/models/catalog');
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('saves readable instructions with their version and keeps a failed save editable', async () => {
    h.data['/api/manage/assets'] = [{ agent_ids: ['codex'], id: 'asset_prompt', version: 8, name: 'My instructions', kind: 'prompt', content: 'Original text', source: { type: 'manual' }, external: 'keep' }];
    await mount('prompt'); await click('编辑'); await change(field('使用规则正文'), 'Keep the six-field confirmation gate.');
    await click('预览保存', dialog()); h.mutate.mockRejectedValueOnce(new Error('synthetic version changed'));
    await click('保存共享定义', dialog()); expect(dialog().textContent).toContain('synthetic version changed');
    await click('返回编辑', dialog()); expect(field('使用规则正文').value).toBe('Keep the six-field confirmation gate.');
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/asset_prompt', 'PATCH', expect.objectContaining({ expected_version: 8, content: 'Keep the six-field confirmation gate.', external: 'keep' }));
    expect(h.mutate.mock.calls.some(([path]) => path.includes('/plans'))).toBe(false);
  });

  it('keeps keys password-only and clears cancelled input without storing it in definitions or browser storage', async () => {
    await mount(); await click('设置密钥');
    const input = dialog().querySelector('input'); expect(input.type).toBe('password');
    await change(input, 'synthetic-private-value'); await click('取消', dialog());
    expect(h.mutate).not.toHaveBeenCalled(); await click('设置密钥');
    expect(dialog().querySelector('input').value).toBe('');
    expect(JSON.stringify({ ...sessionStorage, ...localStorage })).not.toContain('synthetic-private-value');
    await change(dialog().querySelector('input'), 'synthetic-new-value'); h.mutate.mockResolvedValue({ status: 'configured' });
    await click('保存到凭据库', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith(`/api/manage/providers/${provider.id}/credential`, 'POST', { secret: 'synthetic-new-value' });
    expect(document.body.textContent).not.toContain('synthetic-new-value');
  });

  it('restores an unsaved model draft after leaving the page without restoring an editor or plan automatically', async () => {
    const draftStore = { current: new Map() };
    await mount('providers', draftStore); await click('添加模型');
    await change(field('供应商'), provider.id); await change(field('名称'), 'Draft model'); await change(field('请求模型'), 'native-draft');
    await act(async () => { root.render(<section>Another view</section>); });
    await mount('providers', draftStore);
    expect(document.querySelector('dialog')).toBeNull();
    await click('继续编辑草稿');
    expect(field('名称').value).toBe('Draft model'); expect(field('请求模型').value).toBe('native-draft');
    expect(h.mutate).not.toHaveBeenCalled();
    await click('取消', dialog()); expect(draftStore.current.size).toBe(0);
  });

  it('never carries an unsaved password through a cross-page draft store', async () => {
    const draftStore = { current: new Map() };
    await mount('providers', draftStore); await click('设置密钥'); await change(dialog().querySelector('input'), 'synthetic-never-cached');
    await act(async () => { root.render(<section>Another view</section>); });
    expect(draftStore.current.size).toBe(0);
    await mount('providers', draftStore); await click('设置密钥');
    expect(dialog().querySelector('input').value).toBe(''); expect(h.mutate).not.toHaveBeenCalled();
  });
});

describe('per-assistant connection modes', () => {
  const state = { host_id: 'codex', version: 4, mode: 'direct', supported: true, supported_endpoints: ['responses'], definition_state: 'current' };
  const model = { id: 'model_fixture', name: 'Saved model', provider_id: provider.id, model_id: 'native-fixture' };
  async function modes(drafts) {
    h.data['/api/manage/hosts/codex/mode'] ||= state;
    h.data['/api/manage/models'] = [model];
    await act(async () => { root.render(<AssistantConnectionModes agentId="codex" agent="Codex CLI" revision="fixture" drafts={drafts} />); });
  }
  it('views modes without applying them and keeps the independent connection draft across navigation', async () => {
    const drafts = { current: new Map() };
    await modes(drafts); await click('本地转发');
    await change(field('服务商', container), provider.id); await change(field('请求模型', container), model.id);
    await click('模型聚合'); await click('直连');
    expect(document.body.textContent).toContain('当前已应用：直连');
    expect(document.body.textContent).toContain('不证明助手已重新加载');
    expect(h.mutate).not.toHaveBeenCalled();
    await click('本地转发'); expect(field('请求模型', container).value).toBe(model.id);
    await act(async () => root.render(<div>Other page</div>));
    await modes(drafts); await click('本地转发');
    expect(field('请求模型', container).value).toBe(model.id);
    expect(JSON.stringify([...drafts.current.values()])).not.toContain('credential');
  });
  it('previews readable route rows then waits for a separate scoped confirmation', async () => {
    await modes(); await click('本地转发');
    await change(field('服务商', container), provider.id); await change(field('请求模型', container), model.id);
    h.mutate.mockResolvedValueOnce({ id: 'mode_plan', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex', connection_change: { before: 'direct', after: 'routing' } }, files: [] } });
    await click('预览连接更改');
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/hosts/codex/mode/preview', 'POST', expect.objectContaining({ expected_version: 4, mode: 'routing', endpoint: 'responses', target: { provider_id: provider.id, model_id: model.id }, fallbacks: [] }));
    expect(button('确认应用到 Codex CLI', dialog()).disabled).toBe(true);
    expect(h.mutate.mock.calls.some(([path]) => path.endsWith('/apply'))).toBe(false);
    await act(async () => dialog().querySelector('input[type=checkbox]').click());
    h.mutate.mockResolvedValueOnce({ status: 'applied', payload: { files: [], readback: 'verified', host_reload: 'not_run', real_request: 'not_run' } });
    await click('确认应用到 Codex CLI', dialog());
    expect(h.mutate).toHaveBeenLastCalledWith('/api/manage/plans/mode_plan/apply', 'POST', { expected_version: 1, confirmed: true });
    expect(dialog().textContent).toContain('尚未验证');
  });
  it('retains form input on preview failure and blocks a plan for a different assistant', async () => {
    await modes(); await click('本地转发');
    await change(field('服务商', container), provider.id); await change(field('请求模型', container), model.id);
    h.mutate.mockRejectedValueOnce(new Error('fixture profile changed'));
    await click('预览连接更改');
    expect(field('请求模型', container).value).toBe(model.id);
    expect(container.textContent).toContain('fixture profile changed');
    h.mutate.mockResolvedValueOnce({ id: 'wrong', payload: { metadata: { host_id: 'opencode' } } });
    await click('预览连接更改');
    expect(document.querySelector('dialog')).toBeNull();
    expect(container.textContent).toContain('这个预览不属于当前助手');
  });
  it('orders permitted backups without JSON and protects the aggregation default', async () => {
    await modes(); await click('本地转发');
    await click('添加备用服务'); await click('添加备用服务');
    let rows = container.querySelectorAll('.agent-fallbacks .agent-route-row');
    await change(field('服务商', rows[0]), provider.id);
    await change(field('请求模型', rows[0]), model.id);
    await act(async () => [...rows[0].querySelectorAll('button')].find(node => node.textContent === '下移').click());
    rows = container.querySelectorAll('.agent-fallbacks .agent-route-row');
    expect(field('请求模型', rows[1]).value).toBe(model.id);
    await click('模型聚合');
    expect(button('移除此模型', container).disabled).toBe(true);
    await click('添加聚合模型');
    await change(field('模型显示名称 2', container), 'backup');
    await act(async () => container.querySelectorAll('input[type=radio]')[1].click());
    const buttons = [...container.querySelectorAll('button')].filter(node => node.textContent === '移除此模型');
    expect(buttons[0].disabled).toBe(false); expect(buttons[1].disabled).toBe(true);
    expect(h.mutate).not.toHaveBeenCalled();
  });
  it('pages aggregation editors and preserves aliases outside the visible page', async () => {
    h.data['/api/manage/hosts/codex/mode'] = { ...state, mode: 'aggregation', endpoint: 'responses', default_alias: 'fixture-0', members: Array.from({ length: 21 }, (_, i) => ({ ...model, provider_id: provider.id, model_id: model.id, alias: `fixture-${i}` })) };
    await modes();
    expect(container.querySelectorAll('.agent-aggregation .agent-route-row')).toHaveLength(10);
    await click('下一页', container.querySelector('[aria-label="聚合模型分页"]'));
    await change(field('模型显示名称 11', container), 'edited-on-next-page');
    await click('上一页', container.querySelector('[aria-label="聚合模型分页"]'));
    expect(field('模型显示名称 1', container).value).toBe('fixture-0');
    h.mutate.mockRejectedValueOnce(new Error('fixture preview response'));
    await click('预览连接更改');
    const body = h.mutate.mock.calls[0][2];
    expect(body.members).toHaveLength(21); expect(body.members[10].alias).toBe('edited-on-next-page');
    expect(body.members[20].alias).toBe('fixture-20'); expect(body.default_alias).toBe('fixture-0');
  });
  it('explains duplicate aggregation names before allowing preview', async () => {
    await modes(); await click('模型聚合'); await click('添加聚合模型');
    await change(field('模型显示名称 2', container), 'everyday');
    expect(container.querySelector('[role=alert]').textContent).toBe('每个聚合模型需要不同的显示名称。');
    expect(button('预览连接更改', container).disabled).toBe(true);
    expect(h.mutate).not.toHaveBeenCalled();
  });
});

describe('consumer frontend fixes for account, draft and dialog regressions', () => {
  it('strips account status and unchanged kind on edit PATCH payload and disables kind select', async () => {
    const account = { agent_ids: ['codex'], id: 'acc_1', name: 'My OAuth Account', kind: 'oauth', status: 'authorized', provider_id: 'provider_fixture', version: 3 };
    h.data['/api/manage/accounts'] = [account];
    await mount('accounts');
    await click('编辑');

    // kind select must be disabled when editing existing account
    const kindSelect = field('授权类型');
    expect(kindSelect.disabled).toBe(true);

    // Update name
    await change(field('名称'), 'Renamed Account');
    await click('预览保存', dialog());
    h.mutate.mockResolvedValue({ ...account, name: 'Renamed Account', version: 4 });
    await click('保存共享定义', dialog());

    // Verify PATCH payload excludes status and excludes unchanged kind
    expect(h.mutate).toHaveBeenCalledWith('/api/manage/accounts/acc_1', 'PATCH', expect.not.objectContaining({
      status: expect.anything(),
      kind: expect.anything()
    }));
    expect(h.mutate).toHaveBeenCalledWith('/api/manage/accounts/acc_1', 'PATCH', expect.objectContaining({
      name: 'Renamed Account',
      expected_version: 3
    }));
  });

  it('strips status when duplicating an account', async () => {
    const account = { agent_ids: ['codex'], id: 'acc_2', name: 'Prod Account', kind: 'api_key', status: 'configured', provider_id: 'provider_fixture', version: 1 };
    h.data['/api/manage/accounts'] = [account];
    await mount('accounts');

    const copyBtn = document.querySelector('button[aria-label*="复制定义"]');
    expect(copyBtn).toBeTruthy();
    await act(async () => { copyBtn.click(); });
    await click('预览保存', dialog());
    h.mutate.mockResolvedValue({ id: 'acc_copy', version: 1 });
    await click('保存共享定义', dialog());

    expect(h.mutate).toHaveBeenCalledWith('/api/manage/accounts', 'POST', expect.not.objectContaining({
      status: expect.anything()
    }));
  });

  it('retains dirty draft on native dialog cancel event while footer cancel explicitly discards', async () => {
    const draftStore = { current: new Map() };
    await mount('providers', draftStore);
    await click('新建');
    await change(field('名称'), 'Unsaved Provider Draft');

    // Simulate native dialog cancel event (pressing Escape key or backdrop close)
    await act(async () => {
      dialog().dispatchEvent(new Event('cancel'));
    });

    // Dialog is closed, but draft is retained
    expect(document.querySelector('dialog')).toBeNull();
    expect(draftStore.current.size).toBe(1);
    expect(document.body.textContent).toContain('这个助手有一份尚未保存的草稿');

    // Resume draft
    await click('继续编辑草稿');
    expect(field('名称').value).toBe('Unsaved Provider Draft');

    // Now click the explicit footer "取消" button -> explicitly discards
    await click('取消', dialog());
    expect(document.querySelector('dialog[open]')).toBeNull();
    expect(draftStore.current.size).toBe(0);
  });

  it('does not render nested labels in bundle asset picker', async () => {
    const asset = { agent_ids: ['codex'], id: 'asset_1', name: 'Tool A', kind: 'mcp' };
    h.data['/api/manage/assets'] = [asset];
    await mount('bundles');
    await click('新建');

    // Verify that the bundle assets field is not rendered as <label> wrapping other <label>s
    const labels = Array.from(dialog().querySelectorAll('label'));
    for (const lbl of labels) {
      expect(lbl.querySelectorAll('label').length).toBe(0);
    }
  });
});

describe('assistant resource organization', () => {
  it('does not show other or legacy shared resources until explicitly requested; selection uses CAS without applying', async () => {
    const shared = { ...provider, id: 'shared', name: 'Shared only', agent_ids: ['omp'], version: 7 };
    const legacy = { ...provider, id: 'legacy', name: 'Legacy library', agent_ids: undefined };
    h.data['/api/manage/providers'] = [provider, shared, legacy];
    await mount();
    expect(document.body.textContent).not.toContain('Shared only');
    expect(document.body.textContent).not.toContain('Legacy library');
    expect(document.querySelectorAll('.management-card')).toHaveLength(1);
    await click('从共享资料库选用');
    expect(document.body.textContent).toContain('Legacy library');
    h.mutate.mockResolvedValue({ ...shared, agent_ids: ['omp', 'codex'], version: 8 });
    await click('为这个助手选用', document.querySelector('.agent-shared-row'));
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers/shared/assignment', 'POST', { host_id: 'codex', selected: true, expected_version: 7 });
  });
  it('copies shared resources into only the selected assistant, never copying their saved key', async () => {
    h.data['/api/manage/providers'] = [{ ...provider, agent_ids: ['omp'], credential_ref: 'keychain:fixture', has_credential: true }];
    await mount(); await click('从共享资料库选用'); await click('复制后单独修改');
    await click('预览保存', dialog()); h.mutate.mockResolvedValue({ id: 'copy', version: 1 }); await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers', 'POST', expect.objectContaining({ agent_ids: ['codex'] }));
    expect(h.mutate.mock.calls[0][2]).not.toHaveProperty('credential_ref');
  });
  it('can return from a preset to a custom service without losing its name', async () => {
    await mount(); await click('新建'); await change(field('选择服务商'), 'official-fixture');
    await change(field('选择服务商'), '');
    expect(field('服务地址').value).toBe(''); expect(field('名称').value).toBe('Official preset fixture');
    expect(h.mutate).not.toHaveBeenCalled();
  });
});

it('preserves legacy assistant selections inferred from existing model bindings', async () => {
  h.data['/api/manage/providers'] = [{ ...provider, agent_ids: undefined }];
  h.data['/api/manage/bindings'] = [{ provider_id: provider.id, host_id: 'omp' }];
  await mount(); await click('从共享资料库选用'); h.mutate.mockResolvedValue({ ...provider, agent_ids: ['omp', 'codex'] });
  await click('为这个助手选用');
  expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers/provider_fixture/assignment', 'POST', { host_id: 'codex', selected: true, expected_version: provider.version });
});

describe('complete provider and installed resource journeys', () => {
  it('updates an imported ZIP Skill with the ZIP form rather than asking for a local directory', async () => {
    h.data['/api/manage/assets'] = [{ id: 'zip_fixture', agent_ids: ['codex'], kind: 'skill', version: 2, name: 'ZIP Skill', files: { 'SKILL.md': '# Fixture' }, source: { type: 'zip' }, install_mode: 'copy' }];
    await mount('skill');
    await click('更新技能来源');
    expect(field('来源类型').value).toBe('skill-zip');
    expect(field('选择 ZIP 文件').type).toBe('file');
    expect(h.mutate).not.toHaveBeenCalled();
  });
  async function fillSetup() {
    await click('添加服务商');
    await change(field('名称'), 'New everyday provider');
    await change(field('服务地址'), 'https://fixture.example/v1');
    await change(field('默认请求模型'), 'fixture-native-model');
    await change(field('API Key（可选）'), 'synthetic-setup-value');
  }
  const saved = {
    provider: { ...provider, id: 'provider_new', name: 'New everyday provider', has_credential: true },
    model: { id: 'model_new', provider_id: 'provider_new', model_id: 'fixture-native-model', name: 'Fixture model' }
  };

  it('saves endpoint, key and model once, then opens the host-specific preview in the same journey', async () => {
    await mount(); await fillSetup();
    h.mutate
      .mockResolvedValueOnce(saved)
      .mockResolvedValueOnce({ id: 'plan_setup', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } });
    await click('保存连接与模型', dialog());
    expect(h.mutate.mock.calls[0]).toEqual(['/api/manage/providers/setup', 'POST', expect.objectContaining({
      request_id: expect.any(String), host_id: 'codex', model_id: 'fixture-native-model', secret: 'synthetic-setup-value',
      provider: { name: 'New everyday provider', base_url: 'https://fixture.example/v1', protocol: 'openai', wire_api: 'responses', supported_endpoints: ['responses'] }
    })]);
    expect(h.mutate.mock.calls[1]).toEqual(['/api/manage/plans', 'POST', expect.objectContaining({ host_id: 'codex', provider_id: 'provider_new', activate_provider: true, model_id: 'model_new' })]);
    expect(document.querySelector('.agent-next-step')).toBeNull();
    expect(button('确认应用到 Codex CLI', dialog()).disabled).toBe(true);
    expect(h.mutate.mock.calls.some(([path]) => path.endsWith('/apply'))).toBe(false);
  });

  it('revises one saved connection and one model without sending a blank key or writing the assistant file', async () => {
    const model = { id: 'model_fixture', version: 4, provider_id: provider.id, model_id: 'fixture-native-model', context_window: 8192, name: 'Fixture native' };
    const modelsPath = catalogPath('models', { providerId: provider.id, scope: 'all', limit: 100 });
    h.request.mockImplementation(async path => path === modelsPath ? { items: [model], has_more: false } : Promise.reject(new Error(`unexpected ${path}`)));
    await mount();
    expect(h.request).not.toHaveBeenCalled();
    await click('编辑连接与模型');
    expect(h.request).toHaveBeenCalledExactlyOnceWith(modelsPath);
    expect(h.request.mock.calls.some(([path]) => path.startsWith('/api/manage/providers/'))).toBe(false);
    expect(dialog().textContent).toContain('Fixture native');
    expect(dialog().textContent).toContain('留空表示继续使用已保存的密钥');
    await click('工具');
    expect(document.body.textContent).toContain('离开当前编辑');
    await click('继续编辑');
    await change(field('服务地址'), 'https://revised.example/v1');
    await change(field('默认请求模型'), 'revised-model');
    h.mutate
      .mockResolvedValueOnce({ provider: { ...provider, base_url: 'https://revised.example/v1', version: 3 }, model: { ...model, model_id: 'revised-model', version: 5 }, host_configuration: 'unchanged' })
      .mockResolvedValueOnce({ id: 'plan_revise', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } });
    await click('保存连接与模型的修改', dialog());
    const body = h.mutate.mock.calls[0][2];
    expect(h.mutate.mock.calls[0]).toEqual(['/api/manage/providers/setup/revise', 'POST', expect.objectContaining({
      request_id: expect.any(String), host_id: 'codex', provider_id: provider.id, model_entity_id: model.id,
      expected_provider_version: provider.version, expected_model_version: model.version, model_id: 'revised-model', context_window: 8192,
      provider: { name: provider.name, base_url: 'https://revised.example/v1', protocol: 'openai', wire_api: null, supported_endpoints: null }
    })]);
    expect(body).not.toHaveProperty('secret');
    expect(body.provider).not.toHaveProperty('credential_ref');
    expect(JSON.stringify(body)).not.toContain('credential_ref');
    expect(h.mutate.mock.calls[1][0]).toBe('/api/manage/plans');
    expect(button('确认应用到 Codex CLI', dialog()).disabled).toBe(true);
    expect(document.body.textContent).toContain('确认更改后才会写入');
  });

  it('changes which model is saved with the connection and omits an empty replacement key', async () => {
    const first = { id: 'model_a', version: 3, provider_id: provider.id, model_id: 'model-a', context_window: 1000, name: 'Model A' };
    const second = { id: 'model_b', version: 7, provider_id: provider.id, model_id: 'model-b', name: 'Model B' };
    h.request.mockResolvedValue({ items: [first, second], has_more: false });
    await mount();
    await click('编辑连接与模型');
    await change(field('要一起保存的模型'), 'model_b');
    expect(field('默认请求模型').value).toBe('model-b');
    h.mutate
      .mockResolvedValueOnce({ provider, model: second, host_configuration: 'unchanged' })
      .mockResolvedValueOnce({ id: 'plan_model_pick', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } });
    await click('保存连接与模型的修改', dialog());
    expect(h.mutate.mock.calls[0][2]).toEqual(expect.objectContaining({ model_entity_id: 'model_b', expected_model_version: 7, model_id: 'model-b', expected_provider_version: provider.version }));
    expect(h.mutate.mock.calls[0][2]).not.toHaveProperty('context_window');
    expect(h.mutate.mock.calls[0][2]).not.toHaveProperty('secret');
    expect(h.mutate.mock.calls[0][0]).toBe('/api/manage/providers/setup/revise');
    expect(h.mutate.mock.calls[1][0]).toBe('/api/manage/plans');
  });

  it('refuses a partial or empty model list instead of editing one model and missing the rest', async () => {
    h.request.mockResolvedValueOnce({ items: [{ id: 'model_a', version: 1, provider_id: provider.id, model_id: 'only-page' }], has_more: true });
    await mount();
    await click('编辑连接与模型');
    expect(document.querySelector('dialog[open]')).toBeNull();
    expect(document.body.textContent).toContain('模型超过一页');
    expect(h.mutate).not.toHaveBeenCalled();
    h.request.mockResolvedValueOnce({ items: [], has_more: false });
    await click('编辑连接与模型');
    expect(document.body.textContent).toContain('还没有可一起保存的模型');
    expect(document.querySelector('dialog[open]')).toBeNull();
  });

  it('retains the same setup request after a lost response and never caches its key in drafts', async () => {
    const draftStore = { current: new Map() };
    await mount('providers', draftStore); await fillSetup();
    h.mutate.mockRejectedValueOnce(Object.assign(new Error('fixture connection lost'), { status: 0 }));
    await click('保存连接与模型', dialog());
    const firstRequest = h.mutate.mock.calls[0][2];
    expect(field('名称').disabled || field('名称').closest('fieldset').disabled).toBe(true);
    expect(dialog().textContent).toContain('取消只关闭窗口');
    expect(draftStore.current.size).toBe(0);
    h.mutate
      .mockResolvedValueOnce(saved)
      .mockResolvedValueOnce({ id: 'plan_retry', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } });
    await click('核对并重试保存', dialog());
    expect(h.mutate.mock.calls[1][2]).toEqual(firstRequest);
    expect(h.mutate.mock.calls.filter(([path]) => path === '/api/manage/providers/setup')).toHaveLength(2);
    expect(h.mutate.mock.calls.some(([path]) => path === '/api/manage/plans')).toBe(true);
  });

  it('shows actual configured selection while preserving the distinction from host loading', async () => {
    h.data['/api/manage/hosts/codex/state'] = {
      provider_mode: 'single_selection', selection: { provider_id: provider.id, provider_name: provider.name, model_id: 'current-native', status: 'verified', host_loaded: 'not_verified' },
      resources: { [provider.id]: { status: 'verified', paths: 1 } }, recovery: []
    };
    await mount();
    const summary = document.querySelector('.agent-current-configuration');
    expect(summary.textContent).toContain('current-native');
    expect(summary.textContent).toContain('一次选择一个默认服务商');
    expect(summary.textContent).toContain('当前选择');
    expect(summary.textContent).toContain('配置文件');
    expect(summary.textContent).toContain('宿主加载');
    expect(summary.textContent).toContain('连接可达');
    expect(summary.textContent).toContain('鉴权');
    expect(summary.textContent).toContain('模型调用');
    expect(summary.textContent).toContain('尚未检查。写入配置不能说明网络可达。');
    expect(summary.textContent).toContain('不会把配置写入当成真实调用成功');
    expect(summary.textContent).not.toContain('认证已验证');
    const card = document.querySelector('.management-card');
    expect(card.querySelector('.agent-configured-state').textContent).toContain('当前配置选择');
    expect(card.querySelector('button[aria-label*="删除定义"]').disabled).toBe(true);
    expect(card.textContent).toContain('不能在仍被选中时直接卸下');
    expect([...card.querySelectorAll('button')].some(node => node.textContent.startsWith('预览从'))).toBe(false);
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('does not treat unrecognized reachability, auth or inference claims as passed', async () => {
    h.data['/api/manage/hosts/codex/state'] = {
      provider_mode: 'single_selection',
      selection: { provider_id: provider.id, provider_name: provider.name, model_id: 'current-native', status: 'verified', host_loaded: 'loaded' },
      effects: { selection: 'recorded', configuration: 'verified', host_loaded: 'loaded', reachability: 'reachable', authentication: 'authenticated', invocation: 'succeeded' },
      resources: { [provider.id]: { status: 'verified', paths: 1 } }, recovery: []
    };
    await mount();
    const summary = document.querySelector('.agent-current-configuration');
    expect(summary.textContent).toContain('加载状态无法识别，不能当成已经加载。');
    expect(summary.textContent).toContain('这项状态无法识别，不能当成已经通过。');
    expect(summary.textContent).not.toContain('认证已验证');
    expect(summary.textContent).not.toContain('网络可达');
    expect(summary.textContent).not.toContain('尚未检查。写入配置不能说明网络可达。');
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('offers provider removal only after that provider is no longer the current selection', async () => {
    h.data['/api/manage/hosts/codex/state'] = {
      selection: { provider_id: 'provider_other', status: 'verified' },
      resources: { [provider.id]: { status: 'verified', paths: 1 } }
    };
    await mount();
    const card = document.querySelector('.management-card');
    expect(card.textContent).not.toContain('不能在仍被选中时直接卸下');
    expect(button('预览从 Codex CLI 移除', card).disabled).toBe(false);
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('shows the fresh host readback after removal and does not treat a missing readback as removed', async () => {
    const removal = { id: 'plan_remove', version: 3, status: 'planned', payload: { metadata: { host_id: 'codex', operation: 'uninstall', provider_id: provider.id, entity_versions: { [provider.id]: provider.version } }, files: [{ path: '/synthetic/.codex/config.toml', diff: { entries: [] } }] } };
    h.data['/api/manage/hosts/codex/state'] = { host_id: 'codex', selection: { provider_id: 'provider_other', status: 'verified' }, resources: { [provider.id]: { status: 'verified', paths: 1 } }, coverage: 'complete_owned_entries' };
    await mount();
    h.mutate.mockResolvedValueOnce(removal);
    await click('预览从 Codex CLI 移除');
    expect(dialog().textContent).not.toContain('已从助手移除');
    await act(async () => dialog().querySelector('input[type=checkbox]').click());
    h.mutate.mockResolvedValueOnce({ status: 'applied' });
    h.request.mockResolvedValueOnce({ ...removal, status: 'applied', payload: { ...removal.payload, readback: 'verified', host_reload: 'not_run', real_request: 'not_run' } });
    h.request.mockResolvedValueOnce({ host_id: 'codex', selection: { provider_id: null, status: 'not_managed' }, effects: { selection: 'not_selected_by_senrelay', configuration: 'not_managed', host_loaded: 'not_verified', reachability: 'not_checked', authentication: 'not_checked', invocation: 'not_checked' }, resources: { [provider.id]: { status: 'removed' } }, coverage: 'complete_owned_entries' });
    await click('确认应用到 Codex CLI', dialog());
    const readback = dialog().querySelector('.agent-host-readback');
    expect(readback.textContent).toContain(provider.name);
    expect(readback.textContent).toContain('已从助手移除');
    expect(readback.textContent).toContain('尚未由续境选择当前模型');
    expect(readback.textContent).toContain('尚未检查。写入配置不能说明网络可达。');
    expect(readback.textContent).toContain('尚未验证。没有发起模型调用');
    expect(readback.textContent).not.toContain('认证已验证');
    expect(h.request.mock.calls.map(([path]) => path)).toContain('/api/manage/hosts/codex/state');
    await act(async () => root.render(null));
    h.data['/api/manage/hosts/codex/state'] = { host_id: 'codex', selection: { provider_id: 'provider_other', status: 'verified' }, resources: { [provider.id]: { status: 'verified', paths: 1 } }, coverage: 'complete_owned_entries' };
    await mount();
    h.mutate.mockResolvedValueOnce(removal);
    await click('预览从 Codex CLI 移除');
    await act(async () => dialog().querySelector('input[type=checkbox]').click());
    h.mutate.mockRejectedValueOnce(new Error('apply failed'));
    h.request.mockResolvedValueOnce({ ...removal, status: 'failed', payload: { ...removal.payload, readback: 'not_run', host_reload: 'not_run', real_request: 'not_run' } });
    h.request.mockRejectedValueOnce(new Error('state unavailable'));
    await click('确认应用到 Codex CLI', dialog());
    expect(dialog().textContent).toContain('应用后的本机配置还没有读回');
    expect(dialog().textContent).not.toContain('已从助手移除');
  });

  it('searches only the current assistant and presents a useful no-match state', async () => {
    h.data['/api/manage/assets'] = [
      { id: 'tool_1', agent_ids: ['codex'], kind: 'mcp', name: 'Research tool', config: {} },
      { id: 'tool_2', agent_ids: ['omp'], kind: 'mcp', name: 'Private other assistant', config: {} }
    ];
    await mount('mcp');
    await change(field('搜索当前助手的资料', document), 'missing query');
    expect(document.querySelectorAll('.management-card')).toHaveLength(0);
    expect(document.body.textContent).toContain('没有匹配的资料');
    await click('清空搜索');
    expect(document.querySelectorAll('.management-card')).toHaveLength(1);
    expect(document.body.textContent).not.toContain('Private other assistant');
  });

  it('reports externally changed resources and an unfinished change without applying anything', async () => {
    h.data['/api/manage/hosts/codex/state'] = { selection: {}, resources: { [provider.id]: { status: 'externally_changed', paths: 1 } }, recovery: [{ id: 'recovery_fixture' }] };
    await mount();
    const configured = document.querySelector('.agent-configured-state');
    expect(configured.textContent).toContain('配置已被外部修改');
    expect(configured.textContent).toContain('外部已改动');
    expect(configured.getAttribute('title')).toContain('不要直接覆盖外部修改');
    expect(document.querySelector('.agent-current-configuration').textContent).toContain('有未完成更改');
    expect(h.mutate).not.toHaveBeenCalled();
  });
});

const resourcePagePath = (kind, scope = 'assigned', offset = 0, q = '') => '/api/manage/assets?' + new URLSearchParams({ summary: 'true', agent_id: 'codex', kind, scope, limit: '50', offset: String(offset), q });
const pageOf = (items, total = items.length, offset = 0, limit = 50) => ({ items, total, offset, limit, has_more: offset + items.length < total, summary: true, agent_id: 'codex' });
const skillSummary = (n, agent = 'codex') => ({ id: `asset_page_${n}`, name: `Skill ${n}`, kind: 'skill', version: 1, agent_ids: [agent], _summary: true });

describe('bounded assistant libraries and lazy details', () => {
  it('browses every assigned and shared page without fetching unrelated management libraries or bodies', async () => {
    const ours = Array.from({ length: 65 }, (_, i) => skillSummary(i));
    const shared = Array.from({ length: 65 }, (_, i) => skillSummary(i + 100, 'omp'));
    for (const [scope, items] of [['assigned', ours], ['available', shared]]) {
      h.data[resourcePagePath('skill', scope)] = pageOf(items.slice(0, 50), 65);
      h.data[resourcePagePath('skill', scope, 50)] = pageOf(items.slice(50), 65, 50);
    }
    await mount('skill');
    expect(document.querySelectorAll('.management-card')).toHaveLength(50);
    expect(h.paths.filter(Boolean).some(path => path.includes('scope=available'))).toBe(false);
    await click('下一页', document.querySelector('nav[aria-label="助手资料分页"]'));
    expect(document.querySelectorAll('.management-card')).toHaveLength(15);
    expect(document.body.textContent).toContain('Skill 64');
    expect(button('下一页', document.querySelector('nav[aria-label="助手资料分页"]')).disabled).toBe(true);
    await click('从共享资料库选用');
    expect(document.querySelectorAll('.agent-shared-row')).toHaveLength(50);
    await click('下一页', document.querySelector('nav[aria-label="共享资料分页"]'));
    expect(document.querySelectorAll('.agent-shared-row')).toHaveLength(15);
    expect(document.body.textContent).toContain('Skill 164');
    expect(h.request).not.toHaveBeenCalled();
    for (const collection of ['providers', 'models', 'accounts', 'bindings']) expect(h.paths).not.toContain(`/api/manage/${collection}`);
  });

  it('searches the whole scoped library, resets the page and trusts server matches on source paths', async () => {
    h.data[resourcePagePath('skill')] = pageOf(Array.from({ length: 50 }, (_, i) => skillSummary(i)), 65);
    h.data[resourcePagePath('skill', 'assigned', 50)] = pageOf([skillSummary(64)], 65, 50);
    h.data[resourcePagePath('skill', 'assigned', 0, 'archive')] = pageOf([{ ...skillSummary(64), source: { path: '/synthetic/archive' } }]);
    h.data[resourcePagePath('skill', 'assigned', 0, 'absent')] = pageOf([]);
    await mount('skill'); await click('下一页', document.querySelector('nav[aria-label="助手资料分页"]'));
    await change(field('搜索当前助手的资料', document), 'archive');
    expect(document.querySelectorAll('.management-card')).toHaveLength(1);
    expect(document.body.textContent).toContain('Skill 64');
    expect(h.paths).toContain(resourcePagePath('skill', 'assigned', 0, 'archive'));
    expect(h.paths).not.toContain(resourcePagePath('skill', 'assigned', 50, 'archive'));
    await change(field('搜索当前助手的资料', document), 'absent');
    expect(document.body.textContent).toContain('没有匹配的资料');
    await click('清空搜索');
    expect(document.querySelectorAll('.management-card')).toHaveLength(50);
  });

  it('loads just the selected full tool before editing and preserves current unknown fields and version', async () => {
    const summary = { id: 'asset_lazy', kind: 'mcp', name: 'Lazy tool', version: 3, agent_ids: ['codex'], _summary: true };
    const full = { ...summary, version: 4, _summary: undefined, config: { url: 'https://fixture.example/mcp', transport: 'http', headers: { Authorization: '{{credential:synthetic}}' }, custom_setting: { retain: true } }, extra_metadata: { retain: true } };
    h.data[resourcePagePath('mcp')] = pageOf([summary]); h.request.mockResolvedValue(full);
    await mount('mcp'); expect(h.request).not.toHaveBeenCalled();
    await click('编辑'); expect(h.request).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/asset_lazy');
    await change(field('服务网址'), 'https://fixture.example/updated');
    await click('预览保存'); h.mutate.mockResolvedValue({ ...full, version: 5 }); await click('保存共享定义');
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/asset_lazy', 'PATCH', expect.objectContaining({ expected_version: 4, extra_metadata: full.extra_metadata, config: { ...full.config, url: 'https://fixture.example/updated' } }));
  });

  it('copies a shared summary only after loading its files and keeps the new copy scoped to this assistant', async () => {
    const summary = skillSummary(80, 'omp');
    const full = { id: summary.id, kind: 'skill', name: summary.name, version: 2, agent_ids: ['omp'], install_mode: 'copy', files: { 'SKILL.md': '# Full selected body', 'references/extra.md': 'preserve extra' }, outside_field: 'retain', source: { type: 'manual' } };
    h.data[resourcePagePath('skill')] = pageOf([]);
    h.data[resourcePagePath('skill', 'available')] = pageOf([summary]);h.request.mockResolvedValue(full);
    await mount('skill'); await click('从共享资料库选用'); await click('复制后单独修改');
    expect(field('技能说明（SKILL.md）').value).toBe('# Full selected body');
    await click('预览保存');h.mutate.mockResolvedValue({ ...full, id: 'new_copy', version: 1 });await click('保存共享定义');
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets', 'POST', expect.objectContaining({ agent_ids: ['codex'], outside_field: 'retain', files: full.files }));
    expect(h.mutate.mock.calls[0][2]).not.toHaveProperty('id');
  });

  it('rejects mismatched details and ignores a late editor response after leaving the category', async () => {
    const summary = { id: 'asset_lazy', kind: 'mcp', name: 'Lazy tool', version: 1, _summary: true };
    h.data[resourcePagePath('mcp')] = pageOf([summary]);h.request.mockResolvedValueOnce({ ...summary, id: 'another_asset', _summary: undefined });
    await mount('mcp');await click('编辑');
    expect(dialog()).toBeNull();expect(document.body.textContent).toContain('资料详情与当前选择不一致');
    let resolve;h.request.mockReturnValueOnce(new Promise(done => { resolve = done; }));
    await click('编辑');await mount('prompt');
    await act(async () => { resolve({ ...summary, _summary: undefined, config: { command: 'fixture' } }); });
    expect(dialog()).toBeNull();
  });

  it('requires full matching probe details and pins the reviewed version without connecting on open', async () => {
    const summary = { id: 'asset_probe', kind: 'mcp', name: 'Review before connecting', version: 3, _summary: true };
    const render = () => root.render(<AssetTools item={summary} selectedAgent="Codex CLI" onChanged={() => {}} />);
    await act(async () => { render(); });expect(h.paths).not.toContain('/api/manage/assets/asset_probe');
    await click('验证工具发现');expect(button('确认连接并发现工具').disabled).toBe(true);expect(h.mutate).not.toHaveBeenCalled();
    h.data['/api/manage/assets/asset_probe'] = { ...summary, id: 'wrong', _summary: undefined, config: { command: 'wrong-command' } };
    await act(async () => { render(); });expect(button('确认连接并发现工具').disabled).toBe(true);
    h.data['/api/manage/assets/asset_probe'] = { ...summary, version: 4, _summary: undefined, config: { command: 'reviewed-command', args: ['--fixture'] } };
    await act(async () => { render(); });expect(dialog().textContent).toContain('reviewed-command');expect(h.mutate).not.toHaveBeenCalled();
    h.mutate.mockResolvedValue({ status: 'verified' });await click('确认连接并发现工具');
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/asset_probe/probe', 'POST', { confirmed: true, expected_version: 4 });
  });

  it('pages version metadata and loads only the selected old body before allowing a restore', async () => {
    const current = { id: 'asset_history', kind: 'skill', name: 'Versioned fixture', version: 25, files: { 'SKILL.md': '# Current' } };
    const render = () => root.render(<AssetTools item={current} selectedAgent="Codex CLI" onChanged={() => {}} />);
    h.data['/api/manage/assets/asset_history/versions?summary=true&limit=20&offset=0'] = pageOf(Array.from({ length: 20 }, (_, i) => ({ version: 25 - i, created_at: '2026-10-26' })), 25, 0, 20);
    await act(async () => { render(); });await click('版本与恢复');await click('6', dialog());
    expect(button('确认恢复此版本').disabled).toBe(true);
    expect(h.paths).toContain('/api/manage/assets/asset_history/versions/6');
    h.data['/api/manage/assets/asset_history/versions/6'] = { version: 6, data: { kind: 'skill', name: current.name, files: { 'SKILL.md': '# Selected old body' } } };
    await act(async () => { render(); });
    expect(dialog().textContent).toContain('# Selected old body');
    h.mutate.mockResolvedValue({ ...current, version: 26 });await click('确认恢复此版本');
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/asset_history/restore', 'POST', { version: 6, expected_version: 25 });
    expect(h.paths).not.toContain('/api/manage/assets/asset_history/versions');
  });
});

describe('paged model choices, combinations and shared library', () => {
  it('keeps a selected model that is outside the current page or search and resolves only its summary', async () => {
    const models = Array.from({ length: 65 }, (_, i) => ({ id: `model_${i}`, name: `Saved ${i}`, model_id: `request-${i}`, provider_id: provider.id, _summary: true }));
    h.data[catalogPath('models', { providerId: provider.id, scope: 'all' })] = pageOf(models.slice(0, 50), 65);
    h.data[catalogPath('models', { providerId: provider.id, scope: 'all', query: 'absent' })] = pageOf([]);
    h.data[catalogPath('models', { scope: 'all', ids: ['model_64'], limit: 1 })] = pageOf([models[64]]);
    const changed = vi.fn();
    await act(async () => { root.render(<CatalogChoice collection="models" label="Saved models" value="model_64" onChange={changed} providerId={provider.id} scope="all" />); });
    expect(field('Saved models', document).value).toBe('model_64');
    expect(field('Saved models', document).selectedOptions[0].textContent).toContain('Saved 64');
    await change(field('搜索Saved models', document), 'absent');
    expect(field('Saved models', document).value).toBe('model_64');
    expect(changed).not.toHaveBeenCalled();expect(h.request).not.toHaveBeenCalled();
    expect(h.paths.filter(Boolean).every(path => path.includes('summary=true'))).toBe(true);
  });

  it('keeps bundle membership across pages and searches and can inspect or remove a missing selected definition', async () => {
    const assets = Array.from({ length: 65 }, (_, i) => skillSummary(i));
    h.data[catalogPath('assets', { scope: 'all' })] = pageOf(assets.slice(0, 50), 65);
    h.data[catalogPath('assets', { scope: 'all', offset: 50 })] = pageOf(assets.slice(50), 65, 50);
    h.data[catalogPath('assets', { scope: 'all', query: 'rare' })] = pageOf([assets[60]]);
    const selected = ['asset_missing', assets[0].id, assets[64].id, assets[60].id];
    h.data[catalogPath('assets', { scope: 'all', ids: selected })] = pageOf([assets[0], assets[64], assets[60]]);
    let latest;
    function Bundle() { const [value, setValue] = useState(['asset_missing']);latest = value;return <BundleAssetSelection value={value} onChange={setValue} />; }
    async function check(name) { const input = [...document.querySelectorAll('.agent-resource-checklist label')].find(node => node.textContent.startsWith(name))?.querySelector('input');expect(input).toBeTruthy();await act(async () => { input.click(); }); }
    await act(async () => { root.render(<Bundle />); });await check('Skill 0');
    await click('下一页', document.querySelector('nav[aria-label="组合资料分页"]'));await check('Skill 64');
    await change(field('搜索组合资料', document), 'rare');await check('Skill 60');
    expect(latest).toEqual(selected);
    await click('查看已选资料');
    expect(document.querySelectorAll('.agent-resource-checklist input:checked')).toHaveLength(4);
    expect(document.body.textContent).toContain('资料定义已移除');
    await check('资料定义已移除');expect(latest).toEqual(selected.slice(1));
    expect(h.paths).not.toContain('/api/manage/assets');expect(h.request).not.toHaveBeenCalled();
  });

  it('browses the shared library by category, searches all pages and opens management without applying anything', async () => {
    const assets = Array.from({ length: 65 }, (_, i) => skillSummary(i));
    h.data[catalogPath('providers', { scope: 'all' })] = pageOf([]);
    h.data[catalogPath('assets', { scope: 'all', kind: 'skill' })] = pageOf(assets.slice(0, 50), 65);
    h.data[catalogPath('assets', { scope: 'all', kind: 'skill', offset: 50 })] = pageOf(assets.slice(50), 65, 50);
    h.data[catalogPath('assets', { scope: 'all', kind: 'skill', query: 'last' })] = pageOf([assets[64]]);
    const manage = vi.fn();await act(async () => { root.render(<SharedLibrary onManage={manage} />); });
    await click('技能');expect(document.querySelectorAll('.agent-shared-row')).toHaveLength(50);
    await click('下一页', document.querySelector('nav[aria-label="共享资料分页"]'));
    expect(document.querySelectorAll('.agent-shared-row')).toHaveLength(15);
    await change(field('搜索共享资料', document), 'last');expect(document.querySelectorAll('.agent-shared-row')).toHaveLength(1);
    await click('管理这个分类');expect(manage).toHaveBeenCalledExactlyOnceWith('skill');
    expect(h.mutate).not.toHaveBeenCalled();expect(h.paths).not.toContain('/api/manage/assets');
  });

  it('loads provider and model summaries and leaves all binding history untouched until explicitly expanded', async () => {
    const ours = Array.from({ length: 65 }, (_, i) => ({ ...provider, id: `provider_${i}`, name: `Scoped provider ${i}`, _summary: true, model_map: undefined }));
    h.data[catalogPath('providers', { agentId: 'codex' })] = pageOf(ours.slice(0, 50), 65);
    h.data[catalogPath('providers', { agentId: 'codex', offset: 50 })] = pageOf(ours.slice(50), 65, 50);
    h.data[catalogPath('models', { agentId: 'codex' })] = pageOf([]);
    await mount();expect(document.querySelectorAll('.management-card')).toHaveLength(50);
    expect(h.paths.filter(Boolean).some(path => path.includes('/bindings'))).toBe(false);
    await click('下一页', document.querySelector('nav[aria-label="助手资料分页"]'));
    expect(document.querySelectorAll('.management-card')).toHaveLength(15);
    expect(document.body.textContent).toContain('Scoped provider 64');
    for (const path of h.paths.filter(Boolean)) if (/\/api\/manage\/(providers|models|accounts|bindings)$/.test(path)) throw new Error(`Unbounded list requested: ${path}`);
    expect(h.request).not.toHaveBeenCalled();
  });
});

it('returns to a valid resource page after the final page is emptied by another edit', async () => {
  const first = Array.from({ length: 50 }, (_, i) => skillSummary(i));
  h.data[resourcePagePath('skill')] = pageOf(first, 65);
  h.data[resourcePagePath('skill', 'assigned', 50)] = pageOf(Array.from({ length: 15 }, (_, i) => skillSummary(i + 50)), 65, 50);
  await mount('skill');await click('下一页', document.querySelector('nav[aria-label="助手资料分页"]'));
  expect(document.querySelectorAll('.management-card')).toHaveLength(15);
  h.data[resourcePagePath('skill')] = pageOf(first, 50);
  h.data[resourcePagePath('skill', 'assigned', 50)] = pageOf([], 50, 50);
  await mount('skill');
  expect(document.querySelectorAll('.management-card')).toHaveLength(50);
  expect(document.body.textContent).not.toContain('还没有为这个助手添加资料');
});

it('keeps a single primary heading when connection permissions are embedded in an assistant', async () => {
  await mount('connections');
  expect(document.querySelectorAll('h1')).toHaveLength(1);
  expect(document.querySelector('h1').textContent).toBe('Codex CLI');
  expect(document.querySelectorAll('.page-header h2')).toHaveLength(1);
  expect(document.body.textContent).toContain('各自独立确认');
});

describe('Agent management parity controls', () => {
  it('renders top segmented host switcher, 1-click provider switch, provider reorder, multi-host sync, speedtest latency pill, and env conflict alert', async () => {
    const claudeHost = { ...host, id: 'claude-code', name: 'Claude Code' };
    const secondProvider = { ...provider, id: 'provider_second', name: 'Second synthetic provider', sort_index: 1 };
    h.data['/api/manage/hosts'] = [host, claudeHost];
    h.data['/api/manage/providers'] = [{ ...provider, sort_index: 0 }, secondProvider];
    h.data[catalogPath('providers', { agentId: 'codex' })] = {
      items: [{ ...provider, sort_index: 0 }, secondProvider],
      total: 2,
      offset: 0,
      limit: 50,
    };
    h.data['/api/manage/hosts/codex/state'] = {
      selection: { provider_id: 'provider_fixture', provider_name: 'Local synthetic provider', model_id: 'gpt-4.1', status: 'verified' },
      resources: {},
      coverage: 'complete_owned_entries',
      env_conflicts: {
        has_conflicts: true,
        count: 1,
        conflicts: [
          {
            variable: 'OPENAI_API_KEY',
            source: 'process_env',
            message: '进程环境变量 OPENAI_API_KEY 已设置，可能会覆盖配置文件中的模型服务商设置。',
          },
        ],
      },
    };

    const onAgentChange = vi.fn();
    await act(async () => {
      root.render(
        <AgentManagementPage
          agentId="codex"
          section="providers"
          onAgentChange={onAgentChange}
          projects={[]}
          revision={0}
          onChanged={() => {}}
        />,
      );
    });

    // 1. Top segmented host switcher bar
    const switcher = container.querySelector('.cc-top-switcher-bar');
    expect(switcher).toBeTruthy();
    expect(switcher.textContent).toContain('Codex CLI');
    expect(switcher.textContent).toContain('Claude Code');
    await click('Claude Code', switcher);
    expect(onAgentChange).toHaveBeenCalledWith('claude-code', 'providers');

    // 2. Environment variable conflict alert in ConfigurationState
    const envAlert = container.querySelector('.cc-env-conflict-alert');
    expect(envAlert).toBeTruthy();
    expect(envAlert.textContent).toContain('检测到环境变量冲突');
    expect(envAlert.textContent).toContain('OPENAI_API_KEY');

    // 3. Active badge on provider_fixture ("正在使用") and "一键切换" on secondProvider
    const cards = [...container.querySelectorAll('.management-card')];
    expect(cards).toHaveLength(2);
    expect(cards[0].textContent).toContain('正在使用');
    expect(cards[1].textContent).toContain('一键切换');

    // 4. Click "一键切换" on secondProvider
    await click('一键切换', cards[1]);
    expect(dialog().textContent).toContain('选择并启用模型');
    expect(h.mutate).not.toHaveBeenCalled();
    await click('取消', dialog());

    // 5. Reorder provider ("下移" on first card)
    h.mutate.mockResolvedValueOnce([{ ...secondProvider, sort_index: 0 }, { ...provider, sort_index: 1 }]);
    await click('下移', cards[0]);
    expect(h.mutate).toHaveBeenCalledWith('/api/manage/providers/reorder', 'POST', {
      ordered_ids: ['provider_second', 'provider_fixture'],
    });

    // 6. Multi-host sync modal ("同步到多宿主")
    await click('同步到多宿主', cards[0]);
    expect(dialog().textContent).toContain('同步到多宿主');
    const claudeCheckbox = [...dialog().querySelectorAll('label')].find(node => node.textContent.includes('Claude Code'))?.querySelector('input');
    expect(claudeCheckbox).toBeTruthy();
    await act(async () => { claudeCheckbox.click(); });
    h.mutate.mockResolvedValueOnce({ status: 'planned', planned: ['codex', 'claude-code'], results: {} });
    await change(dialog().querySelector('input:not([type=checkbox])'), 'explicit-model');
    await click('生成各宿主预览', dialog());
    expect(h.mutate).toHaveBeenCalledWith('/api/manage/sync-hosts', 'POST', {
      kind: 'provider',
      entity_id: 'provider_fixture',
      host_ids: ['codex', 'claude-code'],
      operation: 'install', model_id: 'explicit-model',
    });

    await click('关闭', dialog());

    // 7. 1-click speedtest ("一键测速") and latency pill in ProviderChecks
    h.mutate.mockResolvedValueOnce({
      level: 'network',
      status: 'reachable',
      latency_ms: 42,
      message: 'Endpoint responded.',
      checked_at: '2026-10-28T02:00:00Z',
    });
    await click('一键测速', cards[0]);
    expect(h.mutate).toHaveBeenCalledWith('/api/manage/providers/provider_fixture/check', 'POST', { mode: 'network' });
    expect(cards[0].querySelector('.cc-latency-pill')?.textContent).toContain('42 ms');
  });
});


it('requires a separate consent click for each reopened configuration preview and cancel never applies', async () => {
  const onApply = vi.fn();
  const onClose = vi.fn();
  const plan = { id: 'consent_preview', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } };
  const show = () => root.render(<AgentPlanDialog plan={plan} agent="Codex CLI" category="工具" valid onApply={onApply} onClose={onClose} />);
  await act(async () => show());
  await click('确认应用到 Codex CLI', dialog());
  expect(onApply).not.toHaveBeenCalled();
  await act(async () => dialog().querySelector('input[type=checkbox]').click());
  expect(button('确认应用到 Codex CLI', dialog()).disabled).toBe(false);
  await click('取消', dialog());
  expect(onClose).toHaveBeenCalledOnce();
  expect(onApply).not.toHaveBeenCalled();
  await act(async () => root.render(null));
  await act(async () => show());
  expect(dialog().querySelector('input[type=checkbox]').checked).toBe(false);
  expect(button('确认应用到 Codex CLI', dialog()).disabled).toBe(true);
  await act(async () => dialog().querySelector('input[type=checkbox]').click());
  await click('确认应用到 Codex CLI', dialog());
  expect(onApply).toHaveBeenCalledOnce();
});

it('evicts an ordinary draft if advanced JSON introduces credentials and never restores the raw secret', async () => {
  const drafts = { current: new Map() };
  await mount('prompt', drafts);
  await click('新建');
  await change(field('名称'), 'Ordinary draft');
  expect(drafts.current.size).toBe(1);
  await click('高级：编辑完整定义');
  await change(field('完整定义 JSON'), JSON.stringify({ name: 'Sensitive draft', kind: 'prompt', content: 'Example', api_key: 'synthetic-marker' }));
  expect(drafts.current.size).toBe(0);
  expect(document.body.textContent).toContain('含敏感内容或未通过校验的草稿不会跨页面保留');
  await act(async () => root.render(<section>Another page</section>));
  await mount('prompt', drafts);
  expect(document.querySelector('dialog')).toBeNull();
  expect(document.body.textContent).not.toContain('继续编辑草稿');
  expect(JSON.stringify([...drafts.current.values()])).not.toContain('synthetic-marker');
  expect(h.mutate).not.toHaveBeenCalled();
});

it('discards a late preview after changing category and does not revive it on return', async () => {
  h.data['/api/manage/assets'] = [{ id: 'scope_prompt', name: 'Scoped rule', kind: 'prompt', content: 'Synthetic', agent_ids: ['codex'], version: 1 }];
  let resolve;
  h.mutate.mockImplementationOnce(() => new Promise(done => { resolve = done; }));
  await mount('prompt');
  await click('预览应用到 Codex CLI');
  expect(resolve).toBeTypeOf('function');
  await mount('providers');
  await act(async () => resolve({ id: 'old_preview', status: 'planned', version: 1, payload: { metadata: { host_id: 'codex' }, files: [] } }));
  expect(document.querySelector('dialog')).toBeNull();
  await mount('prompt');
  expect(document.querySelector('dialog')).toBeNull();
  expect(h.mutate.mock.calls.some(([path]) => path.endsWith('/apply'))).toBe(false);
});

it.each(['wrong-plan', 'wrong-host', 'lost-response'])('multi-host apply treats %s as unknown and prevents a second apply', async failure => {
  await mount();
  await click('同步到多宿主');
  await change(field('模型'), 'explicit-model');
  const plan = { id: 'sync_plan', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } };
  h.mutate.mockResolvedValueOnce({ status: 'planned', results: { codex: { plan } } });
  await click('生成各宿主预览', dialog());
  await click('查看差异并确认', dialog());
  await act(async () => dialog().querySelector('input[type=checkbox]').click());
  if (failure === 'lost-response') {
    h.mutate.mockRejectedValueOnce(new Error('Synthetic response lost'));
    h.request.mockRejectedValueOnce(new Error('Synthetic readback unavailable'));
  } else {
    h.mutate.mockResolvedValueOnce({ status: 'applied' });
    h.request.mockResolvedValueOnce({ ...plan, id: failure === 'wrong-plan' ? 'another_plan' : plan.id, status: 'applied', payload: { metadata: { host_id: failure === 'wrong-host' ? 'claude-code' : 'codex' } } });
  }
  h.request.mockResolvedValueOnce({ host_id: 'codex', resources: {} });
  await click('确认应用到 Codex CLI', dialog());
  expect(dialog().textContent).toContain('未知');
  expect([...dialog().querySelectorAll('button')].some(node => node.textContent === '确认应用到 Codex CLI')).toBe(false);
  await click('关闭', dialog());
  expect(button('查看差异并确认', dialog()).disabled).toBe(true);
  expect(h.mutate.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(1);
});

it('shows model choice separately from file resources after apply', async () => {
  const { readbackResources } = await import('../src/components/agent-management/ConfigurationState.jsx');
  const plan = { payload: { metadata: { host_id: 'codex', provider_id: 'provider_test', entity_versions: { provider_test: 1, model_test: 1, account_test: 1 } } } };
  const state = { host_id: 'codex', selection: { provider_id: 'provider_test', provider_name: 'Synthetic', model_id: 'chosen-native', status: 'verified' }, resources: {
    provider_test: { kind: 'provider', status: 'verified' }, model_test: { kind: 'model', status: 'not_managed' }, account_test: { kind: 'account', status: 'not_managed' }
  } };
  const resources = readbackResources(plan, state, { provider_test: 'Synthetic', model_test: 'Chosen model' });
  expect(resources.map(item => item.id)).toEqual(['provider_test']);
  await act(async () => root.render(<AgentPlanDialog plan={plan} result={{ status: 'applied', payload: { readback: 'verified' } }} hostReadback={{ state, resources }} agent="Codex CLI" category="模型与账号" valid onClose={() => {}} />));
  const summary = document.querySelector('.agent-host-readback');
  expect(summary.textContent).toContain('chosen-native');
  expect(summary.querySelector('ul').textContent).not.toContain('尚未应用');
});

it('displays summary default_model and external active badge even when prior selection was externally changed', async () => {
  const ccSummaryProvider = {
    id: 'provider_cc_current',
    name: 'OpenAI Official',
    protocol: 'openai',
    wire_api: 'responses',
    supported_endpoints: ['chat/completions', 'responses'],
    base_url: 'https://api.openai.com/v1',
    default_model: 'gpt-5.6-sol',
    cc_switch_current_hosts: ['codex'],
    has_credential: true,
    enabled: true,
    version: 1,
    _summary: true,
  };
  h.data[catalogPath('providers', { agentId: 'codex' })] = pageOf([ccSummaryProvider]);
  h.data[catalogPath('models', { agentId: 'codex' })] = pageOf([]);
  h.data['/api/manage/hosts/codex/state'] = {
    selection: { provider_id: 'provider_old', provider_name: 'Old Provider', status: 'externally_changed' },
    resources: {},
    coverage: 'complete_owned_entries',
  };
  await mount();
  const card = container.querySelector('.management-card');
  expect(card.classList.contains('is-active-card')).toBe(true);
  expect(card.querySelector('.cc-model-pill')?.textContent).toBe('gpt-5.6-sol');
  expect(card.textContent).toContain('本机当前生效');
  const currentConfig = container.querySelector('.agent-current-configuration');
  expect(currentConfig.textContent).toContain('OpenAI Official');
  expect(currentConfig.textContent).toContain('gpt-5.6-sol');
  expect(currentConfig.textContent).toContain('本机当前生效');
});

it('displays clean local-config labels for synced/migrated assets and migration source formats without leaking cc-switch', async () => {
  const { ManagementMigration } = await import('../src/components/ManagementMigration.jsx');
  h.data[resourcePagePath('mcp', 'assigned')] = pageOf([
    { id: 'mcp_synced', name: 'node_repl', kind: 'mcp', version: 1, agent_ids: ['codex'], source: { type: 'cc-switch' }, _summary: true },
    { id: 'mcp_migrated', name: 'migrated_tool', kind: 'mcp', version: 1, agent_ids: ['codex'], source: { type: 'cc-switch-migration' }, _summary: true },
  ]);
  await mount('mcp');
  const cards = [...container.querySelectorAll('.management-card')];
  expect(cards).toHaveLength(2);
  for (const card of cards) {
    const facts = card.querySelector('.management-facts')?.textContent || '';
    expect(facts).toContain('本地配置');
    expect(facts).not.toContain('cc-switch');
  }

  h.data['/api/manage/migrations/sources'] = {
    candidates: [
      { source_path: '/tmp/config.db', format: 'cc-switch-sqlite', status: 'available' },
      { source_path: '/tmp/config.json', format: 'cc-switch-json', status: 'available' },
    ],
  };
  await act(async () => root.render(<ManagementMigration onChanged={() => {}} />));
  await click('迁移本地配置');
  const selectText = dialog().querySelector('select')?.textContent || '';
  expect(selectText).toContain('/tmp/config.db · SQLite');
  expect(selectText).toContain('/tmp/config.json · JSON');
  expect(selectText).not.toContain('cc-switch');
});

it('sanitizes compatibility wire keys in EvidenceDetails, maps connection badges, sends confirmed=true on pause/resume, and formats raw warnings', async () => {
  const ccProvider = {
    ...provider,
    id: 'provider_compat',
    name: 'Compat Provider',
    cc_switch_current_hosts: ['codex'],
    source: { type: 'cc-switch-migration', reference_commit: '9f8e7d6c5b4a' },
    _summary: false,
  };
  h.data[catalogPath('providers', { agentId: 'codex' })] = pageOf([ccProvider]);
  h.data['/api/connections'] = [];
  await mount();

  // 1. EvidenceDetails never shows cc_switch, cc-switch, or reference_commit
  const cardText = container.querySelector('.management-card')?.textContent || '';
  expect(cardText).not.toContain('cc_switch');
  expect(cardText).not.toContain('cc-switch');
  expect(cardText).not.toContain('reference_commit');
  expect(cardText).toContain('local_active_hosts');

  // 2. Disconnected StatusBadge renders localized "未接入" instead of raw "not_connected"
  const connBar = container.querySelector('.cc-connection-status-bar');
  expect(connBar.textContent).toContain('未接入');
  expect(connBar.textContent).not.toContain('not_connected');

  // 3. One-click connect warning sanitizes raw Python English exception
  h.mutate.mockResolvedValueOnce({
    ok: true,
    warning: "FileNotFoundError: [Errno 2] No such file or directory: '/Users/private/.codex/config.toml'",
  });
  await click('一键接入续境', connBar);
  const alertText = container.querySelector('[role=alert]')?.textContent || '';
  expect(alertText).toContain('历史采集已开启，但宿主配置未完全自动写入：');
  expect(alertText).not.toContain('FileNotFoundError');
  expect(alertText).not.toContain('/Users/private');

  // 4. Paused StatusBadge renders localized "已暂停" and pause/resume sends confirmed: true
  h.data['/api/connections'] = [{ id: 'codex', label: 'Codex CLI', enabled: true, paused: true }];
  await mount();
  const pausedBar = container.querySelector('.cc-connection-status-bar');
  expect(pausedBar.textContent).toContain('已暂停');
  expect(pausedBar.textContent).not.toContain('paused');
  h.mutate.mockResolvedValueOnce({ id: 'codex', enabled: true, paused: false });
  await click('恢复采集', pausedBar);
  expect(h.mutate).toHaveBeenCalledWith('/api/connections/codex', 'PATCH', {
    paused: false,
    confirmed: true,
  });
});

it('surfaces partial failures and queues multiple conflict_preview plans in multi-host sync', async () => {
  const claudeHost = { ...host, id: 'claude-code', name: 'Claude Code' };
  const cursorHost = { ...host, id: 'cursor', name: 'Cursor' };
  h.data['/api/manage/hosts'] = [host, claudeHost, cursorHost];
  h.data[catalogPath('providers', { agentId: 'codex' })] = pageOf([provider]);
  await mount();

  const card = container.querySelector('.management-card');
  await click('同步到多宿主', card);
  for (const label of ['Claude Code', 'Cursor']) {
    const cb = [...dialog().querySelectorAll('label')].find(node => node.textContent.includes(label))?.querySelector('input');
    await act(async () => { cb.click(); });
  }
  await change(dialog().querySelector('input:not([type=checkbox])'), 'gpt-4.1');

  // Partial failure with planned + failed
  h.mutate.mockResolvedValueOnce({
    ok: false,
    status: 'partial_plan',
    planned: ['codex'],
    failed: ['cursor'],
    results: {
      codex: {
        ok: true,
        status: 'planned',
        plan: { id: 'plan_codex', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } },
      },
      cursor: {
        ok: false,
        status: 'failed',
        error: 'Cursor documents API-key setup in Cursor Settings > Models, not a supported configuration file API',
      },
    },
  });
  await click('生成各宿主预览', dialog());
  const modalAlert = dialog().querySelector('.inline-error')?.textContent || '';
  expect(modalAlert).toContain('Codex CLI');
  expect(modalAlert).toContain('Cursor');

  // Multiple conflict_preview plans queued sequentially
  h.mutate.mockResolvedValueOnce({
    ok: true,
    status: 'planned',
    planned: ['codex', 'claude-code'],
    failed: {},
    results: {
      codex: {
        ok: true,
        status: 'conflict_preview',
        plan: { id: 'plan_conflict_codex', version: 1, status: 'planned', payload: { metadata: { host_id: 'codex' }, files: [] } },
      },
      'claude-code': {
        ok: true,
        status: 'conflict_preview',
        plan: { id: 'plan_conflict_claude', version: 1, status: 'planned', payload: { metadata: { host_id: 'claude-code' }, files: [] } },
      },
    },
  });
  await click('生成各宿主预览', dialog());
  expect(dialog().textContent).toContain('Codex CLI');
  expect(dialog().textContent).toContain('剩余 1 个冲突宿主待确认');
  await act(async () => { dialog().querySelector('input[type=checkbox]').click(); });
  expect(dialog().querySelector('input[type=checkbox]').checked).toBe(true);

  // Closing the 1st conflict dialog immediately opens the 2nd conflict dialog with fresh unchecked consent
  await click('取消', dialog());
  expect(dialog().textContent).toContain('Claude Code');
  expect(dialog().querySelector('input[type=checkbox]').checked).toBe(false);
  await click('取消', dialog());
  expect(dialog().textContent).toContain('同步到多宿主');
});

