import { agentGroup, isPrimaryAgent } from './agentGroups.js';
import { t, locale } from './i18n.jsx';
import { usageDateFilters } from './management.js';

export const unattributed = '\u0000missing';
export const dimensionKeys = ['provider_id', 'model', 'host_id', 'account_id', 'project_id'];
export const dimensionNames = {
  provider_id: '供应商', model: '模型', host_id: '助手', account_id: '账号', project_id: '所属项目',
};

export function dateRange(period, start, end, today = new Date()) {
  if (period === 'custom') return { start: start || '', end: end || '' };
  const localDay = date => [date.getFullYear(), String(date.getMonth() + 1).padStart(2, '0'), String(date.getDate()).padStart(2, '0')].join('-');
  const last = new Date(today);
  if (period === 'yesterday') last.setDate(last.getDate() - 1);
  const first = new Date(last);
  first.setDate(last.getDate() - (period === 'month' ? 29 : period === 'week' ? 6 : 0));
  return { start: localDay(first), end: localDay(last) };
}

export function usageParams(filters, { optionsOnly = false } = {}) {
  const range = dateRange(filters.period, filters.start, filters.end);
  const validDay = day => {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(day)) return false;
    const stamp = Date.parse(day + 'T00:00:00Z');
    return Number.isFinite(stamp) && new Date(stamp).toISOString().slice(0, 10) === day;
  };
  if (!validDay(range.start) || !validDay(range.end) || range.start > range.end) return null;
  const params = new URLSearchParams({
    ...usageDateFilters(range), tz: Intl.DateTimeFormat().resolvedOptions().timeZone, scope: filters.scope || 'user',
  });
  if (!optionsOnly) for (const key of [...dimensionKeys, 'status', 'request_status', 'origin']) {
    const value = filters[key];
    if (value === unattributed) params.append('missing', key);
    else if (value) params.set(key, value);
  }
  return params;
}

export function usagePath(endpoint, params, extras = {}) {
  if (!params) return null;
  const query = new URLSearchParams(params);
  for (const [key, value] of Object.entries(extras)) query.set(key, value);
  return '/api/usage/' + endpoint + '?' + query.toString();
}

export function tokenCount(row) {
  if (!row || row.observed === false || row.known_token_attempts === 0 || row.known_tokens == null) return null;
  return row.known_tokens;
}

export function amountText(value) {
  if (value == null) return t('未知');
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || (numeric === 0 && /[1-9]/.test(String(value).split(/[eE]/)[0]))) return String(value);
  const format = numeric > 0 && numeric < 1e-10 ? { notation: 'scientific', maximumSignificantDigits: 6 } : { maximumFractionDigits: 12 };
  return new Intl.NumberFormat(locale(), format).format(value);
}

export function costText(costs = {}) {
  const entries = Object.entries(costs || {}).filter(([,amount]) => amount != null);
  return entries.map(([ccy, amount]) => ccy + ' ' + amountText(amount)).join(' · ') || t('未知');
}

export function originName(origin) {
  const labels = { proxy: '本机路由', native_log: '原生日志', contextcore_compute: 'Senrelay 整理', provider_check: '服务商检查', manual: '手动记录' };
  return labels[origin] ? t(labels[origin]) : origin || t('未知');
}

// Usage filters follow the six product Agents. Extended configuration hosts stay in Agent setup.
export function productHosts(hosts = []) {
  return (hosts || []).filter(host => host && isPrimaryAgent(agentGroup(host.id)));
}

export function isProductHostValue(hosts, value) {
  if (!value) return false;
  if (isPrimaryAgent(agentGroup(value))) return true;
  return productHosts(hosts).some(host => host.id === value || host.registry_host_id === value);
}

export function hostOptions(hosts, observed) {
  const eligible = productHosts(hosts);
  const labels = new Map();
  for (const row of eligible) {
    const value = row.registry_host_id || row.id;
    if (!value) continue;
    const group = labels.get(value) || [];
    group.push(row.label || row.name || value);
    labels.set(value, group);
  }
  const options = new Map([...labels].map(([value, group]) => [value, group.join(' / ')]));
  for (const row of observed || []) {
    if (!row.value || !isProductHostValue(eligible, row.value)) continue;
    const definition = eligible.find(host => host.id === row.value);
    if (!options.has(row.value)) options.set(row.value, row.label && row.label !== row.value ? row.label : definition?.label || definition?.name || row.value);
  }
  return [...options].map(([value, label]) => ({ value, label }));
}
