import { UsageTrend } from '../components/UsageTrend.jsx';
import { useEffect, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { useResource, queryString } from '../lib/useResource.js';
import { number, display, list, readSavedValue, saveValue, usageDateFilters } from '../lib/management.js';
import { Button, Field, Modal, PageHeader, ResourceState, formatTime } from '../components/ui.jsx';
import { UsagePrices } from '../components/UsagePrices.jsx';
import { DataTable, EvidenceDetails, Facts, StatusBadge, statusLabel } from '../components/ManagementUI.jsx';

function Metric({ label, value, note }) { return <div className="metric-card"><span>{label}</span><strong>{value}</strong><small>{note}</small></div>; }
export function UsagePage({ projects = [], agentId }) {
  const [filters, setFilters] = useState(() => {
    const saved = readSavedValue('usage.filters', {});
    if (!agentId) delete saved.host_id;
    return { ...saved, ...(agentId ? { host_id: agentId } : {}) };
  });
  useEffect(() => {
    setFilters(previous => {
      if (agentId) return { ...previous, host_id: agentId };
      if (previous.host_id) {
        const next = { ...previous };
        delete next.host_id;
        return next;
      }
      return previous;
    });
  }, [agentId]);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(null);
  const [revision, setRevision] = useState(0);
  const [appliedFilters, setAppliedFilters] = useState(filters);
  useEffect(() => {
    const timer = setTimeout(() => { setAppliedFilters(filters); setOffset(0); }, 350);
    return () => clearTimeout(timer);
  }, [filters]);
  const effectiveFilters = usageDateFilters(appliedFilters);
  const params = queryString({ ...effectiveFilters, tz: Intl.DateTimeFormat().resolvedOptions().timeZone });
  const summary = useResource(`/api/usage/summary${params}`, revision);
  const requests = useResource(`/api/usage/requests${queryString({ ...effectiveFilters, limit: 25, offset })}`, revision);
  const hosts = useResource('/api/manage/hosts');
  const coverage = { ...summary, data: summary.data?.coverage };
  const refreshing = summary.loading || requests.loading;
  useEffect(() => {
    if (refreshing) return;
    const timer = setTimeout(() => { if (document.visibilityState === 'visible') setRevision(value => value + 1); }, 30000);
    const resume = () => { if (document.visibilityState === 'visible') setRevision(value => value + 1); };
    document.addEventListener('visibilitychange', resume);
    return () => { clearTimeout(timer); document.removeEventListener('visibilitychange', resume); };
  }, [refreshing, revision]);
  const detail = useResource(selected ? `/api/usage/requests${queryString({ logical_request_id: selected.logical_request_id, limit: 100 })}` : null);
  function update(key, value) {
    const next = { ...filters, [key]: value };
    setFilters(next);
    const toSave = { ...next };
    if (agentId) delete toSave.host_id;
    saveValue('usage.filters', toSave);
  }
  const data = summary.data;
  const heatmap = list(data?.heatmap);
  const rows = list(requests.data);
  function addTokens(acc, key, row) {
    const input = row.usage?.input_tokens;
    const output = row.usage?.output_tokens;
    const knownInput = Number.isFinite(input);
    const knownOutput = Number.isFinite(output);
    const bucket = acc[key] || { count: 0, tokens: 0, known: false };
    bucket.count += 1;
    if (knownInput || knownOutput) {
      bucket.known = true;
      bucket.tokens += (knownInput ? input : 0) + (knownOutput ? output : 0);
    }
    acc[key] = bucket;
    return acc;
  }
  const hostBreakdown = Object.entries(
    rows.reduce((acc, r) => addTokens(acc, r.host_id || 'unknown', r), {})
  ).sort((a, b) => b[1].count - a[1].count).map(([host_id, stats]) => ({ id: host_id, host_id, ...stats }));
  const modelBreakdown = Object.entries(
    rows.reduce((acc, r) => addTokens(acc, r.actual_model || r.requested_model || r.model || 'unknown', r), {})
  ).sort((a, b) => b[1].count - a[1].count).map(([model, stats]) => ({ id: model, model, ...stats }));
  const cost = data?.cost_by_currency ? Object.entries(data.cost_by_currency).map(([currency, value]) => `${currency} ${number(typeof value === 'object' ? value.known_cost ?? value.amount : value, { maximumFractionDigits: 6 })}`).join(' · ') : t('未知');
  const hostList = list(hosts.data);
  const hostOptions = [];
  const seenHostValues = new Set();
  for (const item of hostList) {
    const value = item.registry_host_id || item.id;
    if (!value || seenHostValues.has(value)) continue;
    seenHostValues.add(value);
    const shared = hostList.filter(candidate => (candidate.registry_host_id || candidate.id) === value);
    const label = shared.length > 1
      ? shared.map(candidate => candidate.label || candidate.name || candidate.id).join(' / ')
      : (item.label || item.name || item.id);
    hostOptions.push({ value, label });
  }
  return <>
    <PageHeader title={t('使用情况')} description={t('从实际请求与历史记录查看用量。未经过采集的调用不在这里，缺失的数据保持未知。')} actions={<Button icon={RefreshCw} disabled={refreshing} onClick={() => setRevision(value => value + 1)}>{t('刷新')}</Button>} />
    <section className="management-panel"><div className="filter-grid">
      <Field label={t('开始日期')}><input type="date" value={filters.start || ''} onChange={event => update('start', event.target.value)} /></Field>
      <Field label={t('结束日期')}><input type="date" value={filters.end || ''} onChange={event => update('end', event.target.value)} /></Field>
      <Field label={t('助手')}><select value={filters.host_id || ''} onChange={event => update('host_id', event.target.value)}><option value="">{t('全部助手')}</option>{filters.host_id && !seenHostValues.has(filters.host_id) ? <option value={filters.host_id}>{filters.host_id}</option> : null}{hostOptions.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></Field>
      <Field label={t('所属项目')}><select value={filters.project_id || ''} onChange={event => update('project_id', event.target.value)}><option value="">{t('全部项目')}</option>{filters.project_id && !projects.some(item => item.id === filters.project_id) ? <option value={filters.project_id}>{t('项目待定')}</option> : null}{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
      <Field label={t('状态')}><select value={filters.status || ''} onChange={event => update('status', event.target.value)}><option value="">{t('全部状态')}</option>{['succeeded', 'failed', 'cancelled', 'unknown', 'running'].map(value => <option key={value} value={value}>{statusLabel(value)}</option>)}</select></Field>
    </div><details className="consumer-advanced"><summary>{t('更多用量筛选')}</summary><div className="filter-grid"><Field label={t('供应商')}><input value={filters.provider_id || ''} placeholder={t('全部供应商')} onChange={event => update('provider_id', event.target.value)} /></Field><Field label={t('模型')}><input value={filters.model || ''} placeholder={t('全部模型')} onChange={event => update('model', event.target.value)} /></Field><Field label={t('账号引用')}><input value={filters.account_id || ''} onChange={event => update('account_id', event.target.value)} /></Field></div></details><p className="management-explainer">{t('日期按本机时区筛选；Senrelay 自身整理与其他调用按来源分别记录。')}</p></section>
    <ResourceState resource={summary}>{data ? <>
      <div className="metrics-grid"><Metric label={t('已记录请求')} value={number(data.logical_requests)} note={t('相同请求的可验证观察合并')} /><Metric label={t('已知 Token')} value={data.known_tokens === 0 && data.unknown_usage_count > 0 ? t('未知') : number(data.known_tokens)} note={t('{count} 项用量不完整', { count: number(data.unknown_usage_count) })} /><Metric label={t('已记录费用')} value={cost || t('未知')} note={`${t('仅含可定价部分，不代表订阅余额')} · ${t('{count} 项费用未知', { count: number(data.unknown_cost_count) })}`} /></div>
      <section className="management-panel"><header><div><h2>{t('请求趋势')}</h2><p>{t('查看每天记录的请求次数。')}</p></div></header><UsageTrend key={params} rows={heatmap} /></section>
      {rows.length > 0 ? <>
        <section className="management-panel">
          <header><div><h2>{t('按宿主 Agent 分布')}</h2><p>{t('基于当前账本页请求的宿主调用占比。')}</p></div></header>
          <DataTable rows={hostBreakdown} columns={[{ key: 'host_id', label: t('宿主') }, { key: 'count', label: t('已记录请求'), render: row => `${number(row.count)} ${t('次')}` }, { key: 'tokens', label: t('已知 Token'), render: row => row.known ? number(row.tokens) : t('未知') }]} />
        </section>
        <section className="management-panel">
          <header><div><h2>{t('按模型调用分布')}</h2><p>{t('基于当前账本页请求的模型调用与 Token 消耗。')}</p></div></header>
          <DataTable rows={modelBreakdown} columns={[{ key: 'model', label: t('模型') }, { key: 'count', label: t('已记录请求'), render: row => `${number(row.count)} ${t('次')}` }, { key: 'tokens', label: t('已知 Token'), render: row => row.known ? number(row.tokens) : t('未知') }]} />
        </section>
      </> : null}
      <details className="consumer-advanced"><summary>{t('技术详情：尝试次数与速度')}</summary><Facts values={[[t('物理尝试'), number(data.attempts)]]} />
      <section className="management-panel"><header><div><h2>{t('性能与覆盖')}</h2><p>{t('首字延迟只计首个内容增量；生成速度需要完整流式计时。历史估算不参与实测排名。')}</p></div></header><Facts values={[[t('输入 Token'), number(data.input_tokens)], [t('输出 Token'), number(data.output_tokens)], [t('首字延迟 p50'), data.performance?.ttft_ms?.p50 == null ? t('未知') : `${number(data.performance.ttft_ms.p50)} ms`], [t('首字延迟 p95'), data.performance?.ttft_ms?.p95 == null ? t('未知') : `${number(data.performance.ttft_ms.p95)} ms`], [t('端到端耗时 p50'), data.performance?.end_to_end_ms?.p50 == null ? t('未知') : `${number(data.performance.end_to_end_ms.p50)} ms`], [t('端到端耗时 p95'), data.performance?.end_to_end_ms?.p95 == null ? t('未知') : `${number(data.performance.end_to_end_ms.p95)} ms`], [t('首字计时样本'), number(data.performance?.ttft_ms?.samples)], [t('生成速度样本'), number(data.performance?.output_tokens_per_second?.samples)], [t('输出速度'), data.performance?.output_tokens_per_second?.mean == null ? t('未知') : `${number(data.performance.output_tokens_per_second.mean, { maximumFractionDigits: 1 })} tok/s`]]} /><EvidenceDetails value={{ performance: data.performance, coverage: data.coverage, cost_by_currency: data.cost_by_currency }} /></section>
      </details>
    </> : null}</ResourceState>
    <section className="management-panel"><header><div><h2>{t('请求账本')}</h2><p>{t('打开一条请求，核对来源、尝试与缺失原因。')}</p></div></header><ResourceState resource={requests}><DataTable rows={rows} onRow={setSelected} columns={[{ key: 'created_at', label: t('发生时间'), render: row => formatTime(row.started_at || row.occurred_at || row.created_at) }, { key: 'host_id', label: t('宿主') }, { key: 'model', label: t('模型'), render: row => display(row.actual_model || row.requested_model || row.model) }, { key: 'input_tokens', label: t('输入'), render: row => number(row.usage?.input_tokens) }, { key: 'output_tokens', label: t('输出'), render: row => number(row.usage?.output_tokens) }, { key: 'status', label: t('状态'), render: row => <StatusBadge value={row.status} /> }, { key: 'provenance', label: t('来源'), render: row => row.origins?.join(' · ') || display(null) }]} /><div className="management-pagination"><span>{t('共 {count} 条', { count: number(requests.data?.total) })}</span><div className="toolbar-actions"><Button disabled={offset === 0 || requests.loading} onClick={() => setOffset(Math.max(0, offset - 25))}>{t('上一页')}</Button><Button disabled={requests.loading || offset + rows.length >= (requests.data?.total ?? 0)} onClick={() => setOffset(offset + 25)}>{t('下一页')}</Button></div></div></ResourceState></section>
    <details className="consumer-advanced"><summary>{t('采集范围与价格设置')}</summary>
    <section className="management-panel"><h2>{t('采集覆盖范围')}</h2><ResourceState resource={coverage}>{coverage.data ? <><Facts values={[[t('已观测尝试'), coverage.data.observed_attempts], [t('用量完整尝试'), coverage.data.metered_attempts], [t('用量不完整尝试'), coverage.data.unknown_usage_attempts], [t('全部宿主总用量'), t('未知')], [t('观测来源'), coverage.data.origins?.join(' · ') || t('还没有记录')]]} /><EvidenceDetails value={coverage.data} title={t('来源、覆盖与缺失说明')} /></> : null}</ResourceState></section>
    <UsagePrices />
    </details>
    {selected ? <Modal wide title={t('请求证据')} onClose={() => setSelected(null)}><Facts values={[[t('逻辑请求 ID'), selected.logical_request_id], [t('物理尝试 ID'), selected.id], [t('记录模型'), selected.model], [t('请求模型'), selected.requested_model], [t('实际模型'), selected.actual_model], [t('供应商'), selected.provider_id], [t('账号引用'), selected.account_id]]} /><EvidenceDetails value={selected} title={t('账本记录')} /><ResourceState resource={detail}>{detail.data ? <EvidenceDetails value={detail.data} title={t('尝试与观察证据')} /> : null}</ResourceState></Modal> : null}
  </>;
}
