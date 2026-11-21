import { useEffect, useRef, useState } from 'react';
import { mutate, request } from '../../lib/api.js';
import { locale, t } from '../../lib/i18n.jsx';
import { list, safeEvidence } from '../../lib/management.js';
import { useResource } from '../../lib/useResource.js';
import { Badge, Button, ErrorNotice, Modal, ResourceState } from '../ui.jsx';
import { Facts, statusLabel } from '../ManagementUI.jsx';
import { ResourcePagination } from './ResourcePagination.jsx';

const base = '/api/manage/shared-batches';
const errorHints = {
  stale_asset: '资料版本已更新。请核对最新版后重新预览。',
  shared_target: '此目标与另一助手共用配置位置，没有单独的写入计划。请核对共用范围。',
  unsupported: '这个助手不支持所选操作。',
  plan_expired: '原预览已过期。请重新预览。',
  preview_rejected: '无法生成预览。请检查资料和助手配置。',
  apply_conflict: '文件或配置已变更。请重新预览最新差异。',
  recovery_required: '需要先在这个助手的更改记录中处理恢复。',
  credential_unavailable: '所需凭据暂不可用。请检查绑定的账号。',
  storage_error: '本机存储暂不可用。请检查磁盘空间与文件访问权限。',
};
const stateLabels = { planned: '待应用', partial: '各目标进度不同', applied: '已应用', failed: '失败', recovery_required: '需要恢复' };
const operationLabel = operation => operation === 'uninstall' ? t('移除受管资源') : t('安装或更新');
function BatchStatus({ value }) {
  const tone = { planned: 'neutral', applied: 'success', partial: 'warning', recovery_required: 'danger', failed: 'danger' }[value];
  return <Badge tone={tone}>{t(stateLabels[value] || '未知')}</Badge>;
}
function dateLabel(raw) {
  const date = new Date(raw);
  return raw && Number.isFinite(date.getTime()) ? date.toLocaleString(locale()) : t('未知');
}

export function SharedBatchHistory({ revision, onOpen }) {
  const [offset, setOffset] = useState(0);
  const pageSize = 10;
  const resource = useResource(base + '?offset=' + offset + '&limit=' + pageSize, revision);
  const [opening, setOpening] = useState('');
  const [error, setError] = useState(null);
  const active = useRef(null);
  useEffect(() => () => { active.current?.abort(); }, []);
  async function openBatch(id) {
    if (active.current) return;
    const controller = new AbortController();
    active.current = controller;
    setOpening(id);
    setError(null);
    try {
      const detail = await request(base + '/' + encodeURIComponent(id), { signal: controller.signal });
      if (!controller.signal.aborted) onOpen(detail);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure);
    } finally {
      if (!controller.signal.aborted) setOpening('');
      active.current = null;
    }
  }
  const items = list(resource.data);
  const pagination = { ...resource, data: { ...resource.data, items, summary: true, offset,
    limit: pageSize, has_more: offset + items.length < (resource.data?.total || 0) } };
  return <section className="management-section">
    <h2>{t('分发历史')}</h2>
    <ResourceState resource={resource}>
      {!items.length ? <p className="table-empty">{t('还没有分发记录。')}</p> : <div className="data-table-wrap">
        <table className="data-table"><thead><tr>
          <th scope="col">{t('创建时间')}</th><th scope="col">{t('操作')}</th>
          <th scope="col">{t('完成目标')}</th><th scope="col">{t('状态')}</th>
        </tr></thead><tbody>{items.map(item => <tr key={item.id}><td>
          <Button busy={opening === item.id} disabled={Boolean(opening)} onClick={() => openBatch(item.id)}>{dateLabel(item.created_at)}</Button>
        </td><td>{operationLabel(item.operation)}</td><td>{item.counts?.applied || 0} / {Object.values(item.counts || {}).reduce((sum, n) => sum + n, 0)}</td>
          <td><BatchStatus value={item.status} /></td></tr>)}</tbody></table></div>}
      <ResourcePagination resource={pagination} busy={Boolean(opening)} onPage={setOffset} label={t('分发记录分页')} />
    </ResourceState><ErrorNotice error={error} />
  </section>;
}

export function SharedBatchReceipt(props) {
  if (!props.batch) return null;
  return <BatchReview key={props.batch.id} {...props} />;
}

