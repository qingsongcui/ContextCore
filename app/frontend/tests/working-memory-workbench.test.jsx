// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act } from 'react';
import { createRoot } from 'react-dom/client';

const h = vi.hoisted(() => ({
  resources: [],
  data: {},
  errors: {},
  loading: {},
  request: vi.fn(),
  mutate: vi.fn(),
  reload: vi.fn()
}));

vi.mock('../src/lib/useResource.js', async original => ({
  ...(await original()),
  useResource(path, revision) {
    h.resources.push({ path, revision });
    let data = h.data[path] ?? null;
    if (data === null && path?.startsWith('/api/working-tasks')) {
      const url = new URL(path, 'http://synthetic.test');
      const projectId = url.searchParams.get('project_id') || '';
      const rows = h.data[`tasks:${projectId}`]
        ?? h.data[`/api/working-tasks?project_id=${projectId}&active_only=true`] ?? [];
      const historical = task => ['paused', 'completed', 'cancelled'].includes(task.status);
      if (url.pathname.endsWith('/summary')) {
        data = { active: rows.filter(task => !historical(task)).length, history: rows.filter(historical).length, all: rows.length };
      } else if (url.pathname.endsWith('/position')) {
        const id = url.pathname.split('/').at(-2);
        const target = rows.find(task => task.id === id);
        if (target) {
          const group = historical(target) ? 'history' : 'active';
          const position = rows.filter(task => historical(task) === historical(target)).findIndex(task => task.id === id);
          const limit = Number(url.searchParams.get('limit') || 50);
          data = { task_id: id, group, offset: Math.floor(position / limit) * limit, limit };
        }
      } else if (url.pathname === '/api/working-tasks') {
        const filtered = url.searchParams.get('active_only') === 'true'
          ? rows.filter(task => !historical(task))
          : url.searchParams.get('status') === 'history' ? rows.filter(historical) : rows;
        const offset = Number(url.searchParams.get('offset') || 0);
        data = filtered.slice(offset, offset + Number(url.searchParams.get('limit') || 100));
      }
    }
    return { data, loading: Boolean(h.loading[path]), error: h.errors[path] || null, reload: h.reload };
  }
}));

vi.mock('../src/lib/api.js', async original => ({
  ...(await original()),
  request: h.request,
  mutate: h.mutate
}));

import { KnowledgePage, ContentDetail } from '../src/pages/KnowledgePage.jsx';
import { ProjectsPage } from '../src/pages/ProjectsPage.jsx';
import { Shell } from '../src/components/Shell.jsx';
import { WorkingTasksPanel } from '../src/components/WorkingTasksPanel.jsx';
import { setLang } from '../src/lib/i18n.jsx';
import { ApiError } from '../src/lib/api.js';

let root;
let container;

const projects = [{ id: 'p', name: 'ContextCore', status: 'active', directories: ['/repo/ContextCore'], counts: { memories: 3, tasks: 2, documents: 1, conversations: 4 } }];
const baseProps = {
  projectId: 'p',
  projects,
  kind: 'memory',
  revision: 0,
  onChanged: vi.fn(),
  onProject: vi.fn(),
  onBack: vi.fn()
};

const contentIndexPath = '/api/content/index?status=active&exclude_category=task&limit=50&offset=0&kind=memory&project_id=p&pinned_first=true';
const activeTasksSummaryPath = '/api/working-tasks?project_id=p&active_only=true';
const panelTasksPath = 'tasks:p';

const buttonByText = (text, scope = container) =>
  Array.from(scope.querySelectorAll('button')).find(node => node.textContent.trim() === text || node.textContent.replace(/\s+/g, ' ').trim() === text);

const buttonContaining = (text, scope = container) =>
  Array.from(scope.querySelectorAll('button')).find(node => node.textContent.includes(text));

const fieldControl = (labelText, scope = container) =>
  Array.from(scope.querySelectorAll('label.field')).find(node => node.querySelector(':scope > span')?.textContent === labelText)?.querySelector('input,textarea,select');

async function render(node) {
  await act(async () => root.render(node));
}

async function click(node) {
  expect(node).toBeTruthy();
  await act(async () => node.click());
}

async function change(node, value) {
  expect(node).toBeTruthy();
  await act(async () => {
    const proto = node.tagName === 'SELECT'
      ? HTMLSelectElement.prototype
      : node.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, 'value').set.call(node, value);
    node.dispatchEvent(new Event(node.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  });
}

async function submit(form = container.querySelector('form')) {
  expect(form).toBeTruthy();
  await act(async () => form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })));
}

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  h.data = {};
  h.errors = {};
  h.loading = {};
  h.resources = [];
  h.request.mockReset();
  h.mutate.mockReset();
  h.reload.mockReset();
  baseProps.onChanged.mockReset();
  baseProps.onProject.mockReset();
  baseProps.onBack.mockReset();
  setLang('zh');

  vi.stubGlobal('matchMedia', vi.fn(() => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {}
  })));
  vi.stubGlobal('ResizeObserver', class {
    observe() {}
    disconnect() {}
  });
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function close() {
    this.removeAttribute('open');
  };

  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
  document.body.style.overflow = '';
  vi.unstubAllGlobals();
});

