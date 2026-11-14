// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot } from 'react-dom/client';
const h = vi.hoisted(() => ({ resources: [], data: {}, errors: {}, request: vi.fn(), mutate: vi.fn(), reload: vi.fn() }));
vi.mock('../src/lib/useResource.js', async original => ({ ...await original(), useResource(path, revision) { h.resources.push({ path, revision }); return { data: h.data[path] ?? null, loading: Boolean(h.loading[path]), error: h.errors[path] || null, reload: h.reload }; } }));
vi.mock('../src/lib/api.js', async original => ({ ...await original(), request: h.request, mutate: h.mutate }));
import { KnowledgePage, ContentDetail, ContentEditor } from '../src/pages/KnowledgePage.jsx';
import { InboxPage } from '../src/pages/InboxPage.jsx';
import { ConversationDetail } from '../src/pages/ConversationsPage.jsx';
import { ProjectsPage } from '../src/pages/ProjectsPage.jsx';
import { ProjectDialog } from '../src/components/ProjectDialog.jsx';
import { AccountOperation } from '../src/components/agent-management/AccountOperation.jsx';
import { Shell } from '../src/components/Shell.jsx';
import { UsagePage } from '../src/pages/UsagePage.jsx';
import { Markdown, MARKDOWN_LIMIT } from '../src/components/Markdown.jsx';
import { eventAffectsObject } from '../src/lib/objectRefresh.js';
import { setLang } from '../src/lib/i18n.jsx';
import { ApiError } from '../src/lib/api.js';
import { backendCompatibility } from '../src/lib/management.js';
import { ImportDialog } from '../src/components/agent-management/ImportDialog.jsx';
import { ProviderChecks } from '../src/components/ProviderChecks.jsx';
import { ConsolePage } from '../src/pages/ConsolePage.jsx';
import { SettingsPage } from '../src/pages/SettingsPage.jsx';
import { ProviderSetup } from '../src/components/agent-management/ProviderSetup.jsx';
let root, container, mobile;
const props = { projectId: 'p', projects: [{ id: 'p', name: 'Project' }], kind: 'memory', revision: 0, onChanged: vi.fn(), onProject: vi.fn(), onBack: vi.fn(), onClose: vi.fn() };
const indexPath = '/api/content/index?status=active&exclude_category=task&limit=50&offset=0&kind=memory&project_id=p&pinned_first=true';
const button = (text, scope = container) => Array.from(scope.querySelectorAll('button')).find(item => item.textContent.trim() === text);
const field = text => Array.from(container.querySelectorAll('label')).find(item => item.querySelector(':scope > span')?.textContent === text)?.querySelector('input,textarea,select');
async function render(node) { await act(async () => root.render(node)); }
async function click(node) { expect(node).toBeTruthy(); await act(async () => node.click()); }
async function change(node, value) {
  expect(node).toBeTruthy();
  await act(async () => {
    const prototype = node.tagName === 'SELECT' ? HTMLSelectElement.prototype : node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(prototype, 'value').set.call(node, value);
    node.dispatchEvent(new Event(node.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  });
}
async function submit(form = container.querySelector('form')) { await act(async () => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }))); }
function seedContent(items) { h.data[indexPath] = { total: items.length, items }; }
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  h.data = {}; h.errors = {}; h.loading = {}; h.resources = []; h.request.mockReset(); h.mutate.mockReset(); h.reload.mockReset();
  Object.values(props).forEach(value => { if (vi.isMockFunction(value)) value.mockReset(); });
  mobile = false; setLang('zh');
  vi.stubGlobal('matchMedia', vi.fn(query => ({ get matches() { return query.includes('max-width') ? mobile : false; }, addEventListener() {}, removeEventListener() {} })));
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
  // Only supply the missing native-dialog capability; application code owns focus.
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  container = document.createElement('div'); document.body.append(container); root = createRoot(container);
});
afterEach(async () => {
  await act(async () => root.unmount()); container.remove(); document.body.style.overflow = ''; vi.useRealTimers(); vi.unstubAllGlobals();
});

