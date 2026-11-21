// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { UsagePage } from '../src/pages/UsagePage.jsx';
import { setLang } from '../src/lib/i18n.jsx';
import { amountText, costText, dateRange, unattributed, usageParams } from '../src/lib/usageView.js';

const h = vi.hoisted(() => ({ paths:[], rows:{}, attempts:[] }));
vi.mock('../src/lib/useResource.js', async original => ({
  ...await original(),
  useResource(path) {
    if (!path) return {data:null, loading:false, error:null};
    const url = new URL(path, 'http://localhost');
    h.paths.push(url);
    const key = url.pathname.split('/').at(-1);
    const known = {attempts:2, logical_requests:1, known_token_attempts:1, known_tokens:27, cost_by_currency:{USD:'0.15'}, unknown_cost_count:1, unknown_usage_count:1};
    const summary = {...known, ...h.rows.summary, heatmap:[{...known, date:'2026-11-19', observed:true}]};
    let data = null;
    if (key === 'overview') data = summary;
    else if (key === 'filter-options') data = {provider_id:[{value:null,label:null}], status:[{value:'failed',label:'failed'}]};
    else if (key === 'breakdown') data = {items:[{...known, value:null,label:null}]};
    else if (key === 'logical-requests') data = h.rows.ledger || {total:1,items:[{...known, logical_request_id:'rid/1',model:'fixture-model',status:'failed',request_status:'succeeded',started_at:'2026-11-19T03:00:00Z'}]};
    else if (key === 'attempts') data = {total:h.attempts.length,items:h.attempts};
    else data = h.rows[key] || [];
    return {data,loading:false,error:null,reload:vi.fn()};
  },
}));

let root, container;
const button = text => [...container.querySelectorAll('button')].find(node => node.textContent.trim() === text);
const field = text => [...container.querySelectorAll('label')].find(node => node.querySelector(':scope > span')?.textContent === text)?.querySelector('select,input');
const endpoint = key => h.paths.filter(url => url.pathname.endsWith('/' + key)).at(-1);
async function click(node) { expect(node).toBeTruthy(); await act(async () => node.click()); }

beforeEach(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  setLang('en'); sessionStorage.clear();
  h.paths=[]; h.rows={}; h.attempts=[];
  HTMLDialogElement.prototype.showModal = function() {this.open=true;};
  HTMLDialogElement.prototype.close = function() {this.open=false;};
  container=document.createElement('div'); document.body.append(container); root=createRoot(container);
});
afterEach(async () => {await act(async () => root.unmount()); container.remove(); setLang('zh');});

it('preserves the selected calendar scope and unknown provider across breakdown, logical ledger and CSV export', async () => {
  h.attempts = [
    {id:'a1',attempt_number:1,status:'failed',provider_name:'Original provider',account_name:'At dispatch',error_code:'429',origins:['proxy']},
    {id:'a2',attempt_number:2,status:'succeeded',provider_name:'Fallback provider',origins:['proxy'],actual_model:'accepted-model'},
  ];
  await act(async () => root.render(<UsagePage />));
  expect(endpoint('logical-requests')).toBeUndefined();
  expect(endpoint('breakdown').searchParams.get('dimension')).toBe('provider_id');
  expect(container.querySelector('.usage-chart-day').getAttribute('title')).toMatch(/1$/);
  await click(button('Unattributed'));
  const url = endpoint('logical-requests');
  expect(url.searchParams.getAll('missing')).toEqual(['provider_id']);
  expect(url.searchParams.get('scope')).toBe('user');
  const csv = new URL(container.querySelector('a[download]').href);
  for (const key of ['scope','missing','tz','start','end']) expect(csv.searchParams.get(key)).toBe(url.searchParams.get(key));
  expect(field('Provider').value).toBe(unattributed);
  expect(container.querySelector('.usage-request-row').textContent).toMatch(/Completed/);
  await click(container.querySelector('.usage-request-row'));
  expect(endpoint('attempts').searchParams.get('logical_request_id')).toBe('rid/1');
  expect(endpoint('attempts').searchParams.has('missing')).toBe(false);
  expect(container.querySelector('dialog').textContent).toContain('429');
  expect(container.querySelectorAll('.usage-attempt')[0].textContent).toContain('At dispatch');
  expect(container.querySelectorAll('.usage-attempt')[1].textContent).toContain('accepted-model');
});

it('uses the final request status for the failed requests shortcut and restores overview on history navigation', async () => {
  const onSectionChange=vi.fn();
  await act(async () => root.render(<UsagePage onSectionChange={onSectionChange} />));
  await click(button('View failed requests'));
  expect(onSectionChange).toHaveBeenCalledWith('requests');
  await act(async () => root.render(<UsagePage section="requests" onSectionChange={onSectionChange} />));
  expect(endpoint('logical-requests').searchParams.get('request_status')).toBe('failed');
  expect(endpoint('logical-requests').searchParams.has('status')).toBe(false);
  await act(async () => root.render(<UsagePage onSectionChange={onSectionChange} />));
  expect(container.querySelector('.usage-nav button[aria-current]').textContent).toBe('Overview');
});

