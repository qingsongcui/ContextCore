import { t } from "../lib/i18n.jsx";
import { useEffect, useRef, useState } from 'react';
import { Archive, ArrowLeft, BookOpen, Files, GitMerge, ListTodo, MessagesSquare, MoreHorizontal, Split } from 'lucide-react';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { AGENTS, Badge, Button, ErrorNotice, PageHeader, ResourceState, SectionHeading, SegmentedTabs, formatTime } from '../components/ui.jsx';
import { ProjectDialog } from '../components/ProjectDialog.jsx';
import { isDiscussionOnlyTask, taskStatusLabel } from '../components/WorkingTasksPanel.jsx';
import { MemoryReader } from './MemoryReader.jsx';
import { KnowledgePage, ContentDetail } from './KnowledgePage.jsx';
import { ConversationDetail, ConversationsPage } from './ConversationsPage.jsx';
import { ProjectLibrary } from '../components/ProjectLibrary.jsx';
import '../styles/project-workspace.css';

const TABS = [
  ['memory', '项目记忆', BookOpen],
  ['overview', '工作进度', ListTodo],
  ['conversations', '原始对话', MessagesSquare],
  ['document', '项目资料', Files],
];
function Overview({
  project,
  revision,
  onTab,
  onReviewAssets
}) {
  const taskResource = useResource(`/api/working-tasks?project_id=${encodeURIComponent(project.id)}&active_only=true`, revision);
  const conversations = useResource(`/api/conversations/index?project_id=${encodeURIComponent(project.id)}&limit=4`, revision);
  const rawTasks = Array.isArray(taskResource.data) ? taskResource.data : (taskResource.data?.items || []);
  return <section className="project-progress">
    <div className="project-section-heading">
      <h2>{t('工作进度')}</h2>
      <p>{t('查看正在进行的任务，或从最近的对话回到工作现场。')}</p>
    </div>
    <div className="progress-columns">
      <section className="progress-column">
        <SectionHeading title={t('当前工作')} action={<Button onClick={() => onTab('memory', null, 'task', 'tasks')}>{t('查看全部')}</Button>} />
        <ResourceState resource={taskResource}>
          {rawTasks.length ? rawTasks.slice(0, 4).map(item => <button className="progress-entry" key={item.id} onClick={() => onTab('memory', null, 'task', 'tasks', item.id)}>
            <div className="meta-line"><span>{taskStatusLabel(item.status)}</span>
              {isDiscussionOnlyTask(item) && <Badge className="badge-guard-discussion">{t('仅讨论方案 · 未授权改代码')}</Badge>}
              {item.last_agent && <span>{AGENTS[item.last_agent] || item.last_agent}</span>}
            </div>
            <strong>{item.title}</strong>
            {(item.current_state || item.goal || item.content_preview) && <p>{item.current_state || item.goal || item.content_preview}</p>}
            <small>{formatTime(item.updated_at)} {item.git_branch && <code>{item.git_branch}</code>}</small>
          </button>) : <p className="progress-empty">{t('还没有任务进度记录。')}</p>}
        </ResourceState>
      </section>
      <section className="progress-column">
        <SectionHeading title={t('最近协作')} action={<Button onClick={() => onTab('conversations')}>{t('全部聊天')}</Button>} />
        <ResourceState resource={conversations}>
          {conversations.data?.items?.length ? conversations.data.items.map(item => <button className="progress-entry" key={item.id} onClick={() => onTab('conversations', item.id)}>
            <div className="meta-line">{AGENTS[item.agent] || item.agent}</div>
            <strong>{item.title}</strong><small>{formatTime(item.updated_at)}</small>
          </button>) : <p className="progress-empty">{t('这个项目还没有聊天记录。')}</p>}
        </ResourceState>
      </section>
    </div>
    <div className="progress-to-assets">
      <div><h3>{t('从项目积累能力')}</h3><p>{t('查看这段工作有哪些方法和案例可以存入你的个人能力库。')}</p></div>
      <Button onClick={onReviewAssets}>{t('查看能力线索')}</Button>
    </div>
  </section>;
}
export function ProjectsPage({
  projects,
  selectedId,
  route,
  onSelect,
  revision,
  onChanged,
  navigate,
  loading,
  error
}) {
  const [localTab, setTab] = useState('memory');
  const [memoryCategory, setMemoryCategory] = useState('');
  const [memorySection, setMemorySection] = useState('');
  const [selectedTaskId, setSelectedTaskId] = useState(null);
  const [archived, setArchived] = useState(false);
  const [organize, setOrganize] = useState(null);
  const [menu, setMenu] = useState(false);
  const [localContentId, setLocalContentId] = useState(null);
  const [conversationId, setConversationId] = useState(null);
  const [operationError, setOperationError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [localSearchTab, setGlobalTab] = useState(null);
  const globalTab = route
    ? route.section === 'search' && !selectedId
      ? (route.view === 'unassigned' ? 'conversations' : ['document','conversations'].includes(route.view) ? route.view : 'memory')
      : null
    : localSearchTab;
  const [globalConvScope, setGlobalConvScope] = useState('all');
  const [globalQuery, setGlobalQuery] = useState('');
  const [creating, setCreating] = useState(false);
  function showSearch(tab, scope = 'all') {
    setGlobalTab(tab);
    setGlobalConvScope(scope);
    if (route) navigate({section: tab ? 'search' : null, view: scope === 'unassigned' ? scope : tab, record: null});
  }
  const tab = route ? (TABS.find(([id]) => id === route.section)?.[0] || 'memory') : localTab;
  const contentId = route ? (tab === 'conversations' ? null : route.record) : localContentId;
  const memoryTrigger = useRef(null);
  const detailRef = useRef(null);
  useEffect(() => {
    if (contentId && tab === 'memory' && detailRef.current) detailRef.current.focus();
    else if (!contentId && memoryTrigger.current) {
      memoryTrigger.current.focus();
      memoryTrigger.current = null;
    }
  }, [contentId, tab]);
  function openReaderRecord(id, sourceKind = '') {
    if (id) memoryTrigger.current = document.activeElement;
    setLocalContentId(id);
    setMemorySection(sourceKind);
    if (route) navigate({record: id, view: sourceKind});
  }
  function setContentId(id) {
    setLocalContentId(id);
    if (route) navigate({record: id});
  }
  const activeView = route ? route.view : memorySection;
  const isManaged = tab === 'document' || (tab === 'memory' && ['tasks', 'manage'].includes(activeView));
  const menuRef = useRef(null);
  useEffect(() => {
    if (!menu) return;
    const trigger = menuRef.current?.querySelector('button');
    menuRef.current?.querySelector('.popover-menu button')?.focus();
    const pointer = event => {
      if (!menuRef.current?.contains(event.target)) setMenu(false);
    };
    const keydown = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setMenu(false);
        trigger?.focus();
      }
    };
    document.addEventListener('pointerdown', pointer);
    document.addEventListener('keydown', keydown);
    return () => {
      document.removeEventListener('pointerdown', pointer);
      document.removeEventListener('keydown', keydown);
    };
  }, [menu]);
  const project = projects.find(item => item.id === selectedId);
  const unassignedSummary = useResource(!project && !globalTab ? '/api/conversations/index?project_id=__unassigned__&limit=1' : null, revision);
  const unassignedTotal = unassignedSummary.data?.total || 0;
  function select(id) {
    if (route && globalTab) navigate({page: 'projects', project: id, section: null});
    else onSelect(id);
    setGlobalTab(null);
    setGlobalConvScope('all');
    setTab('memory');
    setMemoryCategory('');
    setMemorySection('');
    setSelectedTaskId(null);
    setLocalContentId(null);
    setConversationId(null);
  }
  function chooseTab(next, id, category = '', section = '', taskId = null) {
    setTab(next);
    setMemoryCategory(category === 'task' ? '' : (category || ''));
    setMemorySection(section || (category === 'task' ? 'tasks' : ''));
    setSelectedTaskId(taskId || null);
    setLocalContentId(next === 'document' ? id || null : null);
    setConversationId(id || null);
    if (route) navigate({section: next, record: id || null, view: section || (category === 'task' ? 'tasks' : '')});
  }
  async function archiveProject() {
    setBusy(true);
    setOperationError(null);
    try {
      await mutate(`/api/projects/${project.id}`, 'PATCH', {
        status: project.status === 'archived' ? 'active' : 'archived'
      });
      onChanged();
      setMenu(false);
    } catch (failure) {
      setOperationError(failure);
    } finally {
      setBusy(false);
    }
  }
  if (!project && globalTab) return <>
    <Button icon={ArrowLeft} onClick={() => showSearch(null)}>{t("返回所有项目")}</Button>
    <PageHeader title={t("搜索全部项目")} description={t("已归档的项目、还没定项目的内容，都能在这里搜到；项目归档不影响共享范围。")} />
    <SegmentedTabs label={t("全局内容")}>{TABS.filter(([id]) => ['memory', 'document', 'conversations'].includes(id)).map(([id, label]) => <button key={id} className={globalTab === id ? 'active' : ''} aria-current={globalTab === id ? 'page' : undefined} onClick={() => showSearch(id)}>{t(label)}</button>)}</SegmentedTabs>
    {globalTab === 'conversations' ? <ConversationsPage projects={projects} initialQuery={globalQuery} initialScope={route ? (route.view === 'unassigned' ? 'unassigned' : 'all') : globalConvScope} initialId={route?.record} onRecordChange={route ? id => navigate({record:id}) : undefined} revision={revision} onChanged={onChanged} onProject={select} /> : <KnowledgePage key={globalTab} projects={projects} kind={globalTab} recordId={route?.record} onRecordChange={route ? setContentId : undefined} initialQuery={globalQuery} revision={revision} onChanged={onChanged} onProject={select} />}
  </>;
  if (!project) return <>
    <ProjectLibrary projects={projects} query={globalQuery} setQuery={setGlobalQuery}
      archived={archived} setArchived={setArchived} selectedId={selectedId}
      error={error} loading={loading} onRetry={onChanged} onCreate={() => setCreating(true)}
      onOpen={select} onSearch={() => showSearch('memory')} unassignedTotal={unassignedTotal}
      onUnassigned={() => showSearch('conversations', 'unassigned')} />
    {creating ? <ProjectDialog mode="create" projects={projects} revision={revision} onClose={() => setCreating(false)} onSaved={saved => {
      onChanged();
      select(saved.id);
    }} /> : null}
  </>;
  return <section className="project-room">
    <div className="breadcrumb"><Button icon={ArrowLeft} onClick={() => select(null)}>{t("所有项目")}</Button>{project.status === 'archived' ? <Badge>{t("已归档 · 仍可检索")}</Badge> : null}</div>
    <PageHeader title={project.name} description={project.description || (project.directories?.length ? t("{count} 个关联目录 · 记忆全项目共享", { count: project.directories.length }) : undefined)} actions={<>
      <div className="popover-anchor" ref={menuRef} onBlur={event => {
        if (!event.currentTarget.contains(event.relatedTarget)) setMenu(false);
      }}>
        <Button icon={MoreHorizontal} onClick={() => setMenu(!menu)} aria-expanded={menu} aria-controls="project-actions" aria-label={t("项目操作")} />
        {menu ? <div className="popover-menu" id="project-actions"><button onClick={() => {
            setOrganize('edit');
            setMenu(false);
          }}>{t("修改名称与目录")}</button><button disabled={projects.length < 2} onClick={() => {
            setOrganize('merge');
            setMenu(false);
          }}><GitMerge size={15} />{t("合并项目")}</button><button onClick={() => {
            setOrganize('split');
            setMenu(false);
          }}><Split size={15} />{t("拆分项目")}</button><button disabled={busy} onClick={archiveProject}><Archive size={15} />{busy ? t("正在保存…") : project.status === 'archived' ? t("取消归档") : t("归档项目")}</button></div> : null}
      </div>
    </>} />
    <ErrorNotice error={operationError || error} />
    <SegmentedTabs label={t("项目内容")}>{TABS.map(([id, label, Icon]) => <button key={id} className={tab === id ? 'active' : ''} aria-current={tab === id ? 'page' : undefined} onClick={() => chooseTab(id)}><Icon size={16} aria-hidden="true"/>{t(label)}</button>)}</SegmentedTabs>
    <div className="tab-content" key={`${project.id}-${tab}`}>
      {tab === 'memory' && !isManaged && <>
        <div hidden={Boolean(contentId)}>
          <MemoryReader projectId={project.id} revision={revision}
            onOpen={id => openReaderRecord(id)}
            onConversations={() => chooseTab('conversations')}
            onSource={source => openReaderRecord(source.id, source.kind === 'conversation' ? 'source-conversation' : 'source-document')}
            onManage={() => chooseTab('memory', null, '', 'manage')}
            onTasks={() => chooseTab('memory', null, '', 'tasks')} />
        </div>
        {contentId && <div ref={detailRef} tabIndex={-1} className="reading-record">
          {activeView === 'source-conversation'
            ? <ConversationDetail key={contentId} id={contentId} projects={projects} revision={revision}
                onProject={select} onChanged={onChanged} backLabel={t('返回项目记忆')} onBack={() => openReaderRecord(null)} />
            : <ContentDetail key={contentId} id={contentId} projects={projects} revision={revision}
                onProject={select} onChanged={onChanged} backLabel={t('返回项目记忆')} onBack={() => openReaderRecord(null)} />}
        </div>}
      </>}
      {tab === 'overview' && contentId && <ContentDetail key={contentId} id={contentId} projects={projects}
        revision={revision} onProject={select} onBack={() => setContentId(null)} onChanged={onChanged} />}
      {tab === 'overview' && !contentId && <Overview project={project} revision={revision} onTab={chooseTab}
        onReviewAssets={() => navigate({page: 'personal', section: 'assets', project: project.id})} />}
      {tab === 'conversations' && <ConversationsPage projectId={project.id} projects={projects} revision={revision}
        onProject={select} onChanged={onChanged} initialId={route ? route.record : conversationId}
        onRecordChange={route ? id => navigate({record: id}) : undefined} onCloseInitial={() => setConversationId(null)} />}
      {isManaged && <KnowledgePage key={`${project.id}-${tab}-${memoryCategory}-${memorySection}`}
        rememberList={Boolean(route)} recordId={contentId} onRecordChange={route ? setContentId : undefined}
        onSectionChange={route ? next => navigate({view: next === 'tasks' ? 'tasks' : 'manage', record: null}) : undefined}
        projectId={project.id} projects={projects} kind={tab}
        initialCategory={tab === 'memory' ? memoryCategory : ''}
        initialSection={tab === 'memory' ? (activeView === 'manage' ? 'long_term' : activeView) : ''}
        initialTaskId={tab === 'memory' ? selectedTaskId : null} revision={revision} onChanged={onChanged} onProject={select} />}
    </div>
    {organize ? <ProjectDialog project={project} projects={projects} revision={revision} mode={organize} onClose={() => setOrganize(null)} onSaved={saved => {
      onChanged();
      if (saved.id !== project.id) select(saved.id);
    }} /> : null}
  </section>;
}
