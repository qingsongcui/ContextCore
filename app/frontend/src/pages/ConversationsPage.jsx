import { ChevronRight, Sparkles } from 'lucide-react';
import { t } from "../lib/i18n.jsx";
import { useEffect, useState } from 'react';
import { ArrowLeft, MessageSquare, Search, Trash2 } from 'lucide-react';
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
  return <article className="conversation-detail">
    <div className="detail-toolbar"><Button icon={ArrowLeft} onClick={onBack}>{backLabel}</Button>{item ? <div><Button icon={Sparkles} disabled={unavailable || busy} onClick={distillConversation}>{busy ? t("正在提炼…") : t("提炼本段记忆")}</Button><Button icon={Trash2} disabled={unavailable} onClick={() => {
        setError(null);
        setConfirmation({
          action: 'delete',
          title: item.title
        });
      }}>{t("删除聊天")}</Button></div> : null}</div>
    {notice ? <p className="operation-notice" role="status">{notice}</p> : null}
    <ResourceState resource={resource}>{item ? <>
      <div className="reading-heading"><p className="eyebrow">{AGENTS[item.agent] || item.agent} {t("/ 原始对话")}</p><h2>{item.title}</h2><p>{formatTime(item.updated_at)}</p></div>
      <div className="conversation-meta"><div><Field label={t("所属项目")} hint={t("只改这份聊天在 Senrelay 里算哪个项目。")}><select value={item.project_id || ''} disabled={unavailable || busy} onChange={event => {
                const target = projects.find(project => project.id === event.target.value);
                if (!target || target.id === item.project_id) return;
                setError(null);
                setConfirmation({
                  action: 'move',
                  from: item.project_id,
                  to: target.id,
                  targetName: target.name,
                  title: item.title
                });
              }}><option value="" disabled>{t("还没选项目")}</option>{projects.map(project => <option key={project.id} value={project.id}>{project.name}{project.status === 'archived' ? t("（已归档）") : ''}</option>)}</select></Field>{owner ? <Button onClick={() => onProject(owner.id)}>{t("打开所属项目")}</Button> : null}</div><div><span className="field-label">{t("记忆整理状态")}</span><Badge>{t(PROCESSING[item.processing_status] || "状态未知")}</Badge>{item.processing_error ? <ErrorNotice error={new Error(errorFeedback(500, item.processing_error))} /> : null}</div></div>
      {item.process_summary ? <details className="process-summary"><summary>{t("关键过程摘要")}</summary><Markdown>{item.process_summary}</Markdown><small>{t("摘要不是完整的工具输出，也不能单独当证据。")}</small></details> : null}
      {item.handoff_page && (item.handoff_page.status === 'ready' || item.handoff_page.status === 'error') ? <details className="process-summary" open={item.handoff_page.status === 'ready'}><summary>{t("交接纸（点选接力后生成）")}</summary>
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
  onCloseInitial,
  onProject
}) {
  const [selected, setSelected] = useState(null);
  const [query, setQuery] = useState('');
  const [agent, setAgent] = useState('');
  const [scope, setScope] = useState(() => initialScope || (projectId ? 'project' : 'all'));
  const [offset, setOffset] = useState(0);
  const [appliedQuery, setAppliedQuery] = useState('');
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
  const id = initialId || selected;
  const effectiveProjectId = scope === 'unassigned' ? '__unassigned__' : scope === 'all' ? '' : projectId;
  const resource = useResource(id ? null : '/api/conversations/index' + queryString({
    project_id: effectiveProjectId,
    query: appliedQuery,
    agent,
    limit: 50,
    offset
  }), revision);
  if (id) return <ConversationDetail key={id} id={id} projects={projects} revision={revision} onProject={onProject} onChanged={() => {
    resource.reload();
    onChanged();
  }} onBack={() => {
    setSelected(null);
    onCloseInitial?.();
    resource.reload();
  }} />;
  return <section><div className="collection-toolbar"><div><h2>{t("相关聊天")}</h2><p>{t("不同 Agent 的协作过程，在同一个项目里相遇。")}</p></div></div>
    <div className="search-bar"><Search size={18} /><input aria-label={t("搜索聊天")} placeholder={t("搜索原始对话…")} value={query} onChange={event => setQuery(event.target.value)} /><select aria-label={t("聊天搜索范围")} value={scope} onChange={event => { setScope(event.target.value); setOffset(0); }}><option value="project" disabled={!projectId}>{t("当前项目")}</option><option value="all">{t("全部项目")}</option><option value="unassigned">{t("未归属项目的聊天")}</option></select></div>
    <div className="memory-pill-bar" role="tablist" aria-label={t("Agent 快捷筛选")}>
      <button type="button" role="tab" aria-selected={!agent} className={`memory-pill ${!agent ? 'active' : ''}`} onClick={() => { setAgent(''); setOffset(0); }}>{t("全部 Agent")}</button>
      {Object.entries(AGENTS).map(([key, label]) => <button key={key} type="button" role="tab" aria-selected={agent === key} className={`memory-pill ${agent === key ? 'active' : ''}`} onClick={() => { setAgent(key); setOffset(0); }}>{label}</button>)}
    </div>
    <div className="collection-filters"><Field label={t("聊天来源")}><select value={agent} onChange={event => { setAgent(event.target.value); setOffset(0); }}><option value="">{t("全部 Agent")}</option>{Object.entries(AGENTS).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></Field>{query || agent ? <Button onClick={() => {
        setQuery('');
        setAgent(''); setOffset(0);
      }}>{t("清除筛选")}</Button> : null}</div>
    <ResourceState resource={resource}>{resource.data?.items?.length ? <div className="conversation-list group">{resource.data.items.map(item => <button className="conversation-row" key={item.id} onClick={() => setSelected(item.id)}><span className="list-icon"><MessageSquare size={20} /></span><div><h3>{item.title}</h3><p>{AGENTS[item.agent] || item.agent} · {formatTime(item.updated_at)}{scope !== 'project' ? ` · ${projects.find(project => project.id === item.project_id)?.name || t("项目待定")}` : ''}</p></div><Badge>{t(PROCESSING[item.processing_status] || item.processing_status || "状态未知")}</Badge><ChevronRight className="row-chevron" size={16} aria-hidden="true" /></button>)}</div> : <EmptyState title={query || agent ? t("没有找到匹配的聊天") : t("还没有采集到聊天")} body={query || agent ? t("清掉关键词或来源筛选，或者切到全部项目；还没定项目和已归档的记录也在里面。") : t("先接入 Agent 并确认能读哪些记录。这里只显示真实采集到的内容。")} />}</ResourceState>
    <div className="management-pagination"><span>{t("共 {count} 条", { count: resource.data?.total ?? 0 })}</span><div className="toolbar-actions"><Button disabled={offset === 0 || resource.loading} onClick={() => setOffset(Math.max(0, offset - 50))}>{t("上一页")}</Button><Button disabled={resource.loading || offset + 50 >= (resource.data?.total ?? 0)} onClick={() => setOffset(offset + 50)}>{t("下一页")}</Button></div></div>
  </section>;
}
