import '../styles/project-evidence.css';
import { t } from "../lib/i18n.jsx";
import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, ChevronRight, MessageSquare, Search, Sparkles, Trash2 } from 'lucide-react';
import { errorFeedback, mutate } from '../lib/api.js';
import { queryString, useResource } from '../lib/useResource.js';
import { AGENTS, Badge, Button, EmptyState, ErrorNotice, Field, Modal, ResourceState, formatTime } from '../components/ui.jsx';
import { Pagination } from '../components/Pagination.jsx';
import { useObjectRevision } from '../lib/objectRefresh.js';
import { Markdown } from '../components/Markdown.jsx';
const PROCESSING = {
  pending: '已采集，待整理',
  processed: '已处理',
  error: '处理失败'
};
export function ConversationDetail({
  id,
  projects,
  revision,
  onBack,
  onChanged,
  onProject,
  backLabel = t("返回聊天列表")
}) {
  const objectRevision = useObjectRevision(revision, 'conversation', id);
  const resource = useResource(`/api/conversations/${id}?include_messages=false`, objectRevision);
  const [offset, setOffset] = useState(0);
  const messages = useResource(`/api/conversations/${id}/messages?limit=30&offset=${offset}`, objectRevision);
  const [confirmation, setConfirmation] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const item = resource.data;
  const owner = projects.find(project => project.id === item?.project_id);
  const unavailable = resource.loading || !!resource.error;
  const staleMove = confirmation?.action === 'move' && (confirmation.from !== item?.project_id || !projects.some(project => project.id === confirmation.to));
  async function submit() {
    if (busy || !confirmation || staleMove) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      if (confirmation.action === 'delete') {
        await mutate(`/api/conversations/${id}`, 'DELETE');
        onChanged();
        onBack();
      } else {
        await mutate(`/api/conversations/${id}`, 'PATCH', {
          project_id: confirmation.to
        });
        setNotice(t("聊天已归入「{name}」。已经生成的记忆还留在原来的项目，需要的话可以在记忆详情里单独调。", { name: confirmation.targetName }));
        setConfirmation(null);
        resource.reload();
        onChanged();
      }
    } catch (failure) {
      setError(failure);
      if ([404, 409, 410].includes(failure.status)) {
        resource.reload();
        onChanged();
      }
    } finally {
      setBusy(false);
    }
  }
  async function distillConversation() {
    if (busy || unavailable || !item) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      const res = await mutate('/api/processing/auto-summarize', 'POST', {
        conversation_id: id,
        ...(item.project_id ? { project_id: item.project_id } : {}),
        actor: 'user',
        auto_extract_from_messages: true
      });
      resource.reload();
      onChanged?.();
      setNotice(res?.summary_note || t("已从本段对话提炼记忆并更新过程摘要。"));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  return <article className="conversation-detail evidence-reader">
    <div className="detail-toolbar">
      <Button icon={ArrowLeft} onClick={onBack}>{backLabel}</Button>
      {item && <Button icon={Sparkles} disabled={unavailable || busy} onClick={distillConversation}>
        {busy ? t("正在提炼…") : t("提炼本段记忆")}
      </Button>}
    </div>
    {notice && <p className="operation-notice" role="status">{notice}</p>}
    {!confirmation && <ErrorNotice error={error} />}
    <ResourceState resource={resource}>{item ? <>
      <header className="reading-heading">
        <p className="eyebrow">{AGENTS[item.agent] || item.agent} {t("/ 原始对话")}</p>
        <h2>{item.title}</h2>
        <div className="evidence-byline">
          {owner && <span>{owner.name}</span>}
          {item.updated_at && <time dateTime={item.updated_at}>{formatTime(item.updated_at)}</time>}
          <Badge>{t(PROCESSING[item.processing_status] || "状态未知")}</Badge>
        </div>
      </header>
      {item.processing_error && <ErrorNotice error={new Error(errorFeedback(500, item.processing_error))} />}
      <details className="evidence-disclosure">
        <summary>{t("归属与管理")}</summary>
        <div className="conversation-meta">
          <Field label={t("所属项目")} hint={t("只改这份聊天在 Senrelay 里算哪个项目。")}>
            <select value={item.project_id || ''} disabled={unavailable || busy} onChange={event => {
              const target = projects.find(project => project.id === event.target.value);
              if (!target || target.id === item.project_id) return;
              setError(null);
              setConfirmation({action: 'move', from: item.project_id, to: target.id, targetName: target.name, title: item.title});
            }}>
              <option value="" disabled>{t("还没选项目")}</option>
              {projects.map(project => <option key={project.id} value={project.id}>{project.name}{project.status === 'archived' ? t("（已归档）") : ''}</option>)}
            </select>
          </Field>
          <div className="toolbar-actions">
            {owner && <Button onClick={() => onProject(owner.id)}>{t("打开所属项目")}</Button>}
            <Button icon={Trash2} disabled={unavailable || busy} onClick={() => {
              setError(null);
              setConfirmation({action: 'delete', title: item.title});
            }}>{t("删除聊天")}</Button>
          </div>
        </div>
      </details>
      {item.process_summary ? <details className="process-summary"><summary>{t("关键过程摘要")}</summary><Markdown>{item.process_summary}</Markdown><small>{t("摘要不是完整的工具输出，也不能单独当证据。")}</small></details> : null}
      {item.handoff_page && (item.handoff_page.status === 'ready' || item.handoff_page.status === 'error') ? <details className="process-summary"><summary>{t("交接纸（点选接力后生成）")}</summary>
        <div className="handoff-page-meta">
          <Badge tone={item.handoff_page.status === 'ready' ? 'success' : 'warning'}>{item.handoff_page.status === 'ready' ? t("已写好") : t("未写成")}</Badge>
          {item.handoff_page.stale ? <Badge tone="badge-status-outdated">{t("有新消息未合入")}</Badge> : null}
          {item.handoff_page.covered_message_count > 0 ? <span className="field-hint">{t("覆盖前 {count} 条消息", { count: item.handoff_page.covered_message_count })}</span> : null}
          {item.handoff_page.error || item.handoff_page.write_error ? <span className="field-hint">{item.handoff_page.error || item.handoff_page.write_error}</span> : null}
        </div>
        {item.handoff_page.status === 'ready' ? <dl className="handoff-page-fields">
          <dt>{t("在做什么")}</dt><dd><Markdown>{item.handoff_page.goal || '—'}</Markdown></dd>
          <dt>{t("已经做完的")}</dt><dd><Markdown>{item.handoff_page.done || '—'}</Markdown></dd>
          <dt>{t("不能重做的")}</dt><dd><Markdown>{item.handoff_page.do_not_redo || '—'}</Markdown></dd>
          <dt>{t("还没验证的")}</dt><dd><Markdown>{item.handoff_page.unverified || '—'}</Markdown></dd>
          <dt>{t("下一步")}</dt><dd><Markdown>{item.handoff_page.next_step || '—'}</Markdown></dd>
        </dl> : null}
        <small>{t("打开详情不会重新写纸；只有点中接力候选时才会写或复用。")}</small>
      </details> : null}
      <div className="evidence-section-title">
        <h3>{t("对话原文")}</h3>
        {messages.data?.total != null && <span>{t("共 {count} 条", { count: messages.data.total })}</span>}
      </div>
      <ResourceState resource={messages}><div className="message-list">{messages.data?.items?.length ? messages.data.items.map((message, index) => <section key={message.id || message.external_id || `${offset}:${index}`} className={`message message-${message.role}`}><header><strong>{message.role === 'user' ? t("你") : message.role === 'assistant' ? AGENTS[item.agent] || item.agent : message.role}</strong><time>{formatTime(message.timestamp)}</time></header><Markdown>{message.content}</Markdown></section>) : <EmptyState title={t("这份记录还没有可显示的消息")} body={t("采集到了，不代表对话是完整的。请检查接入状态。")} />}</div></ResourceState><Pagination total={messages.data?.total} offset={offset} limit={30} loading={messages.loading} onChange={setOffset} />
    </> : null}</ResourceState>
    {confirmation ? <Modal title={confirmation.action === 'delete' ? t("永久删除这份聊天？") : t("把这份聊天移到别的项目？")} busy={busy} onClose={() => setConfirmation(null)}><p className="modal-copy">{confirmation.action === 'delete' ? t("「{title}」将从 Senrelay 永久删除，无法恢复。已经生成的内容不会被一起删掉，也不会改 Agent 自己的原始文件。", { title: confirmation.title }) : t("会移到「{name}」。不会动磁盘上的目录，也不会改原始聊天文件；已经生成的记忆不会跟着走。", { name: confirmation.targetName })}</p><ErrorNotice error={error} />{staleMove || resource.error ? <div className="version-recovery"><p>{t("记录或目标项目有变化，刷新后再操作。")}</p><Button onClick={() => {
          setConfirmation(null);
          resource.reload();
        }}>{t("返回详情并刷新")}</Button></div> : null}<footer className="modal-actions"><Button onClick={() => setConfirmation(null)}>{t("取消")}</Button><Button variant={confirmation.action === 'delete' ? 'danger' : 'primary'} busy={busy} disabled={unavailable || staleMove || [404, 409, 410].includes(error?.status)} onClick={submit}>{confirmation.action === 'delete' ? t("永久删除聊天") : t("确认移动")}</Button></footer></Modal> : null}
  </article>;
}
export function ConversationsPage({
  projectId,
  projects,
  revision,
  onChanged,
  initialId,
  initialScope,
  initialQuery = '',
  onCloseInitial,
  onRecordChange,
  onProject
}) {
  const [selected, setSelected] = useState(null);
  const listRef = useRef(null);
  const restoreRow = useRef(null);
  const [query, setQuery] = useState(initialQuery);
  const [agent, setAgent] = useState('');
  const [scope, setScope] = useState(() => initialScope || (projectId ? 'project' : 'all'));
  const [offset, setOffset] = useState(0);
  const [appliedQuery, setAppliedQuery] = useState(initialQuery);
  useEffect(() => {
    if (initialScope) {
      setScope(initialScope);
      setOffset(0);
    }
  }, [initialScope]);
  useEffect(() => {
    const timer = setTimeout(() => { setAppliedQuery(query); setOffset(0); }, 350);
    return () => clearTimeout(timer);
  }, [query]);
  const id = onRecordChange ? initialId : initialId || selected;
  const effectiveProjectId = scope === 'unassigned' ? '__unassigned__' : scope === 'all' ? '' : projectId;
  const resource = useResource(id ? null : '/api/conversations/index' + queryString({
    project_id: effectiveProjectId,
    query: appliedQuery,
    agent,
    limit: 50,
    offset
  }), revision);
  // Returning to the source list restores the reader's place after results reload.
  useEffect(() => {
    if (id || resource.loading || !restoreRow.current) return;
    const row = Array.from(listRef.current?.querySelectorAll('[data-record-id]') || [])
      .find(node => node.dataset.recordId === restoreRow.current);
    if (!row && !resource.data && !resource.error) return;
    (row || listRef.current?.querySelector('input[type="search"]'))?.focus();
    restoreRow.current = null;
  }, [id, resource.loading, resource.data, resource.error]);
  if (id) return <ConversationDetail key={id} id={id} projects={projects} revision={revision} onProject={onProject} onChanged={() => {
    resource.reload();
    onChanged();
  }} onBack={() => {
    setSelected(null);
    if (onRecordChange) onRecordChange(null);
    else onCloseInitial?.();
    resource.reload();
  }} />;
  return <section className="evidence-index" ref={listRef}>
    <header className="collection-toolbar">
      <div><h2>{t("原始对话")}</h2><p>{t("按关键词和来源查找当时的讨论，打开记录可核对原话。")}</p></div>
    </header>
    <div className="evidence-query">
      <label className="evidence-search">
        <Search size={17} aria-hidden="true" />
        <input aria-label={t("搜索聊天")} type="search" placeholder={t("搜索原始对话…")} value={query} onChange={event => setQuery(event.target.value)} />
      </label>
      <select aria-label={t("聊天搜索范围")} value={scope} onChange={event => { setScope(event.target.value); setOffset(0); }}>
        <option value="project" disabled={!projectId}>{t("当前项目")}</option>
        <option value="all">{t("全部项目")}</option>
        <option value="unassigned">{t("未归属项目的聊天")}</option>
      </select>
      <select aria-label={t("聊天来源")} value={agent} onChange={event => { setAgent(event.target.value); setOffset(0); }}>
        <option value="">{t("全部 Agent")}</option>
        {Object.entries(AGENTS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
      </select>
      {(query || agent || scope !== (projectId ? 'project' : 'all')) && <Button onClick={() => {
        setQuery('');
        setAgent('');
        setScope(projectId ? 'project' : 'all');
        setOffset(0);
      }}>{t("清除筛选")}</Button>}
    </div>
    <ResourceState resource={resource}>
      {resource.data?.items?.length ? <div className="conversation-list group">
        {resource.data.items.map(item => (
          <button className="conversation-row" key={item.id} data-record-id={item.id} onClick={() => {
            restoreRow.current = item.id;
            if (onRecordChange) onRecordChange(item.id);
            else setSelected(item.id);
          }}>
            <span className="list-icon"><MessageSquare size={18} aria-hidden="true" /></span>
            <div>
              <h3>{item.title}</h3>
              <p>{AGENTS[item.agent] || item.agent} · {formatTime(item.updated_at)}{scope !== 'project' ? ` · ${projects.find(project => project.id === item.project_id)?.name || t("项目待定")}` : ''}</p>
            </div>
            <Badge>{t(PROCESSING[item.processing_status] || item.processing_status || "状态未知")}</Badge>
            <ChevronRight className="row-chevron" size={16} aria-hidden="true" />
          </button>
        ))}
      </div> : <EmptyState title={query || agent ? t("没有找到匹配的聊天") : t("还没有采集到聊天")} body={query || agent ? t("清掉关键词或来源筛选，或者切到全部项目；还没定项目和已归档的记录也在里面。") : t("先接入 Agent 并确认能读哪些记录。这里只显示真实采集到的内容。")} />}
    </ResourceState>
    {(offset > 0 || resource.data?.total > 0) && <Pagination total={resource.data?.total} offset={offset} limit={50} loading={resource.loading} onChange={setOffset} />}
  </section>;
}
