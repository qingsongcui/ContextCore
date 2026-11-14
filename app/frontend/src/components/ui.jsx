import { locale, t } from "../lib/i18n.jsx";
import { Children, cloneElement, isValidElement, useEffect, useId, useRef, useState, useLayoutEffect } from 'react';
import { AlertCircle, ArrowRight, LoaderCircle, X } from 'lucide-react';
import { safeEvidence } from '../lib/management.js';
export function Button({
  children,
  icon: Icon,
  variant = 'secondary',
  className = '',
  busy = false,
  disabled,
  ...props
}) {
  const hasLabel = Children.toArray(children).some(child => child !== '' && child !== false && child !== null && child !== undefined);
  const iconOnly = Boolean((Icon || busy) && !hasLabel);
  return <button type="button" className={`button button-${variant}${iconOnly ? ' button-icon-only' : ''}${className ? ` ${className}` : ''}`} disabled={disabled || busy} aria-busy={busy || undefined} {...props}>{busy ? <LoaderCircle size={16} className="spin" aria-hidden="true" /> : Icon ? <Icon size={16} aria-hidden="true" /> : null}{children}</button>;
}
export function Badge({
  children,
  tone = '',
  className = '',
  ...props
}) {
  return <span className={['badge', tone, className].filter(Boolean).join(' ')} {...props}>{children}</span>;
}
export function EmptyState({
  title,
  body,
  action
}) {
  return <div className="empty-state"><span className="empty-orbit" aria-hidden="true"><span /></span><h3>{title}</h3><p>{body}</p>{action}</div>;
}
export function ResourceState({
  resource,
  children
}) {
  const missing = [404, 410].includes(resource.error?.status);
  const notice = resource.error ? <div className="error-state" role="alert"><AlertCircle size={20} /><div><strong>{missing ? t("这条内容已经不可用了") : resource.data ? t("更新失败，以下是上次读到的内容") : t("暂时读不到")}</strong><p>{resource.error.message ? safeEvidence(t(resource.error.message)) : t("看看本机服务是不是没开。")}</p></div><Button onClick={resource.reload}>{t("重新读取")}</Button></div> : null;
  if (resource.error && (!resource.data || missing)) return notice;
  if (resource.loading && !resource.data) return <div className="skeleton-stack" aria-busy="true" aria-label={t("正在读取")}><span /><span /><span /></div>;
  return <>{notice}{children}</>;
}
export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
  headingLevel = 1
}) {
  const Heading = headingLevel === 2 ? 'h2' : 'h1';
  return <header className="page-header"><div>{eyebrow ? <p className="eyebrow">{eyebrow}</p> : null}<Heading>{title}</Heading>{description ? <p className="page-description">{description}</p> : null}</div><div className="header-actions">{actions}</div></header>;
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
  busy = false,
  initialFocus,
  headerActions = null
}) {
  const ref = useRef(null);
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const finished = useRef(false);
  const mounted = useRef(false);
  const initialFocusRef = useRef(initialFocus);
  const finishRef = useRef(null);
  finishRef.current = async () => {
    if (busy || finished.current) return;
    finished.current = true;
    try {
      await onClose();
    } catch {
      // Async onClose rejection must not become an unhandled promise rejection.
    } finally {
      // An async caller can keep the sheet open when its cancellation fails.
      if (mounted.current) {
        finished.current = false;
        closingRef.current = false;
        setClosing(false);
      }
    }
  };
  const titleId = useId();
  const closeRef = useRef(null);
  closeRef.current = () => {
    if (busy || closingRef.current) return;
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
      finishRef.current();
      return;
    }
    closingRef.current = true;
    setClosing(true);
  };
  useEffect(() => {
    if (!closing) return;
    if (busy) {
      closingRef.current = false;
      setClosing(false);
      return;
    }
    const duration = getComputedStyle(ref.current).getPropertyValue('--dur-2').trim();
    const milliseconds = parseFloat(duration) * (duration.endsWith('ms') ? 1 : 1000);
    const timer = window.setTimeout(() => finishRef.current(), Number.isFinite(milliseconds) ? milliseconds : 240);
    return () => window.clearTimeout(timer);
  }, [closing, busy]);
  useEffect(() => {
    mounted.current = true;
    const dialog = ref.current;
    const previous = document.activeElement;
    dialog.showModal();
    const target = initialFocusRef.current;
    const requested = target?.current || (typeof target === 'string' ? dialog.querySelector(target) : null);
    const autofocus = dialog.querySelector('[autofocus]');
    // Native showModal selects the close button before React's autoFocus can run.
    // Only explicit fields opt in; destructive dialogs retain the safe close focus.
    (requested || autofocus)?.focus();
    const cancel = event => {
      event.preventDefault();
      closeRef.current();
    };
    dialog.addEventListener('cancel', cancel);
    const trapTab = event => {
      if (event.key !== 'Tab' || !dialog.open) return;
      const focusable = [...dialog.querySelectorAll(
        'button:not(:disabled), [href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex]:not([tabindex="-1"])'
      )].filter(node => !node.closest('[disabled], [hidden], details:not([open]) > :not(summary)') && node.getClientRects().length > 0);
      // Capture and always move focus. The browser's own Tab can leave a modal
      // when the next control is still disabled, before the bubble handler runs.
      event.preventDefault();
      if (!focusable.length) {
        dialog.focus();
        return;
      }
      const active = document.activeElement;
      const index = focusable.findIndex(node => node === active || node.contains(active));
      const next = event.shiftKey
        ? focusable[(index <= 0 ? focusable.length : index) - 1]
        : focusable[index < 0 || index >= focusable.length - 1 ? 0 : index + 1];
      next.focus();
    };
    document.addEventListener('keydown', trapTab, true);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      mounted.current = false;
      document.removeEventListener('keydown', trapTab, true);
      dialog.removeEventListener('cancel', cancel);
      dialog.close();
      document.body.style.overflow = overflow;
      if (previous?.isConnected) previous.focus?.();else document.querySelector('#main-content')?.focus();
    };
  }, []);
  return <dialog ref={ref} data-closing={closing || undefined} onAnimationEnd={event => {
    if (closingRef.current && event.target === ref.current && event.animationName === 'sheet-out') finishRef.current();
  }} className={`modal ${wide ? 'modal-wide' : ''}`} aria-labelledby={titleId} aria-busy={busy || undefined} onClick={event => {
    if (event.target !== ref.current) return;
    const bounds = ref.current.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) closeRef.current();
  }}><header className="modal-header"><h2 id={titleId}>{title}</h2>{headerActions ? <div className="toolbar-actions">{headerActions}<Button icon={X} disabled={busy} aria-label={t("关闭")} onClick={() => closeRef.current()} /></div> : <Button icon={X} disabled={busy} aria-label={t("关闭")} onClick={() => closeRef.current()} />}</header>{busy ? <p className="dialog-status" role="status">{t("正在提交，请等待结果；提交期间不能关闭窗口。")}</p> : null}<fieldset className="modal-fields" disabled={busy}>{children}</fieldset></dialog>;
}
export function Field({
  label,
  children,
  hint
}) {
  const labelId = useId();
  const hintId = useId();
  const controls = Children.map(children, child => {
    if (!isValidElement(child) || !['input', 'select', 'textarea'].includes(child.type)) return child;
    return cloneElement(child, {
      ...(!child.props['aria-label'] && !child.props['aria-labelledby'] ? { 'aria-labelledby': labelId } : {}),
      ...(hint ? { 'aria-describedby': [child.props['aria-describedby'], hintId].filter(Boolean).join(' ') } : {})
    });
  });
  return <label className="field"><span id={labelId}>{label}</span>{controls}{hint ? <small id={hintId}>{hint}</small> : null}</label>;
}
export function ErrorNotice({
  error
}) {
  return error ? <p className="inline-error" role="alert"><AlertCircle size={16} />{safeEvidence(error.message ? t(error.message) : String(error))}</p> : null;
}
export function SectionHeading({
  title,
  count,
  action
}) {
  return <div className="section-heading"><h2>{title}{count !== undefined ? <span>{count}</span> : null}</h2>{action}</div>;
}
export function TextLink({
  children,
  onClick
}) {
  return <button type="button" className="text-link" onClick={onClick}>{children}<ArrowRight size={14} /></button>;
}
export function formatTime(value) {
  if (!value) return t("还没有记录");
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : new Intl.DateTimeFormat(locale(), {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit'
  }).format(date);
}
export const CATEGORIES = {
  preference: '使用习惯',
  background: '项目背景',
  decision: '决定与约束',
  task: '任务与进度',
  lesson: '踩坑经验',
  knowledge: '参考资料'
};
export const AGENTS = {
  codex: 'Codex',
  omp: 'Oh My Pi (OMP)',
  cursor: 'Cursor',
  antigravity: 'Antigravity',
  'claude-code': 'Claude Code',
  opencode: 'OpenCode'
};

// Measure the active segment after layout, including language/font and viewport changes.
export function SegmentedTabs({ label, children }) {
  const ref = useRef(null);
  useLayoutEffect(() => {
    const nav = ref.current;
    let active = true;
    const measure = () => {
      if (!active) return;
      const selected = nav.querySelector('button.active');
      if (!selected) return;
      nav.style.setProperty('--tab-left', `${selected.offsetLeft}px`);
      nav.style.setProperty('--tab-width', `${selected.offsetWidth}px`);
      // Keep the selected segment visible after a language or viewport change.
      const start = selected.offsetLeft - 4;
      const end = selected.offsetLeft + selected.offsetWidth + 4;
      if (start < nav.scrollLeft) nav.scrollLeft = start;
      else if (end > nav.scrollLeft + nav.clientWidth) nav.scrollLeft = end - nav.clientWidth;
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(nav);
    nav.querySelectorAll('button').forEach(button => observer.observe(button));
    document.fonts?.ready.then(measure);
    return () => { active = false; observer.disconnect(); };
  }, [children]);
  return <nav ref={ref} className="project-tabs" aria-label={label}>{children}</nav>;
}
