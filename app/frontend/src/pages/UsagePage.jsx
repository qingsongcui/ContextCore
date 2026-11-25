import { useEffect, useState } from 'react';
import { Activity, List, RefreshCw, Wallet, Workflow } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { list, readSavedValue, saveValue } from '../lib/management.js';
import { dateRange, dimensionKeys, dimensionNames, hostOptions, isProductHostValue, originName, productHosts, unattributed, usageParams, usagePath } from '../lib/usageView.js';
import { useResource } from '../lib/useResource.js';
import { Button, ErrorNotice, Field, PageHeader } from '../components/ui.jsx';
import { statusLabel } from '../components/ManagementUI.jsx';
import AccountQuotaPanel from '../components/AccountQuotaPanel.jsx';
import PricingPanel from '../components/usage/PricingPanel.jsx';
import { UsageLedger, RequestDetails } from '../components/usage/UsageLedger.jsx';
import { UsageOverview } from '../components/usage/UsageOverview.jsx';
import { UsageSources } from '../components/usage/UsageSources.jsx';
import '../styles/usage-workspace.css';
import '../styles/operations-studio.css';

const tabIcons = { overview: Activity, requests: List, accounts: Wallet, sources: Workflow };
const tabs = { overview: '概览', requests: '请求', accounts: '账号额度', sources: '来源与定价' };
const allChoices = { host_id: '全部助手', project_id: '全部项目', provider_id: '全部供应商', model: '全部模型', account_id: '全部账号', origin: '全部来源', status: '全部状态', request_status: '全部状态' };
const periods = { today: '今天', yesterday: '昨天', week: '近 7 天', month: '近 30 天', custom: '自定义日期' };

function preferenceKey(agentId, projectId) {
  return agentId || projectId ? 'usage.filters.' + JSON.stringify([agentId || '', projectId || '']) : 'usage.filters';
}

function defaultFilters(agentId, projectId, saved = {}) {
  return { ...saved, period: Object.hasOwn(periods, saved.period) ? saved.period : saved.start || saved.end ? 'custom' : 'week',
    host_id: agentId || '', project_id: projectId || saved.project_id || '' };
}

