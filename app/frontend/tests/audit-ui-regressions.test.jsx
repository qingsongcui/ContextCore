// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, useState } from 'react';
import { createRoot } from 'react-dom/client';

const apiMocks = vi.hoisted(() => ({ request: vi.fn(), mutate: vi.fn() }));
vi.mock('../src/lib/api.js', async original => ({
  ...await original(),
  request: apiMocks.request,
  mutate: apiMocks.mutate,
}));

import { AgentPlanDialog } from '../src/components/agent-management/AgentPlanDialog.jsx';
import { RetrievalSettings } from '../src/components/RetrievalSettings.jsx';
import { errorFeedback } from '../src/lib/api.js';
import { setLang } from '../src/lib/i18n.jsx';
import { useResource } from '../src/lib/useResource.js';
import { KnowledgePage } from '../src/pages/KnowledgePage.jsx';
import { ProjectsPage } from '../src/pages/ProjectsPage.jsx';

let root;
let container;

const buttonByText = (text, scope = document.body) =>
  Array.from(scope.querySelectorAll('button')).find(node => node.textContent.trim() === text);

async function flush() {
  await act(async () => {
    await new Promise(resolve => setTimeout(resolve, 0));
  });
}

async function renderNode(node) {
  await act(async () => {
    root.render(node);
  });
  await flush();
}

async function click(node) {
  expect(node).toBeTruthy();
  await act(async () => {
    node.click();
  });
  await flush();
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  setLang('zh');
  apiMocks.request.mockReset();
  apiMocks.mutate.mockReset();
  apiMocks.request.mockResolvedValue({ items: [], total: 0 });
  apiMocks.mutate.mockResolvedValue({});
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
  window.matchMedia = () => ({ matches: true, addEventListener() {}, removeEventListener() {} });
  HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
  HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => {
    root.unmount();
  });
  container.remove();
});

