import { useState } from 'react';
import { Copy, KeyRound, Settings2 } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { list } from '../lib/management.js';
import { Button, ErrorNotice, Field, Modal, ResourceState } from './ui.jsx';
import { EvidenceDetails, Facts, StatusBadge } from './ManagementUI.jsx';
import { CatalogChoice } from './agent-management/CatalogControls.jsx';

export function RoutingSettings({ revision, providers, catalogContext }) {
  const resource = useResource('/api/routing/status', revision);
  const [draft, setDraft] = useState(null);
  const [preview, setPreview] = useState(false);
  const [advanced, setAdvanced] = useState('{}');
  const [tokenDialog, setTokenDialog] = useState(false);
  const [credential, setCredential] = useState('');
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const state = resource.data;
  async function save() {
    setBusy(true); setError(null);
    try { await mutate('/api/routing/settings', 'PATCH', draft); setDraft(null); setPreview(false); resource.reload(); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  async function createCredential() {
    setBusy(true); setError(null);
    try { const result = await mutate('/api/routing/token', 'POST', { confirmed: true }); setCredential(result.token || ''); resource.reload(); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <section className="management-panel"><header><div><h2>{t('本地路由')}</h2><p>{t('路由使用独立客户端凭据。启用路由不改宿主地址，也不授权后台整理。')}</p></div><Button icon={Settings2} disabled={!state || !!resource.error || resource.loading} onClick={() => { setDraft({ enabled: state.enabled, provider_id: state.provider_id, request_timeout_seconds: state.request_timeout_seconds }); setAdvanced(JSON.stringify({ fallback_provider_ids: state.fallback_provider_ids || [], model_routes: state.model_routes || {}, routing_strategy: state.routing_strategy || 'ordered', circuit_threshold: state.circuit_threshold ?? 3, circuit_cooldown_seconds: state.circuit_cooldown_seconds ?? 60 }, null, 2)); setError(null); }}>{t('配置路由')}</Button></header><ResourceState resource={resource}>{state ? <><StatusBadge value={state.state} /><Facts values={[[t('供应商'), state.provider_id], [t('独立客户端凭据'), state.token_configured], [t('在途请求'), state.active_requests], [t('自动重试'), state.automatic_retry], [t('协议转换'), state.protocol_conversion], [t('宿主配置已应用'), state.host_configuration_applied]]} /><Button icon={KeyRound} onClick={() => { setTokenDialog(true); setError(null); setCredential(''); setCopied(false); }}>{t('创建或轮换路由凭据')}</Button><EvidenceDetails value={state} title={t('路由能力与生效范围')} /></> : null}</ResourceState>
    {draft ? <Modal title={preview ? t('检查设置差异') : t('配置路由')} busy={busy} onClose={() => { setDraft(null); setPreview(false); }}><div className="management-form">{preview ? <EvidenceDetails value={{ before: { enabled: state.enabled, provider_id: state.provider_id, request_timeout_seconds: state.request_timeout_seconds }, after: draft }} title={t('变更前后')} open /> : <><label className="inline-checkbox"><input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />{t('启用路由')}</label>{catalogContext ? <CatalogChoice collection="providers" label={t('供应商')} value={draft.provider_id} scope="all" revision={revision} onChange={provider_id => setDraft({ ...draft, provider_id: provider_id || null })} /> : <Field label={t('供应商')}><select value={draft.provider_id || ''} onChange={event => setDraft({ ...draft, provider_id: event.target.value || null })}><option value="">{t('未绑定')}</option>{list(providers).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>}<Field label={t('请求超时（秒）')}><input type="number" min="1" max="600" value={draft.request_timeout_seconds} onChange={event => setDraft({ ...draft, request_timeout_seconds: Number(event.target.value) })} /></Field><Field label={t('回退与熔断策略 JSON')} hint={t('明确指定允许的供应商。只有可安全重试的请求才会回退，结果未知或已输出内容时不会重放。')}><textarea rows="10" value={advanced} onChange={event => setAdvanced(event.target.value)} spellCheck={false} /></Field></>}<ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => preview ? setPreview(false) : setDraft(null)}>{preview ? t('返回编辑') : t('取消')}</Button><Button variant="primary" busy={busy} disabled={draft.request_timeout_seconds < 1 || draft.request_timeout_seconds > 600 || (draft.enabled && !draft.provider_id)} onClick={() => { if (preview) save(); else { try { const value = JSON.parse(advanced); const allowed = ['fallback_provider_ids', 'model_routes', 'routing_strategy', 'circuit_threshold', 'circuit_cooldown_seconds']; if (!value || typeof value !== 'object' || Array.isArray(value) || Object.keys(value).some(key => !allowed.includes(key))) throw new Error(t('路由策略包含不支持的字段。')); setDraft({ ...draft, ...value }); setPreview(true); setError(null); } catch (failure) { setError(failure); } } }}>{preview ? t('确认应用设置') : t('预览差异')}</Button></footer></div></Modal> : null}
    {tokenDialog ? <Modal title={t('独立路由凭据')} busy={busy} onClose={() => { setTokenDialog(false); setCredential(''); }}><p className="modal-copy">{t('轮换后旧凭据立即失效。新凭据仅在此页面内存中短暂保留，关闭后无法再次读取。')}</p>{credential ? <><Field label={t('新凭据（隐藏）')}><input type="password" readOnly value={credential} autoComplete="off" /></Field><Button icon={Copy} onClick={async () => { try { await navigator.clipboard.writeText(credential); setCopied(true); } catch (failure) { setError(failure); } }}>{copied ? t('已复制') : t('复制到剪贴板')}</Button></> : null}<ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => { setTokenDialog(false); setCredential(''); }}>{t('关闭')}</Button>{!credential ? <Button variant="primary" busy={busy} onClick={createCredential}>{t('确认轮换凭据')}</Button> : null}</footer></Modal> : null}
  </section>;
}
