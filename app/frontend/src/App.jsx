import { nextLocation } from './lib/navigation.js';
import { t } from "./lib/i18n.jsx";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from 'react';
import { AlertCircle, RefreshCw } from 'lucide-react';
import { Shell } from './components/Shell.jsx';
import { Button, PageHeader } from './components/ui.jsx';
import { PersonalPage } from './pages/PersonalPage.jsx';
import { ProjectsPage } from './pages/ProjectsPage.jsx';
import { ConsolePage } from './pages/ConsolePage.jsx';
import { InboxPage } from './pages/InboxPage.jsx';
import { SettingsPage } from './pages/SettingsPage.jsx';
const AgentManagementPage = lazy(() => import('./pages/AgentManagementPage.jsx').then(module => ({ default: module.AgentManagementPage })));
const UsagePage = lazy(() => import('./pages/UsagePage.jsx').then(module => ({ default: module.UsagePage })));
import { AuditPage } from './pages/AuditPage.jsx';
import { ThemeProvider } from './lib/theme.js';
import { I18nProvider, useI18n } from './lib/i18n.jsx';
import { useResource } from './lib/useResource.js';
import { ObjectRefreshContext } from './lib/objectRefresh.js';
import { backendCompatibility } from './lib/management.js';
import WorkspaceHome from './pages/WorkspaceHome.jsx';
import ExchangePage from './pages/ExchangePage.jsx';
const PAGES = new Set(['home', 'exchange', 'projects', 'personal', 'connections', 'conflicts', 'audit', 'settings', 'agents', 'usage']);
function locationState() {
  const params = new URLSearchParams(window.location.search);
  return {
    page: PAGES.has(params.get('page')) ? params.get('page') : params.has('project') ? 'projects' : 'home',
    project: params.get('project') || null,
    record: params.get('record') || null,
    view: params.get('view') || null,
    agent: params.get('agent') || null,
    request: params.get('request') || null,
    section: params.get('section') || null
  };
}
function Workbench() {
  const { lang } = useI18n();
  const exchangeDraft = useRef(null);
  const personalDraft = useRef(null);
  const assistantDrafts = useRef(new Map());
  const [location, setLocation] = useState(locationState);
  useEffect(() => {
    const titles = { home: '工作台', exchange: '协作', projects: '项目', personal: ({ memory: '个人记忆', assets: '能力与作品', profile: 'Agent 名片' })[location.section] || '个人记忆', agents: location.section === 'library' && !location.agent ? '全局资源库' : 'Agent 管理', usage: '用量与性能', settings: '设置', conflicts: '待确认', audit: '变更记录', connections: '接入与权限' };
    const label = titles[location.page];
    document.title = label ? 'Senrelay — ' + t(label) : t('Senrelay · 续境 — 项目工作台');
  }, [location.page, location.section, location.agent, lang]);
  const pageRef = useRef(location.page);
  pageRef.current = location.page;
  const [revision, setRevision] = useState(0);
  const [objectRefresh, setObjectRefresh] = useState({ manual: 0, events: [] });
  const [connected, setConnected] = useState(false);
  const status = useResource('/api/status', revision);
  const reloadStatus = status.reload;
  const projects = useResource('/api/projects', revision);
  const management = useResource('/api/manage/status', revision);
  const managementState = backendCompatibility(management);
  const wasReady = useRef(false);
  if (managementState === 'ready') wasReady.current = true;
  const needsManagement = ['agents', 'usage'].includes(location.page);
  const changed = useCallback(() => { setRevision(value => value + 1); setObjectRefresh(previous => ({ manual: previous.manual + 1, events: [] })); }, []);
  const shutdownPending = !status.error && status.data?.service?.stop_status === 'pending';
  useEffect(() => {
    if (!shutdownPending || status.loading) return;
    const timer = setTimeout(status.reload, 1000);
    return () => clearTimeout(timer);
  }, [shutdownPending, status.loading, status.reload]);
  useEffect(() => {
    const onPop = () => setLocation(locationState());
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);
  useEffect(() => {
    const source = new EventSource('/api/events');
    let pending;
    let lastRefresh = 0;
    let events = [];
    const update = event => {
      if (event?.type === 'audit.created') {
        try {
          const action = JSON.parse(event.data).action;
          if (action?.startsWith('usage.')) return;
          if (action === 'connection.sync' && !['audit', 'connections'].includes(pageRef.current)) return;
        } catch { /* An unknown event still invalidates the visible data. */ }
      }
      if (document.visibilityState !== 'visible') return;
      let data;
      try { data = event?.data ? JSON.parse(event.data) : {}; } catch { data = {}; }
      if (events.length < 200) events.push({ type: event?.type || 'changed', data });
      else events = [{ type: 'changed' }];
      if (pending) return;
      pending = setTimeout(() => {
        pending = null;
        if (document.visibilityState !== 'visible') return;
        lastRefresh = Date.now();
        setRevision(value => value + 1);
        const batch = events;
        events = [];
        setObjectRefresh(previous => ({ manual: previous.manual, events: batch }));
      }, Math.max(250, 2000 - (Date.now() - lastRefresh)));
    };
    source.onopen = () => {
      setConnected(true);
      reloadStatus();
      update();
    };
    source.onerror = () => {
      setConnected(false);
      reloadStatus();
    };
    source.onmessage = update;
    for (const event of ['changed', 'content.changed', 'conversation.changed', 'connection.changed', 'conflict.changed', 'handoff.changed', 'audit.created']) source.addEventListener(event, update);
    const visible = () => {
      if (document.visibilityState === 'visible') update();
    };
    document.addEventListener('visibilitychange', visible);
    return () => {
      clearTimeout(pending);
      source.close();
      document.removeEventListener('visibilitychange', visible);
    };
  }, [changed, reloadStatus]);
  function go(next, options = {}) {
    const value = nextLocation(location, next, options);
    const params = new URLSearchParams();
    params.set('page', value.page);
    if (value.page === location.page && value.project === location.project && value.section === location.section && value.view === location.view) {
      for (const [key, val] of new URLSearchParams(window.location.search)) {
        if (key.startsWith('filter_')) params.set(key, val);
      }
    }
    if (value.project) params.set('project', value.project);
    if (value.agent) params.set('agent', value.agent);
    if (value.section) params.set('section', value.section);
    if (value.record) params.set('record', value.record);
    if (value.view) params.set('view', value.view);
    if (value.request) params.set('request', value.request);
    const previousParams = new URLSearchParams(window.location.search);
    if (previousParams.has('lang')) params.set('lang', lang);
    if (params.toString() === previousParams.toString()) return;
    window.history.pushState(null, '', `?${params}`);
    setLocation(value);
    if (value.page !== location.page || value.project !== location.project || value.section !== location.section) window.scrollTo({ top: 0, behavior: 'instant' });
  }
  const activeStatus = status.error ? null : status.data;
  function renderPage() {
    const projectList = projects.data || [];
    switch (location.page) {
      case 'home':
        return <WorkspaceHome navigate={go} projects={projectList} loading={projects.loading}
          error={projects.error} revision={revision} onRetry={changed} />;
      case 'exchange':
        return <ExchangePage navigate={go} revision={revision} draftStore={exchangeDraft} />;
      case 'personal':
        return <PersonalPage route={location} navigate={go} projects={projectList}
          revision={revision} onChanged={changed} draftStore={personalDraft} />;
      case 'projects':
        return <ProjectsPage route={location} projects={projectList} selectedId={location.project}
          onSelect={project => go({ project })} revision={revision} onChanged={changed}
          navigate={go} loading={projects.loading} error={projects.error} />;
      case 'agents':
        return <AgentManagementPage revision={revision} onChanged={changed} projects={projectList}
          agentId={location.agent} section={location.section} libraryView={location.view} libraryRecord={location.record}
          navigate={go} draftStore={assistantDrafts}
          onAgentChange={(agent, section) => go({ page: 'agents', agent, section })} />;
      case 'usage':
        return <UsagePage projects={projectList} projectId={location.project} agentId={location.agent}
          section={location.section} requestId={location.request} navigate={go}
          onSectionChange={section => go({page: 'usage', agent: location.agent, project: location.project, section})} />;
      case 'connections':
        return <ConsolePage key={location.agent || 'all'} agentId={location.agent} navigate={go}
          revision={revision} onChanged={changed} managementAvailable={managementState === 'ready'} />;
      case 'conflicts':
        return <InboxPage revision={revision} onChanged={changed} />;
      case 'audit':
        return <AuditPage revision={revision} />;
      case 'settings':
        return <SettingsPage revision={revision} status={activeStatus} onChanged={changed}
          navigate={go} managementAvailable={managementState === 'ready'} />;
    }
  }
  return <ObjectRefreshContext.Provider value={objectRefresh}><a href="#main-content" className="skip-link">{t("跳到主要内容")}</a><Shell lang={lang} agent={location.agent} page={location.page} section={location.section} navigate={go} projects={projects.data || []} selectedProject={location.project} onProject={project => go({page: 'projects', project, section: 'memory'})} status={activeStatus} statusLoading={status.loading} managementState={managementState}>
    {import.meta.env.VITE_ISOLATED_PREVIEW ? <div className="connection-note" role="status">{t('隔离开发验收环境：此处合成记录仅用于测试，不代表真实会话、模型调用或生产配置。')}</div> : null}
    {managementState === 'upgrade_required' && !needsManagement ? <div className="connection-note" role="status"><span>{t('新增管理功能需要更新本机服务；现有项目和 Agent 接入仍可使用。')}</span><Button onClick={management.reload}>{t('重新检查服务')}</Button></div> : null}
    {status.error ? <div className="service-banner" role="alert"><AlertCircle size={18} /><div><strong>{t("本机服务暂未连接")}</strong><p>{t(status.error.message)}</p></div><Button icon={RefreshCw} onClick={changed}>{t("重试")}</Button></div> : !connected && status.data ? <div className="connection-note" role="status">{t("实时更新正在重连，当前内容可能不是最新。")}<button onClick={changed}>{t("刷新")}</button></div> : null}
    {activeStatus?.service?.stop_status === 'failed' ? <div className="service-banner" role="alert"><AlertCircle size={18} /><div><strong>{t("本机服务停止失败")}</strong><p>{t("服务没有完成停止，请检查运行状态后再决定是否重试。")}</p></div><Button onClick={() => go({
          page: 'settings'
        })}>{t("查看运行设置")}</Button></div> : shutdownPending ? <div className="connection-note" role="status">{t("正在等待本机服务停止结果，停止请求已提交，但尚未确认退出。")}</div> : activeStatus?.service?.stop_status === 'stopped' ? <div className="connection-note" role="status">{t("服务已报告停止。再次使用前请启动本机服务。")}</div> : null}
    {activeStatus?.counts?.pending_conflicts > 0 && location.page !== 'conflicts' ? <div className="connection-note" role="status"><span>{t("{count} 项记忆有分歧，等你确认；普通记忆仍会自动生效。", { count: activeStatus.counts.pending_conflicts })}</span><Button onClick={() => go({
          page: 'conflicts'
        })}>{t("查看待确认")}</Button></div> : null}
    {needsManagement && wasReady.current && managementState === 'unavailable' ? <div className="connection-note" role="status"><span>{t('暂时无法确认新功能是否可用')}</span><Button onClick={management.reload} busy={management.loading}>{t('重新检查服务')}</Button></div> : null}
    <div className="page-surface" key={`${location.page}:${location.project || ''}`}><Suspense fallback={<div className="skeleton-stack" aria-busy="true" aria-label={t('正在读取')}><span /><span /><span /></div>}>
      {needsManagement && (managementState === 'upgrade_required' || (!wasReady.current && managementState !== 'ready')) ? <><PageHeader title={managementState === 'upgrade_required' ? t('需要更新本机服务') : managementState === 'checking' ? t('正在检查服务能力') : t('暂时无法确认新功能是否可用')} description={t('当前后台尚未提供可用的管理接口。新页面已暂停加载，不会发起配置写入；原有项目与接入页面继续可用。')} /><div className="toolbar-actions"><Button onClick={management.reload} busy={management.loading}>{t('重新检查服务')}</Button><Button onClick={() => go({ page: 'projects' })}>{t('返回项目工作台')}</Button><Button onClick={() => go({ page: 'connections' })}>{t('打开现有 Agent 接入')}</Button></div></> : renderPage()}
    </Suspense></div>
  </Shell></ObjectRefreshContext.Provider>;
}
export default function App() {
  return <ThemeProvider><I18nProvider><Workbench /></I18nProvider></ThemeProvider>;
}