function DailyUsage({ projects, agentId, projectId, section, requestId, navigate, onSectionChange }) {
  const [localTab, setLocalTab] = useState('overview');
  const tab = section && tabs[section] ? section : onSectionChange ? 'overview' : localTab;
  const storageKey = preferenceKey(agentId, projectId);
  const [baseFilters, setBaseFilters] = useState(() => defaultFilters(agentId, projectId, readSavedValue(storageKey, {}) || {}));
  const [drillFilter, setDrillFilter] = useState(null);
  const filters = tab === 'requests' && drillFilter ? { ...baseFilters, ...drillFilter } : baseFilters;
  const [selected, setSelected] = useState('');
  const [revision, setRevision] = useState(0);
  const refresh = () => setRevision(value => value + 1);
  const [expanded, setExpanded] = useState(() => ['origin','status','request_status','provider_id','model','account_id'].some(key => filters[key]) || filters.scope && filters.scope !== 'user');
  const options = useResource(tab !== 'accounts' ? usagePath('filter-options', usageParams(filters, { optionsOnly: true })) : null, revision);
  const hosts = useResource('/api/manage/hosts');
  const observed = options.data || {};
  const catalog = list(hosts.data);
  // A deep link to an extended configuration host is not one of the six usage filters.
  const requestedHost = !filters.host_id || filters.host_id === unattributed || isProductHostValue(catalog, filters.host_id) || catalog.length === 0
    ? filters.host_id || '' : '';
  const selectedHost = catalog.find(host => host.id === requestedHost);
  // Native logs may only identify a shared registry host (for example, Codex CLI/Desktop).
  // Select a distinct surface when the period contains observations attributed to it.
  const exactHostObserved = list(observed.host_id).some(item => item.value === requestedHost);
  const reportHost = exactHostObserved ? requestedHost : selectedHost?.id || requestedHost;
  const params = usageParams({ ...filters, host_id: reportHost });
  const summary = useResource(['overview', 'sources'].includes(tab) ? usagePath('overview', params) : null, revision);
  useEffect(() => {
    if (summary.loading || tab === 'accounts') return;
    const timer = setTimeout(() => { if (document.visibilityState === 'visible') setRevision(value => value + 1); }, 30000);
    const resume = () => { if (document.visibilityState === 'visible') setRevision(value => value + 1); };
    document.addEventListener('visibilitychange', resume);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', resume); };
  }, [summary.loading, tab, revision]);
  function storeFilters(next) {
    setBaseFilters(next);
    const saved = { ...next }; delete saved.host_id;
    saveValue(storageKey, saved);
  }
  function update(patch) {
    setDrillFilter(null);
    storeFilters({ ...filters, ...patch });
  }
  function clearFilters() {
    setDrillFilter(null);
    storeFilters(defaultFilters(agentId, projectId));
    setExpanded(false);
  }
  const filtered = filters.period !== 'week' || filters.host_id !== (agentId || '') || filters.project_id !== (projectId || '') ||
    filters.scope && filters.scope !== 'user' || ['origin','status','request_status','provider_id','model','account_id'].some(key => filters[key]);
  function switchTab(value) {
    setSelected('');
    setDrillFilter(null);
    if (onSectionChange) onSectionChange(value);
    setLocalTab(value);
  }
  function drill(key, value) {
    switchTab('requests');
    setDrillFilter({ [key]: value == null ? unattributed : value });
    if (!['host_id','project_id'].includes(key)) setExpanded(true);
  }
  const hostChoices = hostOptions(list(hosts.data), list(observed.host_id));
  function buildChoices(key) {
    const known = new Map();
    if (key === 'request_status') for (const status of ['succeeded','failed','cancelled','running','unknown']) known.set(status, statusLabel(status));
    if (key === 'host_id') for (const item of hostChoices) known.set(item.value, item.label);
    if (key === 'project_id') for (const item of projects) known.set(item.id, item.name);
    for (const item of list(observed[key])) {
      const value = item.value ?? unattributed;
      if (!known.has(value)) known.set(value, item.label);
    }
    const selectedValue = key === 'host_id' ? reportHost : filters[key];
    if (selectedValue && !known.has(selectedValue)) known.set(selectedValue, selectedValue);
    return known;
  }
  const choices = Object.fromEntries([...dimensionKeys, 'origin', 'status', 'request_status'].map(key => [key, buildChoices(key)]));
  function labelFor(key, value, fallback) {
    if (value == null || value === unattributed) return t('未归属');
    if (key === 'origin') return originName(value);
    if (key === 'status' || key === 'request_status') return statusLabel(value);
    return choices[key].get(value) || fallback || value;
  }
  function choiceField(key) {
    const label = key === 'origin' ? '观测来源' : key === 'status' ? '尝试状态' : key === 'request_status' ? '请求结果' : dimensionNames[key];
    const entries = [...choices[key]].filter(([value]) => key !== 'host_id' || value === unattributed || isProductHostValue(catalog, value));
    return <Field key={key} label={t(label)}><select value={(key === 'host_id' ? reportHost : filters[key]) || ''} onChange={e => update({ [key]: e.target.value })}>
      <option value="">{t(allChoices[key])}</option>{entries.map(([value, name]) => <option key={value} value={value}>{labelFor(key, value, name)}</option>)}
    </select></Field>;
  }
  function closeDetail() {
    setSelected('');
    if (requestId && navigate) navigate({ page: 'usage', agent: agentId || '', project: projectId || '', section: 'requests', request: '' });
  }
  const request = requestId || selected?.id;
  const range = dateRange(filters.period, filters.start, filters.end);
  return <div className="usage-workspace operations-studio">
    <PageHeader title={t('用量与性能')} description={t('按项目和助手查看调用记录、费用与性能。')} actions={tab !== 'accounts' ? <Button icon={RefreshCw} onClick={refresh} disabled={summary.loading}>{t('刷新账本')}</Button> : null} />
    <nav className="usage-nav" aria-label={t('用量视图')}>{Object.entries(tabs).map(([key, label]) => {
      const Icon = tabIcons[key];
      return <button type="button" key={key} aria-current={tab === key ? 'page' : undefined} onClick={() => switchTab(key)}><Icon size={16} aria-hidden="true" />{t(label)}</button>;
    })}</nav>
    {tab === 'accounts' ? <>
      <div className="usage-quota-filter"><Field label={t('助手')}><select value={requestedHost === unattributed ? '' : requestedHost || ''} onChange={e => update({ host_id: e.target.value })}>
        <option value="">{t('全部助手')}</option>{productHosts(catalog).map(item => <option key={item.id} value={item.id}>{item.label || item.name || item.id}</option>)}
      </select></Field></div>
      <AccountQuotaPanel hostId={requestedHost === unattributed ? '' : requestedHost} />
    </> : <>
      <section className="management-panel usage-filters" aria-label={t('用量筛选')}>
        <div className="usage-filter-row">
          <Field label={t('时间范围')}><select value={filters.period} onChange={e => update({ period: e.target.value, start: range.start, end: range.end })}>{Object.entries(periods).map(([key, label]) => <option key={key} value={key}>{t(label)}</option>)}</select></Field>
          {choiceField('host_id')}{choiceField('project_id')}
          <div className="toolbar-actions"><Button aria-expanded={Boolean(expanded)} onClick={() => setExpanded(!expanded)}>{t('更多用量筛选')}</Button>
            {filtered && <Button onClick={clearFilters}>{t('清除筛选')}</Button>}</div>
        </div>
        {filters.period === 'custom' && <div className="usage-extra">
          <Field label={t('开始日期')}><input type="date" value={filters.start || ''} max={filters.end || undefined} onChange={e => update({start:e.target.value})} /></Field>
          <Field label={t('结束日期')}><input type="date" value={filters.end || ''} min={filters.start || undefined} onChange={e => update({end:e.target.value})} /></Field>
        </div>}
        {expanded && <div className="usage-extra">
          <Field label={t('调用类型')}><select value={filters.scope || 'user'} onChange={e => update({scope:e.target.value, origin:''})}>
            <option value="user">{t('用户调用')}</option><option value="system">{t('检查与系统整理')}</option><option value="all">{t('全部调用')}</option></select></Field>
          {['provider_id','model','account_id','origin','request_status','status'].map(choiceField)}
        </div>}
        <ErrorNotice error={options.error} />
        {!params && <p role="alert" className="inline-error">{t('请选择完整日期范围，结束日期应在开始日期当天或之后。')}</p>}
      </section>
      {params && tab === 'overview' && <UsageOverview resource={summary} params={params} revision={revision} labelFor={labelFor} onRequests={() => switchTab('requests')} onDrill={drill} onSources={() => switchTab('sources')} />}
      {params && tab === 'requests' && <UsageLedger key={params.toString()} params={params} revision={revision} hostLabel={value => labelFor('host_id', value)} onSelect={(id, sample) => setSelected({id, sample})} />}
      {tab === 'sources' && <><UsageSources overview={summary} revision={revision} onChanged={refresh} navigate={navigate} hostLabel={value => labelFor('host_id', value)} /><PricingPanel onChanged={refresh} /></>}
    </>}
    {request && tab === 'requests' && <RequestDetails key={request} requestId={request} revision={revision} sampleHint={selected?.sample} onClose={closeDetail} />}
  </div>;
}

export function UsagePage(props) {
  return <DailyUsage key={preferenceKey(props.agentId, props.projectId)} {...props} projects={props.projects || []} />;
}
