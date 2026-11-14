import { useEffect, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle2, Clock, GitBranch, Pencil, Plus, ShieldAlert, Sparkles, Trash2 } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { mutate } from '../lib/api.js';
import { queryString, useResource } from '../lib/useResource.js';
import { AGENTS, Badge, Button, EmptyState, ErrorNotice, Field, Modal, ResourceState, formatTime } from './ui.jsx';
import { Pagination } from './Pagination.jsx';

const TASK_PAGE_SIZE = 50;

export const ACTIVE_TASK_STATUSES = ['active', 'in_progress', 'blocked'];
export const HISTORY_TASK_STATUSES = ['paused', 'completed', 'cancelled'];

export const TASK_STATUS_OPTIONS = [
  ['in_progress', '进行中'],
  ['blocked', '阻塞'],
  ['paused', '暂停'],
  ['completed', '已完成'],
  ['cancelled', '已取消']
];

export function taskStatusLabel(status) {
  if (status === 'active' || status === 'in_progress') return t('进行中');
  if (status === 'blocked') return t('阻塞');
  if (status === 'paused') return t('暂停');
  if (status === 'completed') return t('已完成');
  if (status === 'cancelled') return t('已取消');
  return status || t('进行中');
}

export function isDiscussionOnlyTask(task) {
  return Boolean(task?.must_not_execute || task?.execution_mode === 'discussion_only');
}

export function formatActorLabel(actor) {
  const clean = String(actor || '').trim();
  if (!clean || clean === 'user' || clean === 'manual' || clean === 'web') return t('人工维护');
  return AGENTS[clean] || clean;
}

function extractTaskItems(data) {
  if (Array.isArray(data)) return data;
  if (Array.isArray(data?.items)) return data.items;
  return [];
}

function parseLines(text) {
  return String(text || '')
    .split('\n')
    .map(line => line.trim())
    .filter(Boolean);
}

function parseCsv(text) {
  return String(text || '')
    .split(/[,，\n]/)
    .map(part => part.trim())
    .filter(Boolean);
}

export function normalizeCompletedSteps(steps) {
  if (!Array.isArray(steps)) return [];
  return steps
    .map(entry => {
      if (typeof entry === 'string') return { step: entry.trim(), evidence: '', agent: '', at: '' };
      if (!entry || typeof entry !== 'object') return null;
      return {
        step: String(entry.step || entry.title || '').trim(),
        evidence: String(entry.evidence || entry.evidence_ref || '').trim(),
        agent: String(entry.agent || entry.verified_by || '').trim(),
        at: entry.at || ''
      };
    })
    .filter(entry => entry && entry.step);
}

export function normalizeTempConstraints(items) {
  if (!Array.isArray(items)) return [];
  return items
    .map(entry => {
      if (typeof entry === 'string') return entry.trim();
      if (entry && typeof entry === 'object') return String(entry.rule || entry.text || '').trim();
      return '';
    })
    .filter(Boolean);
}

export function normalizeFailedAttempts(items) {
  if (!Array.isArray(items)) return [];
  return items
    .map(entry => {
      if (typeof entry === 'string') return { attempt: entry.trim(), reason: '', agent: '', at: '' };
      if (!entry || typeof entry !== 'object') return null;
      return {
        attempt: String(entry.attempt || entry.hypothesis || '').trim(),
        reason: String(entry.reason || entry.failure_reason || '').trim(),
        agent: String(entry.agent || '').trim(),
        at: entry.at || ''
      };
    })
    .filter(entry => entry && (entry.attempt || entry.reason));
}

export function normalizeStringList(items) {
  if (!Array.isArray(items)) return [];
  return items.map(entry => (typeof entry === 'string' ? entry.trim() : String(entry?.text || entry?.blocker || '').trim())).filter(Boolean);
}

function buildInitialTaskForm(task, projectId, projects) {
  const completed = normalizeCompletedSteps(task?.completed_steps);
  const failed = normalizeFailedAttempts(task?.failed_attempts);
  return {
    project_id: task?.project_id || projectId || projects?.[0]?.id || '',
    title: task?.title || '',
    status: task?.status === 'active' ? 'in_progress' : (task?.status || 'in_progress'),
    execution_mode: isDiscussionOnlyTask(task) ? 'discussion_only' : (task?.execution_mode || 'approved'),
    last_agent: ['user', 'manual', 'web'].includes(task?.last_agent) ? '' : (task?.last_agent || ''),
    git_branch: task?.git_branch || '',
    goal: task?.goal || '',
    current_state: task?.current_state || '',
    completed_steps: completed.length ? completed : [{ step: '', evidence: '', agent: '' }],
    temp_constraints_text: normalizeTempConstraints(task?.temp_constraints).join('\n'),
    failed_attempts: failed.length ? failed : [{ attempt: '', reason: '', agent: '' }],
    blockers_text: normalizeStringList(task?.blockers).join('\n'),
    next_steps_text: normalizeStringList(task?.next_steps).join('\n')
  };
}

