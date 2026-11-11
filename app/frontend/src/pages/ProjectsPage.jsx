import { t } from "../lib/i18n.jsx";
import { useEffect, useRef, useState } from 'react';
import { Archive, ArrowLeft, ArrowRight, ChevronRight, FolderOpen, GitMerge, MoreHorizontal, Search, Split } from 'lucide-react';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { AGENTS, Badge, Button, EmptyState, ErrorNotice, PageHeader, ResourceState, SectionHeading, SegmentedTabs, formatTime } from '../components/ui.jsx';
import { ProjectDialog } from '../components/ProjectDialog.jsx';
import { KnowledgePage, ContentDetail } from './KnowledgePage.jsx';
import { ConversationsPage } from './ConversationsPage.jsx';
const TABS = [['overview', '概览'], ['conversations', '聊天'], ['memory', '记忆'], ['document', '资料']];
function isDaytimeBySystemClock(now = new Date()) {
  const hour = now.getHours();
  return hour >= 6 && hour < 18;
}
function CelestialOrbitArt() {
  const [isDaytime, setIsDaytime] = useState(() => isDaytimeBySystemClock());
  useEffect(() => {
    const syncClock = () => setIsDaytime(isDaytimeBySystemClock());
    const timer = window.setInterval(syncClock, 60_000);
    window.addEventListener('focus', syncClock);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('focus', syncClock);
    };
  }, []);
  return (
    <div
      className={`orbit-art ${isDaytime ? 'is-sun' : 'is-moon'}`}
      data-celestial={isDaytime ? 'sun' : 'moon'}
      onClick={() => setIsDaytime(current => !current)}
      aria-hidden="true"
    >
      {Array.from({ length: 16 }, (_, index) => (
        <i key={index} style={{ '--i': index }} />
      ))}
      <span className="celestial-body">
        {isDaytime ? (
          <svg className="celestial-svg sun-svg" viewBox="0 0 120 120" fill="none">
            <circle className="sun-halo" cx="60" cy="60" r="46" />
            <g className="sun-rays">
              {Array.from({ length: 12 }, (_, idx) => (
                <line
                  key={idx}
                  x1="60"
                  y1="10"
                  x2="60"
                  y2={idx % 2 === 0 ? '17' : '14'}
                  transform={`rotate(${idx * 30} 60 60)`}
                />
              ))}
            </g>
            <circle className="sun-ring" cx="60" cy="60" r="36" />
            <circle className="sun-core" cx="60" cy="60" r="24" />
          </svg>
        ) : (
          <svg className="celestial-svg moon-svg" viewBox="0 0 120 120" fill="none">
            <g className="moon-crescent-group">
              <path
                className="moon-crescent"
                d="M 54.6 26.4 A 34 34 0 1 0 92.4 70.5 A 29 29 0 0 1 54.6 26.4 Z"
              />
              <path
                className="moon-sparkle sparkle-one"
                d="M78 34L79.3 37.5L82.8 38.8L79.3 40.1L78 43.6L76.7 40.1L73.2 38.8L76.7 37.5Z"
              />
              <path
                className="moon-sparkle sparkle-two"
                d="M90 50L90.8 52.2L93 53L90.8 53.8L90 56L89.2 53.8L87 53L89.2 52.2Z"
              />
            </g>
          </svg>
        )}
      </span>
    </div>
  );
}
function Overview({
  project,
  revision,
  onTab,
  onContent
}) {
  const taskResource = useResource(`/api/content/index?project_id=${encodeURIComponent(project.id)}&status=active&category=task&limit=4&offset=0`, revision);
  const decisionResource = useResource(`/api/content/index?project_id=${encodeURIComponent(project.id)}&status=active&category=decision&limit=4&offset=0`, revision);
  const conversations = useResource(`/api/conversations/index?project_id=${encodeURIComponent(project.id)}&limit=4`, revision);
  const decisions = decisionResource.data?.items || [];
  const tasks = taskResource.data?.items || [];
  return <div className="overview"><section className="project-intro"><div><h2>{t("每一步，都有来处。")}</h2><p>{project.description || t("项目的背景、进度和经验都在这儿。新 Agent 不用从头认识它。")}</p></div><CelestialOrbitArt /></section><div className="overview-counts">{[['conversations', t("原始对话"), project.counts?.conversations, ''], ['memory', t("项目记忆"), project.counts?.memories, ''], ['document', t("知识资料"), project.counts?.documents, ''], ['memory', t("任务记录"), project.counts?.tasks, 'task']].map(([tab, label, count, category]) => <button key={`${tab}-${label}`} onClick={() => onTab(tab, null, category)}><strong>{count ?? '—'}</strong><span>{label}<ArrowRight size={14} /></span></button>)}</div><div className="overview-columns"><section className="group"><SectionHeading title={t("当前工作")} action={<Button onClick={() => onTab('memory', null, 'task')}>{t("查看全部")}</Button>} /><ResourceState resource={taskResource}>{tasks.length ? tasks.map(item => <button className="overview-item" key={item.id} onClick={() => onContent(item.id)}><span className="small-orbit" /><div><h3>{item.title}</h3><p>{item.content_preview}</p><small>{formatTime(item.updated_at)}</small></div><ChevronRight className="row-chevron" size={15} aria-hidden="true" /></button>) : <p className="section-empty">{t("还没有任务进度记录。")}</p>}</ResourceState></section><section className="group"><SectionHeading title={t("重要决定")} action={<Button onClick={() => onTab('memory')}>{t("查看记忆")}</Button>} /><ResourceState resource={decisionResource}>{decisions.length ? decisions.map(item => <button className="overview-item" key={item.id} onClick={() => onContent(item.id)}><div><h3>{item.title}</h3><p>{item.content_preview}</p></div><ChevronRight className="row-chevron" size={15} aria-hidden="true" /></button>) : <p className="section-empty">{t("经过确认的决定会在这里留下依据。")}</p>}</ResourceState></section></div><section className="group"><SectionHeading title={t("最近协作")} action={<Button onClick={() => onTab('conversations')}>{t("全部聊天")}</Button>} /><ResourceState resource={conversations}>{conversations.data?.items?.length ? conversations.data.items.map(item => <button className="recent-conversation" key={item.id} onClick={() => onTab('conversations', item.id)}><span className="agent-monogram">{item.agent?.slice(0, 1).toUpperCase()}</span><div><strong>{item.title}</strong><small>{AGENTS[item.agent] || item.agent} · {formatTime(item.updated_at)}</small></div><Badge>{item.processing_status === 'processed' ? t("已处理") : item.processing_status === 'error' ? t("处理失败") : t("待整理")}</Badge><ChevronRight className="row-chevron" size={15} aria-hidden="true" /></button>) : <p className="section-empty">{t("这个项目还没有聊天记录。")}</p>}</ResourceState></section></div>;
}
export function ProjectsPage({
  projects,
  selectedId,
  onSelect,
  revision,
  onChanged,
  navigate,
  loading,
  error
}) {
  const [tab, setTab] = useState('overview');
  const [memoryCategory, setMemoryCategory] = useState('');
  const [archived, setArchived] = useState(false);
  const [organize, setOrganize] = useState(null);
  const [menu, setMenu] = useState(false);
  const [contentId, setContentId] = useState(null);
  const [conversationId, setConversationId] = useState(null);
  const [operationError, setOperationError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [globalTab, setGlobalTab] = useState(null);
  const [globalQuery, setGlobalQuery] = useState('');
  const [creating, setCreating] = useState(false);
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
  const queryLower = globalQuery.trim().toLocaleLowerCase();
  const visibleProjects = projects.filter(item => (archived || item.status !== 'archived') && (!queryLower || [item.name, item.description, ...(item.directories || [])].filter(Boolean).join(' ').toLocaleLowerCase().includes(queryLower)));
  function select(id) {
    onSelect(id);
    setTab('overview');
    setMemoryCategory('');
    setContentId(null);
    setConversationId(null);
  }
  function chooseTab(next, id, category = '') {
    setTab(next);
    setMemoryCategory(category || '');
    setContentId(null);
    setConversationId(id || null);
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
    <Button icon={ArrowLeft} onClick={() => setGlobalTab(null)}>{t("返回所有项目")}</Button>
    <PageHeader title={t("搜索全部项目")} description={t("已归档的项目、还没定项目的内容，都能在这里搜到；项目归档不影响共享范围。")} />
    <SegmentedTabs label={t("全局内容")}>{TABS.filter(([id]) => ['memory', 'document', 'conversations'].includes(id)).map(([id, label]) => <button key={id} className={globalTab === id ? 'active' : ''} aria-current={globalTab === id ? 'page' : undefined} onClick={() => setGlobalTab(id)}>{t(label)}</button>)}</SegmentedTabs>
    {globalTab === 'conversations' ? <ConversationsPage projects={projects} revision={revision} onChanged={onChanged} onProject={select} /> : <KnowledgePage key={globalTab} projects={projects} kind={globalTab} initialQuery={globalQuery} revision={revision} onChanged={onChanged} onProject={select} />}
  </>;
  if (!project) return <>
    <PageHeader title={t("工作继续，记忆留下。")} description={t("先建一个项目记录工作，再选择助手接着做。已有聊天与记忆也能在这里找到。")} actions={<><Button variant="primary" onClick={() => setCreating(true)} disabled={loading}>{t("新建项目")}</Button><Button onClick={() => navigate('agents')}>{t("我的助手")}<ArrowRight size={15} /></Button></>} />
    <form className="search-bar" onSubmit={event => { event.preventDefault(); setGlobalTab('memory'); }}>
      <Search size={18} />
      <input aria-label={t("搜索内容")} placeholder={t("搜索标题、正文或关键词…")} value={globalQuery} onChange={event => setGlobalQuery(event.target.value)} />
      <Button type="submit">{t("搜索全部内容")}</Button>
    </form>
    <div className="project-list-heading"><h2>{t("你的项目")} <span>{visibleProjects.length}</span></h2><div className="toolbar-actions"><Button onClick={() => setGlobalTab('memory')}>{t("搜索全部内容")}</Button><label className="check-label"><input type="checkbox" checked={archived} onChange={event => setArchived(event.target.checked)} />{t("显示已归档")}</label></div></div>
    {error ? <><ErrorNotice error={error} /><Button onClick={onChanged}>{t("重新读取项目")}</Button></> : null}
    {selectedId && !loading && !error ? <p className="operation-notice" role="status">{t("这个项目现在打不开，从下面另选一个，或者搜全部内容。")}</p> : null}
    {loading && !projects.length ? <div className="skeleton-stack" aria-label={t("正在读取项目")} aria-busy="true"><span /><span /></div> : visibleProjects.length ? <div className="project-grid">{visibleProjects.map((item, index) => <button className="project-card" key={item.id} onClick={() => select(item.id)}><div className="project-card-top"><span className="project-number">{String(index + 1).padStart(2, '0')}</span>{item.status === 'archived' ? <Badge>{t("已归档")}</Badge> : <FolderOpen size={19} />}</div><h3>{item.name}</h3><p>{item.description || t("从真实协作记录里积累项目的来龙去脉。")}</p><footer><span>{t("{memories} 条记忆 · {conversations} 次对话", { memories: item.counts?.memories ?? t("未知"), conversations: item.counts?.conversations ?? t("未知") })}</span><ArrowRight size={18} /></footer></button>)}</div> : !error && projects.length ? <EmptyState title={t("项目已归档")} body={t("归档的内容仍然保留，Agent 也还能检索到。")} action={<Button onClick={() => setArchived(true)}>{t("查看已归档项目")}</Button>} /> : !error ? <EmptyState title={t("从你已经做过的事开始")} body={t("先建一个项目记录工作，再选择助手接着做。已有聊天与记忆也能在这里找到。")} action={<Button onClick={() => navigate('agents')}>{t("我的助手")}<ArrowRight size={16} /></Button>} /> : null}
    <p className="workspace-footnote">{t("本机保存 · 全项目共享 · 可追溯的记忆")}</p>
    {creating ? <ProjectDialog mode="create" projects={projects} revision={revision} onClose={() => setCreating(false)} onSaved={saved => {
      onChanged();
      select(saved.id);
    }} /> : null}
  </>;
  return <>
    <div className="breadcrumb"><Button icon={ArrowLeft} onClick={() => select(null)}>{t("所有项目")}</Button><span>/</span><span>{project.name}</span>{project.status === 'archived' ? <Badge>{t("已归档 · 仍可检索")}</Badge> : null}</div>
    <PageHeader title={project.name} description={project.directories?.length ? t("{count} 个关联目录 · 记忆全项目共享", { count: project.directories.length }) : t("按主题管理，不必绑定某个目录")} actions={<>
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
    <SegmentedTabs label={t("项目内容")}>{TABS.map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} aria-current={tab === id ? 'page' : undefined} onClick={() => chooseTab(id)}>{t(label)}</button>)}</SegmentedTabs>
    <div className="tab-content" key={`${project.id}-${tab}`}>
      {contentId ? <ContentDetail key={contentId} id={contentId} projects={projects} revision={revision} onProject={select} onBack={() => setContentId(null)} onChanged={onChanged} /> : tab === 'overview' ? <Overview project={project} revision={revision} onTab={chooseTab} onContent={setContentId} /> : tab === 'conversations' ? <ConversationsPage projectId={project.id} projects={projects} revision={revision} onProject={select} onChanged={onChanged} initialId={conversationId} onCloseInitial={() => setConversationId(null)} /> : <KnowledgePage key={`${project.id}-${tab}-${memoryCategory}`} projectId={project.id} projects={projects} kind={tab} initialCategory={tab === 'memory' ? memoryCategory : ''} revision={revision} onChanged={onChanged} onProject={select} />}
    </div>
    {organize ? <ProjectDialog project={project} projects={projects} revision={revision} mode={organize} onClose={() => setOrganize(null)} onSaved={saved => {
      onChanged();
      if (saved.id !== project.id) select(saved.id);
    }} /> : null}
  </>;
}
