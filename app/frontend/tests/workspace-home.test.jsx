// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import WorkspaceHome from '../src/pages/WorkspaceHome.jsx';
import { setLang } from '../src/lib/i18n.jsx';

const fixture = vi.hoisted(() => ({ card: {}, memory: {}, createdId: 'p-created' }));
vi.mock('../src/lib/useResource.js', () => ({
  useResource: path => ({
    data: path?.startsWith('/api/workbench/memories') ? fixture.memory : fixture.card,
    loading: false, error: null, reload: vi.fn(),
  }),
}));
vi.mock('../src/components/ProjectDialog.jsx', () => ({
  ProjectDialog: ({ onSaved }) => <button type="button" onClick={() => onSaved({ id: fixture.createdId })}>Save fixture</button>,
}));

let root, container, navigate, retry;
beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  setLang('zh');
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  navigate = vi.fn();
  retry = vi.fn();
  fixture.card = {};
  fixture.memory = {};
});
afterEach(async () => {
  await act(() => root.unmount());
  container.remove();
});
async function show(props) {
  await act(() => root.render(<WorkspaceHome navigate={navigate} onRetry={retry} {...props} />));
}
async function click(label) {
  const button = [...container.querySelectorAll('button')].find(item => item.textContent.includes(label));
  expect(button).toBeDefined();
  await act(() => button.click());
}

it('starts a local project from the empty workspace and opens the newly saved record', async () => {
  await show({ projects: [] });
  expect(container.textContent).toContain('建立你的第一个项目');
  await click('连接本机 Agent');
  expect(navigate).toHaveBeenLastCalledWith({ page: 'connections' });
  await click('新建项目');
  await click('Save fixture');
  expect(retry).toHaveBeenCalledOnce();
  expect(navigate).toHaveBeenLastCalledWith({ page: 'projects', project: 'p-created', section: 'memory' });
});

it('resumes the most recently active available project and opens its current memory and original conversations', async () => {
  fixture.memory = { items: [{ id: 'm-new', heading: 'Use the approved brief' }] };
  fixture.card = { name: 'Ink Studio', bio: 'Research', assets: [{ title: 'Case A' }] };
  await show({ projects: [
    { id: 'retired', status: 'archived', name: 'Retired' },
    { id: 'study', name: 'Research sprint', counts: { conversations: 6 } },
    { id: 'design', name: 'Design study' },
  ] });
  expect(container.textContent).not.toContain('Retired');
  expect(container.textContent).toContain('Ink Studio');
  expect(container.textContent).toContain('6 次对话');
  expect(container.textContent).toContain('已选入名片的作品 · 1');
  await click('继续项目');
  expect(navigate).toHaveBeenLastCalledWith({ page: 'projects', project: 'study', section: 'memory' });
  await click('Use the approved brief');
  expect(navigate).toHaveBeenLastCalledWith({ page: 'projects', project: 'study', section: 'memory', record: 'm-new' });
  await click('查阅原始对话');
  expect(navigate).toHaveBeenLastCalledWith({ page: 'projects', project: 'study', section: 'conversations' });
  await click('起草协作说明');
  expect(navigate).toHaveBeenLastCalledWith({ page: 'exchange' });
  await click('Design study');
  expect(navigate).toHaveBeenLastCalledWith({ page: 'projects', project: 'design', section: 'memory' });
  for (const [label, section] of [['查看与编辑名片', 'profile'], ['个人记忆', 'memory'], ['能力与作品', 'assets']]) {
    await click(label);
    expect(navigate).toHaveBeenLastCalledWith({ page: 'personal', section });
  }
});

it('distinguishes loading, retryable errors, and an archive-only workspace from a first run', async () => {
  await show({ projects: [], loading: true });
  expect(container.querySelector('[aria-busy=true]')).toBeTruthy();
  expect(container.textContent).not.toContain('建立你的第一个项目');
  await show({ projects: [], error: new Error('temporarily unavailable') });
  expect(container.textContent).not.toContain('建立你的第一个项目');
  await click('重新读取');
  expect(retry).toHaveBeenCalledOnce();
  await show({ projects: [{ id: 'old', status: 'archived', name: 'Finished' }] });
  expect(container.textContent).toContain('开启下一个项目');
});