function BatchReview({ batch, onUpdate, onClose, onChanged, hosts, onRecovery }) {
  const [confirmedVersion, setConfirmedVersion] = useState(null);
  const [omitted, setOmitted] = useState({ version: null, hosts: [] });
  const [busy, setBusy] = useState(false);
  const [unverified, setUnverified] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState(null);
  const lock = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  const names = new Map(list(hosts).map(host => [host.id, host.name]));
  const url = base + '/' + encodeURIComponent(batch.id);
  const targets = batch.targets || [];
  const excluded = omitted.version === batch.version ? omitted.hosts : [];
  const selected = targets.filter(target => !excluded.includes(target.host_id));
  const ready = selected.filter(target => target.status === 'planned' && target.can_apply).map(target => target.host_id);
  const retryable = selected.filter(target => target.status === 'failed' && target.can_repreview).map(target => target.host_id);
  function selectHost(host, checked) {
    setConfirmedVersion(null);
    setOmitted({ version: batch.version, hosts: checked ? excluded.filter(id => id !== host) : [...excluded, host] });
  }
  const confirmed = batch.version != null && confirmedVersion === batch.version;

  async function run(action, hostIds) {
    if (lock.current || (action === 'apply' && (!confirmed || !hostIds.length)) || (unverified && action !== 'refresh')) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    setNotice('');
    setConfirmedVersion(null);
    try {
      let next;
      if (action === 'refresh') {
        next = await request(url, { cache: 'no-store' });
      } else {
        const body = { expected_version: batch.version, host_ids: hostIds };
        if (action === 'apply') body.confirmed = true;
        next = await mutate(url + '/' + action, 'POST', body);
      }
      if (mounted.current) {
        onUpdate(next);
        setUnverified(false);
        onChanged?.();
      }
    } catch (failure) {
      if (!mounted.current) return;
      setError(failure);
      setUnverified(true);
      if (action !== 'refresh') {
        try {
          const latest = await request(url, { cache: 'no-store' });
          if (mounted.current) {
            onUpdate(latest);
            setUnverified(false);
            setNotice('已重新读取回执，请按最新状态核对各目标。');
            onChanged?.();
          }
        } catch {
          // Keep writes disabled until a fresh readback resolves the outcome.
        }
      }
    } finally {
      lock.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  return <Modal wide title={t('分发回执')} onClose={onClose} busy={busy}>
    <div className="studio-section-heading"><h3>{operationLabel(batch.operation)}</h3><BatchStatus value={batch.status} /></div>
    <p>{t('配置以文件写入结果为准，助手加载状态需另行核验。')}</p>
    {targets.map(target => <section className="matrix-receipt" key={target.host_id}>
      <h3>{names.get(target.host_id) || target.host_id}</h3><BatchStatus value={target.status} />
      {(target.can_apply || target.can_repreview) && target.status !== 'applied' && <label className="checkbox-row">
        <input type="checkbox" disabled={busy || unverified} checked={!excluded.includes(target.host_id)}
          onChange={e => selectHost(target.host_id, e.target.checked)} />
        {t('选择 {host}', { host: names.get(target.host_id) || target.host_id })}</label>}
      {target.status === 'applied' && <Facts values={[
        [t('文件读回'), statusLabel(target.file_readback)], [t('宿主重载'), statusLabel(target.host_reload)],
      ]} />}
      <ul>{Object.entries(target.asset_versions || {}).map(([id, version]) => <li key={id}>{target.asset_names?.[id] || id} · v{version}</li>)}</ul>
      {target.error_code && <p>{t(errorHints[target.error_code] || '请检查这个助手的更改记录。')}</p>}
      {target.files?.length > 0 && <details><summary>{t('查看文件差异')}</summary>
        {target.files.map(file => <div key={file.path}><code>{safeEvidence(file.path)}</code><pre>{safeEvidence(file.diff)}</pre></div>)}</details>}
      {target.plan_id && onRecovery && <Button disabled={busy} onClick={() => onRecovery(target.host_id, target.plan_id)}>{t('查看助手更改记录')}</Button>}
    </section>)}
    <ErrorNotice error={error} />
    {notice && <p role="status">{t(notice)}</p>}
    {unverified && <p role="alert">{t('暂时无法确认本次结果，请重新读取回执后再操作。')}</p>}
    <div className="toolbar-actions"><Button disabled={busy} onClick={() => run('refresh')}>{t('刷新回执')}</Button>
      {retryable.length > 0 && <Button disabled={busy || unverified} onClick={() => run('preview', retryable)}>{t('重新预览失败目标（{count}）', { count: retryable.length })}</Button>}
    </div>
    {ready.length > 0 && <div className="matrix-confirm"><label><input type="checkbox" checked={confirmed} disabled={busy || unverified}
      onChange={e => setConfirmedVersion(e.target.checked ? batch.version : null)} />{t('我已核对待应用目标的文件差异')}</label>
      <Button variant="primary" disabled={!confirmed || busy || unverified} onClick={() => run('apply', ready)}>{t('应用已预览的目标（{count}）', { count: ready.length })}</Button></div>}
  </Modal>;
}
