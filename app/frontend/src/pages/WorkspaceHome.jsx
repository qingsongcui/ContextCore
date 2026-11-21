import { useState } from 'react';
import {
  ArrowRight, ArrowUpRight, BookOpen, ContactRound, Folder, FolderOpen,
  Layers, LibraryBig, Monitor, Plus, Plug,
} from 'lucide-react';
import { ProjectDialog } from '../components/ProjectDialog.jsx';
import { ResourceState, formatTime } from '../components/ui.jsx';
import { t } from '../lib/i18n.jsx';
import { useResource } from '../lib/useResource.js';
import '../styles/workspace-home.css';

function HomeLink({ icon: Icon, label, detail, onClick }) {
  return <button type="button" className="home-list-link" onClick={onClick}>
    <span className="home-list-symbol"><Icon size={18} aria-hidden="true" /></span>
    <span className="home-link-copy"><strong>{label}</strong>{detail && <small>{detail}</small>}</span>
    <ArrowRight size={15} aria-hidden="true" />
  </button>;
}

function CurrentProject({ project, archivedOnly, onCreate, navigate }) {
  const open = section => navigate({ page: 'projects', project: project.id, section });
  return <section className="home-current" aria-labelledby="current-project-heading">
    <div className="home-current-copy">
      <div className="home-kicker"><FolderOpen size={16} aria-hidden="true" />
        {project ? t('最近更新的项目') : t('项目空间')}
      </div>
      <h2 id="current-project-heading">{project ? project.name : archivedOnly ? t('开启下一个项目') : t('建立你的第一个项目')}</h2>
      {project
        ? project.description && <p className="home-project-description">{project.description}</p>
        : <p>{t('为手头的工作建一个项目，把相关对话、决定和资料收在一起。')}</p>}
    </div>
    {project ? <div className="home-current-footer">
      <button type="button" className="home-primary" onClick={() => open('memory')}>{t('继续项目')}<ArrowRight size={16} aria-hidden="true" /></button>
      {project.updated_at && <time dateTime={project.updated_at}>{formatTime(project.updated_at)}</time>}
    </div> : <div className="home-current-footer">
      <button type="button" className="home-primary" onClick={onCreate}><Plus size={16} aria-hidden="true" />{t('新建项目')}</button>
      {archivedOnly && <button type="button" className="home-on-dark" onClick={() => navigate({ page: 'projects', project: null })}>{t('项目库')}<ArrowRight size={14} aria-hidden="true" /></button>}
    </div>}
  </section>;
}

function ProjectShortcuts({ project, revision, navigate }) {
  const result = useResource('/api/workbench/memories?project_id=' + encodeURIComponent(project.id) + '&limit=1', revision);
  const memory = result.data?.items?.[0];
  const open = (section, record) => navigate({ page: 'projects', project: project.id, section, ...(record ? { record } : {}) });
  return <>
    <HomeLink icon={BookOpen} label={memory?.heading || t('阅读项目记忆')} detail={memory?.heading ? t('生效中的记忆 · {project}', { project: project.name }) : project.name} onClick={() => open('memory', memory?.id)} />
    <HomeLink icon={LibraryBig} label={t('查阅原始对话')} detail={Number.isInteger(project.counts?.conversations) ? t('{count} 次对话 · {project}', { count: project.counts.conversations, project: project.name }) : project.name} onClick={() => open('conversations')} />
  </>;
}

