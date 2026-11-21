import { useRef, useState } from 'react';
import '../../styles/global-configuration.css';
import { Button, EmptyState, ErrorNotice, Field, Modal, ResourceState } from '../ui.jsx';
import { t } from '../../lib/i18n.jsx';
import { mutate } from '../../lib/api.js';
import { useResource } from '../../lib/useResource.js';
import { nativeTargetName } from '../../lib/agentGroups.js';
import { ResourcePagination } from './ResourcePagination.jsx';

const catalogs = [
  ['providers', '服务商'], ['models', '模型'], ['accounts', '账号'], ['bundles', '项目配置包']
];

function hostChoices(hosts, collection) {
  if (collection === 'bundles') return hosts.map(host => ({ ids: [host.id], name: nativeTargetName(host) }));
  const combined = hosts.filter(host => ['codex', 'codex-desktop'].includes(host.id) && host.shared_configuration);
  const choices = hosts.filter(host => combined.length !== 2 || !combined.includes(host))
    .map(host => ({ ids: [host.id], name: nativeTargetName(host) }));
  if (combined.length === 2) choices.unshift({ ids: combined.map(host => host.id), name: combined.map(nativeTargetName).join(' / ') });
  return choices;
}

function HostPicker({label, groups, values, onChange}) {
  return <fieldset className="global-host-picker">
    <legend>{label}</legend>
    <div className="toolbar-actions">
      <Button onClick={() => onChange(groups.flatMap(group => group.ids))}>{t('全部选中')}</Button>
      <Button onClick={() => onChange([])}>{t('清空选择')}</Button>
    </div>
    <div className="global-checkboxes">{groups.map(group => <label className="check-label" key={group.ids[0]}>
      <input type="checkbox" checked={group.ids.some(id => values.includes(id))} onChange={event =>
        onChange(event.target.checked ? [...new Set([...values, ...group.ids])] : values.filter(id => !group.ids.includes(id)))}/>{group.name}
    </label>)}</div>
  </fieldset>;
}

function Editor({collection, row, hosts, onClose, onSaved, onProvider, onReload}) {
  const inherited = [...new Set([...(row.host_ids || []), ...(row.inherited_agent_ids || [])])];
  const initialAgents = row.agent_ids || inherited;
  const [agents, setAgents] = useState(initialAgents);
  const [allowed, setAllowed] = useState(row.host_ids || []);
  const [allAllowed, setAllAllowed] = useState(!row.host_ids?.length);
  const [name, setName] = useState(row.name);
  const [capacity, setCapacity] = useState(row.context_window?.toString() || '');
  const [order, setOrder] = useState(row.sort_index?.toString() || '');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const lock = useRef(false);
  const groups = hostChoices(hosts, collection);
  const sameSet = (a, b) => a.length === b.length && a.every(id => b.includes(id));
  async function save(event) {
    event.preventDefault();
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try {
      const patch = { expected_version: row.version };
      if (name.trim() !== row.name) patch.name = name.trim();
      if (collection === 'models' && capacity !== (row.context_window?.toString() || '')) {
        if (capacity && !Number.isSafeInteger(Number(capacity))) throw new Error(t('请输入可准确表示的正整数。'));
        patch.context_window = capacity ? Number(capacity) : null;
      }
      if (collection === 'providers' && order !== (row.sort_index?.toString() || '')) patch.sort_index = order ? Number(order) : null;
      if (collection !== 'models' && !sameSet(agents, initialAgents)) patch.agent_ids = agents;
      if (collection === 'bundles') {
        const next = allAllowed ? [] : allowed;
        if (!allAllowed && !next.length) throw new Error(t('请选择可应用的助手，或明确允许所有助手。'));
        if (!sameSet(next, row.host_ids || [])) {
          patch.host_ids = next;
          // Older bundles infer visibility from their execution scope until explicitly assigned.
          if (!row.agent_ids && !patch.agent_ids) patch.agent_ids = agents;
        }
      }
      if (Object.keys(patch).length > 1) await mutate('/api/manage/' + collection + '/' + encodeURIComponent(row.id), 'PATCH', patch);
      onSaved();
    } catch (failure) { setError(failure); }
    finally { lock.current = false; setBusy(false); }
  }
  return <Modal wide busy={busy} title={t('编辑目录定义')} onClose={onClose}>
    <form onSubmit={save}><fieldset className="global-config-form" disabled={busy}>
      <Field label={t('名称')}><input required maxLength={200} value={name} onChange={e=>setName(e.target.value)}/></Field>
      {collection === 'providers' && <Field label={t('目录排序')}><input type="number" step="1" min="-1000000" max="1000000" value={order} onChange={e=>setOrder(e.target.value)}/></Field>}
      {collection === 'models' && <><Field label={t('上下文窗口')}><input type="number" min="1" step="1" max={Number.MAX_SAFE_INTEGER} value={capacity} onChange={e=>setCapacity(e.target.value)}/></Field>
        <p>{t('模型跟随所属服务商分配。')}</p><Button disabled={name !== row.name || capacity !== (row.context_window?.toString() || '')} onClick={() => onProvider(row.provider_id)}>{t('查看所属服务商')}</Button></>}
      {collection !== 'models' && <HostPicker label={t('分配到助手目录')} groups={groups} values={agents} onChange={setAgents}/>}
      {collection === 'bundles' && <section><label className="check-label"><input type="checkbox" checked={allAllowed} onChange={event => setAllAllowed(event.target.checked)}/>{t('允许所有受支持的助手应用此包')}</label>
        {!allAllowed && <HostPicker label={t('允许应用此包的助手')} groups={groups} values={allowed} onChange={setAllowed}/>}</section>}
      {collection !== 'models' && <p>{t('分配会更新助手目录。已写入的配置仍可从对应助手管理，凭据和运行中的配置保持原状。')}</p>}
      <ErrorNotice error={error}/>{error?.status === 409 && <Button onClick={onReload}>{t('放弃编辑并读取最新目录')}</Button>}<div className="toolbar-actions"><Button type="submit" variant="primary" busy={busy}>{t('保存目录定义')}</Button><Button onClick={onClose}>{t('取消')}</Button></div>
    </fieldset></form>
  </Modal>;
}

