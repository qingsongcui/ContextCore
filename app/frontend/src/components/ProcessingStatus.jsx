import { t } from '../lib/i18n.jsx';
import { useResource } from '../lib/useResource.js';
import { ResourceState } from './ui.jsx';
import { DataTable, EvidenceDetails, StatusBadge } from './ManagementUI.jsx';
import { list } from '../lib/management.js';

export function ProcessingStatus({ revision }) {
  const jobs = useResource('/api/processing/jobs?limit=25', revision);
  const cleanup = useResource('/api/processing/cleanup', revision);
  return <section className="management-panel"><header><div><h2>{t('后台处理与删除传播')}</h2><p>{t('结果未知的写请求先对账；清理未完成时不显示永久删除完成。')}</p></div></header><ResourceState resource={jobs}><DataTable rows={list(jobs.data)} columns={[{ key: 'id', label: t('作业 ID') }, { key: 'status', label: t('状态'), render: row => <StatusBadge value={row.status} /> }, { key: 'stage', label: t('阶段') }, { key: 'attempt', label: t('尝试次数') }, { key: 'updated_at', label: t('更新时间') }]} />{jobs.data ? <EvidenceDetails value={jobs.data} title={t('处理覆盖与回执')} /> : null}</ResourceState><ResourceState resource={cleanup}>{cleanup.data ? <EvidenceDetails value={cleanup.data} title={t('删除清理逐项状态')} /> : null}</ResourceState></section>;
}