describe('Memory Workbench — Short-Term Working Tasks vs Governed Long-Term Memory', () => {
  it('keeps Shell top-level navigation untouched and splits Project Memory cleanly into Active Tasks and Long-Term Memory', async () => {
    await render(
      <Shell page="projects" managementState="ready" projects={projects} selectedProject="p" onProject={vi.fn()} navigate={vi.fn()} status={{ ok: true, counts: {} }}>
        <div>Workspace</div>
      </Shell>
    );
    const navButtons = Array.from(container.querySelectorAll('.main-nav button')).map(btn => btn.textContent.trim());
    expect(navButtons).toEqual(['项目工作台', 'Agent 管理', '用量与性能']);

    h.data[activeTasksSummaryPath] = [
      { id: 'tsk_1', project_id: 'p', title: 'Task 1', status: 'in_progress', execution_mode: 'approved' },
      { id: 'tsk_2', project_id: 'p', title: 'Task 2', status: 'blocked', execution_mode: 'discussion_only', must_not_execute: true }
    ];
    h.data[contentIndexPath] = {
      total: 4,
      items: [
        { id: 'mem_1', project_id: 'p', kind: 'memory', category: 'preference', status: 'active', title: 'Prefer concise diffs', content_preview: 'Keep PRs small', version: 1 },
        { id: 'mem_2', project_id: 'p', kind: 'memory', category: 'decision', status: 'active', title: 'SQLite WAL mode', content_preview: 'Use WAL', version: 1 },
        { id: 'mem_legacy_task', project_id: 'p', kind: 'memory', category: 'task', status: 'active', title: 'Legacy task row', content_preview: 'Should not pollute long-term list', version: 1 },
        { id: 'mem_outdated', project_id: 'p', kind: 'memory', category: 'lesson', status: 'outdated', title: 'Old workaround', content_preview: 'Retired', version: 2 }
      ]
    };

    await render(<KnowledgePage {...baseProps} />);

    // Top 2-view segmented switch with counts
    const workbenchSwitch = container.querySelector('.memory-workbench-switch');
    expect(workbenchSwitch).toBeTruthy();
    expect(workbenchSwitch.textContent).toContain('当前任务');
    expect(workbenchSwitch.textContent).toContain('2');
    expect(workbenchSwitch.textContent).toContain('长期记忆');

    // Category filter pills in Long-Term Memory exclude 'task'
    const categoryBar = container.querySelector('[aria-label="记忆分类快筛"]');
    expect(categoryBar.textContent).toContain('偏好');
    expect(categoryBar.textContent).toContain('背景');
    expect(categoryBar.textContent).toContain('决定');
    expect(categoryBar.textContent).toContain('经验');
    expect(categoryBar.textContent).not.toContain('任务进度');
    expect(categoryBar.textContent).not.toContain('任务与进度');

    // Active Long-Term Memory list excludes legacy category === 'task' and status === 'outdated'
    const rowTitles = Array.from(container.querySelectorAll('.content-row h3')).map(el => el.textContent);
    expect(rowTitles).toEqual(['Prefer concise diffs', 'SQLite WAL mode']);
  });

  it('renders 6 structured working-memory slots, discussion_only execution gate badge in zh/en, branch/agent chips, history toggle, and timeline', async () => {
    h.data[activeTasksSummaryPath] = [{ id: 'tsk_plan', title: 'Design schema migration' }];
    h.data[panelTasksPath] = [
      {
        id: 'tsk_plan',
        project_id: 'p',
        title: '重构 SQLite 状态约束迁移方案',
        status: 'in_progress',
        execution_mode: 'discussion_only',
        must_not_execute: true,
        last_agent: 'claude-code',
        git_branch: 'test/relay-worktree-identity',
        version: 3,
        updated_at: '2026-11-12T02:00:00Z',
        goal: '在不丢失触发器和索引的前提下扩展 contents.status 枚举',
        current_state: '正在讨论方案，尚未授权改代码',
        completed_steps: [
          { step: '盘点现有触发器清单', evidence: 'database.py:519-549', agent: 'claude-code' }
        ],
        temp_constraints: ['这次先别改界面', '不要跑全量压测'],
        failed_attempts: [
          { attempt: '直接 ALTER TABLE 修改 CHECK', reason: 'SQLite 不支持直接修改内联 CHECK 约束', agent: 'codex' }
        ],
        blockers: ['等待确认是否需要离线备份钩子'],
        next_steps: ['编写幂等重建表迁移函数', '补充触发器存活断言测试'],
        events: [
          { id: 'ev_1', version: 2, actor: 'codex', summary: '补充失败尝试记录', created_at: '2026-11-12T01:40:00Z' },
          { id: 'ev_2', version: 3, actor: 'claude-code', summary: '锁定仅讨论方案状态', created_at: '2026-11-12T02:00:00Z' }
        ]
      },
      {
        id: 'tsk_done',
        project_id: 'p',
        title: '历史已完成任务：清理旧日志',
        status: 'completed',
        execution_mode: 'approved',
        must_not_execute: false,
        last_agent: 'antigravity',
        git_branch: 'main',
        version: 4,
        goal: '归档过期调试输出',
        current_state: '已验证合并'
      }
    ];

    await render(<KnowledgePage {...baseProps} initialSection="tasks" />);

    // Active task shown prominently; completed task hidden under history toggle by default
    expect(container.textContent).toContain('重构 SQLite 状态约束迁移方案');
    expect(container.textContent).not.toContain('历史已完成任务：清理旧日志');

    const card = container.querySelector('.working-task-card');
    expect(card.classList.contains('is-discussion-only')).toBe(true);
    expect(card.textContent).toContain('仅讨论方案 · 未授权改代码');
    expect(card.textContent).toContain('Claude Code');
    expect(card.textContent).toContain('test/relay-worktree-identity');

    // Verify all 6 structured slots render human-readably
    expect(card.textContent).toContain('目标');
    expect(card.textContent).toContain('在不丢失触发器和索引的前提下扩展 contents.status 枚举');
    expect(card.textContent).toContain('当前状态');
    expect(card.textContent).toContain('正在讨论方案，尚未授权改代码');
    expect(card.textContent).toContain('已完成与依据');
    expect(card.textContent).toContain('盘点现有触发器清单');
    expect(card.textContent).toContain('依据：database.py:519-549');
    expect(card.textContent).toContain('本次临时限制');
    expect(card.textContent).toContain('仅限当前任务，不升级为长期规则');
    expect(card.textContent).toContain('这次先别改界面');
    expect(card.textContent).toContain('失败尝试与阻塞');
    expect(card.textContent).toContain('阻塞：等待确认是否需要离线备份钩子');
    expect(card.textContent).toContain('直接 ALTER TABLE 修改 CHECK');
    expect(card.textContent).toContain('原因：SQLite 不支持直接修改内联 CHECK 约束');
    expect(card.textContent).toContain('下一步');
    expect(card.textContent).toContain('编写幂等重建表迁移函数');

    // Expand cross-agent timeline
    const details = card.querySelector('.task-events-details');
    details.open = true;
    await act(async () => details.dispatchEvent(new Event('toggle')));
    expect(card.textContent).toContain('补充失败尝试记录');
    expect(card.textContent).toContain('锁定仅讨论方案状态');

    // Verify English badge copy as well
    setLang('en');
    await render(<KnowledgePage {...baseProps} initialSection="tasks" revision={1} />);
    expect(container.querySelector('.badge-guard-discussion').textContent).toContain('Discussion only · Do not execute');
    setLang('zh');

    // Switch to Paused & Completed History filter
    await render(<KnowledgePage {...baseProps} initialSection="tasks" revision={2} />);
    await click(buttonContaining('暂停与已完成历史'));
    expect(container.textContent).toContain('历史已完成任务：清理旧日志');
    expect(container.textContent).toContain('已退出日常注入');
  });

  it('supports switching task status, toggling execution_mode, and promoting a task lesson with required applicability conditions', async () => {
    h.data[panelTasksPath] = [
      {
        id: 'tsk_promote',
        project_id: 'p',
        source_conversation_id: 'synthetic-source',
        title: '修复 SQLite CHECK 迁移丢触发器问题',
        status: 'in_progress',
        execution_mode: 'discussion_only',
        must_not_execute: true,
        last_agent: 'codex',
        git_branch: 'fix/sqlite-check',
        version: 2,
        goal: '安全扩展 status 字段枚举',
        current_state: '已完成本地验证',
        completed_steps: [{ step: '表重建后恢复全部触发器', evidence: 'pytest tests/test_db.py passed', agent: 'codex' }],
        failed_attempts: [{ attempt: '省略触发器重建', reason: '检索索引未同步更新' }]
      }
    ];
    h.mutate.mockImplementation((path, method, body) => {
      if (path.endsWith('/promote')) {
        return Promise.resolve({
          task: { id: 'tsk_promote', version: 3 },
          memory: { id: 'mem_promoted_1', title: body.title, category: body.category }
        });
      }
      return Promise.resolve({ id: 'tsk_promote', ...body, version: 3 });
    });

    await render(<KnowledgePage {...baseProps} initialSection="tasks" />);

    // 1. Toggle execution mode from discussion_only -> approved
    await click(buttonByText('切换为允许执行'));
    expect(h.mutate).toHaveBeenCalledWith('/api/working-tasks/tsk_promote', 'PATCH', {
      execution_mode: 'approved',
      expected_version: 2
    });

    // 2. Switch status to paused
    const statusSelect = container.querySelector('select[aria-label="切换任务状态：修复 SQLite CHECK 迁移丢触发器问题"]');
    await change(statusSelect, 'paused');
    expect(h.mutate).toHaveBeenCalledWith('/api/working-tasks/tsk_promote', 'PATCH', {
      status: 'paused',
      expected_version: 2
    });
    expect(container.textContent).toContain('已自动退出日常上下文注入');

    // 3. Open Promote to Long-Term Lesson modal — submit must be disabled until applicability is provided
    await click(buttonByText('提炼为长期经验'));
    const dialog = container.querySelector('dialog');
    expect(dialog).toBeTruthy();

    const submitBtn = buttonByText('确认提炼为长期经验', dialog);
    expect(submitBtn.disabled).toBe(true);

    // Fill applicability conditions and submit
    await change(fieldControl('触发条件 / 适用场景', dialog), 'SQLite 表包含内联 CHECK 约束且需要新增枚举状态时');
    await change(fieldControl('适用环境', dialog), 'SQLite 3 / Python 3.12');
    await change(fieldControl('关联路径（逗号分隔）', dialog), 'app/backend/app/database.py, app/backend/app/services/retrieval_service.py');
    await change(fieldControl('验证依据摘录', dialog), 'pytest tests/test_db.py passed');
    expect(submitBtn.disabled).toBe(false);

    await submit(dialog.querySelector('form'));
    expect(h.mutate).toHaveBeenCalledWith('/api/working-tasks/tsk_promote/promote', 'POST', {
      title: '修复 SQLite CHECK 迁移丢触发器问题',
      content: expect.stringContaining('安全扩展 status 字段枚举'),
      category: 'lesson',
      applicability: {
        when: 'SQLite 表包含内联 CHECK 约束且需要新增枚举状态时',
        env: 'SQLite 3 / Python 3.12',
        paths: ['app/backend/app/database.py', 'app/backend/app/services/retrieval_service.py']
      },
      evidence_quote: 'pytest tests/test_db.py passed',
      actor: 'manual'
    });
    expect(container.textContent).toContain('查看该长期经验');
  });

  it('displays honest 3-state usage badges (saved / provided / cited), applicability tags, provenance quotes, and lifecycle filters', async () => {
    h.data[contentIndexPath] = {
      total: 3,
      items: [
        {
          id: 'm_saved',
          project_id: 'p',
          kind: 'memory',
          category: 'preference',
          status: 'active',
          title: '代码块统一使用显式语言标记',
          content_preview: 'Markdown fenced code blocks must declare language.',
          version: 1,
          usage_summary: { state: 'saved', provided_count: 0, cited_count: 0 }
        },
        {
          id: 'm_provided',
          project_id: 'p',
          kind: 'memory',
          category: 'background',
          status: 'active',
          title: '本地端口固定绑定 127.0.0.1',
          content_preview: 'Never expose local daemon to 0.0.0.0.',
          version: 1,
          usage_summary: { state: 'provided', provided_count: 4, cited_count: 0, last_provided_agent: 'cursor' }
        },
        {
          id: 'm_cited',
          project_id: 'p',
          kind: 'memory',
          category: 'lesson',
          status: 'active',
          title: 'SQLite 表重建须同步恢复 FTS 触发器',
          content_preview: 'Recreate content_identity and retrieval triggers.',
          version: 2,
          promoted_from_task_id: 'tsk_promote',
          applicability: {
            when: '重建 contents 表结构时',
            env: 'SQLite 3',
            version_range: '>=1.0.0',
            paths: ['app/backend/app/database.py']
          },
          sources: [{ id: 'conv_1', kind: 'conversation', title: 'Schema Audit', agent: 'claude-code', quote: '重建表后必须恢复三个触发器' }],
          usage_summary: {
            state: 'cited',
            provided_count: 5,
            cited_count: 2,
            last_cited_agent: 'claude-code',
            last_cited_note: '编写迁移脚本时按此规则补齐了检索触发器'
          }
        }
      ]
    };

    h.data['/api/content/m_cited?include_versions=false'] = {
      ...h.data[contentIndexPath].items[2],
      content: '表重建迁移执行完毕后，务必在同一事务内重新创建触发器。',
      origin_authority: 'verified_outcome',
      usage_summary: {
        state: 'cited',
        provided_count: 5,
        cited_count: 2,
        last_cited_agent: 'claude-code',
        last_cited_note: '编写迁移脚本时按此规则补齐了检索触发器',
        recent_events: [
          { id: 'ue_1', event_type: 'cited', agent: 'claude-code', note: '迁移脚本已核验触发器', created_at: '2026-11-12T02:10:00Z' }
        ]
      }
    };

    await render(<KnowledgePage {...baseProps} />);

    const rows = container.querySelectorAll('.content-row');
    expect(rows.length).toBe(3);

    // 1. Honest 3-state badges on cards
    expect(rows[0].textContent).toContain('已保存');
    expect(rows[1].textContent).toContain('已提供给 Agent · 4 次');
    expect(rows[2].textContent).toContain('Agent 已明确引用 · 2 次');
    expect(rows[2].textContent).toContain('最近引用：Claude Code');

    // 2. Applicability tags & provenance on card
    expect(rows[2].textContent).toContain('适用条件：重建 contents 表结构时');
    expect(rows[2].textContent).toContain('环境：SQLite 3');
    expect(rows[2].textContent).toContain('版本：>=1.0.0');
    expect(rows[2].textContent).toContain('路径：app/backend/app/database.py');
    expect(rows[2].textContent).toContain('源自任务提炼');
    expect(rows[2].textContent).toContain('来源依据：重建表后必须恢复三个触发器');

    // 3. Open detail view and verify citation note, recent events, source quote, and origin authority
    await click(rows[2].querySelector('.content-row-main'));
    expect(container.querySelector('.content-detail')).toBeTruthy();
    expect(container.textContent).toContain('任务验证提炼');
    expect(container.textContent).toContain('源自工作任务提炼：tsk_promote');
    expect(container.textContent).toContain('编写迁移脚本时按此规则补齐了检索触发器');
    expect(container.textContent).toContain('迁移脚本已核验触发器');
    expect(container.textContent).toContain('重建表后必须恢复三个触发器');

    // 4. Jump back to list and verify server-side status + exclude_category=task params across status pills
    await click(buttonByText('返回列表'));
    expect(h.resources[0].path).toBe('/api/content/index?status=active&exclude_category=task&limit=50&offset=0&kind=memory&project_id=p&pinned_first=true');

    const outdatedPath = '/api/content/index?status=outdated&exclude_category=task&limit=50&offset=0&kind=memory&project_id=p&pinned_first=true';
    const historicalPath = '/api/content/index?status=historical&exclude_category=task&limit=50&offset=0&kind=memory&project_id=p&pinned_first=true';
    const allStatusPath = '/api/content/index?status=all&exclude_category=task&limit=50&offset=0&kind=memory&project_id=p&pinned_first=true';
    h.data[outdatedPath] = {
      total: 1,
      items: [
        { id: 'm_out', project_id: 'p', kind: 'memory', category: 'decision', status: 'outdated', title: '已退役的旧端口约定', content_preview: '8080 port', version: 1 }
      ]
    };
    h.data[historicalPath] = {
      total: 1,
      items: [
        { id: 'm_hist', project_id: 'p', kind: 'memory', category: 'decision', status: 'historical', title: '已被替代的旧配置', content_preview: 'old config', version: 1 }
      ]
    };
    h.data[allStatusPath] = {
      total: 5,
      items: [
        { id: 'm_out', project_id: 'p', kind: 'memory', category: 'decision', status: 'outdated', title: '已退役的旧端口约定', content_preview: '8080 port', version: 1 },
        { id: 'm_hist', project_id: 'p', kind: 'memory', category: 'decision', status: 'historical', title: '已被替代的旧配置', content_preview: 'old config', version: 1 }
      ]
    };

    const statusBar = () => container.querySelector('[aria-label="记忆有效状态筛选"]');
    await click(buttonByText('已过时', statusBar()));
    expect(container.textContent).toContain('已退役的旧端口约定');
    expect(h.resources.some(r => r.path === outdatedPath)).toBe(true);

    await click(buttonByText('已被替代历史', statusBar()));
    expect(container.textContent).toContain('已被替代的旧配置');
    expect(h.resources.some(r => r.path === historicalPath)).toBe(true);

    await click(buttonByText('全部状态', statusBar()));
    expect(container.textContent).toContain('已退役的旧端口约定');
    expect(container.textContent).toContain('已被替代的旧配置');
    expect(h.resources.some(r => r.path === allStatusPath)).toBe(true);

    const beforeActiveClickLen = h.resources.length;
    await click(buttonByText('当前有效', statusBar()));
    expect(h.resources.slice(beforeActiveClickLen).some(r => r.path === contentIndexPath)).toBe(true);
  });

  it('executes 1-click mark outdated, supersede via CorrectContentModal, and superseded lineage navigation', async () => {
    h.data[contentIndexPath] = {
      total: 1,
      items: [
        {
          id: 'm_old_rule',
          project_id: 'p',
          kind: 'memory',
          category: 'decision',
          status: 'active',
          title: '默认测试端口为 3000',
          content: '本地前端调试统一使用 3000 端口。',
          content_preview: '本地前端调试统一使用 3000 端口。',
          version: 3,
          applicability: { env: 'local-dev' }
        }
      ]
    };

    h.mutate.mockImplementation((path, method, body) => {
      if (path.endsWith('/outdated')) {
        return Promise.resolve({ id: 'm_old_rule', status: 'outdated', version: 4 });
      }
      if (path.endsWith('/correct')) {
        return Promise.resolve({ id: 'm_new_rule', title: body.title, content: body.content, status: 'active', version: 1 });
      }
      return Promise.resolve({});
    });

    await render(<KnowledgePage {...baseProps} />);

    // 1. Mark outdated directly from row
    await click(buttonByText('标记过时'));
    expect(h.mutate).toHaveBeenCalledWith('/api/content/m_old_rule/outdated', 'POST', {
      expected_version: 3
    });
    expect(container.textContent).toContain('标记为过时');

    // 2. Correct / Supersede via modal
    await click(buttonByText('纠正说法'));
    const dialog = container.querySelector('dialog');
    expect(dialog).toBeTruthy();
    await change(fieldControl('更新后的标题', dialog), '默认测试端口改为 5173');
    await change(fieldControl('更新后的正确说法', dialog), '迁移到 Vite 7 后，本地前端调试统一使用 5173 端口。');
    await change(fieldControl('版本范围', dialog), 'Vite >=7.0');
    await submit(dialog.querySelector('form'));

    expect(h.mutate).toHaveBeenCalledWith('/api/content/m_old_rule/correct', 'POST', {
      title: '默认测试端口改为 5173',
      content: '迁移到 Vite 7 后，本地前端调试统一使用 5173 端口。',
      category: 'decision',
      applicability: {
        env: 'local-dev',
        version_range: 'Vite >=7.0'
      },
      expected_version: 3
    });
    expect(container.textContent).toContain('原条目已转为历史');

    // 3. Verify superseded historical detail links straight to the superseding item
    h.data['/api/content/m_old_rule?include_versions=false'] = {
      id: 'm_old_rule',
      project_id: 'p',
      kind: 'memory',
      category: 'decision',
      status: 'historical',
      superseded_by_id: 'm_new_rule',
      title: '默认测试端口为 3000',
      content: '旧规则正文',
      version: 4
    };
    h.data['/api/content/m_new_rule?include_versions=false'] = {
      id: 'm_new_rule',
      project_id: 'p',
      kind: 'memory',
      category: 'decision',
      status: 'active',
      title: '默认测试端口改为 5173',
      content: '新规则正文',
      version: 1
    };

    await render(<ContentDetail {...baseProps} id="m_old_rule" />);
    expect(container.textContent).toContain('这条旧说法已被更新版本替代。');
    await click(buttonByText('查看替代后的新说法'));
    expect(container.textContent).toContain('默认测试端口改为 5173');
  });

  it('connects Project Overview "当前工作" to /api/working-tasks?active_only=true and deep-links to Memory -> Active Tasks', async () => {
    h.data[activeTasksSummaryPath] = [
      {
        id: 'tsk_ov_1',
        project_id: 'p',
        title: '梳理跨宿主 MCP 自动引导指令',
        status: 'in_progress',
        execution_mode: 'discussion_only',
        must_not_execute: true,
        last_agent: 'antigravity',
        git_branch: 'feat/mcp-bootstrap',
        current_state: '正在核对 6 个宿主的启动指令长度上限',
        updated_at: '2026-11-12T02:05:00Z'
      }
    ];
    h.data[panelTasksPath] = h.data[activeTasksSummaryPath];
    h.data['/api/content/index?project_id=p&status=active&category=decision&limit=4&offset=0'] = { total: 0, items: [] };
    h.data['/api/conversations/index?project_id=p&limit=4'] = { total: 0, items: [] };
    h.data[contentIndexPath] = { total: 0, items: [] };

    await render(
      <ProjectsPage
        projects={projects}
        selectedId="p"
        onSelect={vi.fn()}
        revision={0}
        onChanged={vi.fn()}
        navigate={vi.fn()}
      />
    );

    expect(h.resources.some(r => r.path === activeTasksSummaryPath)).toBe(true);
    expect(container.textContent).toContain('梳理跨宿主 MCP 自动引导指令');
    expect(container.textContent).toContain('仅讨论方案 · 未授权改代码');
    expect(container.textContent).toContain('feat/mcp-bootstrap');

    // Clicking the task card on Overview deep-links directly into 记忆 -> 当前任务
    await click(buttonContaining('梳理跨宿主 MCP 自动引导指令'));
    expect(container.querySelector('.working-tasks-panel')).toBeTruthy();
    expect(container.querySelector('.working-task-card.is-highlighted')).toBeTruthy();
  });

  it('supports creating/editing/deleting working tasks, editing applicability in ContentEditor, and Overview empty fallback', async () => {
    // 1. Empty fallback on Overview
    h.data[activeTasksSummaryPath] = [];
    h.data['/api/content/index?project_id=p&status=active&category=decision&limit=4&offset=0'] = { total: 0, items: [] };
    h.data['/api/conversations/index?project_id=p&limit=4'] = { total: 0, items: [] };
    await render(
      <ProjectsPage projects={projects} selectedId="p" onSelect={vi.fn()} revision={0} onChanged={vi.fn()} navigate={vi.fn()} />
    );
    expect(container.textContent).toContain('还没有任务进度记录。');

    // 2. Create working task via WorkingTaskModal
    h.data[panelTasksPath] = [];
    h.mutate.mockResolvedValue({ id: 'tsk_created', title: '核验墓碑语义防复活', status: 'in_progress' });
    await render(<KnowledgePage {...baseProps} initialSection="tasks" />);
    await click(buttonContaining('新建任务'));
    const createDialog = container.querySelector('dialog');
    expect(createDialog).toBeTruthy();
    await change(fieldControl('任务标题', createDialog), '核验墓碑语义防复活');
    await change(fieldControl('执行授权', createDialog), 'discussion_only');
    await change(fieldControl('所在分支', createDialog), 'feat/tombstone-canonical');
    await change(fieldControl('目标', createDialog), '确保删除的规则不会被蒸馏重新写回');
    await change(fieldControl('本次临时限制', createDialog), '这次先别改历史快照表');
    await change(fieldControl('下一步', createDialog), '运行集成测试验证');
    await submit(createDialog.querySelector('form'));

    expect(h.mutate).toHaveBeenCalledWith('/api/working-tasks', 'POST', expect.objectContaining({
      project_id: 'p',
      title: '核验墓碑语义防复活',
      execution_mode: 'discussion_only',
      git_branch: 'feat/tombstone-canonical',
      goal: '确保删除的规则不会被蒸馏重新写回',
      temp_constraints: ['这次先别改历史快照表'],
      next_steps: ['运行集成测试验证']
    }));

    // 3. Delete working task confirmation
    h.data[panelTasksPath] = [{ id: 'tsk_del', project_id: 'p', title: '待清理临时任务', status: 'in_progress', version: 1 }];
    await render(<KnowledgePage {...baseProps} initialSection="tasks" revision={2} />);
    await click(container.querySelector('[aria-label="删除任务：待清理临时任务"]'));
    await click(buttonByText('确认删除任务', container.querySelector('dialog')));
    expect(h.mutate).toHaveBeenCalledWith('/api/working-tasks/tsk_del?expected_version=1', 'DELETE');

    // 4. Single-row delete & ContentEditor applicability creation in Long-Term Memory
    h.data['/api/projects'] = projects;
    h.data[contentIndexPath] = {
      total: 1,
      items: [{ id: 'm_del', project_id: 'p', kind: 'memory', category: 'lesson', status: 'active', title: '待删除经验', version: 5 }]
    };
    await render(<KnowledgePage {...baseProps} initialSection="long_term" revision={3} />);
    await click(container.querySelector('[aria-label="删除记忆：待删除经验"]'));
    await click(buttonByText('永久删除', container.querySelector('dialog')));
    expect(h.mutate).toHaveBeenCalledWith('/api/content/m_del?expected_version=5', 'DELETE');

    h.mutate.mockResolvedValue({ id: 'm_created_app', title: '带条件的新经验' });
    await click(buttonByText('添加记忆'));
    const memDialog = container.querySelector('dialog');
    await change(fieldControl('标题', memDialog), '带条件的新经验');
    await change(fieldControl('触发条件 / 适用场景', memDialog), '启用混合检索时');
    await change(fieldControl('正文', memDialog), '不得将读取次数加入排序得分。');
    await submit(memDialog.querySelector('form'));
    expect(h.mutate).toHaveBeenCalledWith('/api/content', 'POST', expect.objectContaining({
      title: '带条件的新经验',
      kind: 'memory',
      applicability: { when: '启用混合检索时' }
    }));
  });

  it('displays project latest activity & directory on project cards and links unassigned local conversations directly into filtered chat view', async () => {
    const projectCards = [
      {
        id: 'proj_alpha',
        name: 'Project Alpha',
        status: 'active',
        description: '',
        directories: ['/workspace/project-alpha'],
        updated_at: '2026-11-12T05:30:00Z',
        counts: { memories: 18, tasks: 2, documents: 0, conversations: 205 }
      },
      {
        id: 'proj_beta',
        name: 'Project Beta',
        status: 'active',
        description: '',
        directories: ['/workspace/project-beta'],
        updated_at: '2026-11-11T09:20:00Z',
        counts: { memories: 5, tasks: 0, documents: 1, conversations: 166 }
      }
    ];
    h.data['/api/conversations/index?project_id=__unassigned__&limit=1'] = { total: 42, items: [{ id: 'c_loose', title: '随手提问' }] };
    h.data['/api/conversations/index?project_id=__unassigned__&limit=50&offset=0'] = {
      total: 42,
      items: [
        { id: 'c_loose', agent: 'antigravity', title: '解释一下 WAL 模式', updated_at: '2026-11-12T03:01:00Z', processing_status: 'processed', project_id: null }
      ]
    };

    await render(
      <ProjectsPage
        projects={projectCards}
        selectedId={null}
        onSelect={vi.fn()}
        revision={0}
        onChanged={vi.fn()}
        navigate={vi.fn()}
      />
    );

    expect(container.textContent).toContain('Project Alpha');
    expect(container.textContent).toContain('/workspace/project-alpha');
    expect(container.textContent).toContain('最近活动：');
    expect(container.textContent).toContain('另有 42 条未绑定具体项目目录的本地聊天记录');

    await click(buttonContaining('查看未归属聊天'));
    expect(h.resources.some(r => r.path === '/api/conversations/index?project_id=__unassigned__&limit=50&offset=0')).toBe(true);
    expect(container.textContent).toContain('解释一下 WAL 模式');
  });

  it('keeps completed history accessible independently from a paginated active list', async () => {
    const activeSplitPath = '/api/working-tasks?project_id=p&active_only=true&limit=50&offset=0';
    const historySplitPath = '/api/working-tasks?project_id=p&status=history&limit=50&offset=0';
    const summarySplitPath = '/api/working-tasks/summary?project_id=p';

    const manyActiveTasks = Array.from({ length: 105 }, (_, idx) => ({
      id: `tsk_active_${idx + 1}`,
      project_id: 'p',
      title: `活跃任务 #${idx + 1}`,
      status: 'in_progress',
      execution_mode: 'approved',
      version: 1
    }));
    const completedHistoryTask = {
      id: 'tsk_history_1',
      project_id: 'p',
      title: '已完成的历史归档任务：修复索引漂移',
      status: 'completed',
      execution_mode: 'approved',
      last_agent: 'claude-code',
      version: 4,
      goal: '确保 100+ 活跃任务不淹没已完成记录',
      current_state: '已归档验收'
    };

    h.data[activeTasksSummaryPath] = manyActiveTasks;
    h.data[panelTasksPath] = [...manyActiveTasks, completedHistoryTask];
    h.data[summarySplitPath] = { active: 105, history: 1, all: 106 };

    await render(<KnowledgePage {...baseProps} initialSection="tasks" />);


    const tabBar = container.querySelector('[aria-label="任务状态筛选"]');
    const activeTab = buttonContaining('进行中与阻塞', tabBar);
    const historyTab = buttonContaining('暂停与已完成历史', tabBar);
    const allTab = buttonContaining('全部任务', tabBar);

    expect(activeTab.querySelector('.pill-count').textContent).toBe('105');
    expect(historyTab.querySelector('.pill-count').textContent).toBe('1');
    expect(allTab.querySelector('.pill-count').textContent).toBe('106');

    await click(historyTab);
    expect(h.resources.some(r => r.path === activeSplitPath)).toBe(true);
    expect(h.resources.some(r => r.path === historySplitPath)).toBe(true);
    expect(container.textContent).toContain('已完成的历史归档任务：修复索引漂移');
    expect(container.textContent).not.toContain('还没有暂停或已完成的任务历史');

    // Summary badge totals also honor exact DB counts above the 200-row page cap
    h.data[summarySplitPath] = { active: 235, history: 12, all: 247 };
    await render(<KnowledgePage {...baseProps} initialSection="tasks" revision={1} />);
    const refreshedBar = container.querySelector('[aria-label="任务状态筛选"]');
    expect(buttonContaining('进行中与阻塞', refreshedBar).querySelector('.pill-count').textContent).toBe('235');
    expect(buttonContaining('暂停与已完成历史', refreshedBar).querySelector('.pill-count').textContent).toBe('12');
    expect(buttonContaining('全部任务', refreshedBar).querySelector('.pill-count').textContent).toBe('247');
  });

  it('stamps agent: "user" on manually added steps/attempts instead of inheriting last_agent and reports OPEN_CONFLICT honestly on promote', async () => {
    const activeSplitPath = '/api/working-tasks?project_id=p&active_only=true&limit=50&offset=0';
    const historySplitPath = '/api/working-tasks?project_id=p&status=history&limit=50&offset=0';

    const taskFixture = {
      id: 'tsk_truth',
      project_id: 'p',
      title: '核对手动步骤归属与冲突提示',
      source_conversation_id: 'synthetic-source',
      status: 'in_progress',
      execution_mode: 'approved',
      last_agent: 'codex',
      git_branch: 'fix/provenance-truth',
      version: 2,
      goal: '区分人工录入与 Agent 自动写入',
      current_state: '验证中',
      completed_steps: [
        { step: '自动扫描回归配置', evidence: 'pytest -q', agent: 'codex' }
      ],
      failed_attempts: [
        { attempt: '省略字段来源', reason: '被误标成 Codex', agent: 'codex' }
      ]
    };
    h.data[activeSplitPath] = [taskFixture];
    h.data[historySplitPath] = [];

    h.mutate.mockImplementation((path, method, body) => {
      if (path.endsWith('/promote')) {
        return Promise.resolve({
          task: { ...taskFixture, version: 3 },
          memory: null,
          action: 'OPEN_CONFLICT',
          conflict: {
            id: 'conf_promote_1',
            existing_id: 'mem_existing_1',
            proposed: { title: body.title, content: body.content }
          }
        });
      }
      return Promise.resolve({ ...taskFixture, ...body, version: 3 });
    });

    await render(<KnowledgePage {...baseProps} initialSection="tasks" />);

    // 1. Edit task and add manual completed step + manual failed attempt while last_agent is codex
    await click(buttonByText('编辑任务'));
    const editDialog = container.querySelector('dialog');
    expect(editDialog).toBeTruthy();

    await click(buttonByText('添加步骤', editDialog));
    await change(editDialog.querySelector('[aria-label="已完成步骤 2"]'), '人工核验备份清单');
    await change(editDialog.querySelector('[aria-label="完成依据 2"]'), '手工走查通过');

    await click(buttonByText('添加失败记录', editDialog));
    await change(editDialog.querySelector('[aria-label="失败尝试 2"]'), '直接改写本地缓存');
    await change(editDialog.querySelector('[aria-label="失败原因 2"]'), '重启后被覆盖');

    await submit(editDialog.querySelector('form'));
    expect(h.mutate).toHaveBeenCalledWith('/api/working-tasks/tsk_truth', 'PATCH', expect.objectContaining({
      last_agent: 'codex',
      expected_version: 2,
      completed_steps: [
        { step: '自动扫描回归配置', evidence: 'pytest -q', agent: 'codex' },
        { step: '人工核验备份清单', evidence: '手工走查通过', agent: 'user' }
      ],
      failed_attempts: [
        { attempt: '省略字段来源', reason: '被误标成 Codex', agent: 'codex' },
        { attempt: '直接改写本地缓存', reason: '重启后被覆盖', agent: 'user' }
      ]
    }));

    // Verify card rendering of agent: 'user' shows human label rather than Codex or raw 'user'
    h.data[activeSplitPath] = [{
      ...taskFixture,
      completed_steps: [
        { step: '人工核验备份清单', evidence: '手工走查通过', agent: 'user' }
      ],
      failed_attempts: [
        { attempt: '直接改写本地缓存', reason: '重启后被覆盖', agent: 'user' }
      ]
    }];
    await render(<KnowledgePage {...baseProps} initialSection="tasks" revision={1} />);
    const stepSub = container.querySelector('.slot-step-sub');
    expect(stepSub.textContent).toContain('人工维护');
    expect(stepSub.textContent).not.toContain('Codex');
    expect(stepSub.textContent).not.toContain('user');

    // 2. Promote task when backend returns action === 'OPEN_CONFLICT'
    await click(buttonByText('提炼为长期经验'));
    const promoteDialog = container.querySelector('dialog');
    expect(promoteDialog).toBeTruthy();
    await change(fieldControl('触发条件 / 适用场景', promoteDialog), '手动补充任务完成步骤时');
    await change(fieldControl('验证依据摘录', promoteDialog), 'Original source evidence');
    await submit(promoteDialog.querySelector('form'));

    const noticeNode = container.querySelector('.operation-notice');
    expect(noticeNode).toBeTruthy();
    expect(noticeNode.textContent).toContain('与现有记忆存在分歧');
    expect(noticeNode.textContent).toContain('待确认');
    expect(noticeNode.textContent).toContain('尚未直接写入长期记忆');
    expect(noticeNode.textContent).not.toContain('已提炼为长期经验');
    expect(buttonByText('查看该长期经验')).toBeUndefined();

    // Also verify English copy for OPEN_CONFLICT notice
    setLang('en');
    await render(<KnowledgePage {...baseProps} initialSection="tasks" revision={2} />);
    expect(container.querySelector('.slot-step-sub').textContent).toContain('Added manually');
    await click(buttonByText('Promote to long-term lesson'));
    const promoteEnDialog = container.querySelector('dialog');
    await change(fieldControl('Trigger / When applicable', promoteEnDialog), 'When manually recording task steps');
    await change(fieldControl('Verification evidence quote', promoteEnDialog), 'Original source evidence');
    await submit(promoteEnDialog.querySelector('form'));
    expect(container.querySelector('.operation-notice').textContent).toContain('conflicts with an existing memory');
    expect(container.querySelector('.operation-notice').textContent).not.toContain('Promoted "');
    setLang('zh');
  });
});