export default function GlobalConfigurationCatalog({hosts = [], revision, onChanged}) {
  const [collection, setCollection] = useState('providers');
  const [focus, setFocus] = useState('');
  const [query, setQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [editing, setEditing] = useState(null);
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState('');
  const params = new URLSearchParams({summary:'true', scope:'all', limit:'20', offset:String(offset), q:query});
  if (focus) params.set('ids',focus);
  const resource = useResource('/api/manage/' + collection + '?' + params, revision + ':' + tick);
  const items = resource.data?.items || [];
  function switchTo(type, id = '') { setCollection(type); setFocus(id); setQuery(''); setOffset(0); setEditing(null); setNotice(''); }
  return <section className="global-config-catalog">
    <h2>{t('配置目录')}</h2>
    <div className="toolbar-actions" role="group" aria-label={t('配置类型')}>{catalogs.map(([key,label]) =>
      <Button key={key} aria-pressed={collection === key} onClick={() => switchTo(key)}>{t(label)}</Button>)}</div>
    {focus ? <Button onClick={()=>switchTo(collection)}>{t('查看所有服务商')}</Button> :
      <Field label={t('搜索配置定义')}><input type="search" maxLength={200} value={query} onChange={e=>{setQuery(e.target.value);setOffset(0);}}/></Field>}
    <ResourceState resource={resource}>{items.length ? <ul className="global-assets">{items.map(row =>
      <li key={row.id} className="global-asset-row"><div className="global-asset-heading"><div><strong>{row.name}</strong>
        <small>{[row.base_url || row.model_id || row.kind, t('版本')+' '+row.version].filter(Boolean).join(' · ')}</small></div>
        <Button onClick={()=>{setEditing(row);setNotice('');}}>{t('编辑定义')}</Button></div></li>)}</ul> :
      <EmptyState title={t('此目录暂无匹配定义')}/>}</ResourceState>
    <ResourcePagination resource={resource} onPage={setOffset} label={t('配置目录分页')}/>
    {notice && <p role="status">{notice}</p>}
    {editing && <Editor key={editing.id} collection={collection} row={editing} hosts={hosts} onClose={()=>setEditing(null)} onProvider={id=>switchTo('providers',id)} onReload={()=>{setEditing(null);setTick(v=>v+1);}}
      onSaved={()=>{setEditing(null);setTick(v=>v+1);onChanged?.();setNotice(t('目录定义已保存'));}}/>}
  </section>;
}
