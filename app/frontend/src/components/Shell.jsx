import { t } from "../lib/i18n.jsx";
import { BarChart3, BookOpen, BriefcaseBusiness, CircleDot, ContactRound, FolderOpen, House, Layers3, Menu, Monitor, Moon, Plug, Settings2, Sun, UsersRound, X } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTheme } from '../lib/theme.js';
import { Button } from './ui.jsx';
import { BrandIdentity } from './BrandIdentity.jsx';
import { LanguageSwitch } from './LanguageSwitch.jsx';
const GROUPS = [
  { name: '工作空间', items: [
    { id: 'home', label: '工作台', icon: House },
    { id: 'projects', label: '项目', icon: FolderOpen },
    { id: 'exchange', label: '协作', icon: UsersRound }
  ] },
  { name: '我的 Agent', items: [
    { id: 'personal', section: 'memory', label: '个人记忆', icon: BookOpen },
    { id: 'personal', section: 'assets', label: '能力与作品', icon: BriefcaseBusiness },
    { id: 'personal', section: 'profile', label: 'Agent 名片', icon: ContactRound }
  ] },
  { name: '管理', items: [
    { id: 'agents', label: 'Agent 管理', icon: Plug },
    { id: 'usage', label: '用量与性能', icon: BarChart3 },
    { id: 'settings', label: '设置', icon: Settings2 }
  ] }
];
export function Shell({
  lang,
  page,
  section,
  navigate,
  projects,
  selectedProject,
  onProject,
  status,
  statusLoading = false,
  managementState = 'checking',
  children
}) {
  const hasConflicts = (status?.counts?.pending_conflicts ?? 0) > 0 || page === 'conflicts';
  const [open, setOpen] = useState(false);
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 1023px)').matches);
  const sidebar = useRef(null);
  const main = useRef(null);
  const mobileHeader = useRef(null);
  useEffect(() => {
    const media = window.matchMedia('(max-width: 1023px)');
    const update = () => {
      setMobile(media.matches);
      if (!media.matches) setOpen(false);
    };
    update();
    media.addEventListener('change', update);
    window.addEventListener('resize', update);
    return () => {
      media.removeEventListener('change', update);
      window.removeEventListener('resize', update);
    };
  }, []);
  useLayoutEffect(() => {
    // One owner for this DOM property; React attribute support differs by version.
    sidebar.current.inert = mobile && !open;
  }, [mobile, open]);
  useEffect(() => {
    if (!open || !mobile) return;
    const previous = document.activeElement;
    const content = main.current;
    const header = mobileHeader.current;
    const overflow = document.body.style.overflow;
    content.inert = true;
    header.inert = true;
    document.body.style.overflow = 'hidden';
    sidebar.current.querySelector('button')?.focus();
    const keydown = event => {
      if (event.key === 'Escape') {
        event.preventDefault();
        setOpen(false);
      }
      if (event.key !== 'Tab') return;
      const controls = Array.from(sidebar.current.querySelectorAll('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), [tabindex="0"]')).filter(element => element.getClientRects().length);
      const first = controls[0],
        last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    };
    document.addEventListener('keydown', keydown);
    return () => {
      content.inert = false;
      header.inert = false;
      document.body.style.overflow = overflow;
      document.removeEventListener('keydown', keydown);
      if (previous?.isConnected && previous.getClientRects().length) previous.focus();
    };
  }, [open, mobile]);
  const {
    theme,
    setTheme
  } = useTheme();
  function go(id) {
    navigate(id);
    setOpen(false);
  }
  const currentSection = section || 'memory';
  const active = item => {
    if (page === 'connections') return item.id === 'agents';
    if (page === 'audit') return item.id === 'settings';
    return item.id === page && (!item.section || item.section === currentSection);
  };
  function navItem(item) {
    const Icon = item.icon;
    const unavailable = ['agents', 'usage'].includes(item.id) && managementState !== 'ready';
    const destination = item.section ? { page: item.id, section: item.section } : item.id === 'projects' ? { page: 'projects', project: null, section: null } : item.id;
    return <button key={item.section || item.id} data-route-page={item.id} data-route-section={item.section}
      disabled={unavailable} aria-current={active(item) ? 'page' : undefined} className={active(item) ? 'active' : ''}
      onClick={() => go(destination)}><Icon size={17} aria-hidden="true" /><span>{t(item.label)}</span>
      {unavailable && <small className="nav-feature-note">{managementState === 'upgrade_required' ? t('需更新') : t('待连接')}</small>}
      {item.id === 'conflicts' && status?.counts?.pending_conflicts > 0 && <span className="nav-count">{status.counts.pending_conflicts}</span>}
    </button>;
  }
  return <div className="app-shell studio-shell" data-lang={lang}>
    <header ref={mobileHeader} className="mobile-header"><Button icon={Menu} onClick={() => setOpen(true)} aria-label={t("打开导航")} aria-expanded={open} aria-controls="workspace-navigation" /><button className="brand mobile-brand" onClick={() => go('home')}><BrandIdentity /></button><LanguageSwitch compact /></header>
    {open && mobile ? <button className="sidebar-scrim" tabIndex={-1} aria-label={t("关闭导航")} onClick={() => setOpen(false)} /> : null}
    <aside ref={sidebar} id="workspace-navigation" className={`sidebar ${open ? 'sidebar-open' : ''}`} role={mobile && open ? 'dialog' : undefined} aria-modal={mobile && open ? true : undefined} aria-label={t("工作台导航")}>
      <div className="studio-brand-row"><button className="brand" onClick={() => go('home')} aria-label={t("返回工作台")}><BrandIdentity /></button>
      {mobile && <Button className="drawer-close" icon={X} onClick={() => setOpen(false)} aria-label={t("关闭导航")} />}</div>
      <nav className="main-nav" aria-label={t("主导航")}>{GROUPS.map((group, index) => <div key={group.name} className="studio-nav-group">
        <p className="nav-label">{t(group.name)}</p>{group.items.map(navItem)}
        {index === 0 && hasConflicts && navItem({ id: 'conflicts', label: '待确认', icon: CircleDot })}
      </div>)}</nav>
      {projects.some(project => project.status !== 'archived') && <div className="sidebar-projects"><p className="nav-label">{t("最近项目")}</p>{projects.filter(project => project.status !== 'archived').slice(0, 3).map(project => <button key={project.id} className={page === 'projects' && selectedProject === project.id ? 'selected' : ''} onClick={() => {
          onProject(project.id);
          setOpen(false);
        }}><Layers3 size={14} aria-hidden="true" /><span>{project.name}</span></button>)}</div>}
      <div className="sidebar-bottom"><div className="local-status"><span className={`status-dot ${status?.ok && status.service?.running === true && !status.service?.stop_status ? 'online' : ''}`} /><span>{!status?.ok && statusLoading ? t("正在读取本机服务状态…") : !status?.ok ? t("本机服务未连接") : status.service?.stop_status === 'pending' ? t("本机服务正在停止") : status.service?.stop_status === 'failed' ? t("停止失败，请检查") : status.service?.stop_status === 'stopped' ? t("本机服务已报告停止") : status.service?.running === true ? t("本机服务运行中") : t("本机服务已响应，但还不能确认它在运行。")}</span></div><div className="sidebar-controls"><LanguageSwitch /><div className="theme-switch" role="group" aria-label={t("主题")}>{[['light', Sun, t("浅色")], ['dark', Moon, t("深色")], ['system', Monitor, t("跟随系统")]].map(([value, Icon, label]) => <button key={value} aria-label={label} title={label} aria-pressed={theme === value} onClick={() => setTheme(value)}><Icon size={15} /></button>)}</div></div></div>
    </aside>
    <main ref={main} id="main-content" tabIndex={-1} className="main-content">{children}</main>
  </div>;
}
