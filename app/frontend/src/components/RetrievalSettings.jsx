import { useState } from 'react';
import { Download, RefreshCw, Settings2 } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { Button, ErrorNotice, Field, Modal, ResourceState } from './ui.jsx';
import { EvidenceDetails, Facts, StatusBadge } from './ManagementUI.jsx';

export function RetrievalSettings({ revision = 0 }) {
  const resource = useResource('/api/retrieval/status', revision);
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [preview, setPreview] = useState(false);
  const [packPath, setPackPath] = useState('');
  const [verification, setVerification] = useState(null);
  const [download, setDownload] = useState(false);
  const [runtimeOpen, setRuntimeOpen] = useState(false);
  const [runtimeResult, setRuntimeResult] = useState(null);
  const runtimePlan = useResource(runtimeOpen ? '/api/retrieval/semantic/runtime/plan' : null);
  const [downloadResult, setDownloadResult] = useState(null);
  const [downloadId, setDownloadId] = useState(null);
  const [cancelling, setCancelling] = useState(false);
  const [cancelRequested, setCancelRequested] = useState(false);
  const [entityId, setEntityId] = useState('');
  const manifest = useResource(download ? '/api/retrieval/semantic/manifest' : null);
  const state = resource.data;
  async function save() {
    setBusy(true); setError(null);
    try { await mutate('/api/retrieval/settings', 'PATCH', draft); setDraft(null); setPreview(false); resource.reload(); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  async function prepareRuntime() {
    setBusy(true); setError(null);
    try { setRuntimeResult(await mutate('/api/retrieval/semantic/runtime/prepare', 'POST', { confirmed: true, plan_id: runtimePlan.data.plan_id })); resource.reload(); }
    catch (failure) { setError(failure); if (!failure.status || failure.status >= 500) setRuntimeResult({ status: 'unknown', automatic_retry: false }); }
    finally { setBusy(false); }
  }
  async function downloadPack() {
    const id = crypto.randomUUID();
    setDownloadId(id); setCancelRequested(false); setBusy(true); setError(null);
    try { setDownloadResult(await mutate('/api/retrieval/semantic/download', 'POST', { confirmed: true, download_id: id })); resource.reload(); }
    catch (failure) { setError(failure); } finally { setBusy(false); setDownloadId(null); }
  }
  async function cancelDownload() {
    setCancelling(true); setError(null);
    try {
      const receipt = await mutate('/api/retrieval/semantic/download/cancel', 'POST', { download_id: downloadId });
      setCancelRequested(receipt.cancellation_requested);
    } catch (failure) { setError(failure); } finally { setCancelling(false); }
  }
  async function indexEntity() {
    setBusy(true); setError(null);
    try { setVerification(await mutate(`/api/retrieval/semantic/index/${encodeURIComponent(entityId)}`, 'POST', {})); resource.reload(); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  async function verify() {
    setBusy(true); setError(null); setVerification(null);
    try { setVerification(await mutate('/api/retrieval/semantic/verify', 'POST', { pack_path: packPath })); resource.reload(); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <section className="management-panel"><header><div><h2>{t('增强检索与索引设置')}</h2><p>{t('默认使用本地词法检索。语义与关系能力分别启用，搜索不会自动下载模型或调用外部模型。')}</p></div><div className="toolbar-actions"><Button icon={RefreshCw} onClick={resource.reload} aria-label={t('刷新检索状态')} /><Button icon={Settings2} disabled={!state?.settings || resource.loading || !!resource.error} onClick={() => { setDraft({ ...state.settings }); setError(null); }}>{t('调整设置')}</Button></div></header>
    <ResourceState resource={resource}>{state ? <><div className="management-grid"><div className="management-card"><header><h3>{t('词法索引')}</h3><StatusBadge value={state.status} /></header><Facts values={[[t('FTS5'), state.fts5], [t('已索引'), state.indexed_count], [t('当前有效内容'), state.active_count]]} /></div><div className="management-card"><header><h3>{t('可选语义检索')}</h3><StatusBadge value={state.semantic?.status} /></header><Facts values={[[t('已启用'), state.semantic?.enabled], [t('模型'), state.semantic?.model_id], [t('运行依赖已安装'), state.semantic?.runtime_installed], [t('搜索时联网'), state.semantic?.network_on_search], [t('自动下载'), state.semantic?.auto_download]]} /></div><div className="management-card"><header><h3>{t('关系证据')}</h3><StatusBadge value={state.relations?.enabled ? 'enabled' : 'disabled'} /></header><Facts values={[[t('关系数量'), state.relations?.count], [t('上下文预算'), state.settings?.context_budget_tokens]]} /><p>{t('关联必须有来源；图中的连接不代表用户已确认的决定。')}</p></div></div><EvidenceDetails value={state} />
      <div className="toolbar-actions"><Button onClick={() => { setRuntimeOpen(true); setRuntimeResult(null); setError(null); }}>{t('准备隔离运行依赖')}</Button><Button icon={Download} onClick={() => { setDownload(true); setDownloadResult(null); setError(null); }}>{t('查看可选模型下载')}</Button></div><details className="evidence-details"><summary>{t('校验本地语义模型包')}</summary><div className="management-panel"><Field label={t('模型包绝对路径')} hint={t('只校验用户提供的模型包；不会因此自动下载或安装依赖。')}><input value={packPath} onChange={event => setPackPath(event.target.value)} placeholder="/absolute/path/to/model-pack" /></Field><Button busy={busy} disabled={!packPath.startsWith('/')} onClick={verify}>{t('校验本地包')}</Button><ErrorNotice error={error} />{verification ? <EvidenceDetails value={verification} /> : null}<Field label={t('建立语义索引的内容 ID')} hint={t('仅在本地模型已就绪后，为指定有效内容建立可重建索引。')}><input value={entityId} onChange={event => setEntityId(event.target.value)} /></Field><Button busy={busy} disabled={!entityId.trim()} onClick={indexEntity}>{t('索引此内容')}</Button></div></details>
    </> : null}</ResourceState>
    {runtimeOpen ? <Modal wide title={t('准备隔离运行依赖')} busy={busy} onClose={() => setRuntimeOpen(false)}><p className="modal-copy">{t('先检查固定依赖、下载域名、体积与目标目录。确认后只在 Senrelay 数据目录准备独立运行环境，不改变项目 Python；模型包另行下载。')}</p><ResourceState resource={runtimePlan}>{runtimePlan.data ? <EvidenceDetails value={runtimePlan.data} title={t('依赖准备计划')} open /> : null}</ResourceState>{runtimeResult ? <EvidenceDetails value={runtimeResult} title={t('依赖准备结果')} open /> : null}<ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setRuntimeOpen(false)}>{t('关闭')}</Button><Button variant="primary" busy={busy} disabled={!runtimePlan.data?.plan_id || !!runtimePlan.error || !!runtimeResult} onClick={prepareRuntime}>{t('确认准备独立运行环境')}</Button></footer></Modal> : null}
    {download ? <Modal wide title={t('可选语义模型下载')} busy={busy} headerActions={downloadId ? <Button busy={cancelling} disabled={cancelRequested} onClick={cancelDownload}>{t('取消下载')}</Button> : null} onClose={() => setDownload(false)}><p className="modal-copy">{t('下载前请检查来源、版本、体积和许可。此操作不自动安装运行依赖。')}</p><ResourceState resource={manifest}>{manifest.data ? <EvidenceDetails value={manifest.data} title={t('固定模型清单')} open /> : null}</ResourceState>{downloadResult ? <EvidenceDetails value={downloadResult} title={t('下载与校验结果')} open /> : null}<ErrorNotice error={error} />{cancelRequested && downloadId ? <p role="status">{t('已请求取消，正在等待当前网络读取结束；收到结果前不会视为已取消。')}</p> : null}{downloadResult?.status === 'cancelled' ? <p role="status">{t('下载已取消，未完成的文件已清理。')}</p> : null}<footer className="modal-actions"><Button disabled={busy} onClick={() => setDownload(false)}>{t('关闭')}</Button>{!downloadResult ? <Button variant="primary" busy={busy} disabled={!manifest.data || !!manifest.error} onClick={downloadPack}>{t('确认下载固定模型包')}</Button> : null}</footer></Modal> : null}
    {draft ? <Modal title={preview ? t('检查设置差异') : t('检索设置')} busy={busy} onClose={() => { setDraft(null); setPreview(false); }}><div className="management-form">{!preview ? <>{[['lexical_enabled', t('词法检索')], ['semantic_enabled', t('可选语义检索')], ['relation_enabled', t('关系证据')]].map(([key, label]) => <label key={key} className="inline-checkbox"><input type="checkbox" checked={Boolean(draft[key])} onChange={event => setDraft({ ...draft, [key]: event.target.checked })} />{label}</label>)}<Field label={t('最多召回条数')}><input type="number" min="1" max="100" value={draft.max_results} onChange={event => setDraft({ ...draft, max_results: Number(event.target.value) })} /></Field><Field label={t('上下文预算')} hint={t('这是上下文输入体积限制，不是费用上限。')}><input type="number" min="64" max="32000" value={draft.context_budget_tokens} onChange={event => setDraft({ ...draft, context_budget_tokens: Number(event.target.value) })} /></Field></> : <><p>{t('以下设置只影响检索召回与上下文装配预算。')}</p><EvidenceDetails value={{ before: state.settings, after: draft }} title={t('变更前后')} open /></>}<ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => preview ? setPreview(false) : setDraft(null)}>{preview ? t('返回编辑') : t('取消')}</Button><Button variant="primary" busy={busy} disabled={draft.max_results < 1 || draft.max_results > 100 || draft.context_budget_tokens < 64 || draft.context_budget_tokens > 32000} onClick={() => preview ? save() : setPreview(true)}>{preview ? t('确认应用设置') : t('预览差异')}</Button></footer></div></Modal> : null}
  </section>;
}
