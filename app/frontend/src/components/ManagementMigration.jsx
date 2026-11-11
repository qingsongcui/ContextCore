import { useState } from 'react';
import { t } from '../lib/i18n.jsx';
import { mutate, request } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { Button, ErrorNotice, Field, Modal } from './ui.jsx';
import { EvidenceDetails, Facts, StatusBadge } from './ManagementUI.jsx';

const formatLabel = fmt => fmt === 'cc-switch-sqlite' ? 'SQLite' : fmt === 'cc-switch-json' ? 'JSON' : fmt;

export function ManagementMigration({ onChanged }) {
  const [open, setOpen] = useState(false);
  const sources = useResource(open ? '/api/manage/migrations/sources' : null);
  const availableSources = (sources.data?.candidates || []).filter(item => item.status === 'available');
  const [sourcePath, setSourcePath] = useState('');
  const [roots, setRoots] = useState('');
  const [excludeSecrets, setExcludeSecrets] = useState(true);
  const [confirmed, setConfirmed] = useState(false);
  const [secretsConfirmed, setSecretsConfirmed] = useState(false);
  const [plan, setPlan] = useState(null);
  const [attempted, setAttempted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  async function preview() {
    setBusy(true); setError(null);
    try { setPlan(await mutate('/api/manage/migrations/plan', 'POST', { source_path: sourcePath.trim(), skill_roots: roots.split('\n').map(value => value.trim()).filter(Boolean), exclude_secrets: excludeSecrets })); setConfirmed(false); setSecretsConfirmed(false); setAttempted(false); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  async function readback() {
    setBusy(true); setError(null);
    try { setPlan(await request(`/api/manage/migrations/${encodeURIComponent(plan.id)}`)); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  async function apply() {
    setBusy(true); setError(null); setAttempted(true);
    try { await mutate(`/api/manage/migrations/${encodeURIComponent(plan.id)}/apply`, 'POST', { confirmed: true, ...(plan.requires_secret_confirmation ? { import_secrets_confirmed: secretsConfirmed } : {}) }); setPlan(await request(`/api/manage/migrations/${encodeURIComponent(plan.id)}`)); onChanged(); }
    catch (failure) { setError(failure); try { setPlan(await request(`/api/manage/migrations/${encodeURIComponent(plan.id)}`)); } catch { setPlan(previous => ({ ...previous, status: 'unknown' })); } }
    finally { setBusy(false); }
  }
  return <><Button onClick={() => { setOpen(true); setError(null); }}>{t('迁移本地配置')}</Button>{open ? <Modal wide title={t('迁移本地配置')} busy={busy} onClose={() => setOpen(false)}><div className="management-form"><p>{t('仅读取你指定的本地配置 JSON / SQLite 文件及可选技能目录，不扫描主目录。先按来源版本预检，无法映射的项目会列出原因。迁移只导入管理定义，宿主仍需单独配置计划。')}</p>{!plan ? <><Field label={t('选择迁移来源')} hint={t('这里只显示已知路径的文件元数据，尚未读取正文。点击预检后才读取所选来源。')}><select value={availableSources.some(item => item.source_path === sourcePath) ? sourcePath : ''} onChange={event => setSourcePath(event.target.value)}><option value="">{sources.loading ? t('加载中') : t('请选择来源')}</option>{availableSources.map(item => <option key={item.source_path} value={item.source_path}>{item.source_path} · {formatLabel(item.format)}</option>)}</select></Field><ErrorNotice error={sources.error} /><details className="evidence-details"><summary>{t('高级：手动指定来源路径')}</summary><Field label={t('配置文件绝对路径')}><input value={sourcePath} onChange={event => setSourcePath(event.target.value)} placeholder="/absolute/path/config.db" /></Field></details><Field label={t('可选技能目录（每行一个绝对路径）')}><textarea rows="4" value={roots} onChange={event => setRoots(event.target.value)} /></Field><label className="checkbox-label"><input type="checkbox" checked={excludeSecrets} onChange={event => setExcludeSecrets(event.target.checked)} />{t('排除来源密钥，仅导入无密钥定义')}</label></> : <><StatusBadge value={plan.status} /><Facts values={[[t('计划 ID'), plan.id], [t('源格式'), formatLabel(plan.source?.format)], [t('版本'), plan.source?.version], [t('可迁移'), plan.counts?.supported], [t('无法迁移'), plan.counts?.unsupported], [t('包含密钥'), plan.counts?.secrets]]} />{(plan.items || []).map((item, index) => <article className="management-card" key={`${item.source_id}:${index}`}><header><h3>{item.name || item.source_id}</h3><StatusBadge value={item.status} /></header><p>{item.reason}</p><EvidenceDetails value={item.redacted_diff} title={t('脱敏迁移差异')} open /></article>)}<EvidenceDetails value={plan} title={t('迁移预检与回执')} />{!attempted && plan.status === 'planned' ? <><label className="checkbox-label"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />{t('我已检查可迁移项、跳过原因与差异，确认导入管理定义。')}</label>{plan.requires_secret_confirmation ? <label className="checkbox-label"><input type="checkbox" checked={secretsConfirmed} onChange={event => setSecretsConfirmed(event.target.checked)} />{t('我另行确认将指定来源中的密钥导入本机凭据库；页面不会显示密钥内容。')}</label> : null}</> : null}{attempted ? <p>{t('迁移已提交，结果以回执读回为准；未知结果不自动重试。')}</p> : null}</>}<ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setOpen(false)}>{t('关闭')}</Button>{plan ? <><Button busy={busy} onClick={readback}>{t('读取迁移回执')}</Button>{!attempted ? <Button onClick={() => { setPlan(null); setError(null); }}>{t('重新预检')}</Button> : null}<Button variant="primary" busy={busy} disabled={attempted || plan.status !== 'planned' || !confirmed || (plan.requires_secret_confirmation && !secretsConfirmed)} onClick={apply}>{t('确认迁移定义')}</Button></> : <Button variant="primary" busy={busy} disabled={!sourcePath.trim().startsWith('/')} onClick={preview}>{t('读取指定来源并预检')}</Button>}</footer></div></Modal> : null}</>;
}
