import { useEffect, useState } from 'react';
import {
  Archive,
  ArrowLeft,
  BookOpen,
  Check,
  ChevronRight,
  Copy,
  FileText,
  GitMerge,
  History,
  Pencil,
  Pin,
  Plus,
  RefreshCw,
  Search,
  Sparkles,
  Trash2,
  Upload
} from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { mutate, request } from '../lib/api.js';
import { queryString, useResource } from '../lib/useResource.js';
import {
  AGENTS,
  Badge,
  Button,
  CATEGORIES,
  EmptyState,
  ErrorNotice,
  Field,
  Modal,
  ResourceState,
  SegmentedTabs,
  formatTime
} from '../components/ui.jsx';
import { Pagination } from '../components/Pagination.jsx';
import { useObjectRevision } from '../lib/objectRefresh.js';
import { Markdown } from '../components/Markdown.jsx';
import { ConversationDetail } from './ConversationsPage.jsx';
import { WorkingTasksPanel } from '../components/WorkingTasksPanel.jsx';

const EMPTY_FILTERS = {
  query: '',
  category: '',
  agent: '',
  tag: '',
  since: '',
  until: '',
  status: '',
  pinned: ''
};

const LONG_TERM_CATEGORIES = Object.entries(CATEGORIES).filter(([key]) => key !== 'task');

const ORIGIN_AUTHORITY_LABELS = {
  user_explicit: '用户明确指定',
  user_confirmed: '用户已确认',
  verified_outcome: '任务验证提炼',
  manual: '人工维护',
  import: '文档导入'
};

const isPinnedItem = item => Boolean(item?.pinned || (Array.isArray(item?.tags) && item.tags.includes('pinned')));