it('opens a deep-linked request without inheriting a restrictive list scope', async () => {
  await act(async () => root.render(<UsagePage section="requests" requestId="id /&?" agentId="omp" />));
  expect(endpoint('logical-requests').searchParams.get('host_id')).toBe('omp');
  expect(endpoint('attempts').searchParams.get('logical_request_id')).toBe('id /&?');
  expect(endpoint('attempts').searchParams.has('host_id')).toBe(false);
});

it('computes calendar presets at month boundaries and rejects reversed custom ranges', () => {
  expect(dateRange('yesterday', '', '', new Date(2026,0,1))).toEqual({start:'2025-12-31',end:'2025-12-31'});
  expect(dateRange('week', '', '', new Date(2026,0,1))).toEqual({start:'2025-12-26',end:'2026-01-01'});
  expect(usageParams({period:'custom', start:'2026-10-02',end:'2026-10-01'})).toBe(null);
  expect(usageParams({period:'custom', start:'2026-02-30',end:'2026-03-01'})).toBe(null);
});

it('keeps positive microcharges, explicit zero, missing prices and different currencies distinguishable', () => {
  expect(amountText('0.00000000000000001')).not.toBe('0');
  expect(amountText('0')).toBe('0');
  expect(costText({})).toBe('Unknown');
  expect(costText({USD:'3.2', EUR:'1.5'})).toBe('USD 3.2 · EUR 1.5');
});

it('shows native cumulative samples separately from actual requests in the overview, ledger and detail', async () => {
  h.rows.summary={ logical_requests:0, attempts:0, unattributed_samples:1 };
  h.rows.ledger={total:1, items:[{ logical_request_id:'sample-1', attempts:0, known_tokens:12, known_token_attempts:1, unattributed_sample:true }]};
  h.attempts=[{id:'sample-a', unattributed_sample:true, model:'meter-model', origins:['native_log']}];
  await act(async () => root.render(<UsagePage />));
  expect(container.querySelector('.metric-card strong').textContent).toBe('0');
  expect(container.querySelector('.usage-sample-note').textContent).toContain('1 usage snapshots');
  await click(button('Requests'));
  expect(container.querySelector('.usage-request-row').textContent).toContain('Usage snapshot');
  await click(container.querySelector('.usage-request-row'));
  expect(container.querySelector('dialog').textContent).toContain('Snapshot ID');
  expect(container.querySelector('dialog').textContent).not.toContain('Attempt 1');
});

it('keeps overview filters intact on return from a drill and isolates assistants from prior global filters', async () => {
  sessionStorage.setItem('senrelay.usage.filters', JSON.stringify({ model:'previous-global-model' }));
  await act(async () => root.render(<UsagePage />));
  expect(endpoint('overview').searchParams.get('model')).toBe('previous-global-model');
  await click(button('Unattributed'));
  expect(endpoint('logical-requests').searchParams.getAll('missing')).toEqual(['provider_id']);
  await click(button('Overview'));
  expect(endpoint('overview').searchParams.has('missing')).toBe(false);
  expect(endpoint('overview').searchParams.get('model')).toBe('previous-global-model');
  await act(async () => root.render(<UsagePage agentId="omp" projectId="project-a" />));
  const scoped = endpoint('overview').searchParams;
  expect(scoped.get('host_id')).toBe('omp');
  expect(scoped.get('project_id')).toBe('project-a');
  expect(scoped.has('model')).toBe(false);
  await click(button('View failed requests'));
  await click(button('Clear filters'));
  const cleared = endpoint('logical-requests').searchParams;
  expect(cleared.has('request_status')).toBe(false);
  expect(cleared.get('host_id')).toBe('omp');
  expect(cleared.get('project_id')).toBe('project-a');
  expect(JSON.parse(sessionStorage.getItem('senrelay.usage.filters')).model).toBe('previous-global-model');
});

it('uses shared native log identity for a desktop menu link and keeps the desktop scope for quotas', async () => {
  h.rows.hosts = [
    { id:'codex', registry_host_id:'codex', label:'Codex CLI' },
    { id:'codex-desktop', registry_host_id:'codex', label:'Codex Desktop' },
  ];
  await act(async () => root.render(<UsagePage agentId="codex-desktop" />));
  expect(endpoint('overview').searchParams.get('host_id')).toBe('codex');
  expect(field('Assistant').selectedOptions[0].textContent).toBe('Codex CLI / Codex Desktop');
  await click(button('Account quotas'));
  expect(endpoint('account-quotas').searchParams.get('host_id')).toBe('codex-desktop');
});

it('offers source coverage for an empty reporting scope and preserves snapshot-only reports', async () => {
  h.rows.summary = { attempts: 0, logical_requests: 0, unattributed_samples: 0 };
  await act(async () => root.render(<UsagePage />));
  expect(container.querySelector('.ops-empty-report')).toBeTruthy();
  expect(container.querySelector('.metrics-grid')).toBeNull();
  await click(container.querySelector('.ops-empty-report button'));
  expect(container.querySelector('.usage-nav button[aria-current]').textContent).toBe('Sources and pricing');
  h.rows.summary.unattributed_samples = 2;
  await click(button('Overview'));
  expect(container.querySelector('.metrics-grid')).toBeTruthy();
  expect(container.querySelector('.ops-empty-report')).toBeNull();
});
