import { useEffect, useRef } from 'react';
import { t } from '../lib/i18n.jsx';
import { number } from '../lib/management.js';
import { DataTable } from './ManagementUI.jsx';
const stamp = value => {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return NaN;
  const result = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(result) && new Date(result).toISOString().slice(0, 10) === value ? result : NaN;
};
export function trendDays(rows) {
  return rows.filter(row => Number.isFinite(stamp(row.date || row.day))).sort((a, b) => stamp(a.date || a.day) - stamp(b.date || b.day)).map(row => ({ ...row, date: row.date || row.day, attempts: typeof row.attempts === 'number' && Number.isFinite(row.attempts) && row.attempts >= 0 ? row.attempts : null }));
}
export function UsageTrend({ rows }) {
  const scroll = useRef(null);
  const points = trendDays(rows);
  const latest = points.at(-1)?.date;
  useEffect(() => { if (scroll.current) scroll.current.scrollLeft = scroll.current.scrollWidth; }, [latest]);
  if (!points.length) return <p className="table-empty">{t('所选范围还没有可绘制的请求观察。')}</p>;
  const peak = Math.max(1, ...points.map(point => point.attempts || 0));
  const step = 10 ** Math.floor(Math.log10(peak));
  const max = Math.ceil(peak / (step * 2)) * step * 2;
  return <div className="usage-chart">
    <div className="usage-chart-toolbar"><strong>{points[0].date} — {latest}</strong><span>{t('左右滚动查看全部日期')}</span></div>
    <div className="usage-chart-scale">{t('每日请求次数')}</div>
    <div className="usage-chart-canvas"><div className="usage-chart-yaxis" aria-hidden="true">{[max, max / 2, 0].map(value => <span key={value}>{number(value, { notation: 'compact' })}</span>)}</div>
      <div ref={scroll} className="usage-chart-scroll" tabIndex={0} role="region" aria-label={t('请求趋势，左右滚动查看')}>
        <div className="usage-chart-columns" style={{ gridTemplateColumns: `repeat(${points.length}, minmax(42px, 1fr))` }}>
          {points.map(point => <div className="usage-chart-day" key={point.date} title={`${point.date}: ${point.attempts === null ? t('未记录') : number(point.attempts)}`}><div className="usage-chart-track">{point.attempts === null ? <span className="usage-chart-gap" /> : <span className="usage-chart-column" style={{ height: `${point.attempts / max * 100}%` }} />}</div><span className="usage-chart-date">{point.date.slice(5)}</span></div>)}
        </div>
      </div>
    </div>
    <p className="usage-chart-note">{t('只展示有记录的日期；缺少记录不代表当天没有使用。')}</p>
    <details><summary>{t('查看每日明细')}</summary><DataTable rows={points} columns={[{ key: 'date', label: t('日期') }, { key: 'attempts', label: t('物理尝试'), render: row => row.attempts === null ? t('未记录') : number(row.attempts) }, { key: 'tokens', label: t('已知 Token'), render: row => number(row.known_tokens ?? row.tokens) }]} /></details>
  </div>;
}
