import { t } from "../lib/i18n.jsx";
import { useRef, useState } from 'react';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { Button, ErrorNotice, Field, Modal, ResourceState } from './ui.jsx';
import { Pagination } from './Pagination.jsx';
export function ProjectDialog({
  project,
  projects,
  revision,
  mode = 'edit',
  onClose,
  onSaved
}) {
  const emptyProject = mode === 'split' || mode === 'create';
  const [name, setName] = useState(emptyProject ? '' : project?.name || '');
  const [description, setDescription] = useState(emptyProject ? '' : project?.description || '');
  const [directories, setDirectories] = useState(emptyProject ? '' : project?.directories?.join('\n') || '');
  const [target, setTarget] = useState(projects.find(item => item.id !== project?.id)?.id || '');
  const [contentIds, setContentIds] = useState([]);
  const [conversationIds, setConversationIds] = useState([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const PAGE_SIZE = 50;
  const [contentOffset, setContentOffset] = useState(0);
  const [conversationOffset, setConversationOffset] = useState(0);
  const content = useResource(mode === 'split' ? `/api/content/index?project_id=${encodeURIComponent(project.id)}&limit=${PAGE_SIZE}&offset=${contentOffset}` : null, revision);
  const conversations = useResource(mode === 'split' ? `/api/conversations/index?project_id=${encodeURIComponent(project.id)}&limit=${PAGE_SIZE}&offset=${conversationOffset}` : null, revision);
  const contentItems = Array.isArray(content.data) ? content.data : (content.data?.items || []);
  const conversationItems = Array.isArray(conversations.data) ? conversations.data : (conversations.data?.items || []);
  const contentTotalRef = useRef(0);
  const conversationTotalRef = useRef(0);
  if (Array.isArray(content.data)) contentTotalRef.current = content.data.length;
  else if (typeof content.data?.total === 'number') contentTotalRef.current = content.data.total;
  if (Array.isArray(conversations.data)) conversationTotalRef.current = conversations.data.length;
  else if (typeof conversations.data?.total === 'number') conversationTotalRef.current = conversations.data.total;
  const contentTotal = contentTotalRef.current;
  const conversationTotal = conversationTotalRef.current;
  const knownContent = useRef(new Map());
  const knownConversations = useRef(new Map());
  for (const item of contentItems) { if (item?.id) knownContent.current.set(item.id, item); }
  for (const item of conversationItems) { if (item?.id) knownConversations.current.set(item.id, item); }
  const titles = {
    create: t("新建项目"),
    edit: t("整理项目"),
    merge: t("合并项目"),
    split: t("拆分项目")
  };
  const staleSelection = contentIds.some(id => {
    const item = knownContent.current.get(id);
    return !item || (item.project_id && item.project_id !== project.id);
  }) || conversationIds.some(id => {
    const item = knownConversations.current.get(id);
    return !item || (item.project_id && item.project_id !== project.id);
  });
  const splitUnavailable = mode === 'split' && (staleSelection || content.loading || conversations.loading || !!content.error || !!conversations.error || !contentIds.length && !conversationIds.length);
  async function save(event) {
    event.preventDefault();
    if (busy || splitUnavailable) return;
    setBusy(true);
    setError(null);
    try {
      let saved;
      if (mode === 'merge') saved = await mutate('/api/projects/merge', 'POST', {
        source_ids: [project.id],
        target_id: target
      });else if (mode === 'split') saved = await mutate(`/api/projects/${project.id}/split`, 'POST', {
        name: name.trim(),
        description,
        directories: directories.split('\n').map(value => value.trim()).filter(Boolean),
        content_ids: contentIds,
        conversation_ids: conversationIds
      });else if (mode === 'create') saved = await mutate('/api/projects', 'POST', {
        name: name.trim(),
        description,
        directories: directories.split('\n').map(value => value.trim()).filter(Boolean)
      });else saved = await mutate(`/api/projects/${project.id}`, 'PATCH', {
        name: name.trim(),
        description,
        directories: directories.split('\n').map(value => value.trim()).filter(Boolean)
      });
      onSaved(saved);
      onClose();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  function toggle(setter, id, checked) {
    setter(previous => checked ? [...previous, id] : previous.filter(value => value !== id));
  }
  return <Modal title={titles[mode]} onClose={onClose} busy={busy} initialFocus={mode === 'merge' ? 'select' : 'input[required]'}><form className="editor-form" onSubmit={save}>
    {mode === 'merge' ? <><p>{t("把「{name}」的内容和关联目录并到目标项目，原项目会归档。不会动磁盘上的文件夹，也不改聊天原文。", { name: project.name })}</p><Field label={t("合并到")}><select required value={target} onChange={event => setTarget(event.target.value)}><option value="" disabled>{t("选择目标项目")}</option>{projects.filter(item => item.id !== project.id).map(item => <option key={item.id} value={item.id}>{item.name}{item.status === 'archived' ? t("（已归档）") : ''}</option>)}</select></Field></> : <>
      <Field label={mode === 'split' ? t("新项目名称") : t("项目名称")}><input autoFocus required value={name} onChange={event => setName(event.target.value)} /></Field>
      <Field label={t("项目说明")}><textarea rows={3} value={description} onChange={event => setDescription(event.target.value)} /></Field>
      <details className="filter-details"><summary>{t("关联目录（可选）")}</summary><Field label={t("关联目录")} hint={t("一行一个本地目录。这里只是登记，不会移动文件，也不代表给了读取权限。")}><textarea rows={3} value={directories} onChange={event => setDirectories(event.target.value)} /></Field></details>
      {mode === 'split' ? <div className="split-selections"><p className="muted">{t("至少选一项。聊天和记忆是分开的，要移哪些请分别勾。")}</p><h3>{t("移入新项目的内容 · 已选")} {contentIds.length}</h3><ResourceState resource={content}>{contentItems.length ? contentItems.map(item => <label key={item.id}><input type="checkbox" checked={contentIds.includes(item.id)} onChange={event => toggle(setContentIds, item.id, event.target.checked)} /><span>{item.title}{item.status === 'historical' ? t("（历史）") : ''}</span></label>) : <p className="muted">{t("没有可移动的记忆或资料。")}</p>}</ResourceState>{contentTotal > PAGE_SIZE ? <Pagination total={contentTotal} offset={contentOffset} limit={PAGE_SIZE} loading={content.loading} onChange={setContentOffset} /> : null}<h3>{t("移入新项目的聊天 · 已选")} {conversationIds.length}</h3><ResourceState resource={conversations}>{conversationItems.length ? conversationItems.map(item => <label key={item.id}><input type="checkbox" checked={conversationIds.includes(item.id)} onChange={event => toggle(setConversationIds, item.id, event.target.checked)} /><span>{item.title}</span></label>) : <p className="muted">{t("没有可移动的聊天。")}</p>}</ResourceState>{conversationTotal > PAGE_SIZE ? <Pagination total={conversationTotal} offset={conversationOffset} limit={PAGE_SIZE} loading={conversations.loading} onChange={setConversationOffset} /> : null}</div> : null}
    </>}
    {mode === 'split' && staleSelection ? <div className="version-recovery"><p>{t("选中的记录有的被删了，有的换了项目，请重新选。")}</p><Button onClick={() => {
          setContentIds([]);
          setConversationIds([]);
          knownContent.current.clear();
          knownConversations.current.clear();
          content.reload();
          conversations.reload();
        }}>{t("清除选择并刷新")}</Button></div> : null}
    <ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={onClose}>{t("取消")}</Button><button className="button button-primary" type="submit" disabled={busy || splitUnavailable || (mode === 'merge' ? !projects.some(item => item.id === target && item.id !== project.id) : !name.trim())}>{busy ? t("正在保存…") : mode === 'merge' ? t("确认合并") : mode === 'split' ? t("拆分并打开新项目") : mode === 'create' ? t("创建并打开项目") : t("确认保存")}</button></footer>
  </form></Modal>;
}
