import { useEffect, useRef, useState } from 'react';
import { mutate } from '../../lib/api.js';
import { t } from '../../lib/i18n.jsx';
import { useResource } from '../../lib/useResource.js';
import { list, mcpNeedsReview } from '../../lib/management.js';
import { Button, EmptyState, ErrorNotice, Field, ResourceState } from '../ui.jsx';
import { AssetTools } from '../AssetTools.jsx';
import { SharedBatchReceipt } from './SharedBatchReceipt.jsx';
import { ResourcePagination } from './ResourcePagination.jsx';
import { configuredLabel } from './ConfigurationState.jsx';
import { agentGroup, agentGroupName, configurationChoices, configurationTarget } from '../../lib/agentGroups.js';
import '../../styles/shared-resources.css';

export { ConfigurationBackup } from './ConfigurationBackup.jsx';

export function ProfileScope({ hosts = [] }) {
  if (hosts.some(host => (host.id === 'codex' || host.id === 'codex-desktop') && (host.shared_configuration === false || host.config_root))) return <p className="muted">{t('方案按助手应用到各自的用户级配置文件。')}</p>;
  return <p className="muted">{t('方案按助手应用到用户级配置文件。Codex CLI 和桌面版共用原生配置。')}</p>;
}

const capability = { mcp: 'mcp', skill: 'skills', prompt: 'prompts' };
const cellKey = (host, asset) => host + '/' + asset;

