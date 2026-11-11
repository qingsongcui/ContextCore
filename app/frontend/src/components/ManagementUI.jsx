import { useState } from 'react';
import { ChevronDown, ChevronRight, Check, FileDiff } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { display, safeEvidence, configurationDiffEntries } from '../lib/management.js';
import { Badge, Button, ErrorNotice, Modal } from './ui.jsx';

export function statusLabel(value) {
  const labels = {
    not_fetched: t('尚未获取'), fresh: t('缓存有效'), reachable: t('网络可达'), unreachable: t('网络不可达'), authenticated: t('认证已验证'), rejected: t('认证被拒绝'), supported: t('可迁移格式'), unknown: t('未知'), not_verified: t('尚未验证'), supported_format: t('格式已支持'), insufficient_budget: t('预算不足'), confirmed: t('已确认'), unconfirmed: t('尚未确认'), running: t('运行中'), verified: t('已验证'), implemented: t('已实现，待验证'), configured: t('已配置'), available: t('可用'),
    unsupported: t('不支持'), blocked: t('受阻'), disabled: t('未启用'), enabled: t('已启用'), not_connected: t('未接入'), paused: t('已暂停'), error: t('异常'),
    authorized: t('已授权'), authorizing: t('等待官方授权'), expired: t('授权已过期'), refreshing: t('正在刷新授权'), logged_out: t('已退出授权'), unconfigured: t('尚未配置'),
    planned: t('待应用'), applied: t('已应用'), failed: t('失败'), pending: t('等待处理'), recovery_required: t('需要恢复'),
    not_run: t('未运行'), partial: t('部分覆盖'), ready: t('已就绪'), stale: t('已过期'), cancelled: t('已取消'), succeeded: t('已完成')
  };
  return labels[value] || value || t('未知');
}
export function StatusBadge({ value }) {
  return <Badge tone={['verified', 'ready', 'succeeded', 'supported_format', 'configured', 'enabled', 'authorized', 'applied', 'fresh', 'reachable', 'authenticated'].includes(value) ? 'success' : ['failed', 'error', 'recovery_required', 'rejected', 'unreachable'].includes(value) ? 'danger' : ['blocked', 'expired', 'stale', 'partial', 'paused'].includes(value) ? 'warning' : ''}>{statusLabel(value)}</Badge>;
}
export function EvidenceDetails({ value, title, open = false }) {
  return <details className="evidence-details" open={open}><summary><FileDiff size={15} />{title || t('查看证据与状态')}</summary><pre>{JSON.stringify(safeEvidence(value), null, 2)}</pre></details>;
}
export function Facts({ values }) {
  return <dl className="management-facts">{values.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{display(value)}</dd></div>)}</dl>;
}
export function DataTable({ columns, rows, keyName = 'id', empty, onRow }) {
  if (!rows.length) return <p className="table-empty">{empty || t('还没有记录')}</p>;
  return <div className="data-table-wrap" tabIndex={0} role="region" aria-label={t('数据表格，可左右滚动')}><table className="data-table" style={columns.length >= 3 ? { minWidth: columns.length * 160 } : undefined}><thead><tr>{columns.map(column => <th key={column.key} scope="col">{column.label}</th>)}</tr></thead><tbody>{rows.map((row, index) => <tr key={row[keyName] || index}>{columns.map((column, col) => <td key={column.key}>{col === 0 && onRow ? <button className="table-row-link" onClick={() => onRow(row)}>{column.render ? column.render(row) : display(row[column.key])}<ChevronRight size={14} /></button> : column.render ? column.render(row) : display(row[column.key])}</td>)}</tr>)}</tbody></table></div>;
}
export function ConfigurationDialog({ plan, onClose, onApply, busy, error, result }) {
  const [confirmed, setConfirmed] = useState(false);
  const files = result?.payload?.files || result?.files || plan.files || [];
  const blocked = Boolean(plan.blocked || plan.status === 'blocked' || plan.status === 'conflict');
  return <Modal wide title={result ? t('配置应用结果') : t('检查配置差异')} onClose={onClose} busy={busy}>
    <p className="modal-copy">{t('只应用本次计划中的目标；外部修改会使计划失效。配置写入与宿主实际加载分别验证。')}</p>
    <StatusBadge value={result?.status || plan.status} />
    {result ? <Facts values={[[t('文件读回'), statusLabel(result.payload?.readback)], [t('宿主重载'), result.payload?.host_reload === 'not_run' ? t('尚未验证') : statusLabel(result.payload?.host_reload)], [t('真实请求'), result.payload?.real_request === 'not_run' ? t('尚未验证') : statusLabel(result.payload?.real_request)]]} /> : null}
    {plan.summary ? <p className="modal-copy">{plan.summary}</p> : null}
    <EvidenceDetails value={result || plan} title={t('计划、差异与逐项回读')} />
    {plan.diff ? <pre className="config-diff">{typeof plan.diff === 'string' ? safeEvidence(plan.diff) : JSON.stringify(safeEvidence(plan.diff), null, 2)}</pre> : null}
    {Array.isArray(files) ? files.map((file, i) => <details className="evidence-details" key={file.path || i} open><summary><ChevronDown size={14} />{file.path || file.target_path}<StatusBadge value={file.stage || file.status} /></summary><div className="file-change-summary">{configurationDiffEntries(file.diff).map((entry, index) => <div key={index}><Facts values={[[t('管理条目'), entry.entry], [t('变更操作'), entry.operation === 'install' ? t('安装或更新') : entry.operation === 'uninstall' ? t('移除自有条目') : entry.operation]]} /><pre>{typeof entry.value === 'string' ? safeEvidence(entry.value) : JSON.stringify(safeEvidence(entry.value), null, 2)}</pre></div>)}</div><details className="raw-config-diff"><summary>{t('查看原始差异')}</summary><pre>{typeof file.diff === 'string' ? safeEvidence(file.diff) : JSON.stringify(safeEvidence(file), null, 2)}</pre></details></details>) : null}
    <ErrorNotice error={error} />
    {!result && !blocked ? <label className="checkbox-label management-consent"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />{t('我已检查目标、差异和恢复方式，确认应用。')}</label> : null}
    <footer className="modal-actions"><Button onClick={onClose}>{result ? t('关闭') : t('取消')}</Button>{!result ? <Button variant="primary" icon={Check} busy={busy} disabled={!confirmed || blocked} onClick={onApply}>{t('应用此计划')}</Button> : null}</footer>
  </Modal>;
}
