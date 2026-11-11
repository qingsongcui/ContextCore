import { useState } from 'react';
import { t } from '../lib/i18n.jsx';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { list } from '../lib/management.js';
import { Button, ErrorNotice, Field, Modal, ResourceState } from './ui.jsx';
import { DataTable, EvidenceDetails, StatusBadge } from './ManagementUI.jsx';

export function ProviderChecks({ provider, contextTitle }) {
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState('network');
  const [model, setModel] = useState('');
  const [paid, setPaid] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [attempted, setAttempted] = useState(false);
  const [quickLatency, setQuickLatency] = useState(null);
  const base = `/api/manage/providers/${encodeURIComponent(provider.id)}`;
  const supported = ['openai', 'anthropic'].includes(provider.protocol);
  const catalog = useResource(open && supported ? `${base}/models/catalog` : null);
  async function quickSpeedtest() {
    if (busy || !supported) return;
    setBusy(true); setError(null);
    try {
      const output = await mutate(`${base}/check`, 'POST', { mode: 'network' });
      setResult(output);
      const ms = output?.network_elapsed_ms ?? output?.latency_ms ?? output?.duration_ms;
      setQuickLatency(output?.status === 'reachable'
        ? (typeof ms === 'number' && Number.isFinite(ms) && ms >= 0 ? `${ms} ms · ${t('可达')}` : t('可达'))
        : output?.status === 'unreachable' ? t('测速失败') : t('未知'));
    } catch (failure) {
      setError(failure);
      setQuickLatency(t('测速失败'));
    } finally {
      setBusy(false);
    }
  }
  async function check() {
    if (busy || attempted || !supported) return;
    setBusy(true); setError(null);
    try {
      const output = await mutate(`${base}/check`, 'POST', { mode, ...(mode === 'inference' ? { model: model.trim(), confirmed_paid: paid } : {}) });
      setAttempted(true);
      setResult(output);
    } catch (failure) {
      setError(failure);
      if (failure?.status !== 0) {
        setAttempted(true);
        setResult({ status: 'unknown' });
      }
    } finally {
      setBusy(false);
    }
  }
  return <><Button disabled={busy || !supported} onClick={quickSpeedtest}>{t('一键测速')}</Button>{quickLatency ? <span className="cc-latency-pill" role="status">{quickLatency}</span> : null}<Button onClick={() => { setOpen(true); setResult(null); setError(null); setAttempted(false); setPaid(false); }}>{t('目录与独立检查')}</Button>{open ? <Modal wide title={`${contextTitle ? `${contextTitle} · ` : ''}${t('目录与独立检查')}`} busy={busy} onClose={() => setOpen(false)}><div className="management-form">
    <h3>{provider.name}</h3><p>{t('检查针对这个共享服务定义，不会应用或切换任何助手配置。')}</p>
    <p>{t('打开页面只读本机目录缓存。刷新目录会访问供应商；网络可达、认证、模型推理各自独立，不能互相替代。')}</p>
    {!supported ? <p>{t('这个协议暂不支持读取模型目录，请填写服务商文档中的准确模型名。不会自动发起检查或推理。')}</p> : null}
    <ResourceState resource={catalog}><StatusBadge value={catalog.data?.status} /><DataTable rows={list(catalog.data)} columns={[{ key: 'id', label: t('模型 ID') }, { key: 'name', label: t('名称') }, { key: 'context_window', label: t('上下文窗口') }]} /><EvidenceDetails value={catalog.data} title={t('模型目录覆盖证据')} /></ResourceState>
    <Button disabled={busy || !supported} onClick={async () => { if (!supported) return; setBusy(true); setError(null); try { await mutate(`${base}/models/catalog`, 'POST', {}); catalog.reload(); } catch (failure) { setError(failure); } finally { setBusy(false); } }}>{t('明确刷新远程目录')}</Button>
    {supported && catalog.data?.has_more && catalog.data?.next_after_id ? <Button disabled={busy} onClick={async () => { setBusy(true); setError(null); try { await mutate(`${base}/models/catalog`, 'POST', { after_id: catalog.data.next_after_id }); catalog.reload(); } catch (failure) { setError(failure); } finally { setBusy(false); } }}>{t('读取下一页远程目录')}</Button> : null}
    <Field label={t('检查范围')}><select value={mode} disabled={busy} onChange={event => { setMode(event.target.value); setResult(null); setAttempted(false); setPaid(false); setError(null); }}><option value="network">{t('仅网络可达')}</option><option value="auth">{t('认证状态')}</option><option value="inference">{t('付费模型推理')}</option></select></Field>
    {mode === 'inference' ? <><Field label={t('请求模型')}><input value={model} disabled={attempted} onChange={event => setModel(event.target.value)} /></Field><label className="checkbox-label"><input type="checkbox" checked={paid} disabled={attempted} onChange={event => setPaid(event.target.checked)} />{t('我确认向此供应商发送少量真实推理请求，可能产生费用，并记录用量。')}</label></> : null}
    {attempted ? <p>{t('本次检查已提交，不会自动重试。结果未知时请先核对请求记录。')}</p> : null}
    {result ? <><StatusBadge value={result.status} /><EvidenceDetails value={result} title={t('独立检查结果')} open /></> : null}
    <ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setOpen(false)}>{t('关闭')}</Button><Button variant="primary" busy={busy} disabled={!supported || attempted || (mode === 'inference' && (!model.trim() || !paid))} onClick={check}>{t('确认执行此检查')}</Button></footer>
  </div></Modal> : null}</>;
}
