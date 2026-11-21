import { useEffect, useRef, useState } from 'react';
import { ArrowLeft, Download, Layers3, Pencil, Plus, Upload } from 'lucide-react';
import { mutate, request } from '../../lib/api.js';
import { configurationTarget, nativeTargetName } from '../../lib/agentGroups.js';
import { downloadText } from '../../lib/downloadText.js';
import { t } from '../../lib/i18n.jsx';
import { list, mcpNeedsReview } from '../../lib/management.js';
import { useResource } from '../../lib/useResource.js';
import { Button, EmptyState, ErrorNotice, Field, PageHeader, ResourceState } from '../ui.jsx';
import { AssetTools } from '../AssetTools.jsx';
import { configuredLabel } from './ConfigurationState.jsx';
import GlobalConfigurationCatalog from './GlobalConfigurationCatalog.jsx';
import { GlobalAssetEditor } from './GlobalAssetEditor.jsx';
import { ImportDialog } from './ImportDialog.jsx';
import { NativeConfiguration } from './NativeConfiguration.jsx';
import { ResourcePagination } from './ResourcePagination.jsx';
import { SharedBatchHistory, SharedBatchReceipt } from './SharedBatchReceipt.jsx';
import '../../styles/global-library.css';

const TYPES = [
  ['mcp', 'MCP', 'mcp'],
  ['skill', 'Skills', 'skills'],
  ['prompt', '使用规则', 'prompts'],
];
const CAPABILITIES = Object.fromEntries(TYPES.map(([id,, capability]) => [id, capability]));
const TYPE_NAMES = Object.fromEntries(TYPES.map(([id, label]) => [id, label]));

// Only combine surfaces when every displayed resource kind goes to the same
// physical target. The mixed view keeps partially shared surfaces selectable.
function hostChoices(hosts, kinds) {
  const groups = new Map();
  for (const host of hosts) {
    const signature = JSON.stringify(kinds.map(kind => configurationTarget(hosts, host.id, kind)));
    const members = groups.get(signature) || [];
    members.push(host);
    groups.set(signature, members);
  }
  return [...groups.values()].map(members => {
    const canonical = configurationTarget(hosts, members[0].id, kinds[0]);
    const first = members.find(member => member.id === canonical) || members[0];
    return members.length === 1 ? first : {...first, label:members.map(nativeTargetName).join(' / ')};
  });
}

const INSTALLABLE = new Set(['verified', 'update_available', 'missing', 'removed', 'not_managed', 'native_available']);
const REMOVABLE = new Set(['verified', 'update_available', 'missing']);

function eligibility(item, host, operation) {
  if (!host?.capabilities?.[CAPABILITIES[item.kind]]) return { label: t('不支持'), eligible: false };
  if (host.config_root?.status === 'blocked') return { label: t('配置目录需要核对'), eligible: false };
  if (operation === 'install' && mcpNeedsReview(item)) return { label: t('请先核对工具定义'), eligible: false };
  const status = item.cells?.[host.id]?.status || 'not_checked';
  return {
    label: status === 'unsupported' ? t('不支持') : configuredLabel(status),
    eligible: (operation === 'install' ? INSTALLABLE : REMOVABLE).has(status),
  };
}