describe('Audit UI regressions (Items 5.1 - 5.6)', () => {
  it('5.1 AgentPlanDialog preserves preview mode when status remains planned and offers reload on unknown', async () => {
    const plan = {
      id: 'plan_1',
      version: 1,
      status: 'planned',
      payload: { metadata: { host_id: 'codex' }, files: [{ path: '/tmp/config.toml', diff: [] }] },
    };

    await renderNode(
      <AgentPlanDialog
        plan={plan}
        definition="Test Provider"
        result={null}
        hostReadback={null}
        agent="Codex"
        category="模型与账号"
        busy={false}
        error={new Error('409 version conflict')}
        valid={true}
        onApply={vi.fn()}
        onReloadResult={vi.fn()}
        onClose={vi.fn()}
      />
    );
    expect(document.body.textContent).toContain('检查本次更改');
    expect(document.body.textContent).not.toContain('配置应用结果');
    expect(buttonByText('确认应用到 Codex')).toBeTruthy();

    const reloadSpy = vi.fn();
    await renderNode(
      <AgentPlanDialog
        plan={plan}
        definition="Test Provider"
        result={{ id: 'plan_1', status: 'unknown', note: '应用结果未知，请重新读取事务状态；不要重复提交。' }}
        hostReadback={null}
        agent="Codex"
        category="模型与账号"
        busy={false}
        error={null}
        valid={true}
        onApply={vi.fn()}
        onReloadResult={reloadSpy}
        onClose={vi.fn()}
      />
    );
    expect(document.body.textContent).toContain('应用结果未知，请重新读取事务状态；不要重复提交。');
    expect(buttonByText('确认应用到 Codex')).toBeUndefined();
    const reloadBtn = buttonByText('重新读取结果');
    expect(reloadBtn).toBeTruthy();
    await click(reloadBtn);
    expect(reloadSpy).toHaveBeenCalledTimes(1);
  });

  it('5.2 KnowledgePage keeps handbook bound to projectId even after switching list scope to all', async () => {
    apiMocks.request.mockImplementation(path => {
      if (path.startsWith('/api/content/handbook')) {
        return Promise.resolve({ markdown: '# Handbook Markdown', counts: { pinned: 1, global: 2, project: 3 } });
      }
      return Promise.resolve({ items: [], total: 0 });
    });

    await renderNode(
      <KnowledgePage
        projectId="proj_1"
        projects={[{ id: 'proj_1', name: 'Demo' }]}
        kind="memory"
        revision={0}
        onChanged={vi.fn()}
        onProject={vi.fn()}
      />
    );

    const scopeSelect = container.querySelector('select[aria-label="搜索范围"]');
    expect(scopeSelect).toBeTruthy();
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(scopeSelect, 'all');
      scopeSelect.dispatchEvent(new Event('change', { bubbles: true }));
    });
    await flush();

    await click(buttonByText('AGENTS.md 规则手册', container));
    const handbookCalls = apiMocks.request.mock.calls.map(c => c[0]).filter(p => p.startsWith('/api/content/handbook'));
    expect(handbookCalls[handbookCalls.length - 1]).toBe('/api/content/handbook?project_id=proj_1&include_global=true');
    expect(container.querySelector('.handbook-panel')?.textContent).toContain('自动汇总当前项目的常驻核心规则');

    apiMocks.request.mockClear();
    await renderNode(
      <KnowledgePage
        projectId=""
        projects={[]}
        kind="memory"
        revision={0}
        onChanged={vi.fn()}
        onProject={vi.fn()}
      />
    );
    await click(buttonByText('AGENTS.md 规则手册', container));
    const globalCalls = apiMocks.request.mock.calls.map(c => c[0]).filter(p => p.startsWith('/api/content/handbook'));
    expect(globalCalls[globalCalls.length - 1]).toBe('/api/content/handbook?include_global=true');
    expect(container.querySelector('.handbook-panel')?.textContent).toContain('自动汇总全局常驻核心规则与使用习惯');
  });

  it('5.3 & 5.4 useResource clears stale data on error, ProjectsPage hides stale cards on error, and breadcrumb resets globalTab', async () => {
    let rev = 0;
    apiMocks.request.mockResolvedValueOnce({ marker: 'fresh-1' });

    function Harness({ tick }) {
      const res = useResource('/api/sample', tick);
      return <pre data-testid="res">{JSON.stringify({ data: res.data, err: Boolean(res.error) })}</pre>;
    }

    await renderNode(<Harness tick={rev} />);
    expect(container.textContent).toContain('"marker":"fresh-1"');

    apiMocks.request.mockRejectedValueOnce(new Error('network down'));
    await renderNode(<Harness tick={1} />);
    expect(container.textContent).toContain('"data":null');
    expect(container.textContent).toContain('"err":true');

    // ProjectsPage must not show stale cards when error is set
    await renderNode(
      <ProjectsPage
        projects={[{ id: 'p1', name: 'AlphaProj', status: 'active', directories: ['/repo'] }]}
        selectedId={null}
        onSelect={vi.fn()}
        revision={0}
        onChanged={vi.fn()}
        navigate={vi.fn()}
        loading={false}
        error={new Error('load failed')}
      />
    );
    expect(container.querySelector('.project-card')).toBeNull();
    expect(container.textContent).not.toContain('AlphaProj');
    expect(buttonByText('重新读取项目', container)).toBeTruthy();

    // Selecting a project from global view clears globalTab so clicking "所有项目" returns to project grid
    apiMocks.request.mockImplementation(path => {
      if (path.includes('project_id=__unassigned__') && path.includes('limit=1')) {
        return Promise.resolve({ total: 1, items: [] });
      }
      if (path.startsWith('/api/conversations/index')) {
        return Promise.resolve({
          total: 1,
          items: [{ id: 'conv_1', title: 'Global Chat 1', agent: 'cursor', project_id: 'p1', updated_at: '2026-11-16T00:00:00Z' }],
        });
      }
      if (path.startsWith('/api/conversations/conv_1/messages')) {
        return Promise.resolve({ items: [], total: 0 });
      }
      if (path.startsWith('/api/conversations/conv_1')) {
        return Promise.resolve({ id: 'conv_1', title: 'Global Chat 1', agent: 'cursor', project_id: 'p1', updated_at: '2026-11-16T00:00:00Z', processing_status: 'processed' });
      }
      return Promise.resolve({ items: [], total: 0 });
    });

    function Wrapper() {
      const [selectedId, setSelectedId] = useState(null);
      return (
        <ProjectsPage
          projects={[{ id: 'p1', name: 'AlphaProj', status: 'active', directories: ['/repo'], counts: { conversations: 1, memories: 0, documents: 0, tasks: 0 } }]}
          selectedId={selectedId}
          onSelect={setSelectedId}
          revision={0}
          onChanged={vi.fn()}
          navigate={vi.fn()}
          loading={false}
          error={null}
        />
      );
    }

    await renderNode(<Wrapper />);
    await click(buttonByText('查看未归属聊天', container));
    expect(container.textContent).toContain('Global Chat 1');
    const convRow = container.querySelector('button.conversation-row');
    await click(convRow);
    await click(buttonByText('打开所属项目', container));
    expect(container.querySelector('.project-room h1')?.textContent).toBe('AlphaProj');
    await click(buttonByText('所有项目', container));
    expect(container.querySelector('.project-card')?.textContent).toContain('AlphaProj');
    expect(buttonByText('返回所有项目', container)).toBeUndefined();
  });

  it('5.5 RetrievalSettings renders reload button on unknown prepare failure and recovers without closing modal', async () => {
    apiMocks.request.mockImplementation(path => {
      if (path === '/api/retrieval/status') {
        return Promise.resolve({
          status: 'ready',
          fts5: true,
          indexed_count: 1,
          active_count: 1,
          settings: { lexical_enabled: true, semantic_enabled: false, relation_enabled: false, max_results: 15, context_budget_tokens: 2000 },
          semantic: { status: 'disabled', enabled: false, runtime_installed: false },
          relations: { enabled: false, count: 0 },
        });
      }
      if (path === '/api/retrieval/semantic/runtime/plan') {
        return Promise.resolve({ plan_id: 'plan-rt-1', retryable: true });
      }
      return Promise.resolve({});
    });
    apiMocks.mutate.mockRejectedValueOnce(Object.assign(new Error('500 error'), { status: 500 }));

    await renderNode(<RetrievalSettings revision={0} />);
    await click(buttonByText('准备隔离运行依赖', container));
    const confirmBtn = buttonByText('确认准备独立运行环境');
    expect(confirmBtn.disabled).toBe(false);
    await click(confirmBtn);

    expect(buttonByText('重新读取结果')).toBeTruthy();
    expect(buttonByText('确认准备独立运行环境').disabled).toBe(true);

    await click(buttonByText('重新读取结果'));
    expect(buttonByText('重新读取结果')).toBeUndefined();
    expect(buttonByText('确认准备独立运行环境').disabled).toBe(false);
  });

  it('5.6 errorFeedback includes safe validation field names while stripping private paths and inputs', () => {
    const msg422 = errorFeedback(422, [
      { loc: ['body', 'provider_id'], msg: 'Field required', input: '/Users/secret/PRIVATE_SENTINEL' },
    ]);
    expect(msg422).toContain('provider_id');
    expect(msg422).not.toContain('PRIVATE_SENTINEL');
    expect(msg422).not.toContain('/Users/secret');

    const msgTs = errorFeedback(422, 'started_at: Timestamps must be ISO8601 with timezone');
    expect(msgTs).toContain('started_at');
    expect(msgTs).toContain('时间戳必须带时区');

    const msgTok = errorFeedback(422, 'input_tokens: Token counts must be nonnegative integers or null');
    expect(msgTok).toContain('input_tokens');
    expect(msgTok).toContain('token 计数必须是非负整数');
  });
});
