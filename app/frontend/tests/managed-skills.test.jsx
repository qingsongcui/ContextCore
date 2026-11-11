// @vitest-environment jsdom
import React, { act, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// Synthetic transport isolates local control behavior. Real React, forms and
// dialogs run here; actual filesystem application remains isolated-browser QA.
const h = vi.hoisted(() => ({ data: {}, errors: {}, mutate: vi.fn(), request: vi.fn() }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), mutate: h.mutate, request: h.request }));
vi.mock('../src/lib/useResource.js', async original => ({ ...await original(), useResource(path) { return { data: h.data[path] ?? h.data[path?.split('?')[0]] ?? null, loading: false, error: h.errors[path] || null, reload() {} }; } }));
import { AgentManagementPage } from '../src/pages/AgentManagementPage.jsx';
import { AssetTools } from '../src/components/AssetTools.jsx';
import { AgentPlanDialog } from '../src/components/agent-management/AgentPlanDialog.jsx';
import { setLang } from '../src/lib/i18n.jsx';

const linked = { agent_ids: ['codex'], id: 'asset_0123456789abcdef', name: 'Linked Skill', kind: 'skill', install_mode: 'symlink', version: 4, files: { 'SKILL.md': '# Four', 'scripts/do.sh': 'echo not-executed' }, source: { type: 'local', path: '/synthetic/source' }, external_metadata: { keep: true } };
const codex = { id: 'codex', name: 'Codex CLI', configuration_status: 'supported_format', verification_status: 'not_verified', capabilities: { skills: true, mcp: true, providers: true, prompts: true, native_activation: true } };
const omp = { ...codex, id: 'omp', name: 'omp', capabilities: { ...codex.capabilities, native_activation: false } };
const plan = (id = 'plan_synthetic', agent = 'codex') => ({ id, version: 1, status: 'planned', payload: { metadata: { host_id: agent, operation: 'install' }, files: [{ path: `/synthetic/${agent}/SKILL.md`, install_mode: 'symlink', stage: 'pending', source_version: 4 }], readback: 'not_run', host_reload: 'not_run', real_request: 'not_run' } });
let root, container;
function ControlledPage({ initialAgent = 'codex', initialSection = 'skill' }) {
  const [location, setLocation] = useState({ agent: initialAgent, section: initialSection });
  return <AgentManagementPage agentId={location.agent} section={location.section} onAgentChange={(agent, section) => setLocation({ agent, section })} revision={0} onChanged={() => {}} projects={[]} />;
}
async function mount(element = <ControlledPage />) { await act(async () => { root.render(element); }); }
function button(text, parent = document) { const value = [...parent.querySelectorAll('button')].find(item => item.textContent === text); if (!value) throw new Error(`Missing button: ${text}`); return value; }
function field(label, parent = document) { const node = [...parent.querySelectorAll('label.field')].find(item => item.querySelector('span')?.textContent === label); if (!node) throw new Error(`Missing field: ${label}`); return node.querySelector('input,select,textarea'); }
async function click(text, parent) { await act(async () => { button(text, parent).click(); }); }
async function change(element, value) { await act(async () => { const setter = Object.getOwnPropertyDescriptor(element.tagName === 'SELECT' ? HTMLSelectElement.prototype : element.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype, 'value').set; setter.call(element, value); element.dispatchEvent(new Event(element.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true })); }); }
function dialog() { return document.querySelector('dialog[open]'); }
async function confirmApply() { const checkbox = dialog().querySelector('input[type=checkbox]'); await act(async () => { checkbox.click(); }); await click('确认应用到 Codex CLI', dialog()); }
function expand(text, parent = document) { const summary = [...parent.querySelectorAll('summary')].find(item => item.textContent === text); if (!summary) throw new Error(`Missing details: ${text}`); summary.parentElement.open = true; }

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  setLang('zh');
  window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
  // jsdom has no native top layer. Do not emulate focus or component behavior.
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  h.data = { '/api/manage/assets': [structuredClone(linked)], '/api/manage/hosts': [codex, omp], '/api/manage/providers': [], '/api/manage/models': [], '/api/manage/accounts': [], '/api/manage/bindings': [], '/api/manage/provider-presets': { presets: [] } };
  h.errors = {}; h.mutate.mockReset(); h.request.mockReset();
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.useRealTimers(); });

