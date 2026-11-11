import { useId, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { mutate } from '../../lib/api.js';
import { useResource } from '../../lib/useResource.js';
import { list } from '../../lib/management.js';
import { Button, ErrorNotice, Field } from '../ui.jsx';
import { StatusBadge } from '../ManagementUI.jsx';

export function ModelCatalogInput({ providerId, value, onChange, supported = true }) {
  const id = useId();
  const cache = useResource(providerId && supported ? `/api/manage/providers/${encodeURIComponent(providerId)}/models/catalog` : null);
  const [remote, setRemote] = useState(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const catalog = remote || cache.data;
  const choices = list(catalog);
  const catalogReadEmpty = Boolean(providerId && supported && ['fresh', 'stale'].includes(catalog?.status) && !choices.length);
  async function refresh() {
    setBusy(true); setError(null);
    try { setRemote(await mutate(`/api/manage/providers/${encodeURIComponent(providerId)}/models/catalog`, 'POST', {})); setConfirm(false); }
    catch (failure) { setError(failure); }
    finally { setBusy(false); }
  }
  return <>
    <Field label={t('请求模型')} hint={t('可搜索已缓存的模型，也可以填写服务商提供的准确模型名。列表不代表账号有权限。')}><input required list={id} value={value || ''} onChange={event => onChange(event.target.value)} /><datalist id={id}>{choices.map(item => <option key={item.id} value={item.id}>{item.name || item.id}</option>)}</datalist></Field>
    {providerId && supported ? <div className="agent-catalog"><StatusBadge value={catalog?.status} /><Button onClick={() => setConfirm(!confirm)}>{t('更新可选模型')}</Button>{confirm ? <div><p>{t('将访问这个服务商的模型目录，可能读取已保存凭据；不会发送推理请求。是否继续？')}</p><Button busy={busy} onClick={refresh}>{t('确认访问模型目录')}</Button><Button disabled={busy} onClick={() => setConfirm(false)}>{t('取消')}</Button></div> : null}<ErrorNotice error={error || cache.error} /></div> : providerId ? <p>{t('这个协议暂不支持读取模型目录，请填写服务商文档中的准确模型名。不会自动发起检查或推理。')}</p> : null}
    {catalogReadEmpty ? <p>{t('已读取的模型目录是空的。仍可填写服务商文档中的准确模型名并保存。目录为空不代表账号有权限，保存也不会向服务商发起请求。')}</p> : null}
  </>;
}