export function serializeTaskPayload(form, task) {
  const completed_steps = (form.completed_steps || [])
    .map(row => ({
      step: String(row.step || '').trim(),
      evidence: String(row.evidence || '').trim(),
      agent: String(row.agent || '').trim() || 'user'
    }))
    .filter(row => row.step);
  const failed_attempts = (form.failed_attempts || [])
    .map(row => ({
      attempt: String(row.attempt || '').trim(),
      reason: String(row.reason || '').trim(),
      agent: String(row.agent || '').trim() || 'user'
    }))
    .filter(row => row.attempt || row.reason);
  const payload = {
    project_id: form.project_id,
    title: form.title.trim(),
    status: form.status,
    execution_mode: form.execution_mode,
    last_agent: form.last_agent.trim() || null,
    git_branch: form.git_branch.trim() || null,
    goal: form.goal.trim(),
    current_state: form.current_state.trim(),
    completed_steps,
    temp_constraints: parseLines(form.temp_constraints_text),
    failed_attempts,
    blockers: parseLines(form.blockers_text),
    next_steps: parseLines(form.next_steps_text)
  };
  if (task?.version !== undefined) payload.expected_version = task.version;
  return payload;
}

function buildLessonDraft(task) {
  const parts = [];
  if (task?.goal) parts.push(task.goal.trim());
  const failed = normalizeFailedAttempts(task?.failed_attempts);
  if (failed.length) {
    const firstFail = failed[0];
    if (firstFail.attempt && firstFail.reason) parts.push(`${firstFail.attempt} -> ${firstFail.reason}`);
  }
  const completed = normalizeCompletedSteps(task?.completed_steps);
  if (completed.length) {
    const lastStep = completed[completed.length - 1];
    parts.push(lastStep.evidence ? `${lastStep.step} (${lastStep.evidence})` : lastStep.step);
  }
  return parts.join('\n\n');
}

function TaskEventsTimeline({ task, open, revision }) {
  const hasInlineEvents = Array.isArray(task?.events);
  const detailPath = open && !hasInlineEvents && task?.id ? `/api/working-tasks/${encodeURIComponent(task.id)}` : null;
  const detail = useResource(detailPath, revision);
  const events = detail.data?.events || task?.events || [];

  if (!open) return null;
  if (detail.loading && !events.length) {
    return <p className="muted task-events-empty">{t('正在读取更新记录…')}</p>;
  }
  if (detail.error && !events.length) {
    return <ErrorNotice error={detail.error} />;
  }
  if (!events.length) {
    return <p className="muted task-events-empty">{t('尚无跨 Agent 更新记录。')}</p>;
  }
  return (
    <ol className="task-events-list">
      {events.map((event, idx) => (
        <li key={event.id || `${event.version || idx}-${idx}`} className="task-event-item">
          <div className="task-event-meta">
            <Badge>{formatActorLabel(event.actor)}</Badge>
            {event.version ? <span className="task-mono-chip">v{event.version}</span> : null}
            <time>{formatTime(event.created_at)}</time>
          </div>
          <p>{event.summary || event.event_type}</p>
        </li>
      ))}
    </ol>
  );
}

