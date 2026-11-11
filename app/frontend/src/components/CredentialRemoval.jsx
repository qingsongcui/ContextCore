import { useState } from 'react';
import { t } from '../lib/i18n.jsx';
import { mutate } from '../lib/api.js';
import { Button, ErrorNotice, Modal } from './ui.jsx';
import { EvidenceDetails } from './ManagementUI.jsx';

export function CredentialRemoval({ collection, item, onChanged, contextTitle }) {
  const [open, setOpen] = useState(false);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  async function remove() {
    setBusy(true); setError(null);
    try { setResult(await mutate(`/api/manage/${collection}/${encodeURIComponent(item.id)}/credential`, 'DELETE')); onChanged(); }
    catch (failure) { setError(failure); } finally { setBusy(false); }
  }
  return <><Button disabled={item.has_credential !== true} onClick={() => { setOpen(true); setResult(null); setError(null); }}>{t('移除本机凭据')}</Button>{open ? <Modal title={`${contextTitle ? `${contextTitle} · ` : ''}${t('移除本机凭据')}`} busy={busy} onClose={() => setOpen(false)}>
    <p className="modal-copy">{t('解除 {name} 的本机凭据绑定并记录清理结果。这不会向供应商撤销远端密钥，也不会自动改写已应用的宿主文件。', { name: item.name })}</p>
    <p className="modal-copy">{t('凭据绑定属于共享定义，移除后可能影响引用它的其他助手；不会自动改写已应用的助手配置。')}</p>
    {result ? <EvidenceDetails value={result} title={t('凭据清理回读')} open /> : null}<ErrorNotice error={error} />
    <footer className="modal-actions"><Button onClick={() => setOpen(false)}>{t('关闭')}</Button><Button variant="danger" disabled={!!result} busy={busy} onClick={remove}>{t('确认移除本机绑定')}</Button></footer>
  </Modal> : null}</>;
}
