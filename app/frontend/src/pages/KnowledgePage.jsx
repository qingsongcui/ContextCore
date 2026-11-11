import { ChevronRight, Copy, Check, Pin, Sparkles, BookOpen } from 'lucide-react';
import { t } from "../lib/i18n.jsx";
import { useEffect, useState } from 'react';
import { ArrowLeft, FileText, GitMerge, History, Pencil, Plus, Search, Trash2, Upload } from 'lucide-react';
import { mutate, request } from '../lib/api.js';
import { queryString, useResource } from '../lib/useResource.js';
import { AGENTS, Badge, Button, CATEGORIES, EmptyState, ErrorNotice, Field, Modal, ResourceState, SegmentedTabs, formatTime } from '../components/ui.jsx';
import { Pagination } from '../components/Pagination.jsx';
import { useObjectRevision } from '../lib/objectRefresh.js';
import { Markdown } from '../components/Markdown.jsx';
import { ConversationDetail } from './ConversationsPage.jsx';
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
const isPinnedItem = item => Boolean(item?.pinned || (Array.isArray(item?.tags) && item.tags.includes('pinned')));
const contentForm = (item, projectId) => ({
  project_id: item ? (item.project_id || '__global__') : (projectId || ''),
  title: item?.title || '',
  content: item?.content || '',
  category: item?.category || 'knowledge',
  tags: (item?.tags || []).filter(tag => tag !== 'pinned').join(', ') || '',
  pinned: isPinnedItem(item)
});
export function ContentEditor({
  item,
  projectId,
  kind,
  onClose,
  onSaved
}) {
  const projects = useResource('/api/projects');
  const [form, setForm] = useState(() => contentForm(item, projectId));
  const [version, setVersion] = useState(item?.version);
  const [latest, setLatest] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  async function save(event) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const isGlobal = form.project_id === '__global__';
      const parsedTags = form.tags.split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
      if (form.pinned && !parsedTags.includes('pinned')) parsedTags.push('pinned');
      const data = {
        ...form,
        project_id: isGlobal ? null : form.project_id,
        ...(isGlobal ? { scope: 'global' } : {}),
        pinned: Boolean(form.pinned),
        title: form.title.trim(),
        tags: parsedTags
      };
      if (item) data.expected_version = version;else Object.assign(data, {
        kind
      });
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
  return <Modal title={item ? t("编辑内容") : kind === 'document' ? t("添加知识") : t("添加记忆")} onClose={onClose} busy={busy} initialFocus="input[required]" wide><form onSubmit={save} className="editor-form">
    <Field label={t("标题")}><input autoFocus required value={form.title} onChange={event => setForm({
          ...form,
          title: event.target.value
        })} /></Field>
    <Field label={t("所属项目")} hint={t("换项目不会动磁盘文件，也不改原始聊天；选「全局通用」则跨所有项目生效。")}><select required value={form.project_id} onChange={event => setForm({
          ...form,
          project_id: event.target.value
        })}><option value="" disabled>{t("选择项目")}</option>{kind !== 'document' ? <option value="__global__">{t("全局通用（跨所有项目生效的个人偏好/规则）")}</option> : null}{(projects.data || []).map(project => <option key={project.id} value={project.id}>{project.name}{project.status === 'archived' ? t("（已归档）") : ''}</option>)}</select></Field>
    {projects.error ? <><ErrorNotice error={projects.error} /><Button onClick={projects.reload}>{t("重新读取项目")}</Button></> : null}
    <div className="form-columns"><Field label={t("分类")}><select value={form.category} onChange={event => setForm({
            ...form,
            category: event.target.value
          })}>{Object.entries(CATEGORIES).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}</select></Field><Field label={t("标签")} hint={t("使用逗号分隔")}><input value={form.tags} onChange={event => setForm({
            ...form,
            tags: event.target.value
          })} /></Field></div>
    {kind !== 'document' ? <label className="checkbox-inline"><input type="checkbox" checked={Boolean(form.pinned)} onChange={event => setForm({
          ...form,
          pinned: event.target.checked
        })} /><span>{t("设为常驻核心规则（每次上下文组装与跨端接棒优先注入受保护区）")}</span></label> : null}
    <Field label={t("正文")} hint={t("支持 Markdown；来源和修改时间会自动记下来。")}><textarea required rows={13} value={form.content} onChange={event => setForm({
          ...form,
          content: event.target.value
        })} /></Field>
    <ErrorNotice error={error} />
    {error?.status === 409 ? <div className="version-recovery"><p>{t("这条内容在别处被改过了。你的草稿还在，不会直接覆盖新版本。")}</p><Button onClick={compareLatest}>{t("看看最新版本")}</Button></div> : null}
    {latest ? <section className="version-recovery"><h3>{t("最新版本")} {latest.version} · {latest.title}</h3><Markdown>{latest.content}</Markdown><p>{t("对照一下草稿和最新正文，再决定怎么办。")}</p><div className="toolbar-actions"><Button onClick={() => {
            setForm(contentForm(latest));
            setVersion(latest.version);
            setLatest(null);
            setError(null);
          }}>{t("放弃草稿，载入最新内容")}</Button><Button onClick={() => {
            setVersion(latest.version);
            setLatest(null);
            setError(null);
          }}>{t("比过了，保留草稿继续改")}</Button></div></section> : null}
    <footer className="modal-actions"><Button onClick={onClose}>{t("取消")}</Button><button type="submit" className="button button-primary" disabled={busy || projects.loading || !!projects.error || !form.project_id || !form.title.trim() || !form.content.trim() || [404, 409, 410].includes(error?.status)}>{busy ? t("正在保存…") : t("保存内容")}</button></footer>
  </form></Modal>;
}