export function WorkingTaskModal({ task, projectId, projects, onClose, onSaved }) {
  const [form, setForm] = useState(() => buildInitialTaskForm(task, projectId, projects));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  function updateCompletedRow(index, key, value) {
    setForm(previous => ({
      ...previous,
      completed_steps: previous.completed_steps.map((row, idx) => (idx === index ? { ...row, [key]: value } : row))
    }));
  }

  function updateFailedRow(index, key, value) {
    setForm(previous => ({
      ...previous,
      failed_attempts: previous.failed_attempts.map((row, idx) => (idx === index ? { ...row, [key]: value } : row))
    }));
  }

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const payload = serializeTaskPayload(form, task);
      const saved = await mutate(
        task ? `/api/working-tasks/${encodeURIComponent(task.id)}` : '/api/working-tasks',
        task ? 'PATCH' : 'POST',
        payload
      );
      onSaved(saved);
      onClose();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={task ? t('编辑当前任务') : t('新建当前任务')} onClose={onClose} busy={busy} wide initialFocus="input[required]">
      <form className="editor-form" onSubmit={save}>
        <div className="form-columns">
          <Field label={t('任务标题')}>
            <input
              autoFocus
              required
              placeholder={t('例如：修复跨端接棒时的分支校验')}
              value={form.title}
              onChange={event => setForm({ ...form, title: event.target.value })}
            />
          </Field>
          <Field label={t('所属项目')}>
            <select
              required
              value={form.project_id}
              disabled={Boolean(task)}
              onChange={event => setForm({ ...form, project_id: event.target.value })}
            >
              <option value="" disabled>{t('选择项目')}</option>
              {(projects || []).map(project => (
                <option key={project.id} value={project.id}>
                  {project.name}{project.status === 'archived' ? t('（已归档）') : ''}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <div className="form-columns">
          <Field label={t('任务状态')} hint={t('暂停、已完成或已取消的任务会自动退出日常上下文注入。')}>
            <select value={form.status} onChange={event => setForm({ ...form, status: event.target.value })}>
              {TASK_STATUS_OPTIONS.map(([value, label]) => (
                <option key={value} value={value}>{t(label)}</option>
              ))}
            </select>
          </Field>
          <Field label={t('执行授权')} hint={t('设为「仅讨论方案」时，其他 Agent 读取接续也不会擅自改代码。')}>
            <select value={form.execution_mode} onChange={event => setForm({ ...form, execution_mode: event.target.value })}>
              <option value="approved">{t('允许执行')}</option>
              <option value="discussion_only">{t('仅讨论方案 · 未授权改代码')}</option>
            </select>
          </Field>
        </div>

        <div className="form-columns">
          <Field label={t('当前负责 Agent')}>
            <select value={form.last_agent} onChange={event => setForm({ ...form, last_agent: event.target.value })}>
              <option value="">{t('人工维护 / 未指定')}</option>
              {Object.entries(AGENTS).map(([key, label]) => (
                <option key={key} value={key}>{label}</option>
              ))}
            </select>
          </Field>
          <Field label={t('所在分支')}>
            <input
              placeholder={t('例如：feat/memory-governance')}
              value={form.git_branch}
              onChange={event => setForm({ ...form, git_branch: event.target.value })}
            />
          </Field>
        </div>

        <div className="form-columns">
          <Field label={t('目标')}>
            <textarea
              rows={3}
              placeholder={t('这次任务要解决的核心目标是什么？')}
              value={form.goal}
              onChange={event => setForm({ ...form, goal: event.target.value })}
            />
          </Field>
          <Field label={t('当前状态')}>
            <textarea
              rows={3}
              placeholder={t('例如：正在讨论方案，尚未授权改代码')}
              value={form.current_state}
              onChange={event => setForm({ ...form, current_state: event.target.value })}
            />
          </Field>
        </div>

        <div className="structured-slot-editor">
          <div className="slot-editor-header">
            <span className="field-label">{t('已完成与依据')}</span>
            <Button
              onClick={() => setForm(previous => ({
                ...previous,
                completed_steps: [...previous.completed_steps, { step: '', evidence: '', agent: '' }]
              }))}
            >
              {t('添加步骤')}
            </Button>
          </div>
          {form.completed_steps.map((row, index) => (
            <div key={index} className="slot-editor-row">
              <input
                aria-label={t('已完成步骤 {index}', { index: index + 1 })}
                placeholder={t('已完成动作')}
                value={row.step}
                onChange={event => updateCompletedRow(index, 'step', event.target.value)}
              />
              <input
                aria-label={t('完成依据 {index}', { index: index + 1 })}
                placeholder={t('验证依据（测试、提交或文件）')}
                value={row.evidence}
                onChange={event => updateCompletedRow(index, 'evidence', event.target.value)}
              />
              {form.completed_steps.length > 1 ? (
                <Button
                  icon={Trash2}
                  aria-label={t('移除步骤 {index}', { index: index + 1 })}
                  onClick={() => setForm(previous => ({
                    ...previous,
                    completed_steps: previous.completed_steps.filter((_, idx) => idx !== index)
                  }))}
                />
              ) : null}
            </div>
          ))}
        </div>

        <Field
          label={t('本次临时限制')}
          hint={t('仅限当前任务，不升级为长期规则（每行一条，例如「这次先别改界面」）。')}
        >
          <textarea
            rows={3}
            placeholder={t('这次先别改界面\n不要改动外部接口签名')}
            value={form.temp_constraints_text}
            onChange={event => setForm({ ...form, temp_constraints_text: event.target.value })}
          />
        </Field>

        <div className="structured-slot-editor">
          <div className="slot-editor-header">
            <span className="field-label">{t('失败尝试')}</span>
            <Button
              onClick={() => setForm(previous => ({
                ...previous,
                failed_attempts: [...previous.failed_attempts, { attempt: '', reason: '', agent: '' }]
              }))}
            >
              {t('添加失败记录')}
            </Button>
          </div>
          {form.failed_attempts.map((row, index) => (
            <div key={index} className="slot-editor-row">
              <input
                aria-label={t('失败尝试 {index}', { index: index + 1 })}
                placeholder={t('尝试过的做法')}
                value={row.attempt}
                onChange={event => updateFailedRow(index, 'attempt', event.target.value)}
              />
              <input
                aria-label={t('失败原因 {index}', { index: index + 1 })}
                placeholder={t('为什么行不通')}
                value={row.reason}
                onChange={event => updateFailedRow(index, 'reason', event.target.value)}
              />
              {form.failed_attempts.length > 1 ? (
                <Button
                  icon={Trash2}
                  aria-label={t('移除失败记录 {index}', { index: index + 1 })}
                  onClick={() => setForm(previous => ({
                    ...previous,
                    failed_attempts: previous.failed_attempts.filter((_, idx) => idx !== index)
                  }))}
                />
              ) : null}
            </div>
          ))}
        </div>

        <div className="form-columns">
          <Field label={t('当前阻塞')} hint={t('每行一条当前卡点')}>
            <textarea
              rows={3}
              placeholder={t('等待上游接口字段确认')}
              value={form.blockers_text}
              onChange={event => setForm({ ...form, blockers_text: event.target.value })}
            />
          </Field>
          <Field label={t('下一步')} hint={t('每行一条接下来要做的动作')}>
            <textarea
              rows={3}
              placeholder={t('补齐边界测试\n在预发环境验证')}
              value={form.next_steps_text}
              onChange={event => setForm({ ...form, next_steps_text: event.target.value })}
            />
          </Field>
        </div>

        <ErrorNotice error={error} />
        <footer className="modal-actions">
          <Button onClick={onClose}>{t('取消')}</Button>
          <button
            type="submit"
            className="button button-primary"
            disabled={busy || !form.project_id || !form.title.trim()}
          >
            {busy ? t('正在保存…') : t('保存任务')}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

export function PromoteLessonModal({ task, onClose, onPromoted }) {
  const [form, setForm] = useState(() => ({
    title: task?.title || '',
    category: 'lesson',
    content: buildLessonDraft(task),
    evidence_quote: '',
    when: '',
    env: '',
    version_range: '',
    paths: ''
  }));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const parsedPaths = parseCsv(form.paths);
  const hasApplicability = Boolean(
    form.when.trim() || form.env.trim() || form.version_range.trim() || parsedPaths.length
  );

  async function submit(event) {
    event.preventDefault();
    if (busy || !hasApplicability || !task.source_conversation_id || !form.evidence_quote.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const applicability = {};
      if (form.when.trim()) applicability.when = form.when.trim();
      if (form.env.trim()) applicability.env = form.env.trim();
      if (form.version_range.trim()) applicability.version_range = form.version_range.trim();
      if (parsedPaths.length) applicability.paths = parsedPaths;

      const submittedTitle = form.title.trim();
      const result = await mutate(`/api/working-tasks/${encodeURIComponent(task.id)}/promote`, 'POST', {
        title: submittedTitle,
        content: form.content.trim(),
        category: form.category,
        applicability,
        evidence_quote: form.evidence_quote.trim(),
        actor: 'manual'
      });
      onPromoted(result, submittedTitle);
      onClose();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={t('提炼为长期经验')} onClose={onClose} busy={busy} wide initialFocus="input[data-autofocus]">
      <form className="editor-form" onSubmit={submit}>
        <p className="promote-guidance">
          {t('只提炼已验证有效、跨对话仍成立的做法。必须写清适用条件，避免一次性的局部修复变成全局死规矩。')}
        </p>
        {!task.source_conversation_id && <p role="alert">{t('请先在任务中关联原始聊天，再提炼有来源的经验。')}</p>}

        <div className="form-columns">
          <Field label={t('经验标题')}>
            <input
              required
              value={form.title}
              onChange={event => setForm({ ...form, title: event.target.value })}
            />
          </Field>
          <Field label={t('长期记忆分类')}>
            <select value={form.category} onChange={event => setForm({ ...form, category: event.target.value })}>
              <option value="lesson">{t('踩坑经验')}</option>
              <option value="decision">{t('决定与约束')}</option>
            </select>
          </Field>
        </div>

        <section className="applicability-fieldset" aria-label={t('适用条件（至少填写一项）')}>
          <div className="applicability-fieldset-header">
            <strong>{t('适用条件（必填至少一项）')}</strong>
            <span>{hasApplicability ? t('已填写适用范围') : t('请至少填写条件、环境、版本或路径之一')}</span>
          </div>
          <div className="form-columns">
            <Field label={t('触发条件 / 适用场景')}>
              <input
                data-autofocus
                placeholder={t('例如：SQLite 表含内联 CHECK 约束且需扩展枚举时')}
                value={form.when}
                onChange={event => setForm({ ...form, when: event.target.value })}
              />
            </Field>
            <Field label={t('适用环境')}>
              <input
                placeholder={t('例如：macOS / SQLite 3 / Python 3.12')}
                value={form.env}
                onChange={event => setForm({ ...form, env: event.target.value })}
              />
            </Field>
          </div>
          <div className="form-columns">
            <Field label={t('版本范围')}>
              <input
                placeholder={t('例如：>=1.0.0 或 Vite 7')}
                value={form.version_range}
                onChange={event => setForm({ ...form, version_range: event.target.value })}
              />
            </Field>
            <Field label={t('关联路径（逗号分隔）')}>
              <input
                placeholder={t('例如：app/backend/app/database.py')}
                value={form.paths}
                onChange={event => setForm({ ...form, paths: event.target.value })}
              />
            </Field>
          </div>
        </section>

        <Field label={t('经验正文')} hint={t('写明在什么前提下应当怎么做，以及忽略后会踩什么坑。')}>
          <textarea
            required
            rows={6}
            value={form.content}
            onChange={event => setForm({ ...form, content: event.target.value })}
          />
        </Field>

        <Field label={t('验证依据摘录')} hint={t('必填：粘贴关联聊天中真实存在的原话，不接受自行编写的验证结果。')}>
          <input
            required
            value={form.evidence_quote}
            onChange={event => setForm({ ...form, evidence_quote: event.target.value })}
          />
        </Field>

        <ErrorNotice error={error} />
        <footer className="modal-actions">
          <Button onClick={onClose}>{t('取消')}</Button>
          <button
            type="submit"
            className="button button-primary"
            disabled={busy || !form.title.trim() || !form.content.trim() || !hasApplicability || !task.source_conversation_id || !form.evidence_quote.trim()}
          >
            {busy ? t('正在提炼…') : t('确认提炼为长期经验')}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

function WorkingTaskCard({
  task,
  projects,
  showProject,
  highlighted,
  busy,
  revision,
  onStatusChange,
  onToggleExecutionMode,
  onEdit,
  onPromote,
  onDelete
}) {
  const [eventsOpen, setEventsOpen] = useState(false);
  const discussionOnly = isDiscussionOnlyTask(task);
  const completedSteps = normalizeCompletedSteps(task.completed_steps);
  const tempConstraints = normalizeTempConstraints(task.temp_constraints);
  const failedAttempts = normalizeFailedAttempts(task.failed_attempts);
  const blockers = normalizeStringList(task.blockers);
  const nextSteps = normalizeStringList(task.next_steps);
  const projectName = showProject ? projects?.find(item => item.id === task.project_id)?.name : null;
  const normalizedStatusValue = task.status === 'active' ? 'in_progress' : (task.status || 'in_progress');
  const isInactive = HISTORY_TASK_STATUSES.includes(task.status);

  return (
    <article
      className={[
        'working-task-card group',
        discussionOnly ? 'is-discussion-only' : '',
        highlighted ? 'is-highlighted' : '',
        isInactive ? 'is-inactive-task' : ''
      ].filter(Boolean).join(' ')}
    >
      <header className="working-task-header">
        <div className="working-task-meta">
          <Badge className={`badge-task-status status-${task.status || 'in_progress'}`}>
            {taskStatusLabel(task.status)}
          </Badge>
          {discussionOnly ? (
            <Badge className="badge-guard-discussion">
              <ShieldAlert size={13} aria-hidden="true" />
              {t('仅讨论方案 · 未授权改代码')}
            </Badge>
          ) : (
            <Badge className="badge-guard-approved">
              <CheckCircle2 size={13} aria-hidden="true" />
              {t('允许执行')}
            </Badge>
          )}
          <Badge className="badge-agent-owner">
            {t('负责 Agent：')}{formatActorLabel(task.last_agent)}
          </Badge>
          {task.git_branch ? (
            <span className="task-branch-chip" title={t('任务关联分支')}>
              <GitBranch size={12} aria-hidden="true" />
              <code>{task.git_branch}</code>
            </span>
          ) : null}
          {projectName ? <Badge>{projectName}</Badge> : null}
          {isInactive ? <span className="task-retired-note">{t('已退出日常注入')}</span> : null}
          <span className="task-updated-at">
            {task.version ? `v${task.version} · ` : ''}{formatTime(task.updated_at)}
          </span>
        </div>
        <h3>{task.title}</h3>
      </header>

      <div className="working-task-slots">
        <section className="task-slot">
          <h4>{t('目标')}</h4>
          <p className={task.goal ? '' : 'slot-placeholder'}>{task.goal || t('尚未填写任务目标')}</p>
        </section>

        <section className="task-slot">
          <h4>{t('当前状态')}</h4>
          <p className={task.current_state ? '' : 'slot-placeholder'}>{task.current_state || t('尚未记录当前状态')}</p>
        </section>

        <section className="task-slot">
          <h4>{t('已完成与依据')}</h4>
          {completedSteps.length ? (
            <ul className="slot-step-list">
              {completedSteps.map((item, idx) => (
                <li key={idx}>
                  <span className="slot-step-main">{item.step}</span>
                  <div className="slot-step-sub">
                    {item.evidence ? <span className="slot-evidence">{t('依据：')}{item.evidence}</span> : null}
                    {item.agent ? <Badge>{formatActorLabel(item.agent)}</Badge> : null}
                  </div>
                </li>
              ))}
            </ul>
          ) : (
            <p className="slot-placeholder">{t('尚无已完成步骤')}</p>
          )}
        </section>

        <section className="task-slot task-slot-temp">
          <div className="task-slot-heading">
            <h4>{t('本次临时限制')}</h4>
            <span className="slot-scope-tag">{t('仅限当前任务，不升级为长期规则')}</span>
          </div>
          {tempConstraints.length ? (
            <ul className="slot-bullet-list">
              {tempConstraints.map((rule, idx) => (
                <li key={idx}>{rule}</li>
              ))}
            </ul>
          ) : (
            <p className="slot-placeholder">{t('无本次临时限制')}</p>
          )}
        </section>

        <section className="task-slot">
          <h4>{t('失败尝试与阻塞')}</h4>
          {blockers.length || failedAttempts.length ? (
            <div className="slot-failures">
              {blockers.map((blocker, idx) => (
                <div key={`b-${idx}`} className="slot-blocker-row">
                  <AlertTriangle size={13} aria-hidden="true" />
                  <span>{t('阻塞：')}{blocker}</span>
                </div>
              ))}
              {failedAttempts.map((entry, idx) => (
                <div key={`f-${idx}`} className="slot-failure-row">
                  <strong>{entry.attempt || t('失败尝试')}</strong>
                  {entry.reason ? <span>{t('原因：')}{entry.reason}</span> : null}
                  {entry.agent ? <small>{formatActorLabel(entry.agent)}</small> : null}
                </div>
              ))}
            </div>
          ) : (
            <p className="slot-placeholder">{t('暂无阻塞或失败尝试')}</p>
          )}
        </section>

        <section className="task-slot">
          <h4>{t('下一步')}</h4>
          {nextSteps.length ? (
            <ol className="slot-next-list">
              {nextSteps.map((step, idx) => (
                <li key={idx}>{step}</li>
              ))}
            </ol>
          ) : (
            <p className="slot-placeholder">{t('尚未列出下一步')}</p>
          )}
        </section>
      </div>

      <details
        className="task-events-details"
        onToggle={event => setEventsOpen(event.currentTarget.open)}
      >
        <summary>
          <Clock size={13} aria-hidden="true" />
          <span>
            {Array.isArray(task.events) && task.events.length
              ? t('跨 Agent 更新记录（{count}）', { count: task.events.length })
              : t('跨 Agent 更新记录')}
          </span>
        </summary>
        <TaskEventsTimeline task={task} open={eventsOpen} revision={revision} />
      </details>

      <footer className="working-task-footer">
        <div className="working-task-status-control">
          <select
            aria-label={t('切换任务状态：{title}', { title: task.title })}
            disabled={busy}
            value={normalizedStatusValue}
            onChange={event => onStatusChange(task, event.target.value)}
          >
            {TASK_STATUS_OPTIONS.map(([value, label]) => (
              <option key={value} value={value}>{t(label)}</option>
            ))}
          </select>
          <Button
            disabled={busy}
            onClick={() => onToggleExecutionMode(task)}
          >
            {discussionOnly ? t('切换为允许执行') : t('设为仅讨论方案')}
          </Button>
        </div>

        <div className="toolbar-actions">
          <Button icon={Sparkles} disabled={busy} onClick={() => onPromote(task)}>
            {t('提炼为长期经验')}
          </Button>
          <Button icon={Pencil} disabled={busy} onClick={() => onEdit(task)}>
            {t('编辑任务')}
          </Button>
          <Button
            icon={Trash2}
            disabled={busy}
            aria-label={t('删除任务：{title}', { title: task.title })}
            onClick={() => onDelete(task)}
          />
        </div>
      </footer>
    </article>
  );
}

export function WorkingTasksPanel({
  projectId,
  projects,
  revision,
  initialTaskId = null,
  onChanged,
  onOpenMemory
}) {
  const [page, setPage] = useState({ projectId, mode: 'active', offset: 0 });
  const filterMode = page.projectId === projectId ? page.mode : 'active';
  const offset = page.projectId === projectId ? page.offset : 0;
  const setFilterMode = mode => setPage({ projectId, mode, offset: 0 });
  const [creating, setCreating] = useState(false);
  const [editingTask, setEditingTask] = useState(null);
  const [promotingTask, setPromotingTask] = useState(null);
  const [deletingTask, setDeletingTask] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const pagePath = '/api/working-tasks' + queryString({
    project_id: projectId || '',
    ...(filterMode === 'active' ? { active_only: 'true' } : {}),
    ...(filterMode === 'history' ? { status: 'history' } : {}),
    limit: TASK_PAGE_SIZE,
    offset
  });
  const summaryPath = '/api/working-tasks/summary' + queryString({ project_id: projectId || '' });
  const positionPath = initialTaskId
    ? `/api/working-tasks/${encodeURIComponent(initialTaskId)}/position` + queryString({
        project_id: projectId || '',
        limit: TASK_PAGE_SIZE
      })
    : null;
  const pageResource = useResource(pagePath, revision);
  const summaryResource = useResource(summaryPath, revision);
  const positionResource = useResource(positionPath, revision);
  const visibleTasks = extractTaskItems(pageResource.data);
  const activeCount = summaryResource.data?.active ?? 0;
  const historyCount = summaryResource.data?.history ?? 0;
  const allCount = summaryResource.data?.all ?? 0;
  const total = filterMode === 'active' ? activeCount : filterMode === 'history' ? historyCount : allCount;

  const appliedInitialTaskRef = useRef(null);
  useEffect(() => {
    if (!initialTaskId) {
      appliedInitialTaskRef.current = null;
      return;
    }
    const key = `${projectId || ''}:${initialTaskId}`;
    const position = positionResource.data;
    if (appliedInitialTaskRef.current === key || positionResource.loading || positionResource.error) return;
    if (position?.task_id !== initialTaskId) return;
    appliedInitialTaskRef.current = key;
    setPage({ projectId, mode: position.group, offset: position.offset });
  }, [initialTaskId, projectId, positionResource.data, positionResource.loading, positionResource.error]);

  useEffect(() => {
    if (!summaryResource.data || summaryResource.loading || summaryResource.error) return;
    if (offset > 0 && offset >= total) {
      setPage({ projectId, mode: filterMode, offset: Math.max(0, Math.floor((total - 1) / TASK_PAGE_SIZE) * TASK_PAGE_SIZE) });
    }
  }, [projectId, filterMode, offset, total, summaryResource.data, summaryResource.loading, summaryResource.error]);

  function reloadResources() {
    pageResource.reload();
    summaryResource.reload();
    positionResource.reload();
  }

  const panelResource = {
    data: pageResource.data,
    loading: pageResource.loading || summaryResource.loading,
    error: pageResource.error || summaryResource.error,
    reload: reloadResources
  };

  function refreshAll() {
    reloadResources();
    onChanged?.();
  }

  async function handleStatusChange(task, nextStatus) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await mutate(`/api/working-tasks/${encodeURIComponent(task.id)}`, 'PATCH', {
        status: nextStatus,
        ...(task.version !== undefined ? { expected_version: task.version } : {})
      });
      refreshAll();
      const label = taskStatusLabel(nextStatus);
      setNotice({
        text: HISTORY_TASK_STATUSES.includes(nextStatus)
          ? t('任务「{title}」已标记为「{status}」，已自动退出日常上下文注入。', { title: task.title, status: label })
          : t('任务「{title}」状态已更新为「{status}」。', { title: task.title, status: label })
      });
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) reloadResources();
    } finally {
      setBusy(false);
    }
  }

  async function handleToggleExecutionMode(task) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    const nextMode = isDiscussionOnlyTask(task) ? 'approved' : 'discussion_only';
    try {
      await mutate(`/api/working-tasks/${encodeURIComponent(task.id)}`, 'PATCH', {
        execution_mode: nextMode,
        ...(task.version !== undefined ? { expected_version: task.version } : {})
      });
      refreshAll();
      setNotice({
        text: nextMode === 'discussion_only'
          ? t('「{title}」已设为仅讨论方案，未获授权前不会执行改代码。', { title: task.title })
          : t('「{title}」已切换为允许执行。', { title: task.title })
      });
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) reloadResources();
    } finally {
      setBusy(false);
    }
  }

  async function confirmDeleteTask() {
    if (busy || !deletingTask) return;
    setBusy(true);
    setError(null);
    try {
      const deletePath =
        deletingTask.version !== undefined && deletingTask.version !== null
          ? `/api/working-tasks/${encodeURIComponent(deletingTask.id)}?expected_version=${encodeURIComponent(deletingTask.version)}`
          : `/api/working-tasks/${encodeURIComponent(deletingTask.id)}`;
      await mutate(deletePath, 'DELETE');
      const removedTitle = deletingTask.title;
      setDeletingTask(null);
      refreshAll();
      setNotice({ text: t('已删除工作任务「{title}」。', { title: removedTitle }) });
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) reloadResources();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="working-tasks-panel">
      <div className="working-tasks-toolbar">
        <div className="memory-pill-bar" role="tablist" aria-label={t('任务状态筛选')}>
          <button
            type="button"
            role="tab"
            aria-selected={filterMode === 'active'}
            className={`memory-pill ${filterMode === 'active' ? 'active' : ''}`}
            onClick={() => setFilterMode('active')}
          >
            {t('进行中与阻塞')} <span className="pill-count">{activeCount}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={filterMode === 'history'}
            className={`memory-pill ${filterMode === 'history' ? 'active' : ''}`}
            onClick={() => setFilterMode('history')}
          >
            {t('暂停与已完成历史')} <span className="pill-count">{historyCount}</span>
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={filterMode === 'all'}
            className={`memory-pill ${filterMode === 'all' ? 'active' : ''}`}
            onClick={() => setFilterMode('all')}
          >
            {t('全部任务')} <span className="pill-count">{allCount}</span>
          </button>
        </div>

        <Button
          icon={Plus}
          variant="primary"
          disabled={busy || (!projectId && !(projects && projects.length))}
          onClick={() => {
            setError(null);
            setCreating(true);
          }}
        >
          {t('新建任务')}
        </Button>
      </div>

      <ErrorNotice error={error} />
      <ErrorNotice error={positionResource.error} />
      {notice ? (
        <div className="operation-notice operation-notice-flex" role="status">
          <span>{notice.text}</span>
          {notice.memoryId && onOpenMemory ? (
            <Button onClick={() => onOpenMemory(notice.memoryId)}>{t('查看该长期经验')}</Button>
          ) : null}
        </div>
      ) : null}

      <ResourceState resource={panelResource}>
        {visibleTasks.length ? (
          <div className="working-task-list">
            {visibleTasks.map(task => (
              <WorkingTaskCard
                key={task.id}
                task={task}
                projects={projects}
                showProject={!projectId}
                highlighted={initialTaskId === task.id}
                busy={busy}
                revision={revision}
                onStatusChange={handleStatusChange}
                onToggleExecutionMode={handleToggleExecutionMode}
                onEdit={target => {
                  setError(null);
                  setEditingTask(target);
                }}
                onPromote={target => {
                  setError(null);
                  setPromotingTask(target);
                }}
                onDelete={target => {
                  setError(null);
                  setDeletingTask(target);
                }}
              />
            ))}
          </div>
        ) : (
          <EmptyState
            title={
              filterMode === 'history'
                ? t('还没有暂停或已完成的任务历史')
                : t('当前没有进行中的任务')
            }
            body={
              filterMode === 'history'
                ? t('暂停、已完成或已取消的任务会归档在这里保留脉络，不再混入日常上下文注入。')
                : t('短期的目标、已完成进展、本次临时限制和下一步都放在这里，完成后不会污染长期记忆。')
            }
            action={
              filterMode === 'active' && historyCount > 0 ? (
                <Button onClick={() => setFilterMode('history')}>
                  {t('查看暂停与已完成历史（{count}）', { count: historyCount })}
                </Button>
              ) : (
                <Button
                  variant="primary"
                  icon={Plus}
                  disabled={!projectId && !(projects && projects.length)}
                  onClick={() => setCreating(true)}
                >
                  {t('新建任务')}
                </Button>
              )
            }
          />
        )}
      </ResourceState>
      <Pagination
        total={total}
        offset={offset}
        limit={TASK_PAGE_SIZE}
        loading={busy || panelResource.loading || Boolean(panelResource.error)}
        onChange={nextOffset => setPage({ projectId, mode: filterMode, offset: nextOffset })}
      />

      {filterMode === 'active' && visibleTasks.length > 0 && historyCount > 0 ? (
        <div className="working-tasks-history-hint">
          <span>{t('另有 {count} 项已暂停或已完成任务已退出日常注入。', { count: historyCount })}</span>
          <Button onClick={() => setFilterMode('history')}>{t('查看任务历史')}</Button>
        </div>
      ) : null}

      {creating ? (
        <WorkingTaskModal
          projectId={projectId}
          projects={projects}
          onClose={() => setCreating(false)}
          onSaved={saved => {
            refreshAll();
            setNotice({ text: t('已创建当前任务「{title}」。', { title: saved?.title || '' }) });
          }}
        />
      ) : null}

      {editingTask ? (
        <WorkingTaskModal
          task={editingTask}
          projectId={projectId}
          projects={projects}
          onClose={() => setEditingTask(null)}
          onSaved={saved => {
            refreshAll();
            setNotice({ text: t('已更新任务「{title}」。', { title: saved?.title || editingTask.title }) });
          }}
        />
      ) : null}

      {promotingTask ? (
        <PromoteLessonModal
          task={promotingTask}
          onClose={() => setPromotingTask(null)}
          onPromoted={(result, submittedTitle) => {
            refreshAll();
            if (result?.action === 'OPEN_CONFLICT') {
              const conflictTitle = result?.conflict?.proposed?.title || submittedTitle || promotingTask.title;
              setNotice({
                text: t('提炼内容「{title}」与现有记忆存在分歧，已转入「待确认」等待核对，尚未直接写入长期记忆。', {
                  title: conflictTitle
                }),
                memoryId: null
              });
              return;
            }
            const memory = result?.memory !== undefined ? result.memory : result;
            setNotice({
              text: t('已提炼为长期经验「{title}」。', {
                title: memory?.title || submittedTitle || promotingTask.title
              }),
              memoryId: memory?.id || null
            });
          }}
        />
      ) : null}

      {deletingTask ? (
        <Modal
          title={t('删除工作任务？')}
          busy={busy}
          onClose={() => {
            setError(null);
            setDeletingTask(null);
          }}
        >
          <p className="modal-copy">
            {t('确认删除工作任务「{title}」？删除后无法恢复，关联的原始聊天记录不受影响。', { title: deletingTask.title })}
          </p>
          <ErrorNotice error={error} />
          {[404, 409, 410].includes(error?.status) ? (
            <div className="version-recovery">
              <p>{t('内容状态已变化或无法确认，请返回详情重新读取后再操作。')}</p>
              <Button
                onClick={() => {
                  setError(null);
                  setDeletingTask(null);
                  reloadResources();
                }}
              >
                {t('返回列表并刷新')}
              </Button>
            </div>
          ) : null}
          <footer className="modal-actions">
            <Button
              onClick={() => {
                setError(null);
                setDeletingTask(null);
              }}
            >
              {t('取消')}
            </Button>
            <Button
              variant="danger"
              busy={busy}
              disabled={[404, 409, 410].includes(error?.status)}
              onClick={confirmDeleteTask}
            >
              {t('确认删除任务')}
            </Button>
          </footer>
        </Modal>
      ) : null}
    </div>
  );
}
