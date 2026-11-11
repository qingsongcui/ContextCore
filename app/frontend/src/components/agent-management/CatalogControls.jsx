import { useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { useResource } from '../../lib/useResource.js';
import { list } from '../../lib/management.js';
import { Button, EmptyState, ErrorNotice, Field, ResourceState } from '../ui.jsx';
import { ResourcePagination } from './ResourcePagination.jsx';

export function catalogPath(collection, { agentId, kind, providerId, scope = 'assigned', offset = 0, query = '', limit = 50, ids, includeUnbound = false } = {}) {
  const params = new URLSearchParams({ summary: 'true', scope, limit: String(limit), offset: String(offset), q: query.trim() });
  if (agentId) params.set('agent_id', agentId);
  if (kind) params.set('kind', kind);
  if (providerId !== undefined && providerId !== null) params.set('provider_id', providerId);
  if (includeUnbound) params.set('include_unbound', 'true');
  if (ids !== undefined) params.set('ids', ids.join(','));
  return `/api/manage/${collection}?${params}`;
}

/** A selection is independent of the currently browsed page. Never clear it
 * because the selected item is outside a search result or a different page. */
export function CatalogChoice({ collection, label, value, onChange, agentId, providerId, revision, scope = 'assigned', allowNone = true, noneLabel, extraOptions = [], includeUnbound = false, excludeIds = [] }) {
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const resource = useResource(catalogPath(collection, { agentId, providerId, scope, query, offset, includeUnbound }), revision);
  const items = list(resource.data);
  const known = [...items, ...extraOptions].find(item => item.id === value);
  const selected = useResource(value && !known ? catalogPath(collection, { scope: 'all', ids: [value], limit: 1 }) : null, revision);
  const selectedItem = known || list(selected.data)[0];
  const options = [...items, ...extraOptions.filter(item => !items.some(row => row.id === item.id))].filter(item => !excludeIds.includes(item.id));
  if (value && !excludeIds.includes(value) && !options.some(item => item.id === value)) options.unshift(selectedItem || { id: value, name: value });
  const optionText = item => {
    const base = `${item.name || item.id}${item.model_id ? ` · ${item.model_id}` : ''}`;
    const shared = options.filter(other => (other.name || other.id) === (item.name || item.id) && (other.model_id || '') === (item.model_id || ''));
    return shared.length > 1 ? `${base} · ${item.base_url || item.id}` : base;
  };
  return <div className="agent-catalog-choice">
    <Field label={label}><select required={!allowNone} value={value || ''} disabled={resource.loading || Boolean(resource.error)} onChange={event => onChange(event.target.value, options.find(item => item.id === event.target.value))}>
      <option value="">{noneLabel || t('未绑定')}</option>{options.map(item => <option key={item.id} value={item.id}>{optionText(item)}</option>)}
    </select></Field>
    <details className="agent-advanced"><summary>{t('查找更多选项')}</summary>
      <Field label={t('搜索{category}', { category: label })}><input type="search" value={query} maxLength={200} onChange={event => { setQuery(event.target.value); setOffset(0); }} /></Field>
      <ResourcePagination label={t('选项分页')} resource={resource} onPage={setOffset} />
      {!resource.loading && !resource.error && !items.length ? <p>{t('没有匹配的资料')}</p> : null}
    </details>
    <ErrorNotice error={resource.error || selected.error} />
  </div>;
}

/** Checkbox membership stays in the parent draft across searches and pages. */
export function BundleAssetSelection({ value = [], onChange, revision }) {
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [selectedOnly, setSelectedOnly] = useState(false);
  const ids = selectedOnly ? value.slice(offset, offset + 50) : undefined;
  const resource = useResource(catalogPath('assets', { scope: 'all', offset: selectedOnly ? 0 : offset, ids, query: selectedOnly ? '' : query }), revision);
  const items = list(resource.data);
  const visible = selectedOnly ? ids.map(id => items.find(item => item.id === id) || { id, name: t('资料定义已移除'), missing: true }) : items;
  const paging = selectedOnly && resource.data?.summary ? { ...resource, data: { ...resource.data, items: visible, total: value.length, offset, has_more: offset + visible.length < value.length } } : resource;
  function toggle(id, checked) { onChange(checked ? [...new Set([...value, id])] : value.filter(item => item !== id)); }
  return <section className="agent-bundle-assets" aria-label={t('组合中的资产')}>
    <h3>{t('组合中的资产')}</h3><p>{t('已选择 {count} 项资料', { count: value.length })}</p>
    <div className="toolbar-actions"><Button aria-pressed={!selectedOnly} onClick={() => { setSelectedOnly(false); setOffset(0); }}>{t('浏览全部资料')}</Button><Button aria-pressed={selectedOnly} onClick={() => { setSelectedOnly(true); setOffset(0); }}>{t('查看已选资料')}</Button></div>
    {!selectedOnly ? <Field label={t('搜索组合资料')}><input type="search" value={query} maxLength={200} onChange={event => { setQuery(event.target.value); setOffset(0); }} /></Field> : null}
    <ResourceState resource={resource}><div className="agent-resource-checklist">{visible.map(item => <label className="inline-checkbox" key={item.id}><input type="checkbox" checked={value.includes(item.id)} onChange={event => toggle(item.id, event.target.checked)} />{item.name}{item.kind ? ` · ${item.kind}` : ''}{item.missing ? ` · ${item.id}` : ''}</label>)}</div>{!visible.length ? <p>{t('没有匹配的资料')}</p> : null}</ResourceState>
    <ResourcePagination label={t('组合资料分页')} resource={paging} onPage={setOffset} />
  </section>;
}

export function SharedLibrary({ revision, onManage }) {
  const [category, setCategory] = useState('providers');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const collection = category === 'providers' ? 'providers' : 'assets';
  const resource = useResource(catalogPath(collection, { scope: 'all', kind: collection === 'assets' ? category : undefined, query, offset }), revision);
  return <>
    <div className="toolbar-actions">{[['providers', '模型与账号'], ['mcp', '工具'], ['skill', '技能'], ['prompt', '使用规则']].map(([id, label]) => <Button key={id} aria-pressed={category === id} onClick={() => { setCategory(id); setOffset(0); setQuery(''); }}>{t(label)}</Button>)}</div>
    <Field label={t('搜索共享资料')}><input type="search" value={query} maxLength={200} onChange={event => { setQuery(event.target.value); setOffset(0); }} /></Field>
    <ResourceState resource={resource}><div className="agent-resource-checklist">{list(resource.data).map(item => <div className="agent-shared-row" key={item.id}><span>{item.name}</span><Button onClick={() => onManage(category)}>{t('管理这个分类')}</Button></div>)}</div>{!list(resource.data).length ? <EmptyState title={t('没有匹配的资料')} /> : null}</ResourceState>
    <ResourcePagination shared resource={resource} onPage={setOffset} />
  </>;
}