/** Keep provenance navigation local so returning never discards the originating filters. */
export function ContentDetail({
  id,
  projects,
  revision,
  onBack,
  onChanged,
  onProject
}) {
  const [stack, setStack] = useState([{
    kind: 'content',
    id
  }]);
  const current = stack[stack.length - 1];
  function back() {
    if (stack.length > 1) setStack(previous => previous.slice(0, -1));else onBack();
  }
  function open(kind, nextId) {
    if (kind !== current.kind || nextId !== current.id) setStack(previous => [...previous, {
      kind,
      id: nextId
    }]);
  }
  return current.kind === 'conversation' ? <ConversationDetail key={`conversation:${current.id}`} id={current.id} projects={projects} revision={revision} onBack={back} onChanged={onChanged} onProject={onProject} backLabel={t("返回来源内容")} /> : <ContentRecord key={current.id} id={current.id} projects={projects} revision={revision} onBack={back} backLabel={stack.length > 1 ? t("返回上一条内容") : t("返回列表")} onChanged={onChanged} onProject={onProject} onOpen={open} />;
}
function ContentRecord({
  id,
  projects,
  revision,
  onBack,
  backLabel,
  onChanged,
  onProject,
  onOpen
}) {
  const objectRevision = useObjectRevision(revision, 'content', id);
  const resource = useResource(`/api/content/${id}?include_versions=false`, objectRevision);
  const [editor, setEditor] = useState(null);
  const [history, setHistory] = useState(false);
  const [confirmation, setConfirmation] = useState(null);
  const [error, setError] = useState(null);
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
  return <article className="content-detail">
    <div className="detail-toolbar"><Button icon={ArrowLeft} onClick={onBack}>{backLabel}</Button>{item ? <div>{item.kind !== 'document' ? <Button icon={Pin} disabled={unavailable || busy} onClick={togglePinDetail} aria-pressed={itemPinned}>{itemPinned ? t("已常驻核心") : t("设为常驻")}</Button> : null}<Button icon={History} onClick={() => setHistory(!history)} aria-pressed={history}>{t("修改历史")}</Button><Button icon={Pencil} disabled={unavailable} onClick={() => {
          setError(null);
          setEditor(item);
        }}>{t("编辑与项目")}</Button><Button icon={Trash2} disabled={unavailable} onClick={() => {
          setError(null);
          setConfirmation({
            action: 'delete',
            expectedVersion: item.version,
            title: item.title
          });
        }} aria-label={t("永久删除内容")} /></div> : null}</div>
    <ResourceState resource={resource}>{item ? <>
      <div className="reading-heading"><div className="meta-line">{itemPinned ? <Badge className="badge-pinned">{t("常驻核心")}</Badge> : null}<Badge>{t(CATEGORIES[item.category] || item.category)}</Badge>{!item.project_id ? <Badge>{t("全局通用")}</Badge> : null}<span>{item.status === 'historical' ? t("历史版本，不再作为当前依据") : t("当前有效")}</span>{owner ? <Button onClick={() => onProject(owner.id)}>{t("所属项目：")}{owner.name}{owner.status === 'archived' ? t("（已归档）") : ''}</Button> : <span>{t("全局通用偏好")}</span>}</div><h2>{item.title}</h2><p>{AGENTS[item.agent] || item.agent || t("人工维护")} {t("· 创建于")} {formatTime(item.created_at)} {t("· 更新于")} {formatTime(item.updated_at)} {t("· 版本")} {item.version}</p><div className="tags">{item.tags?.filter(tag => tag !== 'pinned').map(tag => <span key={tag}>#{tag}</span>)}</div></div>
      <Markdown>{item.content}</Markdown>
      <section className="provenance"><h3>{t("内容来源")}</h3>
        {item.sources?.length ? item.sources.map((source, index) => <div key={`${source.id}-${index}`} className="source-row"><FileText size={16} /><span>{source.title || t("来源记录")}</span>{source.agent ? <span>{AGENTS[source.agent] || source.agent}</span> : null}{source.deleted ? <Badge>{t("原始记录已删除")}</Badge> : !source.id ? <Badge>{t("来源标识不可用")}</Badge> : <Button onClick={() => onOpen(source.kind === 'conversation' ? 'conversation' : 'content', source.id)}>{source.kind === 'conversation' ? t("查看原话") : t("查看文档")}</Button>}</div>) : <p>{t("未关联可追溯的原始会话或文档。")}</p>}
        {item.related_ids?.length ? <><h3>{t("关联内容")}</h3><div className="toolbar-actions">{item.related_ids.map(relatedId => <RelatedContentLink key={relatedId} id={relatedId} revision={revision} onOpen={onOpen} />)}</div></> : null}
      </section>
      {history ? <VersionHistory id={id} revision={objectRevision} currentVersion={item.version} unavailable={unavailable} onRestore={version => {
        setError(null);
        setConfirmation({ action: 'restore', version, expectedVersion: item.version, title: item.title });
      }} /> : null}
    </> : null}</ResourceState>
    {editor ? <ContentEditor item={editor} onClose={() => setEditor(null)} onSaved={changed} /> : null}
    {confirmation ? <Modal title={confirmation.action === 'delete' ? t("永久删除这条内容？") : t("恢复到版本 {version}？", { version: confirmation.version })} busy={busy} onClose={() => setConfirmation(null)}><p className="modal-copy">{confirmation.action === 'delete' ? t("「{title}」删除后无法恢复。关联的原始聊天不会被删除；Agent 已经取得的内容无法撤回。", { title: confirmation.title }) : t("「{title}」的历史内容将成为新的有效版本，本次操作保留修改记录。", { title: confirmation.title })}</p><ErrorNotice error={error} />{staleRestore || resource.error ? <div className="version-recovery"><p>{t("内容状态已变化或无法确认，请返回详情重新读取后再操作。")}</p><Button onClick={() => {
          setConfirmation(null);
          setError(null);
          resource.reload();
        }}>{t("返回详情并刷新")}</Button></div> : null}<footer className="modal-actions"><Button onClick={() => setConfirmation(null)}>{t("取消")}</Button><Button variant={confirmation.action === 'delete' ? 'danger' : 'primary'} busy={busy} disabled={unavailable || staleRestore || [404, 409, 410].includes(error?.status)} onClick={act}>{confirmation.action === 'delete' ? t("永久删除") : t("确认恢复")}</Button></footer></Modal> : null}
  </article>;
}
function RelatedContentLink({ id, revision, onOpen }) {
  const resource = useResource(`/api/content/${encodeURIComponent(id)}?include_versions=false`, revision);
  return <Button disabled={resource.loading || !!resource.error || !resource.data} onClick={() => onOpen('content', id)}>{resource.error ? t('关联内容暂不可用') : resource.data?.title || t('正在读取关联内容')}</Button>;
}
function VersionBody({ id, version }) {
  const resource = useResource(`/api/content/${id}/versions/${version}`);
  return <ResourceState resource={resource}><Markdown>{resource.data?.content}</Markdown></ResourceState>;
}
function VersionHistory({ id, revision, currentVersion, unavailable, onRestore }) {
  const [offset, setOffset] = useState(0);
  const [expanded, setExpanded] = useState(null);
  const resource = useResource(`/api/content/${id}/versions?limit=20&offset=${offset}`, revision);
  return <section className="version-list group"><h3>{t('修改历史')}</h3><ResourceState resource={resource}>{resource.data?.items?.length ? resource.data.items.map(version => <div key={version.version}><div><strong>{t('版本')} {version.version} · {version.title}</strong><small>{version.actor || t('人工维护')} · {formatTime(version.created_at)}</small><Button aria-expanded={expanded === version.version} onClick={() => setExpanded(expanded === version.version ? null : version.version)}>{expanded === version.version ? t('收起正文') : t('查看内容')}</Button>{expanded === version.version ? <VersionBody key={version.version} id={id} version={version.version} /> : null}</div><Button disabled={unavailable || version.version === currentVersion} onClick={() => onRestore(version.version)}>{t('恢复此版本')}</Button></div>) : <p>{t('尚无历史修改。')}</p>}</ResourceState><Pagination total={resource.data?.total} offset={offset} limit={20} loading={resource.loading} onChange={value => { setExpanded(null); setOffset(value); }} /></section>;
}
const MEMORY_PILLS = [
  { key: 'all', label: '全部', category: '', pinned: '' },
  { key: 'pinned', label: '常驻核心', category: '', pinned: 'true' },
  { key: 'preference', label: '使用习惯', category: 'preference', pinned: '' },
  { key: 'decision', label: '决定与约束', category: 'decision', pinned: '' },
  { key: 'lesson', label: '踩坑经验', category: 'lesson', pinned: '' },
  { key: 'task', label: '任务进度', category: 'task', pinned: '' },
  { key: 'background', label: '项目背景', category: 'background', pinned: '' },
  { key: 'knowledge', label: '知识条目', category: 'knowledge', pinned: '' }
];
export function KnowledgePage({
  projectId,
  projects,
  kind = 'memory',
  initialCategory = '',
  initialQuery = '',
  revision,
  onChanged,
  onProject
}) {
  const [filters, setFilters] = useState(() => ({
    ...EMPTY_FILTERS,
    category: initialCategory || '',
    query: initialQuery || ''
  }));
  const [scopeMode, setScopeMode] = useState(projectId ? 'project' : 'all');
  const [viewMode, setViewMode] = useState('list');
  const [copiedHandbook, setCopiedHandbook] = useState(false);
  const [selectedId, setSelectedId] = useState(null);
  const [selected, setSelected] = useState([]);
  const [editing, setEditing] = useState(false);
  const [batch, setBatch] = useState(null);
  const [batchValue, setBatchValue] = useState('');
  const [mergeTitle, setMergeTitle] = useState('');
  const [mergeContent, setMergeContent] = useState('');
  const [mergeProject, setMergeProject] = useState(projectId || projects[0]?.id || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [offset, setOffset] = useState(0);
  const [appliedQuery, setAppliedQuery] = useState(initialQuery || '');
  useEffect(() => {
    setFilters(previous => ({
      ...previous,
      category: initialCategory || ''
    }));
    setOffset(0);
  }, [initialCategory]);
  useEffect(() => { const timer = setTimeout(() => { setAppliedQuery(filters.query); setOffset(0); }, 350); return () => clearTimeout(timer); }, [filters.query]);
  const invalidDates = filters.since && filters.until && filters.since > filters.until;
  const allProjects = scopeMode !== 'project';
  const path = invalidDates || selectedId ? null : '/api/content/index' + queryString({
    ...filters,
    query: appliedQuery,
    limit: 50,
    offset,
    kind,
    project_id: scopeMode === 'project' ? projectId : '',
    scope: scopeMode === 'global' ? 'global' : ''
  });
  const resource = useResource(path, revision);
  const handbookPath = viewMode === 'handbook' && !selectedId ? `/api/content/handbook${queryString({ project_id: scopeMode === 'project' ? projectId : '', include_global: 'true' })}` : null;
  const handbookResource = useResource(handbookPath, revision);
  const rawRows = resource.data?.items || [];
  const rows = kind === 'memory' ? [...rawRows].sort((a, b) => (isPinnedItem(b) ? 1 : 0) - (isPinnedItem(a) ? 1 : 0)) : rawRows;
  const visibleSelection = selected.filter(id => rows.some(row => row.id === id));
  const batchStale = batch?.items.some(item => !rows.some(row => row.id === item.id && row.version === item.version && row.project_id === item.project_id && row.status === item.status));
  const filtered = Object.values(filters).some(Boolean);
  function changed() {
    resource.reload();
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
      setNotice(nextPinned ? t("已将「{title}」设为常驻核心规则。", { title: item.title }) : t("已取消「{title}」的常驻状态。", { title: item.title }));
    } catch (failure) {
      setError(failure);
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
        setNotice(t("已完成记忆整理：合并 {groups} 组相近记忆，归档 {archived} 条冗余/流水账条目。", {
          groups: res.merged_groups || 0,
          archived: res.archived_count || 0
        }));
      } else {
        setNotice(t("当前记忆库已保持精炼，未发现重复或碎片条目。"));
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
      setError(new Error(t("浏览器未允许复制，请手动选择复制。")));
    }
  }
  async function openBatch(action) {
    if (busy) return;
    setError(null);
    setBatchValue(action === 'recategorize' ? 'knowledge' : '');
    const items = rows.filter(row => visibleSelection.includes(row.id));
    setMergeTitle('');
    setMergeContent('');
    setMergeProject(projectId || items[0]?.project_id || projects[0]?.id || '');
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
      } catch (failure) { setError(failure); resource.reload(); }
      finally { setBusy(false); }
    } else setBatch({ action, items });
  }
  async function importFile(event) {
    const input = event.target,
      file = input.files?.[0];
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
      setNotice(t("已导入「{title}」。", { title: saved.title }));
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
          project_id: mergeProject
        });
        setNotice(t("已合并为「{title}」，原条目保留为历史。", { title: saved.title }));
        setSelectedId(saved.id);
      } else {
        const result = await mutate('/api/content/batch', 'POST', {
          ids,
          action: batch.action,
          expected_versions: Object.fromEntries(batch.items.map(item => [item.id, item.version])),
          ...(batch.action === 'retag' ? {
            tags: batchValue.split(/[,，]/).map(value => value.trim()).filter(Boolean)
          } : batch.action === 'recategorize' ? {
            category: batchValue
          } : {})
        });
        setNotice(t("已{action} {count} 项。", { action: batch.action === 'delete' ? t("永久删除") : t("更新"), count: result.count }));
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
  if (selectedId) return <ContentDetail key={selectedId} id={selectedId} projects={projects} revision={revision} onProject={onProject} onBack={() => {
    setSelectedId(null);
    resource.reload();
  }} onChanged={changed} />;
  return <section className="content-browser">
    <div className="collection-toolbar"><div><h2>{kind === 'memory' ? t("项目记忆") : t("知识资料")}</h2><p>{kind === 'memory' ? t("分层管理常驻核心规则、使用偏好与项目经验；支持一键去重整理与 AGENTS.md 导出。") : t("你的资料，也是每个 Agent 能直接用的背景。")}</p></div><div className="toolbar-actions">{kind === 'memory' ? <SegmentedTabs label={t("记忆视图")}><button type="button" className={viewMode === 'list' ? 'active' : ''} onClick={() => setViewMode('list')}>{t("认知记忆流")}</button><button type="button" className={viewMode === 'handbook' ? 'active' : ''} onClick={() => setViewMode('handbook')}>{t("AGENTS.md 规则手册")}</button></SegmentedTabs> : null}{kind === 'memory' ? <Button icon={Sparkles} disabled={busy} onClick={consolidateMemories}>{t("去重整理")}</Button> : null}{kind === 'document' && projectId ? <label className={`button button-secondary ${busy ? 'disabled' : ''}`}><Upload size={16} />{busy ? t("正在导入…") : t("导入文档")}<input aria-label={t("导入文档")} className="file-input" type="file" disabled={busy} accept=".md,.markdown,.txt,.json,.pdf,.doc,.docx" onChange={importFile} /></label> : null}<Button icon={Plus} variant="primary" disabled={busy || !projects.length && kind === 'document'} onClick={() => {
          setError(null);
          setEditing(true);
        }}>{kind === 'memory' ? t("添加记忆") : t("添加知识")}</Button></div></div>
    {!projectId && kind === 'document' ? <p className="muted">{t("导入文档前，请先打开其所属项目。此处可搜索和维护全部项目的资料。")}</p> : null}
    {viewMode === 'handbook' && kind === 'memory' ? <section className="group handbook-panel">
      <div className="collection-toolbar">
        <div>
          <h3><BookOpen size={16} /> {t("实时生成的 AGENTS.md / CLAUDE.md 规则手册")}</h3>
          <p className="muted">{t("自动汇总当前项目的常驻核心规则、全局使用习惯、架构约束与踩坑纪律，可直接粘贴至项目根目录。")}</p>
        </div>
        <Button variant="primary" icon={copiedHandbook ? Check : Copy} onClick={copyHandbook}>{copiedHandbook ? t("已复制规则手册") : t("复制 Markdown")}</Button>
      </div>
      <ResourceState resource={handbookResource}>
        {handbookResource.data ? <>
          <div className="meta-line">
            <Badge className="badge-pinned">{t("常驻核心")} {handbookResource.data.counts?.pinned || 0}</Badge>
            <Badge>{t("全局通用")} {handbookResource.data.counts?.global || 0}</Badge>
            <Badge>{t("项目专属")} {handbookResource.data.counts?.project || 0}</Badge>
          </div>
          <div className="handbook-preview"><Markdown>{handbookResource.data.markdown}</Markdown></div>
        </> : null}
      </ResourceState>
    </section> : <>
    <div className="search-bar"><Search size={18} /><input aria-label={t("搜索内容")} placeholder={t("搜索标题、正文或关键词…")} value={filters.query} onChange={event => filter('query', event.target.value)} /><select aria-label={t("搜索范围")} value={scopeMode} onChange={event => {
        setScopeMode(event.target.value);
        setOffset(0);
        setSelected([]);
      }}><option value="project" disabled={!projectId}>{t("当前项目")}</option><option value="all">{t("全部项目")}</option>{kind === 'memory' ? <option value="global">{t("全局通用偏好")}</option> : null}</select></div>
    {kind === 'memory' ? <div className="memory-pill-bar" role="tablist" aria-label={t("记忆分类快筛")}>
      {MEMORY_PILLS.map(pill => {
        const active = filters.category === pill.category && filters.pinned === pill.pinned;
        return <button key={pill.key} type="button" role="tab" aria-selected={active} className={`memory-pill ${active ? 'active' : ''}`} onClick={() => selectMemoryPill(pill)}>{t(pill.label)}</button>;
      })}
    </div> : null}
    <details className="filter-details"><summary>{t("筛选内容")}{filtered ? t(" · 已应用") : ''}</summary><div className="filter-grid"><Field label={t("分类")}><select value={filters.category} onChange={event => filter('category', event.target.value)}><option value="">{t("所有分类")}</option>{Object.entries(CATEGORIES).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}</select></Field><Field label={t("来源 Agent")}><select value={filters.agent} onChange={event => filter('agent', event.target.value)}><option value="">{t("所有来源")}</option>{Object.entries(AGENTS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field><Field label={t("标签")}><input value={filters.tag} onChange={event => filter('tag', event.target.value)} /></Field><Field label={t("创建日期 · 从")}><input type="date" max={filters.until || undefined} aria-invalid={invalidDates ? true : undefined} value={filters.since} onChange={event => filter('since', event.target.value)} /></Field><Field label={t("创建日期 · 至")}><input type="date" min={filters.since || undefined} aria-invalid={invalidDates ? true : undefined} value={filters.until} onChange={event => filter('until', event.target.value)} /></Field><Field label={t("有效状态")}><select value={filters.status} onChange={event => filter('status', event.target.value)}><option value="">{t("全部状态")}</option><option value="active">{t("当前有效")}</option><option value="historical">{t("历史")}</option></select></Field></div></details>
    {filtered ? <Button onClick={() => {
      setFilters(EMPTY_FILTERS);
      setOffset(0);
      setSelected([]);
    }}>{t("清除筛选")}</Button> : null}
    <ErrorNotice error={error} />{notice ? <p className="operation-notice" role="status">{notice}</p> : null}
    {visibleSelection.length ? <div className="batch-bar"><span>{t("已选 {count} 项", { count: visibleSelection.length })}</span><Button disabled={busy || resource.loading || !!resource.error} onClick={() => openBatch('retag')}>{t("调整标签")}</Button><Button disabled={busy || resource.loading || !!resource.error} onClick={() => openBatch('recategorize')}>{t("调整分类")}</Button>{kind === 'memory' ? <Button icon={GitMerge} disabled={busy || visibleSelection.length < 2 || resource.loading || !!resource.error} onClick={() => openBatch('merge')}>{t("合并")}</Button> : null}<Button icon={Trash2} disabled={busy || resource.loading || !!resource.error} onClick={() => openBatch('delete')}>{t("删除")}</Button><Button onClick={() => setSelected([])}>{t("取消选择")}</Button></div> : null}
    {invalidDates ? <p className="inline-error" role="alert">{t("开始日期不能晚于结束日期，改一下再搜。")}</p> : null}
    <div hidden={!!invalidDates}>
    <ResourceState resource={resource}>{rows.length ? <div className="content-list group">{rows.map(item => {
      const pinned = isPinnedItem(item);
      return <div className={`content-row ${pinned ? 'content-row-pinned' : ''}`} key={item.id}><label className="row-select"><input aria-label={t("选择 {title}", { title: item.title })} type="checkbox" checked={visibleSelection.includes(item.id)} onChange={event => setSelected(previous => event.target.checked ? [...previous, item.id] : previous.filter(id => id !== item.id))} /></label><button className="content-row-main" onClick={() => setSelectedId(item.id)}><div className="meta-line">{pinned ? <Badge className="badge-pinned">{t("常驻核心")}</Badge> : null}<Badge>{t(CATEGORIES[item.category] || item.category)}</Badge>{!item.project_id && kind === 'memory' ? <Badge>{t("全局通用")}</Badge> : null}{item.version > 1 ? <span>v{item.version}</span> : null}{item.status === 'historical' ? <span>{t("历史内容")}</span> : null}{allProjects ? <span>{projects.find(project => project.id === item.project_id)?.name || t("全局通用")}</span> : null}</div><h3>{item.title}</h3><p>{item.content_preview}</p><small>{AGENTS[item.agent] || item.agent || t("人工维护")} <span>·</span> {formatTime(item.updated_at)}</small></button>{kind === 'memory' ? <button type="button" className={`pin-toggle-btn ${pinned ? 'active' : ''}`} title={pinned ? t("取消常驻核心") : t("设为常驻核心规则")} aria-label={pinned ? t("取消常驻核心") : t("设为常驻核心规则")} onClick={event => toggleRowPin(event, item)}><Pin size={14} /></button> : null}<ChevronRight size={15} className="row-chevron" aria-hidden="true" /></div>;
    })}</div> : <EmptyState title={filtered ? t("没有找到匹配内容") : kind === 'memory' ? t("还没有沉淀的记忆") : t("给项目补充一点知识")} body={filtered ? t("可以清掉分类、来源、日期这些筛选，或者切到全部项目再搜；归档项目也算在内。") : kind === 'memory' ? t("采集了不等于已经整理成记忆。可以到「相关聊天」看处理到哪一步、为什么失败，也可以自己补一条。") : t("这里只显示真正存下来的内容。可以自己写一条，也可以导入文档。")} action={filtered ? <Button onClick={() => {
          setFilters(EMPTY_FILTERS);
      setOffset(0);
          setSelected([]);
        }}>{t("清除筛选")}</Button> : null} />}</ResourceState>
    <Pagination total={resource.data?.total} offset={offset} limit={50} loading={resource.loading} onChange={value => { setOffset(value); setSelected([]); }} />
    </div>
    </>}
    {editing ? <ContentEditor projectId={projectId} kind={kind} onClose={() => setEditing(false)} onSaved={saved => {
      changed();
      setSelectedId(saved.id);
    }} /> : null}
    {batch ? <Modal title={batch.action === 'delete' ? t("永久删除 {count} 项？", { count: batch.items.length }) : batch.action === 'merge' ? t("合并选中记忆") : batch.action === 'retag' ? t("批量调整标签") : t("批量调整分类")} onClose={() => setBatch(null)} busy={busy} wide={batch.action === 'merge'}><form className="editor-form" onSubmit={applyBatch}>
      <details><summary>{t("本次操作的 {count} 项内容", { count: batch.items.length })}</summary><ul className="confirmation-targets">{batch.items.map(item => <li key={item.id}>{item.title} · {projects.find(project => project.id === item.project_id)?.name || t("项目待定")}</li>)}</ul></details>
      {batch.action === 'delete' ? <p>{t("此操作无法恢复，不会删除关联的原始聊天。")}</p> : batch.action === 'merge' ? <><p>{t("合并成一条新记忆，原条目留作历史，不再作为当前依据。请核对正文与所属项目。")}</p><Field label={t("合并后的所属项目")}><select value={mergeProject} required onChange={event => setMergeProject(event.target.value)}>{projects.map(project => <option key={project.id} value={project.id}>{project.name}{project.status === 'archived' ? t("（已归档）") : ''}</option>)}</select></Field><Field label={t("合并后的标题")}><input required value={mergeTitle} onChange={event => setMergeTitle(event.target.value)} /></Field><Field label={t("合并后的正文")}><textarea required rows={10} value={mergeContent} onChange={event => setMergeContent(event.target.value)} /></Field></> : batch.action === 'retag' ? <Field label={t("新标签（逗号分隔）")} hint={t("替换原有标签；留空会清除标签。")}><input value={batchValue} onChange={event => setBatchValue(event.target.value)} /></Field> : <Field label={t("分类")}><select value={batchValue} onChange={event => setBatchValue(event.target.value)}>{Object.entries(CATEGORIES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>}
      <ErrorNotice error={error} />{batchStale || resource.error ? <div className="version-recovery"><p>{t("选中的内容有变化，或者状态读不到。系统没有替你缩小范围，请重新选。")}</p><Button onClick={() => {
            setBatch(null);
            setSelected([]);
            resource.reload();
          }}>{t("返回列表并刷新")}</Button></div> : null}
      <footer className="modal-actions"><Button onClick={() => setBatch(null)}>{t("取消")}</Button><button type="submit" className={`button button-${batch.action === 'delete' ? 'danger' : 'primary'}`} disabled={busy || batchStale || resource.loading || !!resource.error || batch.action === 'merge' && (!mergeProject || !mergeTitle.trim() || !mergeContent.trim())}>{busy ? t("正在提交…") : batch.action === 'delete' ? t("确认永久删除") : t("确认保存")}</button></footer>
    </form></Modal> : null}
  </section>;
}