describe('consumer Skill copy, link and application controls', () => {
  it('creates a usable independent Skill without requiring JSON or executing scripts', async () => {
    await mount(); await click('新建');
    expect(dialog().textContent).toContain('独立副本（推荐）');
    await change(field('名称', dialog()), 'My independent Skill');
    await change(field('技能说明（SKILL.md）', dialog()), '# Skill\nUse carefully.');
    await click('预览保存', dialog());
    expect(h.mutate).not.toHaveBeenCalled();
    h.mutate.mockResolvedValue({ id: 'new', version: 1 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets', 'POST', expect.objectContaining({ name: 'My independent Skill', install_mode: 'copy', files: { 'SKILL.md': '# Skill\nUse carefully.' } }));
    expect(document.querySelector('dialog')).toBeNull();
  });

  it('makes a new shared-version link only through the advanced install choice', async () => {
    await mount(); await click('新建');
    await change(field('名称', dialog()), 'Reviewed linked Skill');
    await change(field('技能说明（SKILL.md）', dialog()), '# Linked Skill');
    expand('高级：共享版本链接', dialog());
    await change(field('技能安装方式', dialog()), 'symlink');
    await click('预览保存', dialog());
    h.mutate.mockResolvedValue({ ...linked, name: 'Reviewed linked Skill', version: 1 });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets', 'POST', expect.objectContaining({ name: 'Reviewed linked Skill', install_mode: 'symlink', files: { 'SKILL.md': '# Linked Skill' } }));
  });

  it('changes linked mode to copy while retaining extra files, metadata and the expected version', async () => {
    await mount(); await click('编辑');
    await change(field('技能安装方式', dialog()), 'copy');
    await change(field('技能说明（SKILL.md）', dialog()), '# Edited');
    await click('预览保存', dialog());
    h.mutate.mockResolvedValue({ ...linked, version: 5, install_mode: 'copy' });
    await click('保存共享定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith(`/api/manage/assets/${linked.id}`, 'PATCH', expect.objectContaining({ expected_version: 4, install_mode: 'copy', files: { 'SKILL.md': '# Edited', 'scripts/do.sh': 'echo not-executed' }, external_metadata: { keep: true } }));
  });

  it('inherits source-update link mode and invalidates preview before changing to an independent copy', async () => {
    await mount(); await click('更新技能来源');
    expect(field('技能安装方式', dialog()).value).toBe('symlink');
    await click('解析预览', dialog());
    expect(h.mutate).not.toHaveBeenCalled();
    expect(button('确认导入定义', dialog())).toBeTruthy();
    await change(field('技能安装方式', dialog()), 'copy');
    expect([...dialog().querySelectorAll('button')].some(item => item.textContent === '确认导入定义')).toBe(false);
    await click('解析预览', dialog());
    h.mutate.mockResolvedValue({ imported: [{ ...linked, install_mode: 'copy', version: 5 }], unsupported: [] });
    await click('确认导入定义', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/import', 'POST', { agent_id: 'codex', format: 'skill-local', data: '/synthetic/source', install_mode: 'copy', target_id: linked.id, expected_version: 4, name: linked.name });
  });

  it('requires explicit single-Agent confirmation and reads back independently from apply transport', async () => {
    await mount(); const planned = plan(); h.mutate.mockResolvedValueOnce(planned);
    await click('预览应用到 Codex CLI');
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/plans', 'POST', { host_id: 'codex', operation: 'install', asset_ids: [linked.id] });
    expect(button('确认应用到 Codex CLI', dialog()).disabled).toBe(true);
    const receipt = { ...planned, status: 'applied', payload: { ...planned.payload, metadata: { ...planned.payload.metadata, entity_versions: { [linked.id]: linked.version } }, readback: 'verified' } };
    h.mutate.mockResolvedValueOnce({ status: 'applied' });
    h.request.mockResolvedValueOnce(receipt);
    h.request.mockResolvedValueOnce({ host_id: 'codex', selection: { status: 'not_managed' }, effects: { selection: 'not_selected_by_senrelay', configuration: 'not_managed', host_loaded: 'not_verified', reachability: 'not_checked', authentication: 'not_checked', invocation: 'not_checked' }, resources: { [linked.id]: { status: 'verified' } }, coverage: 'complete_owned_entries' });
    await confirmApply();
    expect(h.mutate).toHaveBeenLastCalledWith('/api/manage/plans/plan_synthetic/apply', 'POST', { confirmed: true, expected_version: 1 });
    expect(h.request).toHaveBeenNthCalledWith(1, '/api/manage/plans/plan_synthetic');
    expect(h.request).toHaveBeenNthCalledWith(2, '/api/manage/hosts/codex/state');
    expect(dialog().textContent).toContain('已验证');
    expect(dialog().textContent).toContain('未运行');
    expect(dialog().querySelector('.agent-host-readback').textContent).toContain('Linked Skill');
    expect(dialog().querySelector('.agent-host-readback').textContent).toContain('配置已写入');
    expect(dialog().textContent).toContain('尚未检查。写入配置不能说明网络可达。');
    expect(dialog().textContent).not.toContain('已就绪');
  });

  it('keeps a partial receipt and error visible without offering another unsafe apply', async () => {
    await mount(); const planned = plan('plan_partial'); h.mutate.mockResolvedValueOnce(planned);
    await click('预览应用到 Codex CLI');
    h.mutate.mockRejectedValueOnce(new Error('synthetic partial failure'));
    h.request.mockResolvedValueOnce({ ...planned, status: 'recovery_required' });
    await confirmApply();
    expect(dialog().textContent).toContain('需要恢复'); expect(dialog().textContent).toContain('synthetic partial failure');
    expect([...dialog().querySelectorAll('button')].some(item => item.textContent === '确认应用到 Codex CLI')).toBe(false);
    expect(h.mutate.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(1);
  });

  it('restores a selected shared definition version without modifying any Agent files', async () => {
    const old = { version: 2, created_at: '2026-01-01', data: { ...linked, install_mode: 'copy', files: { 'SKILL.md': '# Two' } } };
    h.data[`/api/manage/assets/${linked.id}/versions`] = [old];
    await mount(<AssetTools item={linked} selectedAgent="Codex CLI" onChanged={() => {}} />);
    await click('版本与恢复');
    expect(button('确认恢复此版本', dialog()).disabled).toBe(true);
    await click('2', dialog());
    expand('变更前后', dialog()); expect(dialog().textContent).toContain('# Two');
    h.mutate.mockResolvedValue({ ...linked, version: 5, install_mode: 'copy' });
    await click('确认恢复此版本', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith(`/api/manage/assets/${linked.id}/restore`, 'POST', { version: 2, expected_version: 4 });
  });

  it('does not connect to a tool or start a process merely by opening probe details', async () => {
    const tool = { id: 'tool_fixture', name: 'Local fixture', kind: 'mcp', version: 1, config: { command: 'fixture-command', args: ['--metadata-only'] } };
    await mount(<AssetTools item={tool} selectedAgent="Codex CLI" onChanged={() => {}} />);
    await click('验证工具发现'); expect(h.mutate).not.toHaveBeenCalled();
    expect(dialog().textContent).toContain('fixture-command');
    h.mutate.mockResolvedValue({ status: 'verified', tools: [] });
    await click('确认连接并发现工具', dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/assets/tool_fixture/probe', 'POST', { confirmed: true, expected_version: 1 });
  });
});

describe('Agent-scoped uncertainty and failed drafts', () => {
  it('starts at an explicit chooser and never applies a default Agent', async () => {
    await mount(<ControlledPage initialAgent="" initialSection="" />);
    expect(document.querySelector('h1').textContent).toBe('Agent 管理'); expect(h.mutate).not.toHaveBeenCalled();
    await act(async () => { [...document.querySelectorAll('.agent-choice')].find(node => node.textContent.includes('Oh My Pi')).click(); });
    expect(document.querySelector('h1').textContent).toBe('Oh My Pi (OMP)');
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('discards late previews after changing Agent and never carries a prior confirmation', async () => {
    let resolve;
    h.mutate.mockReturnValueOnce(new Promise(done => { resolve = done; }));
    await mount(<AgentManagementPage agentId="codex" section="skill" onAgentChange={() => {}} revision={0} onChanged={() => {}} projects={[]} />); await click('预览应用到 Codex CLI');
    await mount(<AgentManagementPage agentId="omp" section="skill" onAgentChange={() => {}} revision={1} onChanged={() => {}} projects={[]} />);
    await act(async () => { resolve(plan()); await Promise.resolve(); });
    expect(document.querySelector('h1').textContent).toBe('Oh My Pi (OMP)');
    expect(document.querySelector('dialog')).toBeNull();
    expect(h.mutate.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(0);
  });

  it('rejects a returned preview for another Agent even when transport succeeds', async () => {
    await mount(); h.mutate.mockResolvedValueOnce(plan('wrong', 'omp'));
    await click('预览应用到 Codex CLI');
    expect(document.querySelector('[role=alert]').textContent).toContain('不属于当前助手');
    expect(document.querySelector('dialog')).toBeNull();
    expect(h.mutate.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(0);
  });

  it('invalidates an already confirmed preview when the URL category changes', async () => {
    await mount(<AgentManagementPage agentId="codex" section="skill" onAgentChange={() => {}} revision={0} onChanged={() => {}} projects={[]} />);
    h.mutate.mockResolvedValueOnce(plan());
    await click('预览应用到 Codex CLI');
    await act(async () => { dialog().querySelector('input[type=checkbox]').click(); });
    expect(button('确认应用到 Codex CLI', dialog()).disabled).toBe(false);
    await mount(<AgentManagementPage agentId="codex" section="mcp" onAgentChange={() => {}} revision={1} onChanged={() => {}} projects={[]} />);
    expect(document.querySelector('dialog')).toBeNull();
    expect(h.mutate.mock.calls.filter(([path]) => path.endsWith('/apply'))).toHaveLength(0);
  });

  it('retains failed form edits and preserves unknown fields across ordinary and advanced modes', async () => {
    await mount(); await click('编辑');
    await change(field('技能说明（SKILL.md）', dialog()), '# Unsaved readable edit');
    await click('高级：编辑完整定义', dialog());
    const raw = field('完整定义 JSON', dialog());
    expect(JSON.parse(raw.value).external_metadata).toEqual({ keep: true });
    await change(raw, '{broken');
    expect(button('预览保存', dialog()).disabled).toBe(true);
    await click('返回表单', dialog()); expect(field('完整定义 JSON', dialog()).value).toBe('{broken');
    await change(field('完整定义 JSON', dialog()), JSON.stringify({ ...linked, files: { ...linked.files, 'SKILL.md': '# Unsaved readable edit' }, external_metadata: { keep: true, extended: 'retained' } }));
    await click('返回表单', dialog());
    expect(field('技能说明（SKILL.md）', dialog()).value).toBe('# Unsaved readable edit');
    await click('预览保存', dialog()); h.mutate.mockRejectedValueOnce(new Error('synthetic stale version'));
    await click('保存共享定义', dialog());
    expect(dialog().textContent).toContain('synthetic stale version');
    await click('返回编辑', dialog());
    expect(field('技能说明（SKILL.md）', dialog()).value).toBe('# Unsaved readable edit');
    expect(h.mutate.mock.calls.filter(([path]) => path === '/api/manage/plans')).toHaveLength(0);
  });

  it('preserves a scoped draft on category changes but does not carry the editor or application preview', async () => {
    await mount(); await click('编辑'); await change(field('技能说明（SKILL.md）', dialog()), '# Draft to retain');
    await click('工具');
    // Native background interaction is prevented in the browser; this direct
    // control path also proves the explicit navigation-discard guard.
    await click('确认离开', [...document.querySelectorAll('dialog')].at(-1));
    expect(document.querySelector('dialog')).toBeNull();
    await click('技能'); await click('继续编辑草稿');
    expect(field('技能说明（SKILL.md）', dialog()).value).toBe('# Draft to retain');
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it('keeps an import source after failure and requires preview again after any source edit', async () => {
    await mount(); await click('导入技能'); await change(field('来源类型'), 'skill-local');
    await change(field('技能目录的完整路径', dialog()), '/synthetic/missing');
    await click('解析预览', dialog()); h.mutate.mockRejectedValueOnce(new Error('synthetic unreadable source'));
    await click('确认导入定义', dialog());
    expect(dialog().textContent).toContain('synthetic unreadable source');
    expect(field('技能目录的完整路径', dialog()).value).toBe('/synthetic/missing');
    await change(field('技能目录的完整路径', dialog()), '/synthetic/corrected');
    expect([...dialog().querySelectorAll('button')].some(item => item.textContent === '确认导入定义')).toBe(false);
    await click('取消', dialog()); expect(document.querySelector('dialog')).toBeNull();
    expect(h.mutate).toHaveBeenCalledTimes(1);
  });
});

describe('new native management consumers', () => {
  it.each(['omp', 'cursor', 'antigravity', 'codex-desktop'].flatMap(id => ['skill', 'prompt'].map(kind => [id, kind])))('reaches %s %s through a scoped preview without applying implicitly', async (id, kind) => {
    const native = { ...codex, id, name: id, selection_config_only: id === 'omp' || id === 'codex-desktop' };
    const asset = { ...linked, kind, agent_ids: [id], ...(kind === 'prompt' ? { content: 'Wait for confirmation' } : {}) };
    h.data['/api/manage/hosts'] = [native];
    h.data['/api/manage/assets'] = [asset];
    await mount(<ControlledPage initialAgent={id} initialSection={kind} />);
    const name = id === 'omp' ? 'Oh My Pi (OMP)' : id;
    const planned = plan(`plan_${id}_${kind}`, id);
    h.mutate.mockResolvedValueOnce(planned);
    await click(`预览应用到 ${name}`);
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/plans', 'POST', { host_id: id, operation: 'install', asset_ids: [linked.id] });
    expect(button(`确认应用到 ${name}`, dialog()).disabled).toBe(true);
    expect(h.mutate.mock.calls.some(([path]) => path.endsWith('/apply'))).toBe(false);
  });

  it.each(['cursor', 'antigravity'])('keeps %s native provider restrictions actionable and refuses to preview', async id => {
    const reason = id === 'cursor'
      ? '请到 Cursor 设置的 Models 页面管理密钥并选择模型。这里可以保存和导出定义，但不会改写它的私人配置数据库。'
      : '请在 Antigravity 对话的模型菜单选择模型。它没有公开的自定义 API 配置接口，这里不会改写或借用账号缓存。';
    const native = { ...codex, id, name: id, capabilities: { ...codex.capabilities, providers: false, native_activation: false }, operation_limits: { providers: { reason: 'backend source notice' } } };
    h.data['/api/manage/hosts'] = [native];
    h.data['/api/manage/providers'] = [{ id: 'provider_native', name: 'Saved definition', protocol: 'openai', enabled: true, agent_ids: [id], version: 1, base_url: 'https://synthetic.invalid/v1' }];
    await mount(<ControlledPage initialAgent={id} initialSection="providers" />);
    expect(document.querySelector('.agent-unsupported').textContent).toContain(reason);
    expect(button(`预览应用到 ${id}`).disabled).toBe(true);
    expect(button('一键切换').disabled).toBe(true);
    await act(async () => { button(`预览应用到 ${id}`).click(); });
    expect(h.mutate).not.toHaveBeenCalled();
  });

  it.each(['omp', 'codex-desktop'])('allows %s default configuration selection but never claims a running model switch', async id => {
    const native = { ...codex, id, name: id, selection_config_only: true, configuration_notice: 'File selection is not running-session adoption.' };
    const value = { id: 'provider_native', name: 'Native service', protocol: 'openai', enabled: true, agent_ids: [id], model_map: { friendly: 'native-model' }, version: 1, base_url: 'https://synthetic.invalid/v1' };
    h.data['/api/manage/hosts'] = [native];
    h.data['/api/manage/providers'] = [value];
    await mount(<ControlledPage initialAgent={id} initialSection="providers" />);
    expect(button('预览默认模型配置').disabled).toBe(false);
    expect(container.textContent).toContain(id === 'omp' ? '这里只修改 OMP 默认配置' : '这些配置文件与 Codex CLI 共用');
    await click('预览默认模型配置');
    expect(dialog().textContent).toContain('不会切换正在运行的对话');
    expect(h.mutate).not.toHaveBeenCalled();
    const planned = plan('native_selection', id);
    planned.payload.metadata.selection_config_only = true;
    planned.payload.metadata.model_selection = { provider_name: value.name, native_model: 'native-model' };
    h.mutate.mockResolvedValueOnce(planned);
    const name = id === 'omp' ? 'Oh My Pi (OMP)' : id;
    await click(`预览应用到 ${name}`, dialog());
    expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/plans', 'POST', { host_id: id, operation: 'install', asset_ids: [], provider_id: value.id, activate_provider: true, model_id: 'friendly' });
    expect(dialog().textContent).toContain('宿主是否采用仍需核验');
    expect(button(`确认应用到 ${name}`, dialog()).disabled).toBe(true);
  });

  it('requires consent to shared Codex files instead of falsely promising all other clients stay unchanged', async () => {
    const onApply = vi.fn();
    const planned = plan('shared', 'codex-desktop');
    Object.assign(planned.payload.metadata, { shared_configuration: true, configuration_notice: 'CLI and Desktop share these files; Desktop consumption is unverified.' });
    await mount(<AgentPlanDialog plan={planned} agent="Codex Desktop" category="模型与账号" valid onClose={() => {}} onApply={onApply} />);
    expect(dialog().textContent).toContain('共享配置文件更改');
    expect(dialog().textContent).toContain('共用这些文件的入口也会受到影响');
    expect(dialog().textContent).toContain('这些配置文件与 Codex CLI 共用');
    expect(dialog().textContent).not.toContain('其他助手保持不变');
    expect(button('确认应用到 Codex Desktop', dialog()).disabled).toBe(true);
    await act(async () => { dialog().querySelector('input[type=checkbox]').click(); });
    await click('确认应用到 Codex Desktop', dialog());
    expect(onApply).toHaveBeenCalledTimes(1);
  });
});
