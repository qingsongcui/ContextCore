import { ArrowRight, Clock3, FolderOpen, Plus, Search } from 'lucide-react';
import { Badge, Button, EmptyState, ErrorNotice, PageHeader, formatTime } from './ui.jsx';
import { t } from '../lib/i18n.jsx';

function latestActiveProject(projects) {
  return projects.reduce((latest, project) => {
    if (project.status === 'archived' || !project.updated_at) return latest;
    const timestamp = Date.parse(project.updated_at);
    if (!Number.isFinite(timestamp)) return latest;
    return !latest || timestamp > Date.parse(latest.updated_at) ? project : latest;
  }, null);
}

export function ProjectLibrary({projects, query, setQuery, archived, setArchived, selectedId, error, loading, onRetry, onCreate, onOpen, onSearch, unassignedTotal, onUnassigned}) {
  const needle = query.trim().toLocaleLowerCase();
  const visible = projects.filter(project => (
    (archived || project.status !== 'archived') &&
    [project.name, project.description, ...(project.directories || [])].some(value => value?.toLocaleLowerCase().includes(needle))
  ));
  const recent = !error && !loading && !needle && latestActiveProject(projects);
  return <section className="project-library">
    <PageHeader title={t('项目空间')} description={t('从一个项目进入，查看当前记忆、工作进度和原始记录。')}
      actions={<Button icon={Plus} variant="primary" disabled={loading} onClick={onCreate}>{t('新建项目')}</Button>} />
    {recent && <section className="project-resume">
      <div>
        <p className="project-context-label"><Clock3 size={15}/>{t('最近更新的项目')}</p>
        <h2>{recent.name}</h2>
        {recent.description && <p className="resume-description">{recent.description}</p>}
        <small>{t('最近活动：{time}', {time: formatTime(recent.updated_at)})}</small>
      </div>
      <Button onClick={() => onOpen(recent.id)}>{t('继续项目')}<ArrowRight size={16}/></Button>
    </section>}
    <div className="project-index-header"><h2>{t('项目目录')} <span>{error || loading ? '—' : visible.length}</span></h2>
      <label className="check-label"><input type="checkbox" checked={archived} onChange={event => setArchived(event.target.checked)} />{t('显示已归档')}</label>
    </div>
    <form className="project-library-search" onSubmit={event => {event.preventDefault(); onSearch();}}>
      <label><Search size={17}/><input aria-label={t('搜索内容')} placeholder={t('按名称、说明或目录筛选项目')} value={query} onChange={event => setQuery(event.target.value)} /></label>
      <Button type="submit">{t('搜索全部内容')}<ArrowRight size={14}/></Button>
    </form>
    {error && <><ErrorNotice error={error}/><Button onClick={onRetry}>{t('重新读取项目')}</Button></>}
    {selectedId && !loading && !error && <p className="operation-notice" role="status">{t('这个项目现在打不开，从下面另选一个，或者搜全部内容。')}</p>}
    {loading && !projects.length ? <div className="skeleton-stack" aria-label={t('正在读取项目')} aria-busy="true"><span/><span/></div> :
      !error && visible.length ? <ul className="project-directory">{visible.map(project => <li key={project.id}>
        <button type="button" className="project-card directory-row" onClick={() => onOpen(project.id)}>
          <span className="directory-symbol"><FolderOpen size={20}/></span>
          <span className="directory-title"><strong>{project.name}</strong>{(project.description || project.directories?.[0]) && <small>{project.description || project.directories[0]}</small>}</span>
          <span className="directory-meta">{project.status === 'archived' && <Badge>{t('已归档')}</Badge>}
            <small>{t('{memories} 条记忆 · {conversations} 次对话', {memories: project.counts?.memories ?? t('未知'), conversations: project.counts?.conversations ?? t('未知')})}</small>
            {project.updated_at && <time dateTime={project.updated_at}>{t('最近活动：{time}', {time: formatTime(project.updated_at)})}</time>}
          </span>
          <ArrowRight className="directory-arrow" size={17}/>
        </button></li>)}</ul> :
      !error && needle ? <EmptyState title={t('没有匹配的项目')} body={t('试试其他关键词，或在全部内容中查找记忆、对话和资料。')} action={<Button onClick={() => setQuery('')}>{t('清除筛选')}</Button>}/> :
      !error && projects.length ? <EmptyState title={t('项目已归档')} body={t('归档的内容仍然保留，Agent 也还能检索到。')} action={<Button onClick={() => setArchived(true)}>{t('查看已归档项目')}</Button>}/> :
      !error ? <EmptyState title={t('创建第一个项目')} body={t('为手头的工作命名，也可以关联本机目录。项目记忆和对话会在这里汇集。')} action={<Button icon={Plus} onClick={onCreate}>{t('新建项目')}</Button>}/> : null}
    {unassignedTotal > 0 && <aside className="project-unassigned" aria-label={t('未归属项目的聊天')}>
      <span>{t('另有 {count} 条未绑定具体项目目录的本地聊天记录（如随手提问或无目录会话）。', {count: unassignedTotal})}</span>
      <Button onClick={onUnassigned}>{t('查看未归属聊天')}<ArrowRight size={14}/></Button></aside>}
  </section>;
}