export function SharedResourceMatrix({ hostId, allHosts, revision, onChanged, onRecovery }) {
  const [kind, setKind] = useState('mcp');
  const [search, setSearch] = useState('');
  const [offset, setOffset] = useState(0);
  const [firstHost, setFirstHost] = useState('');
  const [selection, setSelection] = useState([]);
  const [operation, setOperation] = useState('install');
  const [batch, setBatch] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const mounted = useRef(false);
  const lock = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const currentTarget = configurationTarget(allHosts, hostId, kind);
  const targets = configurationChoices(allHosts, kind);
  const sorted = targets
    .map(host => targets.filter(item => agentGroup(item.id) === agentGroup(host.id)).length === 1
      ? { ...host, name: agentGroupName(host) } : host)
    .sort((a, b) => Number(b.id === currentTarget) - Number(a.id === currentTarget));
  const start = Math.max(0, sorted.findIndex(host => host.id === firstHost));
  const visible = sorted.slice(start, start + 3);
  const hostIds = visible.map(host => host.id).join(',');
  const matrix = useResource(hostIds ? '/api/manage/shared-resource-matrix?' + new URLSearchParams({
    host_ids: hostIds, kind, q: search, offset: String(offset)
  }) : null, revision);

  function selectCell(host, asset, checked) {
    const key = cellKey(host.id, asset.id);
    setSelection(prev => checked ? [...prev.filter(entry => entry.key !== key), { key, host: host.id, asset: asset.id, version: asset.version }] : prev.filter(entry => entry.key !== key));
  }

  async function preview() {
    if (lock.current || !selection.length) return;
    lock.current = true; setBusy(true); setError(null);
    const byHost = new Map();
    for (const { host, asset, version } of selection) {
      if (!byHost.has(host)) byHost.set(host, {});
      byHost.get(host)[asset] = version;
    }
    try {
      const receipt = await mutate('/api/manage/shared-batches', 'POST', {
        operation, targets: Array.from(byHost, ([host_id, asset_versions]) => ({ host_id, asset_versions })),
      });
      if (mounted.current) {
        setBatch(receipt);
        setSelection([]);
        onChanged?.();
      }
    } catch (failure) {
      if (mounted.current) setError(failure);
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  }
  return <section className="management-panel resource-matrix">
    <h2>{t('共享资源')}</h2>
    <div className="toolbar-actions">{[['mcp', 'MCP'], ['skill', 'Skills'], ['prompt', t('使用规则')]].map(([id, label]) =>
      <Button key={id} aria-pressed={kind === id} onClick={() => { setKind(id); setOffset(0); }}>{label}</Button>)}</div>
    <div className="matrix-controls">
      <Field label={t('搜索共享资料')}><input type="search" maxLength={200} value={search} onChange={e => { setSearch(e.target.value); setOffset(0); }} /></Field>
      <Field label={t('查看助手列，从此处开始')}><select value={visible[0]?.id || ''} onChange={e => setFirstHost(e.target.value)}>{sorted.map(host => <option key={host.id} value={host.id}>{host.name}</option>)}</select></Field>
      <Field label={t('选中项的操作')}><select disabled={busy} value={operation} onChange={e => { setOperation(e.target.value); setSelection([]); }}><option value="install">{t('安装或更新')}</option><option value="uninstall">{t('移除受管资源')}</option></select></Field>
    </div>
    <ResourceState resource={matrix}>
      {list(matrix.data).length ? <table className="resource-grid"><thead><tr><th scope="col">{t('共享资源')}</th>{visible.map(host => <th key={host.id} scope="col">{host.name}</th>)}</tr></thead>
        <tbody>{list(matrix.data).map(item => <tr key={item.id}><th scope="row"><details><summary>{item.name}</summary>
          <small>{t('版本')} {item.version}{item.source?.type ? ' · ' + item.source.type : ''}</small>
          {item.source?.url || item.source?.path ? <small>{item.source.url || item.source.path}</small> : null}
          {item.kind === 'skill' ? <small>{item.install_mode === 'symlink' ? t('共享版本链接') : t('独立副本（推荐）')}</small> : null}
          <AssetTools item={item} selectedAgent={t('共享资源')} isCurrent={() => mounted.current} onChanged={() => { matrix.reload(); onChanged?.(); }} />
        </details></th>
          {visible.map(host => {
            const status = item.cells?.[host.id]?.status || 'not_checked';
            const supported = Boolean(host.capabilities?.[capability[kind]]);
            const reviewRequired = mcpNeedsReview(item);
            const rootBlocked = host.config_root?.status === 'blocked';
            const label = rootBlocked ? t('配置目录需要核对') : !supported ? t('不支持') : reviewRequired && operation === 'install' ? t('请先核对工具定义') : configuredLabel(status);
            const disabled = rootBlocked || !supported || (reviewRequired && operation === 'install') || ['externally_changed', 'unreadable', 'not_checked', 'native_read_only', 'native_source_changed', 'native_source_missing'].includes(status) || (operation === 'uninstall' && ['not_managed', 'removed', 'native_available'].includes(status));
            return <td key={host.id}><span className="matrix-mobile-host">{host.name}</span><label>
              <input type="checkbox" disabled={busy || disabled} aria-label={t('{resource} · {agent} · {status}', { resource: item.name, agent: host.name, status: label })}
                checked={selection.some(entry => entry.key === cellKey(host.id, item.id))} onChange={e => selectCell(host, item, e.target.checked)} />
              <span>{label}{item.cells?.[host.id]?.applied_version != null ? <small>{t('文件版本 {version}', { version: item.cells[host.id].applied_version })}</small> : null}</span>
            </label></td>;
          })}</tr>)}</tbody></table> : <EmptyState title={t('没有匹配的资料')} />}
    </ResourceState>
    <ResourcePagination label={t('共享资料分页')} resource={matrix} onPage={setOffset} />
    <ErrorNotice error={error} />
    {error?.status === 0 && <p role="status">{t('可在共享库的分发历史中检查预览是否已保存。')}</p>}
    <div className="toolbar-actions"><Button variant="primary" busy={busy} disabled={!selection.length} onClick={preview}>{t('预览所选更改（{count}）', { count: selection.length })}</Button>
      {selection.length ? <Button disabled={busy} onClick={() => setSelection([])}>{t('清空选择')}</Button> : null}</div>
    {batch && <SharedBatchReceipt batch={batch} hosts={sorted} onUpdate={setBatch} onClose={() => setBatch(null)}
      onChanged={() => { matrix.reload(); onChanged?.(); }} onRecovery={onRecovery} />}
  </section>;
}
