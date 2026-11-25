import { useState } from 'react';
import { ArrowUpRight, Cable, ChevronRight, Code2, Command, Compass, MousePointer2, Orbit, Star, Terminal } from 'lucide-react';
import { list } from '../../lib/management.js';
import { t } from '../../lib/i18n.jsx';
import { Button, EmptyState, ErrorNotice, Field } from '../ui.jsx';

const marks = { 'claude-code': Command, codex: Terminal, opencode: Code2, cursor: MousePointer2, omp: Compass, antigravity: Orbit };

function discoveryLabel(host) {
  const discovery = host.discovery;
  if (!discovery) return t('本机状态待检查');
  if (discovery.surfaces?.some(surface => surface.installation === 'detected')) return t('已检测到安装');
  if (discovery.discovered || discovery.evidence?.some(entry => ['present', 'executable_present', 'executable_link_present'].includes(entry.status))) return t('检测到本机记录');
  return t('未检测到本机记录');
}

export function AgentDirectory({ hosts, connections, nameOf, favorites, recent, onChoose, onPin, busy }) {
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const order = [...new Set([...favorites, ...recent])];
  const rank = id => order.includes(id) ? order.indexOf(id) : order.length;
  const statusFor = host => list(connections.data).find(item => item.id === (host.id));
  const matchesFilter = host => filter === 'all' || (filter === 'favorites' ? favorites.includes(host.id) : statusFor(host)?.enabled);
  const entries = hosts.filter(host => matchesFilter(host) && (nameOf(host) + ' ' + host.id).toLowerCase().includes(query.toLowerCase().trim()))
    .sort((a, b) => rank(a.id) - rank(b.id));
  return <section className="ops-directory">
    <div className="ops-directory-tools">
      <div role="group" aria-label={t('助手筛选')} className="ops-filters">
        {[['all', '全部助手'], ['favorites', '常用助手'], ['connected', '已授权读取聊天']].map(([id, label]) =>
          <button type="button" key={id} aria-pressed={filter === id} disabled={id === 'connected' && (!connections.data || !!connections.error)} onClick={() => setFilter(id)}>{t(label)}</button>)}
      </div>
      <Field label={t('查找助手')}><input type="search" placeholder={t('输入助手名称')} value={query} onChange={event => setQuery(event.target.value)} maxLength={100} /></Field>
    </div>
    <ErrorNotice error={connections.error} />
    <div className="agent-chooser">{entries.map(host => {
      const name = nameOf(host);
      const connection = statusFor(host);
      const Mark = marks[host.id] || Terminal;
      const connectionLabel = connection ? (connection.enabled ? connection.paused ? t('采集已暂停') : t('已授权读取聊天') : t('未授权读取聊天')) : t('读取权限未知');
      return <div className="agent-directory-row" key={host.id}>
        <button type="button" className="agent-choice" onClick={() => onChoose(host.id)}>
          <span className="ops-host-mark" aria-hidden="true"><Mark size={21} strokeWidth={1.6} /></span>
          <span className="ops-host-name"><strong className="agent-choice-name">{name}</strong><small>{discoveryLabel(host)}{recent.includes(host.id) && <> · {t('最近打开')}</>}</small></span>
          <span className="agent-choice-action">{t('管理这个助手')}<ChevronRight size={14} aria-hidden="true" /></span>
        </button>
        <button type="button" className="ops-source-link" aria-label={t('查看 {name} 的读取权限', { name })} onClick={() => onChoose(host.id, 'connections')}>
          <Cable size={15} aria-hidden="true" /><span>{connectionLabel}</span><ArrowUpRight size={13} aria-hidden="true" />
        </button>
        <Button icon={Star} aria-pressed={favorites.includes(host.id)} aria-label={t('常用助手 · {name}', { name })} disabled={busy} onClick={() => onPin(host.id, !favorites.includes(host.id))} />
      </div>;
    })}</div>
    {!entries.length && !(filter === 'connected' && (connections.loading || connections.error)) ? <EmptyState title={t('没有匹配的助手')} body={t('切换筛选条件或搜索其他名称。')} action={(query || filter !== 'all') ? <Button onClick={() => { setQuery(''); setFilter('all'); }}>{t('重置筛选')}</Button> : null} /> : null}
  </section>;
}
