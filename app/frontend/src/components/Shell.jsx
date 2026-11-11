import { t } from "../lib/i18n.jsx";
import { BarChart3, ArrowUpRight, CircleDot, Command, FolderOpen, Menu, Monitor, Moon, Plug, Settings2, Sun, X } from 'lucide-react';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { useTheme } from '../lib/theme.js';
import { Button } from './ui.jsx';
import { BrandIdentity } from './BrandIdentity.jsx';
import { LanguageSwitch } from './LanguageSwitch.jsx';
const NAV = [
  { id: 'projects', label: '项目工作台', icon: FolderOpen },
  { id: 'agents', label: 'Agent 管理', icon: Plug },
  { id: 'usage', label: '用量与性能', icon: BarChart3 }
];
export function Shell({
  lang,
  page,
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
  const navigation = hasConflicts
    ? [...NAV, { id: 'conflicts', label: '待确认', icon: CircleDot }]
    : NAV;
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
  return <div className="app-shell" data-lang={lang}>
    <header ref={mobileHeader} className="mobile-header"><Button icon={Menu} onClick={() => setOpen(true)} aria-label={t("打开导航")} aria-expanded={open} aria-controls="workspace-navigation" /><span className="brand mobile-brand"><BrandIdentity /></span><LanguageSwitch compact /></header>
    {open && mobile ? <button className="sidebar-scrim" tabIndex={-1} aria-label={t("关闭导航")} onClick={() => setOpen(false)} /> : null}
    <aside ref={sidebar} id="workspace-navigation" className={`sidebar ${open ? 'sidebar-open' : ''}`} role={mobile && open ? 'dialog' : undefined} aria-modal={mobile && open ? true : undefined} aria-label={t("工作台导航")}>
      {mobile ? <Button className="drawer-close" icon={X} onClick={() => setOpen(false)}>{t("关闭导航")}</Button> : null}
      <button className="brand" onClick={() => go('projects')} aria-label={t("Senrelay 续境项目工作台")}><BrandIdentity /></button>
      <p className="sidebar-caption">{t("换个 Agent，思路接着走。")}</p>
      <nav className="main-nav" aria-label={t("主导航")}>{navigation.map(({
          id,
          label,
          icon: Icon
        }) => {
          const isActive = page === id || (managementState === 'ready' && id === 'agents' && page === 'connections');
          return <button key={id} disabled={['agents', 'usage'].includes(id) && managementState !== 'ready'} title={['agents', 'usage'].includes(id) && managementState !== 'ready' ? managementState === 'upgrade_required' ? t('需要更新本机服务') : t('等待确认服务能力') : undefined} className={isActive ? 'active' : ''} aria-current={isActive ? 'page' : undefined} onClick={() => go(id)}><Icon size={17} /><span>{t(label)}</span>{['agents', 'usage'].includes(id) && managementState !== 'ready' ? <small className="nav-feature-note">{managementState === 'upgrade_required' ? t('需更新') : t('待连接')}</small> : null}{id === 'conflicts' && status?.counts?.pending_conflicts > 0 ? <span className="nav-count">{status.counts.pending_conflicts}</span> : null}</button>;
        })}</nav>
      <div className="sidebar-projects"><p className="nav-label">{t("最近项目")}</p>{projects.filter(item => item.status !== 'archived').slice(0, 7).map(project => <button key={project.id} className={page === 'projects' && selectedProject === project.id ? 'selected' : ''} onClick={() => {
          onProject(project.id);
          setOpen(false);
        }}><span className="project-initial">{project.name.slice(0, 1)}</span><span>{project.name}</span></button>)}{!projects.some(item => item.status !== 'archived') ? <p className="sidebar-empty">{projects.length ? t("项目均已归档，可在所有项目中查看。") : t("接入 Agent 后，从记录中还原项目。")}</p> : null}</div>
      <div className="sidebar-bottom"><div className="local-status"><span className={`status-dot ${status?.ok && status.service?.running === true && !status.service?.stop_status ? 'online' : ''}`} /><span>{!status?.ok && statusLoading ? t("正在读取本机服务状态…") : !status?.ok ? t("本机服务未连接") : status.service?.stop_status === 'pending' ? t("本机服务正在停止") : status.service?.stop_status === 'failed' ? t("停止失败，请检查") : status.service?.stop_status === 'stopped' ? t("本机服务已报告停止") : status.service?.running === true ? t("本机服务运行中") : t("本机服务已响应，但还不能确认它在运行。")}</span><ArrowUpRight size={13} /></div><div className="sidebar-controls"><Button icon={Settings2} onClick={() => go('settings')} aria-label={t("设置")} /><LanguageSwitch /><div className="theme-switch" role="group" aria-label={t("主题")}>{[['light', Sun, t("浅色")], ['dark', Moon, t("深色")], ['system', Monitor, t("跟随系统")]].map(([value, Icon, label]) => <button key={value} aria-label={label} title={label} aria-pressed={theme === value} onClick={() => setTheme(value)}><Icon size={15} /></button>)}</div></div><p className="local-note"><Command size={11} /> {t("上下文留在这里，工作继续向前。")}</p></div>
    </aside>
    <main ref={main} id="main-content" tabIndex={-1} className="main-content">{children}</main>
  </div>;
}