export function parseApplicability(raw) {
  if (!raw) return {};
  let parsed = raw;
  if (typeof raw === 'string') {
    try {
      parsed = JSON.parse(raw);
    } catch {
      return {};
    }
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
  const paths = Array.isArray(parsed.paths)
    ? parsed.paths.map(item => String(item).trim()).filter(Boolean)
    : typeof parsed.paths === 'string'
      ? parsed.paths.split(/[,，\n]/).map(item => item.trim()).filter(Boolean)
      : [];
  return {
    when: typeof parsed.when === 'string' ? parsed.when.trim() : '',
    env: typeof parsed.env === 'string' ? parsed.env.trim() : '',
    version_range: typeof parsed.version_range === 'string' ? parsed.version_range.trim() : '',
    paths
  };
}

function buildApplicabilityPayload(form) {
  const result = {};
  const when = String(form.app_when || '').trim();
  const env = String(form.app_env || '').trim();
  const versionRange = String(form.app_version_range || '').trim();
  const paths = String(form.app_paths || '')
    .split(/[,，\n]/)
    .map(item => item.trim())
    .filter(Boolean);
  if (when) result.when = when;
  if (env) result.env = env;
  if (versionRange) result.version_range = versionRange;
  if (paths.length) result.paths = paths;
  return result;
}

export function formatApplicabilityTags(raw) {
  const app = parseApplicability(raw);
  const tags = [];
  if (app.when) tags.push({ key: 'when', label: t('适用条件：{value}', { value: app.when }) });
  if (app.env) tags.push({ key: 'env', label: t('环境：{value}', { value: app.env }) });
  if (app.version_range) tags.push({ key: 'version_range', label: t('版本：{value}', { value: app.version_range }) });
  if (app.paths?.length) tags.push({ key: 'paths', label: t('路径：{value}', { value: app.paths.join(', ') }) });
  return tags;
}

export function normalizeUsageSummary(raw) {
  if (!raw || typeof raw !== 'object') {
    return {
      state: 'saved',
      provided_count: 0,
      cited_count: 0,
      last_provided_at: null,
      last_provided_agent: null,
      last_cited_at: null,
      last_cited_agent: null,
      last_cited_note: null,
      recent_events: []
    };
  }
  const citedCount = Number(raw.cited_count || 0);
  const providedCount = Number(raw.provided_count || 0);
  const state = raw.state || (citedCount > 0 ? 'cited' : providedCount > 0 ? 'provided' : 'saved');
  return {
    state,
    provided_count: providedCount,
    cited_count: citedCount,
    last_provided_at: raw.last_provided_at || null,
    last_provided_agent: raw.last_provided_agent || null,
    last_cited_at: raw.last_cited_at || null,
    last_cited_agent: raw.last_cited_agent || null,
    last_cited_note: raw.last_cited_note || null,
    recent_events: Array.isArray(raw.recent_events) ? raw.recent_events : []
  };
}

export function UsageSummaryBadge({ usageSummary, showCitedAgent = true }) {
  const usage = normalizeUsageSummary(usageSummary);
  if (usage.state === 'cited') {
    const count = usage.cited_count || 1;
    const citedAgent = usage.last_cited_agent ? (AGENTS[usage.last_cited_agent] || usage.last_cited_agent) : '';
    return (
      <>
        <Badge className="badge-usage-cited">
          {t('Agent 已明确引用 · {count} 次', { count })}
        </Badge>
        {showCitedAgent && citedAgent ? (
          <Badge className="badge-cited-agent">
            {t('最近引用：{agent}', { agent: citedAgent })}
          </Badge>
        ) : null}
      </>
    );
  }
  if (usage.state === 'provided') {
    const count = usage.provided_count || 1;
    return (
      <Badge className="badge-usage-provided">
        {t('已提供给 Agent · {count} 次', { count })}
      </Badge>
    );
  }
  return <Badge className="badge-usage-saved">{t('已保存')}</Badge>;
}

function ApplicabilityEditorFields({ form, setForm }) {
  return (
    <div className="applicability-editor-grid">
      <div className="form-columns">
        <Field label={t('触发条件 / 适用场景')}>
          <input
            placeholder={t('例如：需要迁移 SQLite 内联 CHECK 约束时')}
            value={form.app_when}
            onChange={event => setForm({ ...form, app_when: event.target.value })}
          />
        </Field>
        <Field label={t('适用环境')}>
          <input
            placeholder={t('例如：macOS / Python 3.12 / SQLite 3')}
            value={form.app_env}
            onChange={event => setForm({ ...form, app_env: event.target.value })}
          />
        </Field>
      </div>
      <div className="form-columns">
        <Field label={t('版本范围')}>
          <input
            placeholder={t('例如：>=1.0.0')}
            value={form.app_version_range}
            onChange={event => setForm({ ...form, app_version_range: event.target.value })}
          />
        </Field>
        <Field label={t('关联路径（逗号分隔）')}>
          <input
            placeholder={t('例如：app/backend/app/database.py')}
            value={form.app_paths}
            onChange={event => setForm({ ...form, app_paths: event.target.value })}
          />
        </Field>
      </div>
    </div>
  );
}

const contentForm = (item, projectId, kind) => {
  const app = parseApplicability(item?.applicability);
  const defaultCategory = item?.category || (kind === 'document' ? 'knowledge' : 'preference');
  return {
    project_id: item ? (item.project_id || '__global__') : (projectId || ''),
    title: item?.title || '',
    content: item?.content || '',
    category: defaultCategory,
    tags: (item?.tags || []).filter(tag => tag !== 'pinned').join(', ') || '',
    pinned: isPinnedItem(item),
    app_when: app.when || '',
    app_env: app.env || '',
    app_version_range: app.version_range || '',
    app_paths: (app.paths || []).join(', ')
  };
};

export function CorrectContentModal({ item, onClose, onSaved }) {
  const initialApp = parseApplicability(item?.applicability);
  const [form, setForm] = useState(() => ({
    title: item?.title || '',
    content: item?.content ?? item?.content_preview ?? '',
    category: item?.category && item.category !== 'task' ? item.category : 'decision',
    app_when: initialApp.when || '',
    app_env: initialApp.env || '',
    app_version_range: initialApp.version_range || '',
    app_paths: (initialApp.paths || []).join(', ')
  }));
  const [editedBody, setEditedBody] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (item?.content !== undefined || !item?.id) return undefined;
    let active = true;
    request(`/api/content/${encodeURIComponent(item.id)}?include_versions=false`)
      .then(full => {
        if (!active || !full || editedBody) return;
        const fullApp = parseApplicability(full.applicability);
        setForm(previous => ({
          ...previous,
          content: full.content ?? previous.content,
          app_when: previous.app_when || fullApp.when || '',
          app_env: previous.app_env || fullApp.env || '',
          app_version_range: previous.app_version_range || fullApp.version_range || '',
          app_paths: previous.app_paths || (fullApp.paths || []).join(', ')
        }));
      })
      .catch(() => {});
    return () => {
      active = false;
    };
  }, [item?.id, item?.content, editedBody]);

  async function submit(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const applicability = buildApplicabilityPayload(form);
      const payload = {
        title: form.title.trim(),
        content: form.content.trim(),
        category: form.category,
        applicability,
        ...(item?.version !== undefined ? { expected_version: item.version } : {})
      };
      const result = await mutate(`/api/content/${encodeURIComponent(item.id)}/correct`, 'POST', payload);
      const created = result?.new_content || result?.memory || result?.item || result;
      onSaved(created);
      onClose();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={t('纠正 / 更新说法')} onClose={onClose} busy={busy} wide initialFocus="textarea[required]">
      <form className="editor-form" onSubmit={submit}>
        <p className="promote-guidance">
          {t('保存后将生成一条新的有效记忆，并把原说法「{title}」自动转为已被替代的历史，不再注入日常上下文。', { title: item?.title || '' })}
        </p>
        <div className="form-columns">
          <Field label={t('更新后的标题')}>
            <input
              required
              value={form.title}
              onChange={event => setForm({ ...form, title: event.target.value })}
            />
          </Field>
          <Field label={t('分类')}>
            <select value={form.category} onChange={event => setForm({ ...form, category: event.target.value })}>
              {LONG_TERM_CATEGORIES.map(([key, label]) => (
                <option key={key} value={key}>{t(label)}</option>
              ))}
            </select>
          </Field>
        </div>

        <Field label={t('更新后的正确说法')}>
          <textarea
            required
            rows={6}
            value={form.content}
            onChange={event => {
              setEditedBody(true);
              setForm({ ...form, content: event.target.value });
            }}
          />
        </Field>

        <details open={Boolean(form.app_when || form.app_env || form.app_version_range || form.app_paths)}>
          <summary>{t('适用条件（可选：限制环境、版本或路径）')}</summary>
          <ApplicabilityEditorFields form={form} setForm={setForm} />
        </details>

        <ErrorNotice error={error} />
        <footer className="modal-actions">
          <Button onClick={onClose}>{t('取消')}</Button>
          <button
            type="submit"
            className="button button-primary"
            disabled={busy || !form.title.trim() || !form.content.trim()}
          >
            {busy ? t('正在提交…') : t('确认更新并替代旧说法')}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

export function ContentEditor({
  item,
  projectId,
  kind,
  onClose,
  onSaved
}) {
  const projects = useResource('/api/projects');
  const [form, setForm] = useState(() => contentForm(item, projectId, kind));
  const [version, setVersion] = useState(item?.version);
  const [latest, setLatest] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const categoryEntries = kind === 'memory' && item?.category !== 'task'
    ? LONG_TERM_CATEGORIES
    : Object.entries(CATEGORIES);
  const hadApplicability = Object.values(parseApplicability(item?.applicability)).some(v => (Array.isArray(v) ? v.length : Boolean(v)));

  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const isGlobal = form.project_id === '__global__';
      const parsedTags = form.tags.split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
      if (form.pinned && !parsedTags.includes('pinned')) parsedTags.push('pinned');
      const applicability = buildApplicabilityPayload(form);
      const hasNewApplicability = Object.keys(applicability).length > 0;
      const data = {
        project_id: isGlobal ? null : form.project_id,
        ...(isGlobal ? { scope: 'global' } : {}),
        pinned: Boolean(form.pinned),
        title: form.title.trim(),
        content: form.content,
        category: form.category,
        tags: parsedTags,
        ...(kind !== 'document' && (hasNewApplicability || hadApplicability) ? { applicability } : {})
      };
      if (item) data.expected_version = version;
      else if (kind) Object.assign(data, { kind });
      const saved = await mutate(item ? `/api/content/${item.id}` : '/api/content', item ? 'PATCH' : 'POST', data);
      onSaved(saved);
      onClose();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function compareLatest() {
    setBusy(true);
    try {
      setLatest(await request(`/api/content/${item.id}?include_versions=false`));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal
      title={item ? t('编辑内容') : kind === 'document' ? t('添加知识') : t('添加记忆')}
      onClose={onClose}
      busy={busy}
      initialFocus="input[required]"
      wide
    >
      <form onSubmit={save} className="editor-form">
        <Field label={t('标题')}>
          <input
            autoFocus
            required
            value={form.title}
            onChange={event => setForm({ ...form, title: event.target.value })}
          />
        </Field>
        <Field label={t('所属项目')} hint={t('换项目不会动磁盘文件，也不改原始聊天；选「全局通用」则跨所有项目生效。')}>
          <select
            required
            value={form.project_id}
            onChange={event => setForm({ ...form, project_id: event.target.value })}
          >
            <option value="" disabled>{t('选择项目')}</option>
            {kind !== 'document' ? <option value="__global__">{t('全局通用（跨所有项目生效的个人偏好/规则）')}</option> : null}
            {(projects.data || []).map(project => (
              <option key={project.id} value={project.id}>
                {project.name}{project.status === 'archived' ? t('（已归档）') : ''}
              </option>
            ))}
          </select>
        </Field>
        {projects.error ? (
          <>
            <ErrorNotice error={projects.error} />
            <Button onClick={projects.reload}>{t('重新读取项目')}</Button>
          </>
        ) : null}
        <div className="form-columns">
          <Field label={t('分类')}>
            <select
              value={form.category}
              onChange={event => setForm({ ...form, category: event.target.value })}
            >
              {categoryEntries.map(([key, label]) => (
                <option key={key} value={key}>{t(label)}</option>
              ))}
            </select>
          </Field>
          <Field label={t('标签')} hint={t('使用逗号分隔')}>
            <input
              value={form.tags}
              onChange={event => setForm({ ...form, tags: event.target.value })}
            />
          </Field>
        </div>
        {kind !== 'document' ? (
          <label className="checkbox-inline">
            <input
              type="checkbox"
              checked={Boolean(form.pinned)}
              onChange={event => setForm({ ...form, pinned: event.target.checked })}
            />
            <span>{t('设为常驻核心规则（每次上下文组装与跨端接棒优先注入受保护区）')}</span>
          </label>
        ) : null}
        {kind !== 'document' ? (
          <details open={hadApplicability}>
            <summary>{t('适用条件（可选：限制环境、版本或路径）')}</summary>
            <ApplicabilityEditorFields form={form} setForm={setForm} />
          </details>
        ) : null}
        <Field label={t('正文')} hint={t('支持 Markdown；来源和修改时间会自动记下来。')}>
          <textarea
            required
            rows={11}
            value={form.content}
            onChange={event => setForm({ ...form, content: event.target.value })}
          />
        </Field>
        <ErrorNotice error={error} />
        {error?.status === 409 ? (
          <div className="version-recovery">
            <p>{t('这条内容在别处被改过了。你的草稿还在，不会直接覆盖新版本。')}</p>
            <Button onClick={compareLatest}>{t('看看最新版本')}</Button>
          </div>
        ) : null}
        {latest ? (
          <section className="version-recovery">
            <h3>{t('最新版本')} {latest.version} · {latest.title}</h3>
            <Markdown>{latest.content}</Markdown>
            <p>{t('对照一下草稿和最新正文，再决定怎么办。')}</p>
            <div className="toolbar-actions">
              <Button
                onClick={() => {
                  setForm(contentForm(latest, projectId, kind));
                  setVersion(latest.version);
                  setLatest(null);
                  setError(null);
                }}
              >
                {t('放弃草稿，载入最新内容')}
              </Button>
              <Button
                onClick={() => {
                  setVersion(latest.version);
                  setLatest(null);
                  setError(null);
                }}
              >
                {t('比过了，保留草稿继续改')}
              </Button>
            </div>
          </section>
        ) : null}
        <footer className="modal-actions">
          <Button onClick={onClose}>{t('取消')}</Button>
          <button
            type="submit"
            className="button button-primary"
            disabled={
              busy ||
              projects.loading ||
              !!projects.error ||
              !form.project_id ||
              !form.title.trim() ||
              !form.content.trim() ||
              [404, 409, 410].includes(error?.status)
            }
          >
            {busy ? t('正在保存…') : t('保存内容')}
          </button>
        </footer>
      </form>
    </Modal>
  );
}

/** Keep provenance navigation local so returning never discards the originating filters. */
export function ContentDetail({
  id,
  projects,
  revision,
  onBack,
  onChanged,
  onProject,
  onOpenTasks
}) {
  const [stack, setStack] = useState([{ kind: 'content', id }]);
  const current = stack[stack.length - 1];
  function back() {
    if (stack.length > 1) setStack(previous => previous.slice(0, -1));
    else onBack();
  }
  function open(kind, nextId) {
    if (kind !== current.kind || nextId !== current.id) {
      setStack(previous => [...previous, { kind, id: nextId }]);
    }
  }
  return current.kind === 'conversation' ? (
    <ConversationDetail
      key={`conversation:${current.id}`}
      id={current.id}
      projects={projects}
      revision={revision}
      onBack={back}
      onChanged={onChanged}
      onProject={onProject}
      backLabel={t('返回来源内容')}
    />
  ) : (
    <ContentRecord
      key={current.id}
      id={current.id}
      projects={projects}
      revision={revision}
      onBack={back}
      backLabel={stack.length > 1 ? t('返回上一条内容') : t('返回列表')}
      onChanged={onChanged}
      onProject={onProject}
      onOpen={open}
      onOpenTasks={onOpenTasks}
    />
  );
}

function ContentRecord({
  id,
  projects,
  revision,
  onBack,
  backLabel,
  onChanged,
  onProject,
  onOpen,
  onOpenTasks
}) {
  const objectRevision = useObjectRevision(revision, 'content', id);
  const resource = useResource(`/api/content/${id}?include_versions=false`, objectRevision);
  const [editor, setEditor] = useState(null);
  const [correcting, setCorrecting] = useState(null);
  const [history, setHistory] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const item = resource.data;
  const unavailable = !!resource.error || resource.loading;
  const staleRestore = !!confirmation && confirmation.expectedVersion !== item?.version;

  function changed() {
    resource.reload();
    onChanged();
  }

  async function act() {
    if (busy || !confirmation || staleRestore) return;
    setBusy(true);
    setError(null);
    try {
      if (confirmation.action === 'delete') {
        await mutate(`/api/content/${id}?expected_version=${confirmation.expectedVersion}`, 'DELETE');
        onChanged();
        onBack();
      } else {
        await mutate(`/api/content/${id}/restore`, 'POST', {
          version: confirmation.version,
          expected_version: confirmation.expectedVersion
        });
        setConfirmation(null);
        changed();
      }
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) changed();
    } finally {
      setBusy(false);
    }
  }

  const owner = projects.find(project => project.id === item?.project_id);
  const itemPinned = isPinnedItem(item);
  const applicabilityTags = formatApplicabilityTags(item?.applicability);
  const usage = normalizeUsageSummary(item?.usage_summary);

  async function togglePinDetail() {
    if (busy || unavailable || !item) return;
    setBusy(true);
    setError(null);
    try {
      await mutate(`/api/content/${id}/pin`, 'POST', {
        pinned: !itemPinned,
        expected_version: item.version
      });
      changed();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function markOutdatedDetail() {
    if (busy || unavailable || !item) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      await mutate(`/api/content/${encodeURIComponent(id)}/outdated`, 'POST', {
        expected_version: item.version
      });
      changed();
      setNotice(t('已标记为过时，后续不会再注入日常上下文，历史记录仍保留。'));
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) changed();
    } finally {
      setBusy(false);
    }
  }

  const statusText = item?.status === 'historical'
    ? t('历史版本，不再作为当前依据')
    : item?.status === 'outdated'
      ? t('已过时，已退出日常注入')
      : t('当前有效');

  return (
    <article className="content-detail">
      <div className="detail-toolbar">
        <Button icon={ArrowLeft} onClick={onBack}>{backLabel}</Button>
        {item ? (
          <div>
            {item.kind !== 'document' ? (
              <Button icon={Pin} disabled={unavailable || busy} onClick={togglePinDetail} aria-pressed={itemPinned}>
                {itemPinned ? t('已常驻核心') : t('设为常驻')}
              </Button>
            ) : null}
            {item.kind !== 'document' ? (
              <Button
                icon={RefreshCw}
                disabled={unavailable || busy}
                onClick={() => {
                  setError(null);
                  setCorrecting(item);
                }}
              >
                {t('纠正 / 更新说法')}
              </Button>
            ) : null}
            {item.kind !== 'document' && item.status !== 'outdated' && item.status !== 'historical' ? (
              <Button icon={Archive} disabled={unavailable || busy} onClick={markOutdatedDetail}>
                {t('标记过时')}
              </Button>
            ) : null}
            <Button icon={History} onClick={() => setHistory(!history)} aria-pressed={history}>
              {t('修改历史')}
            </Button>
            <Button
              icon={Pencil}
              disabled={unavailable}
              onClick={() => {
                setError(null);
                setEditor(item);
              }}
            >
              {t('编辑与项目')}
            </Button>
            <Button
              icon={Trash2}
              disabled={unavailable}
              onClick={() => {
                setError(null);
                setConfirmation({
                  action: 'delete',
                  expectedVersion: item.version,
                  title: item.title
                });
              }}
              aria-label={t('永久删除内容')}
            />
          </div>
        ) : null}
      </div>

      <ErrorNotice error={error} />
      {notice ? <p className="operation-notice" role="status">{notice}</p> : null}

      <ResourceState resource={resource}>
        {item ? (
          <>
            <div className="reading-heading">
              <div className="meta-line">
                {itemPinned ? <Badge className="badge-pinned">{t('常驻核心')}</Badge> : null}
                <Badge>{t(CATEGORIES[item.category] || item.category)}</Badge>
                {!item.project_id ? <Badge>{t('全局通用')}</Badge> : null}
                {item.kind !== 'document' ? <UsageSummaryBadge usageSummary={item.usage_summary} /> : null}
                {item.origin_authority && ORIGIN_AUTHORITY_LABELS[item.origin_authority] ? (
                  <Badge>{t(ORIGIN_AUTHORITY_LABELS[item.origin_authority])}</Badge>
                ) : null}
                <span>{statusText}</span>
                {owner ? (
                  <Button onClick={() => onProject(owner.id)}>
                    {t('所属项目：')}{owner.name}{owner.status === 'archived' ? t('（已归档）') : ''}
                  </Button>
                ) : (
                  <span>{t('全局通用偏好')}</span>
                )}
              </div>

              {item.superseded_by_id || item.promoted_from_task_id || item.outdated_reason ? (
                <div className="lifecycle-lineage-banner">
                  {item.superseded_by_id ? (
                    <div className="lineage-item">
                      <span>{t('这条旧说法已被更新版本替代。')}</span>
                      <Button onClick={() => onOpen('content', item.superseded_by_id)}>
                        {t('查看替代后的新说法')}
                      </Button>
                    </div>
                  ) : null}
                  {item.promoted_from_task_id ? (
                    <div className="lineage-item">
                      <Badge className="badge-provenance">
                        {t('源自工作任务提炼：{id}', { id: item.promoted_from_task_id })}
                      </Badge>
                      {onOpenTasks ? (
                        <Button onClick={() => onOpenTasks(item.promoted_from_task_id)}>
                          {t('前往当前任务视图')}
                        </Button>
                      ) : null}
                    </div>
                  ) : null}
                  {item.outdated_reason ? (
                    <p className="muted">{t('过时说明：{reason}', { reason: item.outdated_reason })}</p>
                  ) : null}
                </div>
              ) : null}

              <h2>{item.title}</h2>
              <p>
                {AGENTS[item.agent] || item.agent || t('人工维护')} {t('· 创建于')} {formatTime(item.created_at)}{' '}
                {t('· 更新于')} {formatTime(item.updated_at)} {t('· 版本')} {item.version}
              </p>

              {applicabilityTags.length ? (
                <div className="applicability-tags" aria-label={t('适用条件')}>
                  {applicabilityTags.map(tag => (
                    <span key={tag.key} className="applicability-tag">{tag.label}</span>
                  ))}
                </div>
              ) : null}

              <div className="tags">
                {item.tags?.filter(tag => tag !== 'pinned').map(tag => <span key={tag}>#{tag}</span>)}
              </div>
            </div>

            <Markdown>{item.content}</Markdown>

            {item.kind !== 'document' ? (
              <section className="usage-receipt-section">
                <h3>{t('Agent 使用与引用回执')}</h3>
                <div className="usage-receipt-summary">
                  <UsageSummaryBadge usageSummary={item.usage_summary} />
                  <p className="muted">
                    {usage.state === 'cited'
                      ? t('Agent 已在实际工作中明确声明采用了这条记忆（累计明确引用 {cited} 次，提供 {provided} 次）。', {
                          cited: usage.cited_count || 1,
                          provided: Math.max(usage.provided_count || 0, usage.cited_count || 1)
                        })
                      : usage.state === 'provided'
                        ? t('已在上下文组装或检索中提供给 Agent {count} 次，尚未收到 Agent 明确声明引用。', {
                            count: usage.provided_count || 1
                          })
                        : t('已保存到本机记忆库，尚未在上下文包中提供给 Agent。')}
                  </p>
                </div>
                {usage.last_cited_note ? (
                  <blockquote className="citation-note-box">
                    <strong>
                      {t('最近引用说明（{agent}）：', {
                        agent: AGENTS[usage.last_cited_agent] || usage.last_cited_agent || t('未知 Agent')
                      })}
                    </strong>
                    <span>{usage.last_cited_note}</span>
                  </blockquote>
                ) : null}
                {usage.recent_events?.length ? (
                  <ul className="usage-events-list">
                    {usage.recent_events.map((ev, idx) => (
                      <li key={ev.id || idx}>
                        <Badge className={ev.event_type === 'cited' ? 'badge-usage-cited' : 'badge-usage-provided'}>
                          {ev.event_type === 'cited' ? t('明确引用') : t('提供上下文')}
                        </Badge>
                        <span>{AGENTS[ev.agent] || ev.agent || t('未知 Agent')}</span>
                        {ev.note ? <span className="usage-event-note">{ev.note}</span> : null}
                        <time>{formatTime(ev.created_at)}</time>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </section>
            ) : null}

            <section className="provenance">
              <h3>{t('内容来源')}</h3>
              {item.sources?.length ? (
                item.sources.map((source, index) => (
                  <div key={`${source.id}-${index}`} className="source-block">
                    <div className="source-row">
                      <FileText size={16} />
                      <span>{source.title || t('来源记录')}</span>
                      {source.agent ? <Badge>{AGENTS[source.agent] || source.agent}</Badge> : null}
                      {source.deleted ? (
                        <Badge>{t('原始记录已删除')}</Badge>
                      ) : !source.id ? (
                        <Badge>{t('来源标识不可用')}</Badge>
                      ) : (
                        <Button onClick={() => onOpen(source.kind === 'conversation' ? 'conversation' : 'content', source.id)}>
                          {source.kind === 'conversation' ? t('查看原话') : t('查看文档')}
                        </Button>
                      )}
                    </div>
                    {source.quote ? <blockquote className="source-quote">{source.quote}</blockquote> : null}
                  </div>
                ))
              ) : (
                <p>{t('未关联可追溯的原始会话或文档。')}</p>
              )}
              {item.related_ids?.length ? (
                <>
                  <h3>{t('关联内容')}</h3>
                  <div className="toolbar-actions">
                    {item.related_ids.map(relatedId => (
                      <RelatedContentLink key={relatedId} id={relatedId} revision={revision} onOpen={onOpen} />
                    ))}
                  </div>
                </>
              ) : null}
            </section>

            {history ? (
              <VersionHistory
                id={id}
                revision={objectRevision}
                currentVersion={item.version}
                unavailable={unavailable}
                onRestore={version => {
                  setError(null);
                  setConfirmation({ action: 'restore', version, expectedVersion: item.version, title: item.title });
                }}
              />
            ) : null}
          </>
        ) : null}
      </ResourceState>

      {editor ? <ContentEditor item={editor} kind={item?.kind} onClose={() => setEditor(null)} onSaved={changed} /> : null}
      {correcting ? (
        <CorrectContentModal
          item={correcting}
          onClose={() => setCorrecting(null)}
          onSaved={created => {
            changed();
            if (created?.id && created.id !== id) onOpen('content', created.id);
          }}
        />
      ) : null}
      {confirmation ? (
        <Modal
          title={confirmation.action === 'delete' ? t('永久删除这条内容？') : t('恢复到版本 {version}？', { version: confirmation.version })}
          busy={busy}
          onClose={() => setConfirmation(null)}
        >
          <p className="modal-copy">
            {confirmation.action === 'delete'
              ? t('「{title}」删除后无法恢复。关联的原始聊天不会被删除；Agent 已经取得的内容无法撤回。', { title: confirmation.title })
              : t('「{title}」的历史内容将成为新的有效版本，本次操作保留修改记录。', { title: confirmation.title })}
          </p>
          <ErrorNotice error={error} />
          {staleRestore || resource.error ? (
            <div className="version-recovery">
              <p>{t('内容状态已变化或无法确认，请返回详情重新读取后再操作。')}</p>
              <Button
                onClick={() => {
                  setConfirmation(null);
                  setError(null);
                  resource.reload();
                }}
              >
                {t('返回详情并刷新')}
              </Button>
            </div>
          ) : null}
          <footer className="modal-actions">
            <Button onClick={() => setConfirmation(null)}>{t('取消')}</Button>
            <Button
              variant={confirmation.action === 'delete' ? 'danger' : 'primary'}
              busy={busy}
              disabled={unavailable || staleRestore || [404, 409, 410].includes(error?.status)}
              onClick={act}
            >
              {confirmation.action === 'delete' ? t('永久删除') : t('确认恢复')}
            </Button>
          </footer>
        </Modal>
      ) : null}
    </article>
  );
}

function RelatedContentLink({ id, revision, onOpen }) {
  const resource = useResource(`/api/content/${encodeURIComponent(id)}?include_versions=false`, revision);
  return (
    <Button
      disabled={resource.loading || !!resource.error || !resource.data}
      onClick={() => onOpen('content', id)}
    >
      {resource.error ? t('关联内容暂不可用') : resource.data?.title || t('正在读取关联内容')}
    </Button>
  );
}

function VersionBody({ id, version }) {
  const resource = useResource(`/api/content/${id}/versions/${version}`);
  return (
    <ResourceState resource={resource}>
      <Markdown>{resource.data?.content}</Markdown>
    </ResourceState>
  );
}

function VersionHistory({ id, revision, currentVersion, unavailable, onRestore }) {
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState(null);
  const resource = useResource(`/api/content/${id}/versions?limit=20&offset=${offset}`, revision);
  return (
    <section className="version-list group">
      <h3>{t('修改历史')}</h3>
      <ResourceState resource={resource}>
        {resource.data?.items?.length ? (
          resource.data.items.map(version => (
            <div key={version.version}>
              <div>
                <strong>{t('版本')} {version.version} · {version.title}</strong>
                <small>{version.actor || t('人工维护')} · {formatTime(version.created_at)}</small>
                <Button
                  aria-expanded={expanded === version.version}
                  onClick={() => setExpanded(expanded === version.version ? null : version.version)}
                >
                  {expanded === version.version ? t('收起正文') : t('查看内容')}
                </Button>
                {expanded === version.version ? <VersionBody key={version.version} id={id} version={version.version} /> : null}
              </div>
              <Button
                disabled={unavailable || version.version === currentVersion}
                onClick={() => onRestore(version.version)}
              >
                {t('恢复此版本')}
              </Button>
            </div>
          ))
        ) : (
          <p>{t('尚无历史修改。')}</p>
        )}
      </ResourceState>
      <Pagination
        total={resource.data?.total}
        offset={offset}
        limit={20}
        loading={resource.loading}
        onChange={value => {
          setExpanded(null);
          setOffset(value);
        }}
      />
    </section>
  );
}

const MEMORY_PILLS = [
  { key: 'all', label: '全部', category: '', pinned: '' },
  { key: 'pinned', label: '常驻核心', category: '', pinned: 'true' },
  { key: 'preference', label: '偏好', category: 'preference', pinned: '' },
  { key: 'background', label: '背景', category: 'background', pinned: '' },
  { key: 'decision', label: '决定', category: 'decision', pinned: '' },
  { key: 'lesson', label: '经验', category: 'lesson', pinned: '' },
  { key: 'knowledge', label: '知识条目', category: 'knowledge', pinned: '' }
];

const MEMORY_STATUS_PILLS = [
  { key: 'active', label: '当前有效' },
  { key: 'outdated', label: '已过时' },
  { key: 'historical', label: '已被替代历史' },
  { key: 'all', label: '全部状态' }
];

export function KnowledgePage({
  projectId,
  projects,
  kind = 'memory',
  initialCategory = '',
  initialSection = '',
  initialTaskId = null,
  initialQuery = '',
  revision,
  onChanged,
  onProject
}) {
  const [memorySection, setMemorySection] = useState(() => (
    initialSection || (initialCategory === 'task' ? 'tasks' : 'long_term')
  ));
  const [focusedTaskId, setFocusedTaskId] = useState(initialTaskId);
  const [filters, setFilters] = useState(() => ({
    ...EMPTY_FILTERS,
    category: initialCategory && initialCategory !== 'task' ? initialCategory : '',
    query: initialQuery || ''
  }));
  const [scopeMode, setScopeMode] = useState(projectId ? 'project' : 'all');
  const [viewMode, setViewMode] = useState('list');
  const [copiedHandbook, setCopiedHandbook] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const [selected, setSelected] = useState([]);
  const [editing, setEditing] = useState(false);
  const [correctingItem, setCorrectingItem] = useState(null);
  const [deletingItem, setDeletingItem] = useState(null);
  const [batch, setBatch] = useState(null);
  const [batchValue, setBatchValue] = useState('');
  const [mergeTitle, setMergeTitle] = useState('');
  const [mergeContent, setMergeContent] = useState('');
  const [mergeProject, setMergeProject] = useState(projectId || projects[0]?.id || '');
  const [mergeCategory, setMergeCategory] = useState('knowledge');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [offset, setOffset] = useState(0);
  const [appliedQuery, setAppliedQuery] = useState(initialQuery || '');

  useEffect(() => {
    if (initialSection) setMemorySection(initialSection);
    else if (initialCategory === 'task') setMemorySection('tasks');
  }, [initialSection, initialCategory]);

  useEffect(() => {
    setFocusedTaskId(initialTaskId || null);
  }, [initialTaskId]);

  useEffect(() => {
    setFilters(previous => ({
      ...previous,
      category: initialCategory && initialCategory !== 'task' ? initialCategory : ''
    }));
    setOffset(0);
  }, [initialCategory]);

  useEffect(() => {
    const timer = setTimeout(() => {
      setAppliedQuery(filters.query);
      setOffset(0);
    }, 350);
    return () => clearTimeout(timer);
  }, [filters.query]);

  const invalidDates = filters.since && filters.until && filters.since > filters.until;
  const allProjects = scopeMode !== 'project';
  const statusQueryValue = kind === 'memory'
    ? (filters.status === 'all' ? 'all' : (filters.status || 'active'))
    : (filters.status === 'all' ? 'all' : filters.status);
  const excludeCategoryValue = kind === 'memory' ? 'task' : '';
  const path = invalidDates || selectedId ? null : '/api/content/index' + queryString({
    ...filters,
    status: statusQueryValue,
    exclude_category: excludeCategoryValue,
    query: appliedQuery,
    limit: 50,
    offset,
    kind,
    project_id: scopeMode === 'project' ? projectId : '',
    scope: scopeMode === 'global' ? 'global' : '',
    pinned_first: kind === 'memory' ? 'true' : ''
  });
  // Keep main content index as the first resource hook for stable path expectations.
  const resource = useResource(path, revision);
  const handbookPath = viewMode === 'handbook' && !selectedId
    ? `/api/content/handbook${queryString({ project_id: scopeMode === 'project' ? projectId : '', include_global: 'true' })}`
    : null;
  const handbookResource = useResource(handbookPath, revision);
  const workingTasksSummaryPath = kind === 'memory' && !selectedId
    ? '/api/working-tasks' + queryString({
        project_id: scopeMode === 'project' ? projectId : '',
        active_only: 'true'
      })
    : null;
  const workingTasksSummary = useResource(workingTasksSummaryPath, revision);

  const rawRows = resource.data?.items || [];
  const nonTaskRows = kind === 'memory' ? rawRows.filter(item => item.category !== 'task') : rawRows;
  const statusFilteredRows = kind === 'memory'
    ? nonTaskRows.filter(item => {
        if (filters.status === 'all') return true;
        if (filters.status === 'outdated') return item.status === 'outdated';
        if (filters.status === 'historical') return item.status === 'historical';
        return !item.status || item.status === 'active';
      })
    : nonTaskRows;
  const rows = kind === 'memory'
    ? [...statusFilteredRows].sort((a, b) => (isPinnedItem(b) ? 1 : 0) - (isPinnedItem(a) ? 1 : 0))
    : statusFilteredRows;

  const activeTaskList = Array.isArray(workingTasksSummary.data)
    ? workingTasksSummary.data
    : Array.isArray(workingTasksSummary.data?.items)
      ? workingTasksSummary.data.items
      : null;
  const projectActiveTaskCount = scopeMode === 'project' && projectId
    ? (projects.find(p => p.id === projectId)?.counts?.tasks ?? 0)
    : 0;
  const activeTaskCount = activeTaskList
    ? Math.max(activeTaskList.length, projectActiveTaskCount)
    : projectActiveTaskCount;
  const excludedCount = rawRows.length - statusFilteredRows.length;
  const effectiveTotal = resource.data?.total !== undefined
    ? Math.max(rows.length, resource.data.total - excludedCount)
    : undefined;
  const longTermCount = effectiveTotal !== undefined ? effectiveTotal : rows.length;

  const visibleSelection = selected.filter(id => rows.some(row => row.id === id));
  const batchStale = batch?.items.some(item => !rows.some(row => row.id === item.id && row.version === item.version && row.project_id === item.project_id && row.status === item.status));
  const filtered = Object.values(filters).some(Boolean);
  const activeStatusPill = filters.status || 'active';

  function changed() {
    resource.reload();
    workingTasksSummary.reload();
    if (viewMode === 'handbook') handbookResource.reload();
    onChanged();
    setSelected([]);
  }

  function filter(key, value) {
    setOffset(0);
    setFilters(previous => ({
      ...previous,
      [key]: value
    }));
    setSelected([]);
    setNotice('');
  }

  function selectMemoryPill(pill) {
    setOffset(0);
    setFilters(previous => ({
      ...previous,
      category: pill.category,
      pinned: pill.pinned
    }));
    setSelected([]);
    setNotice('');
  }

  async function toggleRowPin(event, item) {
    event.stopPropagation();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const nextPinned = !isPinnedItem(item);
      await mutate(`/api/content/${item.id}/pin`, 'POST', {
        pinned: nextPinned,
        expected_version: item.version
      });
      changed();
      setNotice(
        nextPinned
          ? t('已将「{title}」设为常驻核心规则。', { title: item.title })
          : t('已取消「{title}」的常驻状态。', { title: item.title })
      );
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function markRowOutdated(event, item) {
    event.stopPropagation();
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      await mutate(`/api/content/${encodeURIComponent(item.id)}/outdated`, 'POST', {
        expected_version: item.version
      });
      changed();
      setNotice(t('已将「{title}」标记为过时，后续不会再注入日常上下文，历史记录仍保留。', { title: item.title }));
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) resource.reload();
    } finally {
      setBusy(false);
    }
  }

  async function confirmSingleDelete() {
    if (busy || !deletingItem) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      await mutate(`/api/content/${encodeURIComponent(deletingItem.id)}?expected_version=${deletingItem.version}`, 'DELETE');
      const deletedTitle = deletingItem.title;
      setDeletingItem(null);
      changed();
      setNotice(t('已删除记忆「{title}」。', { title: deletedTitle }));
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) resource.reload();
    } finally {
      setBusy(false);
    }
  }

  async function consolidateMemories() {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      const res = await mutate('/api/content/consolidate', 'POST', {
        project_id: scopeMode === 'project' ? projectId : null,
        scope: scopeMode === 'global' ? 'global' : scopeMode === 'project' ? 'project' : 'all'
      });
      changed();
      if (res.merged_groups > 0 || res.archived_count > 0) {
        setNotice(
          t('已完成记忆整理：合并 {groups} 组相近记忆，归档 {archived} 条冗余/流水账条目。', {
            groups: res.merged_groups || 0,
            archived: res.archived_count || 0
          })
        );
      } else {
        setNotice(t('当前记忆库已保持精炼，未发现重复或碎片条目。'));
      }
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }

  async function copyHandbook() {
    if (!handbookResource.data?.markdown) return;
    try {
      await navigator.clipboard.writeText(handbookResource.data.markdown);
      setCopiedHandbook(true);
      setTimeout(() => setCopiedHandbook(false), 2000);
    } catch {
      setError(new Error(t('浏览器未允许复制，请手动选择复制。')));
    }
  }

  async function openBatch(action) {
    if (busy) return;
    setError(null);
    setBatchValue(action === 'recategorize' ? 'knowledge' : '');
    const items = rows.filter(row => visibleSelection.includes(row.id));
    const allGlobal = items.length > 0 && items.every(item => !item.project_id);
    const sharedCats = [...new Set(items.map(item => item.category).filter(c => c && c !== 'task'))];
    setMergeTitle('');
    setMergeContent('');
    setMergeProject(
      kind !== 'document' && (scopeMode === 'global' || allGlobal)
        ? '__global__'
        : projectId || items.find(item => item.project_id)?.project_id || projects[0]?.id || (kind !== 'document' ? '__global__' : '')
    );
    setMergeCategory(sharedCats.length === 1 ? sharedCats[0] : 'knowledge');
    if (action === 'merge') {
      setBusy(true);
      try {
        // Read only selected sources, and reject a preview made from mixed revisions.
        const bodies = [];
        for (const item of items) {
          const body = await request(`/api/content/${item.id}?include_versions=false`);
          if (body.version !== item.version) throw new Error(t('选中的内容有变化，请刷新列表后重新选择。'));
          bodies.push(body.content);
        }
        setMergeContent(bodies.join('\n\n'));
        setBatch({ action, items });
      } catch (failure) {
        setError(failure);
        resource.reload();
      } finally {
        setBusy(false);
      }
    } else setBatch({ action, items });
  }

  async function importFile(event) {
    const input = event.target;
    const file = input.files?.[0];
    if (!file || busy) return;
    setBusy(true);
    setError(null);
    setNotice('');
    const body = new FormData();
    body.append('file', file);
    if (projectId) body.append('project_id', projectId);
    try {
      const saved = await request('/api/import', {
        method: 'POST',
        body
      });
      changed();
      setNotice(t('已导入「{title}」。', { title: saved.title }));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
      input.value = '';
    }
  }

  async function applyBatch(event) {
    event.preventDefault();
    if (busy || batchStale || !batch?.items.length) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      const ids = batch.items.map(item => item.id);
      if (batch.action === 'merge') {
        const saved = await mutate('/api/content/merge', 'POST', {
          ids,
          expected_versions: Object.fromEntries(batch.items.map(item => [item.id, item.version])),
          title: mergeTitle.trim(),
          content: mergeContent,
          project_id: mergeProject === '__global__' ? null : mergeProject,
          category: mergeCategory
        });
        setNotice(t('已合并为「{title}」，原条目保留为历史。', { title: saved.title }));
        setSelectedId(saved.id);
      } else {
        const result = await mutate('/api/content/batch', 'POST', {
          ids,
          action: batch.action,
          expected_versions: Object.fromEntries(batch.items.map(item => [item.id, item.version])),
          ...(batch.action === 'retag'
            ? { tags: batchValue.split(/[,，]/).map(value => value.trim()).filter(Boolean) }
            : batch.action === 'recategorize'
              ? { category: batchValue }
              : {})
        });
        setNotice(
          t('已{action} {count} 项。', {
            action: batch.action === 'delete' ? t('永久删除') : t('更新'),
            count: result.count
          })
        );
      }
      setBatch(null);
      changed();
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) resource.reload();
    } finally {
      setBusy(false);
    }
  }

  if (selectedId) {
    return (
      <ContentDetail
        key={selectedId}
        id={selectedId}
        projects={projects}
        revision={revision}
        onProject={onProject}
        onOpenTasks={taskId => {
          setSelectedId(null);
          setFocusedTaskId(taskId || null);
          setMemorySection('tasks');
        }}
        onBack={() => {
          setSelectedId(null);
          resource.reload();
        }}
        onChanged={changed}
      />
    );
  }

  return (
    <section className="content-browser">
      {kind === 'memory' ? (
        <div className="memory-workbench-bar">
          <div className="memory-workbench-switch" role="tablist" aria-label={t('记忆分层工作台')}>
            <button
              type="button"
              role="tab"
              aria-selected={memorySection === 'tasks'}
              className={`workbench-tab ${memorySection === 'tasks' ? 'active' : ''}`}
              onClick={() => setMemorySection('tasks')}
            >
              <span>{t('当前任务')}</span>
              <span className="workbench-tab-count">{activeTaskCount}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={memorySection === 'long_term'}
              className={`workbench-tab ${memorySection === 'long_term' ? 'active' : ''}`}
              onClick={() => setMemorySection('long_term')}
            >
              <span>{t('长期记忆')}</span>
              <span className="workbench-tab-count">{longTermCount}</span>
            </button>
          </div>
          <p className="memory-workbench-caption">
            {memorySection === 'tasks'
              ? t('记录正在推进的任务目标、已完成依据、本次临时限制与下一步；暂停或完成后自动退出日常注入。')
              : t('管理跨对话长期生效的偏好、背景、决定与经验；支持真实引用回执、适用条件与纠错替代。')}
          </p>
        </div>
      ) : null}

      {kind === 'memory' && memorySection === 'tasks' ? (
        <WorkingTasksPanel
          projectId={scopeMode === 'project' ? projectId : ''}
          projects={projects}
          revision={revision}
          initialTaskId={focusedTaskId}
          onChanged={changed}
          onOpenMemory={memoryId => {
            setMemorySection('long_term');
            setSelectedId(memoryId);
          }}
        />
      ) : (
        <>
          <div className="collection-toolbar">
            <div>
              <h2>{kind === 'memory' ? t('项目记忆') : t('知识资料')}</h2>
              <p>
                {kind === 'memory'
                  ? t('分层管理常驻核心规则、使用偏好与项目经验；支持一键去重整理与 AGENTS.md 导出。')
                  : t('你的资料，也是每个 Agent 能直接用的背景。')}
              </p>
            </div>
            <div className="toolbar-actions">
              {kind === 'memory' ? (
                <SegmentedTabs label={t('记忆视图')}>
                  <button
                    type="button"
                    className={viewMode === 'list' ? 'active' : ''}
                    onClick={() => setViewMode('list')}
                  >
                    {t('认知记忆流')}
                  </button>
                  <button
                    type="button"
                    className={viewMode === 'handbook' ? 'active' : ''}
                    onClick={() => setViewMode('handbook')}
                  >
                    {t('AGENTS.md 规则手册')}
                  </button>
                </SegmentedTabs>
              ) : null}
              {kind === 'memory' ? (
                <Button icon={Sparkles} disabled={busy} onClick={consolidateMemories}>
                  {t('去重整理')}
                </Button>
              ) : null}
              {kind === 'document' && projectId ? (
                <label className={`button button-secondary ${busy ? 'disabled' : ''}`}>
                  <Upload size={16} />
                  {busy ? t('正在导入…') : t('导入文档')}
                  <input
                    aria-label={t('导入文档')}
                    className="file-input"
                    type="file"
                    disabled={busy}
                    accept=".md,.markdown,.txt,.json,.pdf,.doc,.docx"
                    onChange={importFile}
                  />
                </label>
              ) : null}
              <Button
                icon={Plus}
                variant="primary"
                disabled={busy || (!projects.length && kind === 'document')}
                onClick={() => {
                  setError(null);
                  setEditing(true);
                }}
              >
                {kind === 'memory' ? t('添加记忆') : t('添加知识')}
              </Button>
            </div>
          </div>

          {!projectId && kind === 'document' ? (
            <p className="muted">{t('导入文档前，请先打开其所属项目。此处可搜索和维护全部项目的资料。')}</p>
          ) : null}

          {viewMode === 'handbook' && kind === 'memory' ? (
            <section className="group handbook-panel">
              <div className="collection-toolbar">
                <div>
                  <h3><BookOpen size={16} /> {t('实时生成的 AGENTS.md / CLAUDE.md 规则手册')}</h3>
                  <p className="muted">
                    {t('自动汇总当前项目的常驻核心规则、全局使用习惯、架构约束与踩坑纪律，可直接粘贴至项目根目录。')}
                  </p>
                </div>
                <Button variant="primary" icon={copiedHandbook ? Check : Copy} onClick={copyHandbook}>
                  {copiedHandbook ? t('已复制规则手册') : t('复制 Markdown')}
                </Button>
              </div>
              <ResourceState resource={handbookResource}>
                {handbookResource.data ? (
                  <>
                    <div className="meta-line">
                      <Badge className="badge-pinned">{t('常驻核心')} {handbookResource.data.counts?.pinned || 0}</Badge>
                      <Badge>{t('全局通用')} {handbookResource.data.counts?.global || 0}</Badge>
                      <Badge>{t('项目专属')} {handbookResource.data.counts?.project || 0}</Badge>
                    </div>
                    <div className="handbook-preview">
                      <Markdown>{handbookResource.data.markdown}</Markdown>
                    </div>
                  </>
                ) : null}
              </ResourceState>
            </section>
          ) : (
            <>
              <div className="search-bar">
                <Search size={18} />
                <input
                  aria-label={t('搜索内容')}
                  placeholder={t('搜索标题、正文或关键词…')}
                  value={filters.query}
                  onChange={event => filter('query', event.target.value)}
                />
                <select
                  aria-label={t('搜索范围')}
                  value={scopeMode}
                  onChange={event => {
                    setScopeMode(event.target.value);
                    setOffset(0);
                    setSelected([]);
                  }}
                >
                  <option value="project" disabled={!projectId}>{t('当前项目')}</option>
                  <option value="all">{t('全部项目')}</option>
                  {kind === 'memory' ? <option value="global">{t('全局通用偏好')}</option> : null}
                </select>
              </div>

              {kind === 'memory' ? (
                <div className="memory-filter-bars">
                  <div className="memory-pill-bar" role="tablist" aria-label={t('记忆分类快筛')}>
                    {MEMORY_PILLS.map(pill => {
                      const active = filters.category === pill.category && filters.pinned === pill.pinned;
                      return (
                        <button
                          key={pill.key}
                          type="button"
                          role="tab"
                          aria-selected={active}
                          className={`memory-pill ${active ? 'active' : ''}`}
                          onClick={() => selectMemoryPill(pill)}
                        >
                          {t(pill.label)}
                        </button>
                      );
                    })}
                  </div>

                  <div className="memory-pill-bar" role="tablist" aria-label={t('记忆有效状态筛选')}>
                    {MEMORY_STATUS_PILLS.map(pill => {
                      const active = activeStatusPill === pill.key;
                      return (
                        <button
                          key={pill.key}
                          type="button"
                          role="tab"
                          aria-selected={active}
                          className={`memory-pill ${active ? 'active' : ''}`}
                          onClick={() => filter('status', pill.key === 'active' ? '' : pill.key)}
                        >
                          {t(pill.label)}
                        </button>
                      );
                    })}
                  </div>
                </div>
              ) : null}

              <details className="filter-details">
                <summary>{t('筛选内容')}{filtered ? t(' · 已应用') : ''}</summary>
                <div className="filter-grid">
                  <Field label={t('分类')}>
                    <select value={filters.category} onChange={event => filter('category', event.target.value)}>
                      <option value="">{t('所有分类')}</option>
                      {(kind === 'memory' ? LONG_TERM_CATEGORIES : Object.entries(CATEGORIES)).map(([key, label]) => (
                        <option key={key} value={key}>{t(label)}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label={t('来源 Agent')}>
                    <select value={filters.agent} onChange={event => filter('agent', event.target.value)}>
                      <option value="">{t('所有来源')}</option>
                      {Object.entries(AGENTS).map(([key, label]) => (
                        <option key={key} value={key}>{label}</option>
                      ))}
                    </select>
                  </Field>
                  <Field label={t('标签')}>
                    <input value={filters.tag} onChange={event => filter('tag', event.target.value)} />
                  </Field>
                  <Field label={t('创建日期 · 从')}>
                    <input
                      type="date"
                      max={filters.until || undefined}
                      aria-invalid={invalidDates ? true : undefined}
                      value={filters.since}
                      onChange={event => filter('since', event.target.value)}
                    />
                  </Field>
                  <Field label={t('创建日期 · 至')}>
                    <input
                      type="date"
                      min={filters.since || undefined}
                      aria-invalid={invalidDates ? true : undefined}
                      value={filters.until}
                      onChange={event => filter('until', event.target.value)}
                    />
                  </Field>
                  <Field label={t('有效状态')}>
                    {kind === 'memory' ? (
                      <select value={filters.status} onChange={event => filter('status', event.target.value)}>
                        <option value="">{t('当前有效')}</option>
                        <option value="outdated">{t('已过时')}</option>
                        <option value="historical">{t('已被替代历史')}</option>
                        <option value="all">{t('全部状态')}</option>
                      </select>
                    ) : (
                      <select value={filters.status} onChange={event => filter('status', event.target.value)}>
                        <option value="">{t('全部状态')}</option>
                        <option value="active">{t('当前有效')}</option>
                        <option value="outdated">{t('已过时')}</option>
                        <option value="historical">{t('已被替代历史')}</option>
                      </select>
                    )}
                  </Field>
                </div>
              </details>

              {filtered ? (
                <Button
                  onClick={() => {
                    setFilters(EMPTY_FILTERS);
                    setOffset(0);
                    setSelected([]);
                  }}
                >
                  {t('清除筛选')}
                </Button>
              ) : null}

              <ErrorNotice error={error} />
              {notice ? <p className="operation-notice" role="status">{notice}</p> : null}

              {visibleSelection.length ? (
                <div className="batch-bar">
                  <span>{t('已选 {count} 项', { count: visibleSelection.length })}</span>
                  <Button disabled={busy || resource.loading || !!resource.error} onClick={() => openBatch('retag')}>
                    {t('调整标签')}
                  </Button>
                  <Button disabled={busy || resource.loading || !!resource.error} onClick={() => openBatch('recategorize')}>
                    {t('调整分类')}
                  </Button>
                  {kind === 'memory' ? (
                    <Button
                      icon={GitMerge}
                      disabled={busy || visibleSelection.length < 2 || resource.loading || !!resource.error}
                      onClick={() => openBatch('merge')}
                    >
                      {t('合并')}
                    </Button>
                  ) : null}
                  <Button
                    icon={Trash2}
                    disabled={busy || resource.loading || !!resource.error}
                    onClick={() => openBatch('delete')}
                  >
                    {t('删除')}
                  </Button>
                  <Button onClick={() => setSelected([])}>{t('取消选择')}</Button>
                </div>
              ) : null}

              {invalidDates ? (
                <p className="inline-error" role="alert">{t('开始日期不能晚于结束日期，改一下再搜。')}</p>
              ) : null}

              <div hidden={!!invalidDates}>
                <ResourceState resource={resource}>
                  {rows.length ? (
                    <div className="content-list group">
                      {rows.map(item => {
                        const pinned = isPinnedItem(item);
                        const applicabilityTags = formatApplicabilityTags(item.applicability);
                        const firstQuote = item.sources?.find(src => src.quote)?.quote;
                        return (
                          <div className={`content-row ${pinned ? 'content-row-pinned' : ''}`} key={item.id}>
                            <label className="row-select">
                              <input
                                aria-label={t('选择 {title}', { title: item.title })}
                                type="checkbox"
                                checked={visibleSelection.includes(item.id)}
                                onChange={event => setSelected(previous => (
                                  event.target.checked
                                    ? [...previous, item.id]
                                    : previous.filter(id => id !== item.id)
                                ))}
                              />
                            </label>

                            <button className="content-row-main" onClick={() => setSelectedId(item.id)}>
                              <div className="meta-line">
                                {pinned ? <Badge className="badge-pinned">{t('常驻核心')}</Badge> : null}
                                <Badge>{t(CATEGORIES[item.category] || item.category)}</Badge>
                                {!item.project_id && kind === 'memory' ? <Badge>{t('全局通用')}</Badge> : null}
                                {kind === 'memory' ? <UsageSummaryBadge usageSummary={item.usage_summary} /> : null}
                                {item.status === 'outdated' ? (
                                  <Badge className="badge-status-outdated">{t('已过时')}</Badge>
                                ) : null}
                                {item.status === 'historical' ? (
                                  <Badge className="badge-status-historical">{t('历史内容')}</Badge>
                                ) : null}
                                {item.promoted_from_task_id ? (
                                  <Badge className="badge-provenance">{t('源自任务提炼')}</Badge>
                                ) : null}
                                {item.superseded_by_id ? (
                                  <Badge className="badge-provenance">{t('已有新说法替代')}</Badge>
                                ) : null}
                                {item.version > 1 ? <span>v{item.version}</span> : null}
                                {allProjects ? (
                                  <span>{projects.find(project => project.id === item.project_id)?.name || t('全局通用')}</span>
                                ) : null}
                              </div>

                              <h3>{item.title}</h3>
                              <p>{item.content_preview || item.content}</p>

                              {applicabilityTags.length ? (
                                <div className="applicability-tags">
                                  {applicabilityTags.map(tag => (
                                    <span key={tag.key} className="applicability-tag">{tag.label}</span>
                                  ))}
                                </div>
                              ) : null}

                              {firstQuote ? (
                                <p className="row-source-quote">{t('来源依据：')}{firstQuote}</p>
                              ) : null}

                              <small>
                                {AGENTS[item.agent] || item.agent || t('人工维护')} <span>·</span> {formatTime(item.updated_at)}
                              </small>
                            </button>

                            {kind === 'memory' ? (
                              <div className="row-quick-actions">
                                <button
                                  type="button"
                                  className="row-action-pill"
                                  disabled={busy}
                                  onClick={event => {
                                    event.stopPropagation();
                                    setError(null);
                                    setCorrectingItem(item);
                                  }}
                                >
                                  {t('纠正说法')}
                                </button>
                                {item.status !== 'outdated' && item.status !== 'historical' ? (
                                  <button
                                    type="button"
                                    className="row-action-pill"
                                    disabled={busy}
                                    onClick={event => markRowOutdated(event, item)}
                                  >
                                    {t('标记过时')}
                                  </button>
                                ) : null}
                                <button
                                  type="button"
                                  className={`pin-toggle-btn ${pinned ? 'active' : ''}`}
                                  title={pinned ? t('取消常驻核心') : t('设为常驻核心规则')}
                                  aria-label={pinned ? t('取消常驻核心') : t('设为常驻核心规则')}
                                  onClick={event => toggleRowPin(event, item)}
                                >
                                  <Pin size={14} />
                                </button>
                                <button
                                  type="button"
                                  className="pin-toggle-btn"
                                  title={t('删除记忆：{title}', { title: item.title })}
                                  aria-label={t('删除记忆：{title}', { title: item.title })}
                                  disabled={busy}
                                  onClick={event => {
                                    event.stopPropagation();
                                    setError(null);
                                    setDeletingItem(item);
                                  }}
                                >
                                  <Trash2 size={14} />
                                </button>
                              </div>
                            ) : null}

                            <ChevronRight size={15} className="row-chevron" aria-hidden="true" />
                          </div>
                        );
                      })}
                    </div>
                  ) : (
                    <EmptyState
                      title={filtered ? t('没有找到匹配内容') : kind === 'memory' ? t('还没有沉淀的记忆') : t('给项目补充一点知识')}
                      body={
                        filtered
                          ? t('可以清掉分类、来源、日期这些筛选，或者切到全部项目再搜；归档项目也算在内。')
                          : kind === 'memory'
                            ? t('采集了不等于已经整理成记忆。可以到「相关聊天」看处理到哪一步、为什么失败，也可以自己补一条。')
                            : t('这里只显示真正存下来的内容。可以自己写一条，也可以导入文档。')
                      }
                      action={
                        filtered ? (
                          <Button
                            onClick={() => {
                              setFilters(EMPTY_FILTERS);
                              setOffset(0);
                              setSelected([]);
                            }}
                          >
                            {t('清除筛选')}
                          </Button>
                        ) : null
                      }
                    />
                  )}
                </ResourceState>
                <Pagination
                  total={effectiveTotal}
                  offset={offset}
                  limit={50}
                  loading={resource.loading}
                  onChange={value => {
                    setOffset(value);
                    setSelected([]);
                  }}
                />
              </div>
            </>
          )}
        </>
      )}

      {editing ? (
        <ContentEditor
          projectId={projectId}
          kind={kind}
          onClose={() => setEditing(false)}
          onSaved={saved => {
            changed();
            setSelectedId(saved.id);
          }}
        />
      ) : null}

      {correctingItem ? (
        <CorrectContentModal
          item={correctingItem}
          onClose={() => setCorrectingItem(null)}
          onSaved={created => {
            changed();
            setNotice(t('已更新说法并生成新记忆「{title}」，原条目已转为历史。', { title: created?.title || correctingItem.title }));
          }}
        />
      ) : null}

      {deletingItem ? (
        <Modal
          title={t('永久删除这条内容？')}
          busy={busy}
          onClose={() => {
            setError(null);
            setDeletingItem(null);
          }}
        >
          <p className="modal-copy">
            {t('「{title}」删除后无法恢复。关联的原始聊天不会被删除；Agent 已经取得的内容无法撤回。', { title: deletingItem.title })}
          </p>
          <ErrorNotice error={error} />
          {[404, 409, 410].includes(error?.status) ? (
            <div className="version-recovery">
              <p>{t('内容状态已变化或无法确认，请返回详情重新读取后再操作。')}</p>
              <Button
                onClick={() => {
                  setError(null);
                  setDeletingItem(null);
                  resource.reload();
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
                setDeletingItem(null);
              }}
            >
              {t('取消')}
            </Button>
            <Button
              variant="danger"
              busy={busy}
              disabled={[404, 409, 410].includes(error?.status)}
              onClick={confirmSingleDelete}
            >
              {t('永久删除')}
            </Button>
          </footer>
        </Modal>
      ) : null}

      {batch ? (
        <Modal
          title={
            batch.action === 'delete'
              ? t('永久删除 {count} 项？', { count: batch.items.length })
              : batch.action === 'merge'
                ? t('合并选中记忆')
                : batch.action === 'retag'
                  ? t('批量调整标签')
                  : t('批量调整分类')
          }
          onClose={() => setBatch(null)}
          busy={busy}
          wide={batch.action === 'merge'}
        >
          <form className="editor-form" onSubmit={applyBatch}>
            <details>
              <summary>{t('本次操作的 {count} 项内容', { count: batch.items.length })}</summary>
              <ul className="confirmation-targets">
                {batch.items.map(item => (
                  <li key={item.id}>
                    {item.title} · {!item.project_id ? t('全局通用') : (projects.find(project => project.id === item.project_id)?.name || t('项目待定'))}
                  </li>
                ))}
              </ul>
            </details>
            {batch.action === 'delete' ? (
              <p>{t('此操作无法恢复，不会删除关联的原始聊天。')}</p>
            ) : batch.action === 'merge' ? (
              <>
                <p>{t('合并成一条新记忆，原条目留作历史，不再作为当前依据。请核对正文与所属项目。')}</p>
                <Field label={t('合并后的所属项目')}>
                  <select value={mergeProject} required onChange={event => setMergeProject(event.target.value)}>
                    {kind !== 'document' ? (
                      <option value="__global__">{t('全局通用（跨所有项目生效的个人偏好/规则）')}</option>
                    ) : null}
                    {projects.map(project => (
                      <option key={project.id} value={project.id}>
                        {project.name}{project.status === 'archived' ? t('（已归档）') : ''}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label={t('分类')}>
                  <select value={mergeCategory} onChange={event => setMergeCategory(event.target.value)}>
                    {LONG_TERM_CATEGORIES.map(([key, label]) => (
                      <option key={key} value={key}>{t(label)}</option>
                    ))}
                  </select>
                </Field>
                <Field label={t('合并后的标题')}>
                  <input required value={mergeTitle} onChange={event => setMergeTitle(event.target.value)} />
                </Field>
                <Field label={t('合并后的正文')}>
                  <textarea required rows={10} value={mergeContent} onChange={event => setMergeContent(event.target.value)} />
                </Field>
              </>
            ) : batch.action === 'retag' ? (
              <Field label={t('新标签（逗号分隔）')} hint={t('替换原有标签；留空会清除标签。')}>
                <input value={batchValue} onChange={event => setBatchValue(event.target.value)} />
              </Field>
            ) : (
              <Field label={t('分类')}>
                <select value={batchValue} onChange={event => setBatchValue(event.target.value)}>
                  {LONG_TERM_CATEGORIES.map(([key, label]) => (
                    <option key={key} value={key}>{t(label)}</option>
                  ))}
                </select>
              </Field>
            )}
            <ErrorNotice error={error} />
            {batchStale || resource.error ? (
              <div className="version-recovery">
                <p>{t('选中的内容有变化，或者状态读不到。系统没有替你缩小范围，请重新选。')}</p>
                <Button
                  onClick={() => {
                    setBatch(null);
                    setSelected([]);
                    resource.reload();
                  }}
                >
                  {t('返回列表并刷新')}
                </Button>
              </div>
            ) : null}
            <footer className="modal-actions">
              <Button onClick={() => setBatch(null)}>{t('取消')}</Button>
              <button
                type="submit"
                className={`button button-${batch.action === 'delete' ? 'danger' : 'primary'}`}
                disabled={
                  busy ||
                  batchStale ||
                  resource.loading ||
                  !!resource.error ||
                  (batch.action === 'merge' && (!mergeProject || !mergeTitle.trim() || !mergeContent.trim()))
                }
              >
                {busy ? t('正在提交…') : batch.action === 'delete' ? t('确认永久删除') : t('确认保存')}
              </button>
            </footer>
          </form>
        </Modal>
      ) : null}
    </section>
  );
}