describe('actual consumer controls with synthetic API responses', () => {
  it('distinguishes service status loading from a confirmed connection failure', async () => {
    const base = { lang: 'zh', page: 'projects', navigate: vi.fn(), projects: [], managementState: 'ready' };
    await render(<Shell {...base} status={null} statusLoading={true}><div /></Shell>);
    expect(container.querySelector('.local-status').textContent).toContain('正在读取本机服务状态');
    expect(container.querySelector('.local-status').textContent).not.toContain('未连接');
    await render(<Shell {...base} status={{ ok: true, service: { running: true } }} statusLoading={false}><div /></Shell>);
    expect(container.querySelector('.local-status').textContent).toContain('本机服务运行中');
    expect(container.querySelector('.local-status .online')).not.toBeNull();
    await render(<Shell {...base} status={{ ok: true, service: { running: false } }} statusLoading={false}><div /></Shell>);
    expect(container.querySelector('.local-status').textContent).toContain('还不能确认它在运行');
    expect(container.querySelector('.local-status').textContent).not.toContain('本机服务运行中');
    expect(container.querySelector('.local-status .online')).toBeNull();
    await render(<Shell {...base} status={null} statusLoading={false}><div /></Shell>);
    expect(container.querySelector('.local-status').textContent).toContain('本机服务未连接');
  });

  it('does not describe an unverified process as running in settings', async () => {
    h.data['/api/settings'] = { autostart: false, compute_authorized: false, compute_host: null };
    await render(<SettingsPage revision={0} status={{ ok: true, service: { running: false } }} onChanged={vi.fn()} navigate={vi.fn()} />);
    expect(container.textContent).toContain('还不能确认这个进程在运行');
    expect(container.textContent).not.toContain('正在运行，只读你授权过的那几个位置');
    expect(Array.from(container.querySelectorAll('button')).find(item => item.textContent.includes('停止服务'))?.disabled).toBe(true);
    await render(<SettingsPage revision={0} status={{ ok: true, service: { running: true } }} onChanged={vi.fn()} navigate={vi.fn()} />);
    expect(container.textContent).toContain('正在运行，只读你授权过的那几个位置');
    expect(Array.from(container.querySelectorAll('button')).find(item => item.textContent.includes('停止服务'))?.disabled).toBe(false);
  });

  it('uses bounded content previews and suspends the parent list while reading a detail', async () => {
    seedContent([{ id: 'a', title: 'A', version: 1 }]);
    h.data['/api/content/a?include_versions=false'] = { id: 'a', title: 'A', version: 1, content: 'Body' };
    await render(<KnowledgePage {...props} />);
    expect(h.resources[0].path).toBe(indexPath);
    await click(container.querySelector('.content-row-main'));
    expect(container.querySelector('.content-detail')).toBeTruthy();
    expect(h.resources.some(item => item.path === null)).toBe(true);
  });
  it('reads selected merge bodies, preserves them in the editor and carries their confirmed versions to save', async () => {
    seedContent([{ id: 'a', title: 'A', version: 1, project_id: 'p' }, { id: 'b', title: 'B', version: 3, project_id: 'p' }]);
    h.request.mockImplementation(path => Promise.resolve(path.includes('/a?') ? { content: 'full A', version: 1 } : { content: 'full B', version: 3 }));
    h.mutate.mockResolvedValue({ id: 'merged', title: 'Merged' });
    await render(<KnowledgePage {...props} />);
    for (const node of container.querySelectorAll('.content-row input')) await click(node);
    await click(button('合并'));
    expect(h.request.mock.calls.map(call => call[0])).toEqual(['/api/content/a?include_versions=false', '/api/content/b?include_versions=false']);
    expect(field('合并后的正文').value).toBe('full A\n\nfull B');
    await change(field('合并后的标题'), 'Merged'); await submit();
    expect(h.mutate).toHaveBeenCalledWith('/api/content/merge', 'POST', expect.objectContaining({ content: 'full A\n\nfull B', expected_versions: { a: 1, b: 3 } }));
  });
  it('rejects merge preview when a selected source changed instead of assembling mixed revisions', async () => {
    seedContent([{ id: 'a', title: 'A', version: 1 }, { id: 'b', title: 'B', version: 1 }]);
    h.request.mockResolvedValue({ content: 'new body', version: 2 });
    await render(<KnowledgePage {...props} />);
    for (const node of container.querySelectorAll('.content-row input')) await click(node);
    await click(button('合并'));
    expect(container.querySelector('dialog')).toBeNull(); expect(h.mutate).not.toHaveBeenCalled(); expect(container.querySelector('[role=alert]')).toBeTruthy();
  });
  it('sends the readback version for single and batch permanent deletion', async () => {
    h.data['/api/content/a?include_versions=false'] = { id: 'a', title: 'A', version: 7 };
    h.mutate.mockResolvedValue({ count: 1 });
    await render(<ContentDetail {...props} id="a" />);
    await click(container.querySelector('[aria-label="永久删除内容"]')); await click(button('永久删除'));
    expect(h.mutate).toHaveBeenCalledWith('/api/content/a?expected_version=7', 'DELETE');
    seedContent([{ id: 'a', title: 'A', version: 7 }]);
    await render(<KnowledgePage {...props} />); await click(container.querySelector('.content-row input')); await click(button('删除')); await submit();
    expect(h.mutate).toHaveBeenCalledWith('/api/content/batch', 'POST', expect.objectContaining({ ids: ['a'], action: 'delete', expected_versions: { a: 7 } }));
  });
  it('keeps conversation messages and content versions out of the initial full-body request', async () => {
    await render(<ConversationDetail {...props} id="c" />);
    expect(h.resources.map(item => item.path)).toEqual(['/api/conversations/c?include_messages=false', '/api/conversations/c/messages?limit=30&offset=0']);
    h.resources = []; await render(<ContentDetail {...props} id="a" />);
    expect(h.resources.map(item => item.path)).toEqual(['/api/content/a?include_versions=false']);
  });
  it('loads history metadata only when opened and only expands the selected historical body', async () => {
    h.data['/api/content/a?include_versions=false'] = { id: 'a', title: 'A', version: 2 };
    h.data['/api/content/a/versions?limit=20&offset=0'] = { total: 2, items: [{ version: 1, title: 'Old' }, { version: 2, title: 'New' }] };
    h.data['/api/content/a/versions/1'] = { content: 'HISTORICAL_BODY' };
    await render(<ContentDetail {...props} id="a" />); await click(button('修改历史'));
    expect(h.resources.some(item => item.path === '/api/content/a/versions?limit=20&offset=0')).toBe(true);
    expect(h.resources.some(item => item.path === '/api/content/a/versions/1')).toBe(false);
    await click(button('查看内容'));
    expect(container.textContent).toContain('HISTORICAL_BODY');
    expect(h.resources.some(item => item.path === '/api/content/a/versions/2')).toBe(false);
  });
  it('keeps every long-body character reachable without splitting surrogate pairs at page boundaries', async () => {
    const text = 'a'.repeat(MARKDOWN_LIMIT - 1) + '😀' + 'b'.repeat(MARKDOWN_LIMIT) + 'LAST_SENTINEL';
    await render(<Markdown>{text}</Markdown>); let combined = '';
    for (;;) {
      const section = container.querySelector('pre').textContent;
      expect(section).not.toMatch(/^[\uDC00-\uDFFF]|[\uD800-\uDBFF]$/); combined += section;
      const next = button('下一页'); if (next.disabled) break; await click(next);
    }
    expect(combined).toBe(text); expect(combined.endsWith('LAST_SENTINEL')).toBe(true);
  });
  it('focuses the project name explicitly and creates a project without requiring a directory', async () => {
    h.mutate.mockResolvedValue({ id: 'new', name: 'New project' });
    const saved = vi.fn(); await render(<ProjectDialog {...props} mode="create" onSaved={saved} />);
    expect(document.activeElement).toBe(field('项目名称'));
    await change(field('项目名称'), 'New project'); await submit();
    expect(h.mutate).toHaveBeenCalledWith('/api/projects', 'POST', { name: 'New project', description: '', directories: [] });
    expect(saved).toHaveBeenCalledWith({ id: 'new', name: 'New project' });
  });
  it('keeps an edited draft and its original compare-and-save version across background revision ticks', async () => {
    h.data['/api/projects'] = props.projects;
    const item = { id: 'a', project_id: 'p', title: 'Original', content: 'Original body', version: 1 };
    await render(<ContentEditor item={item} onClose={props.onClose} onSaved={vi.fn()} />);
    expect(document.activeElement).toBe(field('标题')); await change(field('标题'), 'My draft'); await change(field('正文'), 'My retained body');
    await render(<ContentEditor item={{ ...item, title: 'External edit', version: 2 }} onClose={props.onClose} onSaved={vi.fn()} />);
    expect(field('标题').value).toBe('My draft'); expect(field('正文').value).toBe('My retained body');
    h.mutate.mockRejectedValue(new ApiError('Changed', 409)); await submit();
    expect(h.mutate).toHaveBeenCalledWith('/api/content/a', 'PATCH', expect.objectContaining({ expected_version: 1, title: 'My draft', content: 'My retained body' }));
    expect(field('标题').value).toBe('My draft'); expect(button('看看最新版本')).toBeTruthy();
  });
  it('does not present wholly missing observed token usage or unpriced cost as zero', async () => {
    const tz = new URLSearchParams({ tz: Intl.DateTimeFormat().resolvedOptions().timeZone });
    h.data[`/api/usage/summary?${tz}`] = { logical_requests: 1, attempts: 1, known_tokens: 0, unknown_usage_count: 1, unknown_cost_count: 1, cost_by_currency: {}, heatmap: [] };
    h.data['/api/usage/requests?limit=25&offset=0'] = { items: [
      { host_id: 'codex', actual_model: 'missing-model', usage: {} },
      { host_id: 'omp', actual_model: 'known-model', usage: { input_tokens: 0, output_tokens: 0 } },
    ] };
    await render(<UsagePage projects={props.projects} />);
    expect(Array.from(container.querySelectorAll('.metric-card strong')).map(node => node.textContent)).toEqual(['1', '未知', '未知']);
    const tables = container.querySelectorAll('table');
    expect(tables[0].textContent).toContain('codex');
    expect(tables[0].textContent).toContain('未知');
    expect(tables[0].textContent).toContain('omp');
    expect(tables[1].textContent).toContain('missing-model');
    expect(tables[1].textContent).toContain('未知');
    expect(tables[1].textContent).toContain('known-model');
  });
  it('restores navigation interactivity after mobile drawer close and resize to desktop', async () => {
    mobile = true; await render(<Shell lang="zh" page="projects" navigate={vi.fn()} projects={[]} status={null} managementState="ready"><input aria-label="work" /></Shell>);
    const sidebar = container.querySelector('aside'), main = container.querySelector('main');
    expect(sidebar.inert).toBe(true); await click(container.querySelector('[aria-label="打开导航"]'));
    expect(sidebar.inert).toBe(false); expect(main.inert).toBe(true);
    await act(async () => document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    expect(sidebar.inert).toBe(true); expect(main.inert).toBe(false);
    mobile = false; await act(async () => window.dispatchEvent(new Event('resize')));
    expect(sidebar.inert).toBe(false); expect(main.inert).toBe(false); expect(document.body.style.overflow).toBe('');
  });
});

describe('account operation lifecycle through actual controls', () => {
  it('aborts A polling and discards a late success after closing A and opening B', async () => {
    vi.useFakeTimers(); const changed = vi.fn(); h.mutate.mockResolvedValue({ status: 'authorizing' });
    await render(<AccountOperation key="A" action="authorize" item={{ id: 'A' }} agent="Cursor" onChanged={changed} onClose={() => root.render(null)} />); await click(button('确认操作'));
    let resolveA; h.request.mockReturnValue(new Promise(resolve => { resolveA = resolve; }));
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    const signal = h.request.mock.calls[0][1].signal;
    await click(button('关闭'));
    await render(<AccountOperation key="B" action="authorize" item={{ id: 'B' }} agent="OMP" onChanged={changed} onClose={vi.fn()} />);
    const before = changed.mock.calls.length;
    await act(async () => { resolveA({ id: 'A', status: 'authenticated' }); await Promise.resolve(); });
    expect(signal.aborted).toBe(true); expect(changed.mock.calls).toHaveLength(before);
    expect(container.textContent).toContain('B'); expect(container.textContent).not.toContain('认证已验证');
  });
  it('offers a real retry after the current account poll fails and accepts a matching later readback', async () => {
    vi.useFakeTimers(); h.mutate.mockResolvedValue({ status: 'authorizing' }); h.request.mockRejectedValueOnce(new ApiError('Network unavailable', 0)).mockResolvedValueOnce({ id: 'A', status: 'authenticated' });
    await render(<AccountOperation action="authorize" item={{ id: 'A' }} agent="Cursor" onChanged={vi.fn()} onClose={vi.fn()} />); await click(button('确认操作'));
    await act(async () => vi.advanceTimersByTimeAsync(2500)); await click(button('重新读取账号状态'));
    await act(async () => vi.advanceTimersByTimeAsync(2500));
    expect(h.request).toHaveBeenCalledTimes(2); expect(container.textContent).toContain('认证已验证');
  });
});

describe('object refresh boundaries', () => {
  it('refreshes matching object events with a conservative generic fallback', () => {
    const audit = (action, target) => ({ type: 'audit.created', data: { action, target } });
    expect(eventAffectsObject(audit('usage.create', 'x'), 'content', 'a')).toBe(false);
    expect(eventAffectsObject(audit('content.update', 'b'), 'content', 'a')).toBe(false);
    expect(eventAffectsObject(audit('content.restore', 'a@v2'), 'content', 'a')).toBe(true);
    expect(eventAffectsObject(audit('content.reassign', 'a->p'), 'content', 'a')).toBe(true);
    expect(eventAffectsObject({ type: 'changed' }, 'content', 'a')).toBe(true);
    expect(eventAffectsObject(audit('content.update', 123), 'content', 'a')).toBe(true);
  });
});

describe('consumer frontend regression tests for audit findings', () => {
  it('evaluates backendCompatibility according to contract', () => {
    const readyData = { capabilities: { mcp: true }, counts: { agents: 1 } };
    expect(backendCompatibility({ data: readyData, loading: false, error: null })).toBe('ready');
    expect(backendCompatibility({ data: readyData, error: { status: 404 } })).toBe('upgrade_required');
    expect(backendCompatibility({ data: readyData, error: { status: 0 } })).toBe('unavailable');
    expect(backendCompatibility({ data: null, loading: false, error: new Error('Network timeout') })).toBe('unavailable');
    expect(backendCompatibility({ data: null, loading: false, error: { status: 404 } })).toBe('upgrade_required');
  });

  it('keeps the primary navigation entries in main-nav and surfaces conflicts when pending', async () => {
    const navigate = vi.fn();
    await render(<Shell lang="zh" page="projects" navigate={navigate} projects={[]} status={null} managementState="ready"><div /></Shell>);
    const nav = container.querySelector('.main-nav');
    const entries = Array.from(nav.querySelectorAll('button'));
    expect(entries.map(item => item.textContent)).toEqual(['项目工作台', 'Agent 管理', '用量与性能']);
    for (const entry of entries) await click(entry);
    expect(navigate.mock.calls.map(call => call[0])).toEqual(['projects', 'agents', 'usage']);
    expect(nav.compareDocumentPosition(container.querySelector('.sidebar-projects')) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(container.querySelector('.secondary-nav')).toBeNull();

    // When pending_conflicts > 0, "待确认" appears in main-nav with count badge
    await render(<Shell lang="zh" page="projects" navigate={navigate} projects={[]} status={{ ok: true, counts: { pending_conflicts: 2 } }} managementState="ready"><div /></Shell>);
    const conflictEntries = Array.from(container.querySelector('.main-nav').querySelectorAll('button'));
    expect(conflictEntries.map(item => item.textContent)).toEqual(['项目工作台', 'Agent 管理', '用量与性能', '待确认2']);
  });

  it('keeps agents nav item disabled with honest indicator when management is not ready', async () => {
    const navigate = vi.fn();
    await render(<Shell lang="zh" page="projects" navigate={navigate} projects={[]} status={null} managementState="checking"><div /></Shell>);
    const navButtons = Array.from(container.querySelectorAll('.main-nav button'));
    const agentsButton = navButtons.find(btn => btn.textContent.includes('Agent 管理'));
    expect(agentsButton).toBeTruthy();
    expect(agentsButton.disabled).toBe(true);
    expect(agentsButton.textContent).toContain('待连接');
    await click(agentsButton);
    expect(navigate).not.toHaveBeenCalled();
  });

  it('paginates split selection beyond 50 items and preserves cross-page choices', async () => {
    const page1Items = Array.from({ length: 50 }, (_, i) => ({ id: `cnt_${i}`, title: `Item ${i}`, project_id: 'p' }));
    const page2Items = Array.from({ length: 20 }, (_, i) => ({ id: `cnt_${50 + i}`, title: `Item ${50 + i}`, project_id: 'p' }));
    h.data['/api/content/index?project_id=p&limit=50&offset=0'] = { total: 70, items: page1Items };
    h.data['/api/content/index?project_id=p&limit=50&offset=50'] = { total: 70, items: page2Items };
    h.data['/api/conversations/index?project_id=p&limit=50&offset=0'] = { total: 0, items: [] };

    const onSaved = vi.fn();
    await render(<ProjectDialog mode="split" project={{ id: 'p', name: 'Original Project' }} projects={[{ id: 'p', name: 'Original Project' }]} onClose={vi.fn()} onSaved={onSaved} />);
    expect(h.resources.every(item => !item.path || item.path.includes('/index'))).toBe(true);
    expect(h.resources.some(item => item.path.startsWith('/api/content/index'))).toBe(true);
    expect(h.resources.some(item => item.path.startsWith('/api/conversations/index'))).toBe(true);

    const firstCheck = container.querySelector('input[type="checkbox"]');
    await click(firstCheck);
    expect(container.textContent).toContain('已选 1');

    const nextBtn = button('下一页');
    expect(nextBtn).toBeTruthy();
    await click(nextBtn);

    const page2FirstCheck = container.querySelector('input[type="checkbox"]');
    await click(page2FirstCheck);
    expect(container.textContent).toContain('已选 2');

    expect(container.textContent).not.toContain('选中的记录有的被删了');

    await change(field('新项目名称'), 'Split Output');
    h.mutate.mockResolvedValue({ id: 'new_p', name: 'Split Output' });
    await submit();

    expect(h.mutate).toHaveBeenCalledWith('/api/projects/p/split', 'POST', expect.objectContaining({
      name: 'Split Output',
      content_ids: ['cnt_0', 'cnt_50'],
      conversation_ids: []
    }));
  });

  it('keeps split paging after a page request returns no items yet', async () => {
    h.data['/api/content/index?project_id=p&limit=50&offset=0'] = {
      total: 70,
      items: Array.from({ length: 50 }, (_, i) => ({ id: `cnt_${i}`, title: `Item ${i}`, project_id: 'p' })),
    };
    h.data['/api/conversations/index?project_id=p&limit=50&offset=0'] = { total: 0, items: [] };
    await render(<ProjectDialog mode="split" project={{ id: 'p', name: 'Original Project' }} projects={[{ id: 'p', name: 'Original Project' }]} onClose={vi.fn()} onSaved={vi.fn()} />);
    await click(container.querySelector('input[type="checkbox"]'));
    await click(button('下一页'));
    expect(button('上一页')).toBeTruthy();
    expect(container.textContent).toContain('已选 1');
    expect(container.textContent).not.toContain('选中的记录有的被删了');
  });

  it('pages split chats in English without requesting a full transcript list', async () => {
    setLang('en');
    const page1Items = Array.from({ length: 50 }, (_, i) => ({ id: `chat_${i}`, title: `Chat ${i}`, project_id: 'p' }));
    const page2Items = [{ id: 'chat_50', title: 'Chat 50', project_id: 'p' }];
    h.data['/api/content/index?project_id=p&limit=50&offset=0'] = { total: 0, items: [] };
    h.data['/api/conversations/index?project_id=p&limit=50&offset=0'] = { total: 51, items: page1Items };
    h.data['/api/conversations/index?project_id=p&limit=50&offset=50'] = { total: 51, items: page2Items };
    const onSaved = vi.fn();
    await render(<ProjectDialog mode="split" project={{ id: 'p', name: 'Original Project' }} projects={[{ id: 'p', name: 'Original Project' }]} onClose={vi.fn()} onSaved={onSaved} />);
    expect(h.resources.every(item => !item.path || item.path.includes('/index'))).toBe(true);
    await click(container.querySelector('input[type="checkbox"]'));
    expect(container.textContent).toContain('Chats moving · selected 1');
    await click(button('Next page'));
    await click(container.querySelector('input[type="checkbox"]'));
    await change(field('New project name'), 'Split Output');
    h.mutate.mockResolvedValue({ id: 'new_p', name: 'Split Output' });
    await submit();
    expect(h.mutate).toHaveBeenCalledWith('/api/projects/p/split', 'POST', expect.objectContaining({
      name: 'Split Output',
      content_ids: [],
      conversation_ids: ['chat_0', 'chat_50'],
    }));
  });

  it('surfaces localized validation error for empty or invalid import JSON without throwing raw SyntaxError', async () => {
    await render(<ImportDialog agent="Codex" category="mcp" onClose={vi.fn()} onChanged={vi.fn()} />);
    // Click "解析预览" with empty input
    await submit();
    expect(container.textContent).toContain('请先填写来源或选择文件。');

    // Enter invalid JSON
    const textarea = container.querySelector('textarea');
    if (textarea) {
      await change(textarea, '{ invalid');
      await submit();
      expect(container.textContent).toContain('JSON 格式不正确，请检查后再预览。');
    }
  });

  it('does not permanently lock out provider check when preflight fails with status 0', async () => {
    const provider = { id: 'prov_test', name: 'Test Provider', protocol: 'openai' };
    await render(<ProviderChecks provider={provider} contextTitle="Codex" />);
    await click(button('目录与独立检查'));

    h.mutate.mockRejectedValueOnce(new ApiError('CSRF token failed', 0));
    await click(button('确认执行此检查'));

    // Because failure status was 0 (client preflight), attempted is false, button is still enabled
    expect(container.textContent).not.toContain('本次检查已提交，不会自动重试');
    const checkBtn = button('确认执行此检查');
    expect(checkBtn.disabled).toBe(false);
  });

  it('fills an open connection dialog when discovery paths arrive, and does not confirm a stale empty list', async () => {
    const codex = { id: 'codex', label: 'Codex Desktop', enabled: false, paused: false, discovered: true, paths: [], capabilities: {}, authorizations: {} };
    const changed = vi.fn();
    h.data['/api/connections'] = [codex];
    await render(<ConsolePage agentId="codex" revision={0} onChanged={changed} />);
    await click(button('确认接入'));
    const area = () => container.querySelector('dialog[open] textarea');
    expect(area().value).toBe('');
    h.data['/api/connections'] = [{ ...codex, paths: ['/isolated/.codex/sessions'] }];
    await render(<ConsolePage agentId="codex" revision={1} onChanged={changed} />);
    expect(area().value).toBe('/isolated/.codex/sessions');
    await change(area(), 'user typed');
    h.data['/api/connections'] = [{ ...codex, paths: ['/other'] }];
    await render(<ConsolePage agentId="codex" revision={2} onChanged={changed} />);
    expect(area().value).toBe('user typed');

    h.loading['/api/connections'] = true;
    await render(<ConsolePage agentId="codex" revision={3} onChanged={changed} />);
    await click(container.querySelector('dialog[open] input[type=checkbox]'));
    expect(button('确认授权范围').disabled).toBe(true);
    h.loading['/api/connections'] = false;
    await render(<ConsolePage agentId="codex" revision={4} onChanged={changed} />);
    expect(button('确认授权范围').disabled).toBe(false);
  });

  it('navigates back to the management host via returnAgent in ConsolePage', async () => {
    const navigate = vi.fn();
    await render(<ConsolePage agentId="antigravity" returnAgent="antigravity-cli" navigate={navigate} revision={0} onChanged={vi.fn()} />);
    const backBtn = button('返回我的助手');
    expect(backBtn).toBeTruthy();
    await click(backBtn);
    expect(navigate).toHaveBeenCalledWith('agents', { agent: 'antigravity-cli', section: null });
  });

  it('supports tiered memory pinning, consolidation, AGENTS.md handbook preview, and conflict diff/merge', async () => {
    seedContent([
      { id: 'm1', title: 'Normal rule', version: 1, category: 'preference', project_id: 'p', pinned: false, tags: [] },
      { id: 'm2', title: 'Pinned core rule', version: 2, category: 'decision', project_id: 'p', pinned: true, tags: ['pinned'] },
    ]);
    h.data['/api/content/handbook?project_id=p&include_global=true'] = {
      project_id: 'p',
      project_name: 'Project',
      markdown: '# AGENTS.md / CLAUDE.md — Project\n\n## 📌 常驻核心规则\n- **Pinned core rule**',
      counts: { total: 2, pinned: 1, global: 0, project: 2 },
    };
    h.mutate.mockImplementation((path) => {
      if (path.endsWith('/pin')) return Promise.resolve({ id: 'm1', pinned: true, version: 2 });
      if (path.endsWith('/consolidate')) return Promise.resolve({ ok: true, merged_groups: 1, archived_count: 2 });
      if (path.endsWith('/resolve')) return Promise.resolve({ id: 'c1', status: 'resolved', resolution: 'merge' });
      return Promise.resolve({});
    });

    await render(<KnowledgePage {...props} />);
    // Pinned item sorted first with .content-row-pinned
    const rows = container.querySelectorAll('.content-row');
    expect(rows.length).toBe(2);
    expect(rows[0].classList.contains('content-row-pinned')).toBe(true);
    expect(rows[0].textContent).toContain('Pinned core rule');

    // Click inline pin button on second row
    await click(rows[1].querySelector('.pin-toggle-btn'));
    expect(h.mutate).toHaveBeenCalledWith('/api/content/m1/pin', 'POST', { pinned: true, expected_version: 1 });

    // Click consolidate button
    await click(button('去重整理'));
    expect(h.mutate).toHaveBeenCalledWith('/api/content/consolidate', 'POST', { project_id: 'p', scope: 'project' });

    // Switch to AGENTS.md handbook view
    await click(button('AGENTS.md 规则手册'));
    expect(container.querySelector('.handbook-panel')).toBeTruthy();
    expect(container.textContent).toContain('AGENTS.md / CLAUDE.md — Project');

    // Conflict Inbox diff toggle & merge resolution
    h.data['/api/conflicts'] = [
      {
        id: 'c1',
        project_id: 'p',
        existing_id: 'm1',
        status: 'pending',
        reason: 'Updated constraint',
        created_at: '2026-10-28T08:00:00Z',
        proposed: { title: 'Merged Rule Title', content: 'Line 1\nLine 2 new', category: 'preference' },
      },
    ];
    h.data['/api/conflicts/c1'] = h.data['/api/conflicts'][0];
    h.data['/api/content/m1'] = { id: 'm1', title: 'Old Title', content: 'Line 1\nLine 2 old', version: 3 };

    await render(<InboxPage revision={0} onChanged={vi.fn()} />);
    await click(button('差异对比 (Diff)'));
    expect(container.querySelector('.conflict-diff-box')).toBeTruthy();
    expect(container.querySelector('.diff-added')).toBeTruthy();
    expect(container.querySelector('.diff-removed')).toBeTruthy();

    await click(button('融合编辑后采纳'));
    await change(field('融合后的标题'), 'Final Merged Title');
    await change(field('融合后的正文'), 'Line 1\nLine 2 combined');
    await click(button('确认并保存'));
    expect(h.mutate).toHaveBeenCalledWith('/api/conflicts/c1/resolve', 'POST', {
      resolution: 'merge',
      expected_version: 3,
      merged_title: 'Final Merged Title',
      merged_content: 'Line 1\nLine 2 combined',
    });
  });
});

it('provider presets suggest a model without selecting it or replacing an explicit choice', async () => {
  const onSave = vi.fn();
  await render(<ProviderSetup agent="Codex" agentId="codex" presets={[
    { id: 'one', name: 'Synthetic One', protocol: 'openai', base_url: 'https://one.invalid/v1', default_model: 'suggested-one' },
    { id: 'two', name: 'Synthetic Two', protocol: 'openai', base_url: 'https://two.invalid/v1', default_model: 'suggested-two' }
  ]} onSave={onSave} onClose={vi.fn()} />);
  await click(button('Synthetic One'));
  expect(field('默认请求模型').value).toBe('');
  expect(field('默认请求模型').placeholder).toBe('suggested-one');
  expect(button('保存连接与模型').disabled).toBe(true);
  expect(onSave).not.toHaveBeenCalled();
  await change(field('默认请求模型'), 'explicit-choice');
  await change(field('选择服务商'), 'two');
  expect(field('默认请求模型').value).toBe('explicit-choice');
  await submit();
  expect(onSave).toHaveBeenCalledWith(expect.objectContaining({ model_id: 'explicit-choice' }));
});

it.each(['unreachable', 'unknown'])('does not infer reachability from latency when the check status is %s', async status => {
  h.mutate.mockResolvedValue({ status, latency_ms: 42 });
  await render(<ProviderChecks provider={{ id: 'fixture', name: 'Synthetic', protocol: 'openai' }} />);
  await click(button('一键测速'));
  const label = container.querySelector('.cc-latency-pill').textContent;
  expect(label).not.toContain('可达');
  expect(label).not.toContain('42 ms');
  expect(label).toBe(status === 'unreachable' ? '测速失败' : '未知');
  expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers/fixture/check', 'POST', { mode: 'network' });
});

it('shows the backend network measurement without claiming authentication or inference', async () => {
  h.mutate.mockResolvedValue({ status: 'reachable', network_elapsed_ms: 42, authentication: 'unknown', inference_performed: false });
  await render(<ProviderChecks provider={{ id: 'fixture', name: 'Synthetic', protocol: 'openai' }} />);
  await click(button('一键测速'));
  expect(container.querySelector('.cc-latency-pill').textContent).toBe('42 ms · 可达');
  expect(container.textContent).not.toContain('认证已验证');
  expect(h.mutate).toHaveBeenCalledExactlyOnceWith('/api/manage/providers/fixture/check', 'POST', { mode: 'network' });
});

it('cancelling file selection does not start an import', async () => {
  await render(<KnowledgePage {...props} kind="document" />);
  const input = container.querySelector('input[type=file]');
  expect(input).toBeTruthy();
  await act(async () => input.dispatchEvent(new Event('change', { bubbles: true })));
  expect(h.request).not.toHaveBeenCalled();
  expect(h.mutate).not.toHaveBeenCalled();
});

it.each(['调整标签', '调整分类', '合并', '删除'])('cancelling %s never submits a batch mutation', async action => {
  seedContent([{ id: 'one', title: 'One', version: 1, kind: 'memory', project_id: 'p', category: 'knowledge' }, { id: 'two', title: 'Two', version: 1, kind: 'memory', project_id: 'p', category: 'knowledge' }]);
  h.request.mockImplementation(async path => ({ id: path.includes('one') ? 'one' : 'two', version: 1, content: 'Synthetic batch body' }));
  await render(<KnowledgePage {...props} />);
  const checks = container.querySelectorAll('input[type=checkbox]');
  expect(checks.length).toBeGreaterThanOrEqual(2);
  await click(checks[0]); await click(checks[1]);
  await click(button(action));
  expect(container.querySelector('dialog')).toBeTruthy();
  await click(button('取消', container.querySelector('dialog')));
  expect(container.querySelector('dialog')).toBeNull();
  expect(h.mutate).not.toHaveBeenCalled();
  expect(h.request.mock.calls.some(([, options]) => options?.method && options.method !== 'GET')).toBe(false);
});

it('keeps Shell main-nav free of removed retrieval/audit entries, keeps ProjectsPage menu free of web handoff button, and lets enabled connections authorize newly discovered paths', async () => {
  const onNavigate = vi.fn();
  await render(<Shell page="projects" managementState="ready" projects={[{ id: 'p', name: 'Project' }]} selectedProject="p" onProject={vi.fn()} navigate={onNavigate} status={{ ok: true, counts: {} }}><div>Body</div></Shell>);
  const mainNav = container.querySelector('.main-nav');
  expect(button('检索与上下文', mainNav)).toBeUndefined();
  expect(button('变更记录', mainNav)).toBeUndefined();

  h.data['/api/projects/p/summary'] = { id: 'p', name: 'Project', counts: { memories: 1, documents: 0, conversations: 1, conflicts: 0 }, directories: [] };
  await render(<ProjectsPage projects={[{ id: 'p', name: 'Project', status: 'active' }]} selectedId="p" onSelect={vi.fn()} revision={0} onChanged={vi.fn()} />);
  await click(container.querySelector('[aria-label="项目操作"]'));
  expect(button('准备接棒卡')).toBeUndefined();
  expect(button('修改名称与目录')).toBeTruthy();

  const enabledCodex = {
    id: 'codex',
    label: 'Codex Desktop',
    enabled: true,
    paused: false,
    discovered: true,
    paths: ['/isolated/.codex/sessions'],
    discovery: { suggested_paths: ['/isolated/.codex/sessions', '/isolated/.codex/archived_sessions'] },
    capabilities: {},
    authorizations: {},
  };
  h.data['/api/connections'] = [enabledCodex];
  h.mutate.mockResolvedValueOnce({ ...enabledCodex, paths: ['/isolated/.codex/sessions', '/isolated/.codex/archived_sessions'] });
  await render(<ConsolePage agentId="codex" revision={0} onChanged={vi.fn()} />);
  await click(button('授权读取的记录位置'));
  const openModal = container.querySelector('dialog[open]');
  const area = openModal.querySelector('textarea');
  // Enabled connection preserves configured paths only and surfaces unadded discovered paths as one-click buttons
  expect(area.value).toBe('/isolated/.codex/sessions');
  await click(button('+ 添加建议路径：/isolated/.codex/archived_sessions', openModal));
  expect(area.value).toBe('/isolated/.codex/sessions\n/isolated/.codex/archived_sessions');
  await click(openModal.querySelector('input[type=checkbox]'));
  await click(button('确认授权范围', openModal));
  expect(h.mutate).toHaveBeenCalledWith('/api/connections/codex', 'PATCH', {
    enabled: true,
    paths: ['/isolated/.codex/sessions', '/isolated/.codex/archived_sessions'],
    confirmed: true,
  });
});

it('guards enabled connections against saving empty path lists and avoids error-wall loop when management is unavailable', async () => {
  const enabledCodex = {
    id: 'codex',
    label: 'Codex Desktop',
    enabled: true,
    paused: false,
    discovered: true,
    paths: ['/isolated/.codex/sessions'],
    discovery: {
      suggested_paths: ['/isolated/.codex/sessions'],
      evidence: [{ kind: 'history_root', path: '/isolated/.codex/symlink_sessions', status: 'symlink_requires_authorization' }],
    },
    capabilities: {},
    authorizations: {},
  };
  h.data['/api/connections'] = [enabledCodex];
  const navigate = vi.fn();
  await render(<ConsolePage agentId="codex" navigate={navigate} managementAvailable={false} revision={0} onChanged={vi.fn()} />);

  // When managementAvailable is false, header points to projects instead of agents
  expect(button('返回我的助手')).toBeUndefined();
  await click(button('返回项目工作台'));
  expect(navigate).toHaveBeenCalledWith('projects');

  // Symlink candidate button is offered and clearing textarea disables save with guidance
  await click(button('授权读取的记录位置'));
  const modal = container.querySelector('dialog[open]');
  expect(button('+ 添加建议路径：/isolated/.codex/symlink_sessions（符号链接需确认）', modal)).toBeTruthy();
  await click(modal.querySelector('input[type=checkbox]'));
  await change(modal.querySelector('textarea'), '   ');
  expect(modal.textContent).toContain('至少保留一个授权目录，或先停止接入');
  expect(button('停止接入', modal)).toBeTruthy();
  expect(button('确认授权范围', modal).disabled).toBe(true);
});

it('keeps ComputeSettings removed while supporting database backup and embedded audit log in SettingsPage', async () => {
  h.data['/api/settings'] = { autostart: false, compute_authorized: false, compute_host: null };
  h.data['/api/audit?limit=100'] = [];
  const onChanged = vi.fn();
  await render(<SettingsPage revision={0} status={{ ok: true, service: { running: true } }} onChanged={onChanged} navigate={vi.fn()} />);

  expect(container.textContent).not.toContain('用模型整理记忆');

  // Database backup action calls POST /api/settings/backup and displays path + integrity check
  h.mutate.mockResolvedValueOnce({
    ok: true,
    backup_path: '/Users/local/.contextcore/backups/contextcore-20261111.db',
    integrity: 'ok',
  });
  await click(button('立即备份数据库'));
  expect(h.mutate).toHaveBeenCalledWith('/api/settings/backup', 'POST', {});
  expect(container.textContent).toContain('/Users/local/.contextcore/backups/contextcore-20261111.db');
  expect(container.textContent).toContain('完整性校验已通过（quick_check: ok）');

  await click(button('查看变更记录'));
  expect(container.querySelector('.embedded-audit')).toBeTruthy();
});

it('keeps Shell aria-current aligned, deduplicates UsagePage host options, and hides cc-switch in ImportDialog preview', async () => {
  // 1. Shell aria-current matches active class when viewing connections while managementState is ready
  await render(<Shell lang="zh" page="connections" navigate={vi.fn()} projects={[]} status={null} managementState="ready"><div /></Shell>);
  const agentsNavBtn = Array.from(container.querySelectorAll('.main-nav button')).find(btn => btn.textContent.includes('Agent 管理'));
  expect(agentsNavBtn.classList.contains('active')).toBe(true);
  expect(agentsNavBtn.getAttribute('aria-current')).toBe('page');

  // 2. UsagePage deduplicates host filter options sharing the same registry_host_id
  h.data['/api/manage/hosts'] = [
    { id: 'codex', name: 'Codex CLI', registry_host_id: 'codex' },
    { id: 'codex-desktop', name: 'Codex Desktop', registry_host_id: 'codex' },
    { id: 'antigravity', name: 'Antigravity', registry_host_id: 'antigravity' },
    { id: 'antigravity-cli', name: 'Antigravity CLI', registry_host_id: 'antigravity' },
    { id: 'claude-code', name: 'Claude Code', registry_host_id: 'claude_code' },
  ];
  h.data['/api/usage/summary?tz=UTC'] = { logical_requests: 0, known_tokens: 0, unknown_usage_count: 0, unknown_cost_count: 0, attempts: 0 };
  await render(<UsagePage projects={[]} />);
  const hostSelect = field('助手');
  const optionValues = Array.from(hostSelect.querySelectorAll('option')).map(opt => opt.value);
  expect(new Set(optionValues).size).toBe(optionValues.length);
  expect(optionValues).toEqual(['', 'codex', 'antigravity', 'claude_code']);
  expect(hostSelect.textContent).toContain('Codex CLI / Codex Desktop');

  // 3. ImportDialog keeps wire format="cc-switch" for backend while sanitizing visible DOM preview
  await render(<ImportDialog agent="Codex" category="providers" onClose={vi.fn()} onChanged={vi.fn()} />);
  await change(field('来源类型'), 'cc-switch');
  await change(container.querySelector('textarea'), JSON.stringify({ mcpServers: { demo: { command: 'npx' } } }));
  await submit();
  expect(container.textContent).toContain('将保存到共享资料库');
  expect(container.textContent).not.toContain('cc-switch');
  h.mutate.mockResolvedValueOnce({ status: 'configured', imported: [{ id: 'mcp_1', name: 'demo', kind: 'mcp', version: 1 }] });
  await submit();
  expect(h.mutate).toHaveBeenCalledWith('/api/manage/assets/import', 'POST', expect.objectContaining({
    format: 'cc-switch',
  }));
});