describe('Working task pagination beyond the old cap', () => {
  const rows = (prefix, count, status) => Array.from({ length: count }, (_, index) => ({
    id: `${prefix}-${index + 1}`, project_id: 'p',
    title: `${prefix} item ${index + 1}`, status, execution_mode: 'approved', version: 1
  }));

  it('reaches history item 201, navigates backwards and resets the page on group/project changes', async () => {
    h.data[panelTasksPath] = [...rows('Active', 205, 'active'), ...rows('History', 201, 'completed')];
    await render(<WorkingTasksPanel {...baseProps} />);
    expect(container.textContent).toContain('Active item 1');
    expect(container.textContent).not.toContain('History item 201');
    await click(buttonContaining('暂停与已完成历史', container.querySelector('[role="tablist"]')));
    for (let i = 0; i < 4; i += 1) await click(buttonByText('下一页'));
    expect(container.textContent).toContain('History item 201');
    expect(buttonByText('下一页').disabled).toBe(true);
    await click(buttonByText('上一页'));
    expect(container.textContent).toContain('History item 151');
    expect(container.textContent).not.toContain('History item 201');
    await click(buttonContaining('全部任务', container.querySelector('[role="tablist"]')));
    expect(container.textContent).toContain('Active item 1');
    expect(buttonByText('上一页').disabled).toBe(true);
    await click(buttonByText('下一页'));
    h.data['tasks:q'] = [{ id: 'q-task', project_id: 'q', title: 'Other project only', status: 'active' }];
    await render(<WorkingTasksPanel {...baseProps} projectId="q" />);
    expect(container.textContent).toContain('Other project only');
    expect(container.textContent).not.toContain('Active item 51');
    expect(buttonByText('上一页').disabled).toBe(true);
  });

  it('locates a task beyond page 200 and recovers when its last page becomes empty', async () => {
    h.data[panelTasksPath] = rows('History', 201, 'completed');
    await render(<WorkingTasksPanel {...baseProps} initialTaskId="History-201" />);
    expect(container.textContent).toContain('History item 201');
    expect(container.querySelector('[aria-selected="true"]').textContent).toContain('暂停与已完成历史');
    h.data[panelTasksPath] = h.data[panelTasksPath].slice(0, 200);
    await render(<WorkingTasksPanel {...baseProps} initialTaskId="History-201" revision={1} />);
    expect(container.textContent).toContain('History item 151');
    expect(container.textContent).not.toContain('History item 201');
    expect(buttonByText('下一页').disabled).toBe(true);
  });

  it('surfaces next-page failures without rendering the old page as the new page', async () => {
    h.data[panelTasksPath] = rows('History', 201, 'completed');
    h.errors['/api/working-tasks?project_id=p&status=history&limit=50&offset=50'] = new Error('Page unavailable');
    await render(<WorkingTasksPanel {...baseProps} />);
    await click(buttonContaining('暂停与已完成历史', container.querySelector('[role="tablist"]')));
    await click(buttonByText('下一页'));
    expect(container.textContent).toContain('Page unavailable');
    expect(Array.from(container.querySelectorAll('h3,h4')).some(node => node.textContent === 'History item 1')).toBe(false);
  });
});

