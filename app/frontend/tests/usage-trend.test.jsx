import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, it, expect } from 'vitest';
import { trendDays, UsageTrend } from '../src/components/UsageTrend.jsx';
describe('scrollable populated usage chart', () => {
  it('retains every observed day without paging or inventing zero-count dates', () => {
    const result = trendDays([{ date: '2026-10-03', attempts: 4 }, { date: '2026-10-01', attempts: 0 }]);
    expect(result.map(p=>p.date)).toEqual(['2026-10-01','2026-10-03']);
    expect(result.map(p=>p.attempts)).toEqual([0,4]);
  });
  it('keeps a full year and gives every day a non-shrinking column', () => {
    const rows = Array.from({ length: 365 }, (_, i) => ({ date: new Date(Date.UTC(2025,0,1+i)).toISOString().slice(0,10), attempts:i }));
    const html = renderToStaticMarkup(<UsageTrend rows={rows} />);
    expect((html.match(/class="usage-chart-date"/g)||[]).length).toBe(365);
    expect(html).toContain('repeat(365, minmax(42px, 1fr))');
    expect(html).toContain('tabindex="0"');
    expect(html).not.toContain('aria-pressed');
  });
  it('rejects impossible dates and preserves invalid counts as unknown', () => {
    expect(trendDays([{ date:'2026-02-30',attempts:9 }])).toEqual([]);
    expect(trendDays([{date:'2026-10-01',attempts:-1}])[0].attempts).toBe(null);
  });
});