export function GlobalLibrary({ hosts = [], revision, onChanged, onBack, onHostPlans, initialKind, focusedRecord, onLocation }) {
  const [kind, setKind] = useState(() => TYPES.some(([id]) => id === initialKind) ? initialKind : 'all');
  const [newKind, setNewKind] = useState('mcp');
  const [focused, setFocused] = useState(focusedRecord || '');
  const focusedSelected = useRef(false);
  const [panel, setPanel] = useState(() => ['history', 'config'].includes(initialKind) ? initialKind : 'assets');
  const [offset, setOffset] = useState(0);
  const [query, setQuery] = useState('');
  const [operation, setOperation] = useState('install');
  const [selection, setSelection] = useState([]);
  const [hostIds, setHostIds] = useState([]);
  const [editor, setEditor] = useState(undefined);
  const [importing, setImporting] = useState(false);
  const [nativeHostId, setNativeHostId] = useState('');
  const [nativeOpen, setNativeOpen] = useState(false);
  const [batch, setBatch] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [refresh, setRefresh] = useState(0);
  const pending = useRef(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const allKinds = Object.keys(CAPABILITIES);
  const mixed = kind === 'all';
  const formKind = mixed ? newKind : kind;
  const targets = hostChoices(hosts, mixed ? allKinds : [kind]);
  const hostById = new Map(hosts.map(host => [host.id, host]));
  function ownerOf(host, itemKind) {
    return hostById.get(configurationTarget(hosts, host.id, itemKind));
  }
  const ids = hosts.map(host => host.id).join(',');
  const tick = revision + ':' + refresh;
  const matrixPath = ids ? '/api/manage/shared-resource-matrix?' + new URLSearchParams({
    scope: 'global', host_ids: ids, kind, q: query.trim(), offset: String(offset), ...(focused ? {asset_id: focused} : {}),
  }) : '/api/manage/assets?' + new URLSearchParams({ summary: 'true', scope: 'all', ...(mixed ? {} : {kind}), q: query.trim(), offset: String(offset), limit: '15', ...(focused ? {ids: focused} : {}) });
  const matrix = useResource(panel === 'assets' ? matrixPath : null, tick);
  const rows = list(matrix.data);
  const focusedItem = focused && rows.find(item => item.id === focused && item.kind === kind);
  useEffect(() => {
    if (focusedItem && !focusedSelected.current) {
      focusedSelected.current = true;
      setSelection([focusedItem]);
    }
  }, [focusedItem]);
  const nativeHosts = hostChoices(hosts, [formKind]).filter(host => host.capabilities?.[CAPABILITIES[formKind]] && host.config_root?.status !== 'blocked');
  const sourceHost = nativeHosts.find(host => host.id === nativeHostId) || nativeHosts[0];
  const chosen = new Set(selection.map(item => item.id));
  // Keep off-page selections. Use fresh state for a visible item only when its selected version still matches.
  const selected = selection.map(item => rows.find(row => row.id === item.id && row.version === item.version) || item);
  const options = targets.map(host => ({
    host,
    items: selected.filter(item => eligibility(item, ownerOf(host, item.kind), operation).eligible),
    supported: (mixed ? allKinds : [kind]).some(itemKind => ownerOf(host, itemKind)?.capabilities?.[CAPABILITIES[itemKind]]),
  }));
  const assignments = new Map();
  for (const {host, items} of options) {
    if (!hostIds.includes(host.id)) continue;
    for (const item of items) {
      const owner = ownerOf(host, item.kind);
      if (!assignments.has(owner.id)) assignments.set(owner.id, new Map());
      assignments.get(owner.id).set(item.id, item);
    }
  }
  const picked = [...assignments].map(([hostId, assets]) => ({host:hostById.get(hostId), items:[...assets.values()]}));
  const oversized = picked.some(entry => entry.items.length > 100);
  const total = picked.reduce((sum, entry) => sum + entry.items.length, 0);

  function changed() { setRefresh(value => value + 1); onChanged?.(); }
  function clear() { setSelection([]); setHostIds([]); setError(null); }
  function select(row, checked) {
    setSelection(current => checked ? [...current.filter(item => item.id !== row.id), row] : current.filter(item => item.id !== row.id));
  }
  async function action(run) {
    if (pending.current) return;
    pending.current = true; setBusy(true); setError(null);
    try { await run(); } catch (failure) { if (mounted.current) setError(failure); }
    finally { pending.current = false; if (mounted.current) setBusy(false); }
  }
  function preview() {
    if (!total || oversized) return;
    action(async () => {
      const result = await mutate('/api/manage/shared-batches', 'POST', {
        operation, targets: picked.map(({host, items}) => ({
          host_id: host.id, asset_versions: Object.fromEntries(items.map(item => [item.id, item.version])),
        })),
      });
      if (!result?.id || !Array.isArray(result.targets)) throw new Error(t('批量预览未返回可核对的回执，请查看分发记录。'));
      if (mounted.current) { setBatch(result); setRefresh(value => value + 1); }
    });
  }
  function exportCurrent() {
    action(async () => {
      const data = await request('/api/manage/assets/export' + (mixed ? '' : '?kind=' + kind));
      if (mounted.current) downloadText('Senrelay-' + kind + '.json', JSON.stringify(data, null, 2));
    });
  }

  return <section className="global-library">
    <Button className="global-back" icon={ArrowLeft} onClick={onBack}>{t('返回助手列表')}</Button>
    <PageHeader title={t('全局资源库')} description={t('统一维护 Skill、MCP 和使用规则，选择目标助手后按差异批量应用。')} />
    <nav className="global-tabs" aria-label={t('全局资源分类')}>{[['all', '全部资源'], ...TYPES].map(([id, label]) =>
      <Button key={id} disabled={busy} aria-pressed={panel === 'assets' && kind === id} onClick={() => {
        if (kind !== id) { clear(); setFocused(''); setKind(id); setQuery(''); setOffset(0); onLocation?.(id); }
        if (kind === id && panel !== 'assets') onLocation?.(id);
        setPanel('assets');
      }}>{t(label)}</Button>)}
      <Button disabled={busy} aria-pressed={panel === 'config'} onClick={() => {setPanel('config'); onLocation?.('config');}}>{t('配置目录')}</Button>
      <Button disabled={busy} aria-pressed={panel === 'history'} onClick={() => {setPanel('history'); onLocation?.('history');}}>{t('分发记录')}</Button>
    </nav>
    <ErrorNotice error={error}/>
    {panel === 'config' ? <GlobalConfigurationCatalog hosts={hosts} revision={tick} onChanged={changed}/> : panel === 'history' ? <SharedBatchHistory revision={tick} onOpen={setBatch}/> : <>
      <div className="global-toolbar">
        {focused ? <div className="global-focus"><strong>{t('来自个人工作台的资源')}</strong>
          <Button onClick={() => {setFocused(''); clear(); onLocation?.(kind);}}>{t('查看此分类全部资源')}</Button></div>
          : <Field label={t('搜索共享资料')}><input type="search" value={query} maxLength={200} onChange={event => { setQuery(event.target.value); setOffset(0); }}/></Field>}
        <div className="toolbar-actions">
          {mixed && <Field label={t('新建与导入分类')}><select value={newKind} onChange={event => setNewKind(event.target.value)}>{TYPES.map(([id,label]) => <option key={id} value={id}>{t(label)}</option>)}</select></Field>}
          <Button icon={Plus} disabled={busy} onClick={() => setEditor(null)}>{t('新建定义')}</Button>
          <Button icon={Upload} disabled={busy} onClick={() => setImporting(true)}>{t('导入共享定义')}</Button>
          <Button icon={Download} disabled={busy} onClick={exportCurrent}>{t('导出当前分类')}</Button>
        </div>
      </div>
      <details className="global-native"><summary>{t('从本机助手收集已有资源')}</summary>
        <Field label={t('来源助手')}><select value={sourceHost?.id || ''} onChange={event => setNativeHostId(event.target.value)}>{nativeHosts.map(host => <option key={host.id} value={host.id}>{nativeTargetName(host)}</option>)}</select></Field>
        <Button disabled={busy || !sourceHost} onClick={() => setNativeOpen(true)}>{t('读取助手现有配置')}</Button>
      </details>
      <div className="global-work-area"><section className="global-inventory">
        <div className="global-section-head"><h2>{t('资源清单')}</h2><Button disabled={busy || !rows.length || matrix.loading} onClick={() => {
          setSelection(current => [...current.filter(item => !rows.some(row => row.id === item.id)), ...rows]);
        }}>{t('全选本页资源')}</Button></div>
        <ResourceState resource={matrix}>{rows.length ? <ul className="global-assets">{rows.map(item =>
          <li className="global-asset-row" key={item.id}>
            <div className="global-asset-heading">
              <label><input type="checkbox" disabled={busy} checked={chosen.has(item.id)} onChange={e => select(item, e.target.checked)}/><span><strong>{item.name}</strong><small>{mixed && <>{t(TYPE_NAMES[item.kind] || item.kind)} · </>}{t('版本')} {item.version}</small></span></label>
              <Button icon={Pencil} disabled={busy} onClick={() => setEditor(item)}>{t('编辑定义')}</Button>
            </div>
            <details><summary>{t('查看各助手状态及版本')}</summary>
              <ul className="global-states">{hostChoices(hosts,[item.kind]).map(host => <li key={host.id}><span>{nativeTargetName(host)}</span><small>{eligibility(item,host,operation).label}{item.cells?.[host.id]?.applied_version != null && <> · {t('文件版本 {version}', {version:item.cells[host.id].applied_version})}</>}</small></li>)}</ul>
              <AssetTools item={{...item, _summary: true}} selectedAgent={t('全局资源库')} isCurrent={() => mounted.current} onChanged={changed}/>
            </details>
          </li>)}</ul> : <EmptyState title={t('没有匹配的资料')}/>}</ResourceState>
        <ResourcePagination shared resource={matrix} onPage={setOffset} busy={busy}/>
      </section>
      <aside className="global-targets">
        <h2><Layers3 size={18}/>{t('批量分发')}</h2>
        <Field label={t('选中项的操作')}><select disabled={busy} value={operation} onChange={event => { setOperation(event.target.value); setHostIds([]); }}>
          <option value="install">{t('安装或更新')}</option><option value="uninstall">{t('移除受管资源')}</option>
        </select></Field>
        <div className="global-selection"><strong>{t('已选 {count} 项资源', {count: selection.length})}</strong>
          {!!selection.length && <><Button disabled={busy} onClick={clear}>{t('清空选择')}</Button><ul>{selection.map(item => <li key={item.id}>{item.name}</li>)}</ul></>}
        </div>
        <fieldset disabled={busy} className="global-hosts"><legend>{t('目标助手')}</legend>
          <Button disabled={!options.some(entry => entry.items.length)} onClick={() => setHostIds(options.filter(entry => entry.items.length).map(entry => entry.host.id))}>{t('全选可用助手')}</Button>
          <div className="global-host-scroll">{options.map(({host,items,supported}) => <label key={host.id} className="global-host-choice">
            <input type="checkbox" disabled={!items.length} checked={!!items.length && hostIds.includes(host.id)} onChange={e =>
              setHostIds(current => e.target.checked ? [...current.filter(id => id !== host.id), host.id] : current.filter(id => id !== host.id))}/>
            <span>{nativeTargetName(host)}</span><small>{!supported ? t('不支持') : selection.length ? t('{ready}/{total} 项可应用', {ready:items.length, total:selection.length}) : '—'}</small>
          </label>)}</div>
        </fieldset>
        {mixed && targets.some(host => allKinds.some(type => ownerOf(host,type)?.id !== host.id)) && <small>{t('部分入口共用同一资源目录，预览按实际目标合并。')}</small>}
        <div className="global-share-actions"><Button variant="primary" busy={busy} disabled={!total || oversized || matrix.loading || !!matrix.error} onClick={preview}>{t('预览批量更改')}</Button>
        {oversized && <small role="status">{t('每个助手一次最多分发 100 项资源，请缩小本次选择。')}</small>}
        {total > 0 && <small>{t('{count} 项更改，涉及 {hosts} 个助手', {count: total, hosts: picked.length})}</small>}</div>
      </aside></div>
    </>}
    {editor !== undefined && <GlobalAssetEditor kind={editor?.kind || formKind} item={editor} onClose={() => setEditor(undefined)} onSaved={() => { setEditor(undefined); clear(); changed(); }}/>}
    {importing && <ImportDialog category={formKind} agent={t('全局资源库')} onClose={() => setImporting(false)} onChanged={changed} isCurrent={() => mounted.current}/>}
    {nativeOpen && sourceHost && <NativeConfiguration host={sourceHost} initialKind={{mcp:'mcp',skill:'skills',prompt:'rules'}[formKind]} onClose={() => setNativeOpen(false)} onChanged={changed}/>}
    {batch && <SharedBatchReceipt batch={batch} hosts={hosts} onClose={() => setBatch(null)} onUpdate={setBatch} onChanged={changed} onRecovery={onHostPlans}/>}
  </section>;
}