describe('Audit regressions — pinned_first, global preference merge, and 404/409/410 delete recovery', () => {
  it('requests pinned_first=true for long-term memories (Bug #10)', async () => {
    h.data[contentIndexPath] = { total: 0, items: [] };
    await render(<KnowledgePage {...baseProps} />);
    expect(h.resources.some(entry => entry.path === contentIndexPath && entry.path.includes('pinned_first=true'))).toBe(true);
  });

  it('allows merging global preferences even when projects=[] while keeping project_id=null and preference category (Bug #11)', async () => {
    const globalIndexPath = '/api/content/index?status=active&exclude_category=task&limit=50&offset=0&kind=memory&pinned_first=true';
    h.data[globalIndexPath] = {
      total: 2,
      items: [
        { id: 'g1', project_id: null, kind: 'memory', category: 'preference', status: 'active', title: 'Global Pref 1', content_preview: 'Body 1', version: 1, tags: [] },
        { id: 'g2', project_id: null, kind: 'memory', category: 'preference', status: 'active', title: 'Global Pref 2', content_preview: 'Body 2', version: 1, tags: [] }
      ]
    };
    h.request.mockImplementation(async url => {
      if (url === '/api/content/g1?include_versions=false') return { id: 'g1', version: 1, content: 'Body 1' };
      if (url === '/api/content/g2?include_versions=false') return { id: 'g2', version: 1, content: 'Body 2' };
      return null;
    });
    h.mutate.mockResolvedValueOnce({ id: 'g_merged', title: 'Merged Global Pref' });

    await render(<KnowledgePage {...baseProps} projectId={null} projects={[]} />);
    await click(container.querySelector('[aria-label="选择 Global Pref 1"]'));
    await click(container.querySelector('[aria-label="选择 Global Pref 2"]'));
    await click(buttonByText('合并'));

    const modal = container.querySelector('.modal');
    expect(modal).toBeTruthy();
    expect(fieldControl('合并后的所属项目', modal).value).toBe('__global__');
    expect(fieldControl('分类', modal).value).toBe('preference');
    expect(buttonByText('确认保存', modal).disabled).toBe(true);

    await change(fieldControl('合并后的标题', modal), 'Merged Global Pref');
    expect(buttonByText('确认保存', modal).disabled).toBe(false);
    await submit(modal.querySelector('form'));

    expect(h.mutate).toHaveBeenCalledWith('/api/content/merge', 'POST', {
      ids: ['g1', 'g2'],
      title: 'Merged Global Pref',
      content: 'Body 1\n\nBody 2',
      project_id: null,
      expected_versions: { g1: 1, g2: 1 },
      category: 'preference'
    });
  });

  it('disables confirm delete and shows refresh recovery on 409 conflict in both row quick-delete and task delete modals (Bug #12)', async () => {
    h.data[contentIndexPath] = {
      total: 1,
      items: [
        { id: 'm1', project_id: 'p', kind: 'memory', category: 'knowledge', status: 'active', title: 'Memory One', content_preview: 'Text', version: 3, tags: [] }
      ]
    };
    h.mutate.mockRejectedValueOnce(new ApiError('Conflict', 409));

    await render(<KnowledgePage {...baseProps} />);
    await click(container.querySelector('[aria-label="删除记忆：Memory One"]'));
    const memModal = container.querySelector('.modal');
    expect(memModal).toBeTruthy();
    await click(buttonByText('永久删除', memModal));
    expect(buttonByText('永久删除', memModal).disabled).toBe(true);
    expect(memModal.textContent).toContain('返回列表并刷新');

    h.data[panelTasksPath] = [
      { id: 't1', project_id: 'p', title: 'Deploy API', status: 'active', execution_mode: 'approved', version: 2 }
    ];
    h.mutate.mockRejectedValueOnce(new ApiError('Conflict', 409));

    await render(<WorkingTasksPanel {...baseProps} />);
    await click(container.querySelector('[aria-label="删除任务：Deploy API"]'));
    const taskModal = container.querySelector('.modal');
    expect(taskModal).toBeTruthy();
    await click(buttonByText('确认删除任务', taskModal));
    expect(h.mutate).toHaveBeenCalledWith('/api/working-tasks/t1?expected_version=2', 'DELETE');
    expect(buttonByText('确认删除任务', taskModal).disabled).toBe(true);
    expect(taskModal.textContent).toContain('返回列表并刷新');
  });
});
