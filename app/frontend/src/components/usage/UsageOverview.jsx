import { useState } from 'react';
import { Activity, ChevronRight } from 'lucide-react';
import { t } from '../../lib/i18n.jsx';
import { list, number } from '../../lib/management.js';
import { costText, dimensionKeys, dimensionNames, tokenCount, usagePath } from '../../lib/usageView.js';
import { useResource } from '../../lib/useResource.js';
import { Button, Field, ResourceState } from '../ui.jsx';
import { UsageTrend } from '../UsageTrend.jsx';

function Metric({ label, value, note, onClick }) {
  const content = <><span>{label}</span><strong>{value}</strong><small>{note}</small></>;
  return onClick ? <button type="button" className="metric-card usage-metric-link" onClick={onClick}>{content}</button> : <div className="metric-card">{content}</div>;
}

function Performance({ data }) {
  const groups = [['stream', t('路由实测')], ['reported', t('来源报告')]];
  return <details className="management-panel usage-details"><summary>{t('性能与计时样本')}</summary>
    <p>{t('首字延迟只计首个内容增量；生成速度需要完整流式计时。历史估算不参与实测排名。')}</p>
    <div className="usage-performance">{groups.map(([key, label]) => {
      const stats = data?.[key] || {};
      return <div key={key}><h3>{label}</h3><dl>{[
        ['ttft_ms', t('首字延迟'), 'ms'], ['end_to_end_ms', t('端到端耗时'), 'ms'], ['output_tokens_per_second', t('输出速度'), 'tok/s'],
      ].map(([metric, name, unit]) => {
        const { p50, p95, samples = 0 } = stats[metric] || {};
        return <div key={metric}><dt>{name}</dt><dd>{p50 != null ? 'p50 ' + number(p50, { maximumFractionDigits: 1 }) + ' ' + unit : t('未知')}
          {p95 != null && ' · p95 ' + number(p95, { maximumFractionDigits: 1 }) + ' ' + unit}<small>{t('{count} 个样本', { count: number(samples) })}</small></dd></div>;
      })}</dl></div>;
    })}</div>
  </details>;
}

function Distribution({ params, revision, labelFor, currencies, onDrill }) {
  const [dimension, setDimension] = useState('provider_id');
  const [order, setOrder] = useState('requests');
  const [chosenCurrency, setChosenCurrency] = useState('');
  const currency = currencies.includes(chosenCurrency) ? chosenCurrency : currencies[0];
  const resource = useResource(usagePath('breakdown', params, { dimension }), revision);
  function rank(row) {
    if (order === 'cost') return Number(row.cost_by_currency?.[currency] ?? -1);
    if (order === 'tokens') return tokenCount(row) ?? -1;
    return row.logical_requests;
  }
  const rows = [...list(resource.data)].sort((a, b) => rank(b) - rank(a));
  return <section className="management-panel usage-distribution">
    <header><h2>{t('用量分布')}</h2><div className="usage-chart-controls"><Field label={t('分组')}><select value={dimension} onChange={e => setDimension(e.target.value)}>
      {dimensionKeys.map(key => <option key={key} value={key}>{t(dimensionNames[key])}</option>)}</select></Field>
      <Field label={t('排序')}><select value={order} onChange={e => setOrder(e.target.value)}><option value="requests">{t('已记录请求')}</option><option value="cost">{t('估算费用')}</option><option value="tokens">{t('已知 Token')}</option></select></Field>
      {order === 'cost' && currencies.length > 1 && <Field label={t('币种')}><select value={currency} onChange={e => setChosenCurrency(e.target.value)}>{currencies.map(ccy => <option key={ccy}>{ccy}</option>)}</select></Field>}
    </div></header>
    <ResourceState resource={resource}>{rows.length ? <>
      <table className="usage-group-table"><thead><tr><th>{t(dimensionNames[dimension])}</th><th>{t('请求 / 尝试')}</th><th>{t('已知 Token')}</th><th>{t('估算费用')}</th></tr></thead><tbody>{rows.map(row => <tr key={JSON.stringify(row.value)}>
        <th scope="row"><button className="table-row-link" type="button" onClick={() => onDrill(dimension, row.value)}>{labelFor(dimension, row.value, row.label)} <ChevronRight size={14} aria-hidden="true" /></button></th>
        <td data-label={t('请求 / 尝试')}>{number(row.logical_requests)} / {number(row.attempts)}{row.unattributed_samples > 0 && <small className="usage-incomplete">{t('{count} 条用量快照', { count: number(row.unattributed_samples) })}</small>}</td><td data-label={t('已知 Token')}>{number(tokenCount(row))}</td><td data-label={t('估算费用')}>{costText(row.cost_by_currency)}{row.unknown_cost_count > 0 && Object.keys(row.cost_by_currency || {}).length > 0 && <small className="usage-incomplete">{t('费用部分已知')}</small>}</td>
      </tr>)}</tbody></table>
      <p className="management-explainer">{t('同一请求可经过多个分组。费用和 Token 按实际尝试归属。')}</p>
    </> : <p className="table-empty">{t('当前条件下没有请求记录。')}</p>}</ResourceState>
  </section>;
}

export function UsageOverview({ resource, params, revision, labelFor, onRequests, onDrill, onSources }) {
  const data = resource.data;
  const currencies = Object.keys(data?.cost_by_currency || {}).sort();
  const noSamples = data?.attempts === 0 && data?.logical_requests === 0 && data?.unattributed_samples === 0;
  return <ResourceState resource={resource}>{noSamples ? <section className="ops-empty-report">
    <Activity size={28} strokeWidth={1.6} aria-hidden="true" />
    <h2>{t('此范围还没有用量记录')}</h2>
    <p>{t('调整时间范围，或查看助手已授权的采集来源。')}</p>
    <Button onClick={onSources}>{t('查看采集覆盖')}</Button>
  </section> : data && <>
    <div className="metrics-grid"><Metric onClick={onRequests} label={t('已记录请求')} value={number(data.logical_requests)} note={t('{count} 次匹配尝试', { count: number(data.attempts) })} />
      <Metric label={t('已知 Token')} value={number(tokenCount(data))} note={t('{count} 项用量不完整', { count: number(data.unknown_usage_count) })} />
      <Metric label={t('估算费用')} value={costText(data.cost_by_currency)} note={t('{count} 项费用未知', { count: number(data.unknown_cost_count) })} /></div>
    <p className="usage-coverage-note"><span>{t('仅统计已采集记录。')}</span><button type="button" className="table-row-link" onClick={onSources}>{t('查看采集覆盖')} <ChevronRight size={14} aria-hidden="true" /></button><button type="button" className="table-row-link" onClick={() => onDrill('request_status', 'failed')}>{t('查看失败请求')} <ChevronRight size={14} aria-hidden="true" /></button></p>
    {data.unattributed_samples > 0 && <p className="usage-sample-note">{t('另有 {count} 条用量快照无法归属到具体请求，已知 Token 与费用仍计入。', { count: number(data.unattributed_samples) })}</p>}
    <UsageTrend rows={list(data.heatmap)} currencies={currencies} />
    <Distribution params={params} revision={revision} labelFor={labelFor} currencies={currencies} onDrill={onDrill} />
    <Performance data={data.performance_by_measurement} />
  </>}</ResourceState>;
}