function IdentityCard({ revision, navigate }) {
  const profile = useResource('/api/workbench/profile/export', revision);
  const { name, bio, assets = [] } = profile.data || {};
  const hasCard = Boolean(name || bio || assets.length);
  return <section className="home-identity" aria-labelledby="home-identity-heading">
    <h2 className="home-kicker" id="home-identity-heading"><ContactRound size={16} aria-hidden="true" />{t('Agent 名片')}</h2>
    <ResourceState resource={profile}>
      <div className="home-identity-copy">
        <h3>{name || t('我的 Agent')}</h3>
        {bio ? <p>{bio}</p> : !hasCard && <p>{t('写下你擅长什么，选出能代表你的作品。')}</p>}
        {assets.length > 0 && <small>{t('已选入名片的作品 · {count}', { count: assets.length })}</small>}
      </div>
      <div className="home-card-actions"><button type="button" className="home-card-action" onClick={() => navigate({ page: 'personal', section: 'profile' })}>
        {hasCard ? t('查看与编辑名片') : t('编辑我的名片')}<ArrowUpRight size={15} aria-hidden="true" />
      </button>
        {assets.length > 0 && <button type="button" className="home-card-action" onClick={() => navigate({ page: 'exchange' })}>{t('起草协作说明')}<ArrowRight size={15} aria-hidden="true" /></button>}
      </div>
    </ResourceState>
  </section>;
}

export default function WorkspaceHome({ navigate, projects = [], loading, error, revision, onRetry }) {
  const [creating, setCreating] = useState(false);
  // The project index is ordered by the latest effective activity, including conversations and tasks.
  const active = projects.filter(project => project.status !== 'archived');
  const [current, ...others] = active;
  const showProjectData = !error && (!loading || projects.length > 0);
  const resource = { data: showProjectData ? projects : null, loading, error, reload: onRetry };
  const openPersonal = section => navigate({ page: 'personal', section });
  return <div className="workspace-home">
    <header className="home-heading">
      <h1>{t('工作台')}</h1>
      <span className="home-local"><Monitor size={15} aria-hidden="true" />{t('本机工作空间')}</span>
    </header>
    <div className="home-feature">
      <ResourceState resource={resource}>
        <CurrentProject project={current} archivedOnly={!current && projects.length > 0} navigate={navigate} onCreate={() => setCreating(true)} />
      </ResourceState>
      <IdentityCard revision={revision} navigate={navigate} />
    </div>
    <div className="home-index">
      {showProjectData && <section className="home-group" aria-labelledby="home-project-index">
        <div className="home-group-heading">
          <h2 id="home-project-index">{current ? t('项目速览') : t('接入工作记录')}</h2>
          {current && <button type="button" className="home-text-action" onClick={() => navigate({ page: 'projects', project: null })}>{t('项目库')}<ArrowUpRight size={14} aria-hidden="true" /></button>}
        </div>
        {current ? <>
          <ProjectShortcuts project={current} revision={revision} navigate={navigate} />
          {others.slice(0, 2).map(project => <HomeLink key={project.id} icon={Folder} label={project.name} detail={project.updated_at && formatTime(project.updated_at)} onClick={() => navigate({ page: 'projects', project: project.id, section: 'memory' })} />)}
        </> : <HomeLink icon={Plug} label={t('连接本机 Agent')} detail={t('选择要接入的工具，授权读取它的工作记录。')} onClick={() => navigate({ page: 'connections' })} />}
        {!current && !loading && !error && <button type="button" className="home-text-action home-browse" onClick={() => navigate({ page: 'projects', project: null })}>{t('浏览全部项目与对话')}<ArrowRight size={14} aria-hidden="true" /></button>}
      </section>}
      <section className="home-group" aria-labelledby="home-assets-index">
        <div className="home-group-heading"><h2 id="home-assets-index">{t('个人积累')}</h2></div>
        <HomeLink icon={BookOpen} label={t('个人记忆')} detail={t('跨项目沿用的偏好和工作约定')} onClick={() => openPersonal('memory')} />
        <HomeLink icon={Layers} label={t('能力与作品')} detail={t('从项目整理案例和可复用的 Skill')} onClick={() => openPersonal('assets')} />
      </section>
    </div>
    {creating && <ProjectDialog mode="create" projects={projects} revision={revision} onClose={() => setCreating(false)} onSaved={saved => {
      onRetry();
      navigate({ page: 'projects', project: saved.id, section: 'memory' });
    }} />}
  </div>;
}
