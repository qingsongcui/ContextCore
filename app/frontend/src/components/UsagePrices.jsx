import { useState } from 'react';
import { Plus } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { list } from '../lib/management.js';
import { Button, ErrorNotice, Field, Modal, ResourceState } from './ui.jsx';
import { DataTable, EvidenceDetails } from './ManagementUI.jsx';

export function UsagePrices() {
  const resource = useResource('/api/usage/prices');
  const providers = useResource('/api/manage/providers');
  const [draft, setDraft] = useState(null);
  const [preview, setPreview] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  async function save() {
    setBusy(true); setError(null);
    try {
      const body = Object.fromEntries(Object.entries(draft).filter(([, value]) => value !== ''));
      await mutate('/api/usage/prices', 'POST', body); setDraft(null); setPreview(false); resource.reload();
    } catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <section className="management-panel"><header><div><h2>{t('定价版本')}</h2><p>{t('费用按请求的用量与定价快照计算。新定价不倒改旧账；缺少缓存价格或用量时保留不完整状态。')}</p></div><Button icon={Plus} onClick={() => { setDraft({ provider_id: '', model: '', currency: 'USD', effective_from: new Date().toISOString(), input_per_million: '', output_per_million: '', cache_read_per_million: '', cache_write_per_million: '' }); setError(null); }}>{t('添加定价')}</Button></header><ResourceState resource={resource}><DataTable rows={list(resource.data)} columns={[{ key: 'provider_id', label: t('供应商') }, { key: 'model', label: t('模型') }, { key: 'currency', label: t('币种') }, { key: 'input_per_million', label: t('每百万输入') }, { key: 'output_per_million', label: t('每百万输出') }, { key: 'effective_from', label: t('生效时间') }]} /></ResourceState>
    {draft ? <Modal title={preview ? t('检查定价版本') : t('添加定价')} busy={busy} onClose={() => { setDraft(null); setPreview(false); }}><div className="management-form">{preview ? <EvidenceDetails value={draft} title={t('定价内容')} open /> : <><Field label={t('供应商')}><select value={draft.provider_id} onChange={event => setDraft({ ...draft, provider_id: event.target.value })}><option value="">{t('未绑定')}</option>{list(providers.data).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field><Field label={t('计费模型')}><input value={draft.model} onChange={event => setDraft({ ...draft, model: event.target.value })} /></Field><div className="form-columns"><Field label={t('币种')}><input value={draft.currency} onChange={event => setDraft({ ...draft, currency: event.target.value.toUpperCase() })} maxLength={3} /></Field><Field label={t('生效时间')} hint={t('使用包含时区的 ISO 时间。')}><input value={draft.effective_from} onChange={event => setDraft({ ...draft, effective_from: event.target.value })} /></Field></div>{[['input_per_million', t('每百万新增输入')], ['output_per_million', t('每百万输出')], ['cache_read_per_million', t('每百万缓存读取')], ['cache_write_per_million', t('每百万缓存创建')]].map(([key, label]) => <Field key={key} label={label} hint={t('留空表示未知；明确免费才填 0。')}><input inputMode="decimal" value={draft[key]} onChange={event => setDraft({ ...draft, [key]: event.target.value })} /></Field>)}</>}<ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => preview ? setPreview(false) : setDraft(null)}>{preview ? t('返回编辑') : t('取消')}</Button><Button variant="primary" busy={busy} disabled={!draft.provider_id || !draft.model.trim() || !draft.effective_from.trim()} onClick={() => preview ? save() : setPreview(true)}>{preview ? t('保存定价版本') : t('预览差异')}</Button></footer></div></Modal> : null}
  </section>;
}
