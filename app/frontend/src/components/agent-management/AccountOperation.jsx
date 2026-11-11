import { useEffect, useRef, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { mutate, request } from '../../lib/api.js';
import { Button, ErrorNotice, Modal } from '../ui.jsx';
import { EvidenceDetails, Facts } from '../ManagementUI.jsx';
import { AccountStatus, accountStatusAt, useAccountStatus } from './AccountStatus.jsx';

const currentByDefault = () => true;

export function AccountOperation({ action, item, agent, onClose, onChanged, isCurrent = currentByDefault }) {
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [poll, setPoll] = useState(0);
  const status = useAccountStatus(result);
  const alive = useRef(true);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    if (status !== 'authorizing' || error) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      if (!alive.current || !isCurrent() || accountStatusAt(result) !== 'authorizing') return;
      try {
        const output = await request(`/api/manage/accounts/${encodeURIComponent(item.id)}`, { signal: controller.signal });
        if (!alive.current || controller.signal.aborted || !isCurrent() || accountStatusAt(result) !== 'authorizing') return;
        if (output.id !== item.id) throw new Error(t('账号状态与当前窗口不匹配，请重新读取。'));
        if (output.status !== 'authorizing') { setResult(output); onChanged(); }
        else setPoll(value => value + 1);
      } catch (failure) { if (alive.current && !controller.signal.aborted) setError(failure); }
    }, 2500);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [status, result, poll, error, item.id, isCurrent, onChanged]);
  async function run() {
    if (busy || !isCurrent()) return;
    setBusy(true); setError(null);
    try {
      const output = await mutate(`/api/manage/${action === 'recover' ? 'plans' : 'accounts'}/${encodeURIComponent(item.id)}/${action}`, 'POST', { confirmed: true });
      if (alive.current && isCurrent()) { setResult(output); onChanged(); }
    } catch (failure) { if (alive.current) setError(failure); }
    finally { if (alive.current) setBusy(false); }
  }
  return <Modal title={`${agent} · ${action === 'recover' ? t('检查恢复') : t('账号授权操作')}`} busy={busy} onClose={onClose}>
    <p className="modal-copy">{action === 'recover' ? t('请求恢复此事务的原文件。后台会校验归属与外部变更，结果需要逐项回读。') : action === 'revoke' ? t('将请求已配置的官方撤销端点并清除本机凭据。供应商接受请求不等于所有远程会话均已退出；结果分别记录。') : t('只使用供应商官方授权流程。若不具备授权资格或能力，结果会标为受阻。')}</p>
    <p className="modal-copy">{t('账号属于共享资料库；退出或更新这个账号可能影响引用它的其他助手。不会自动应用助手配置。')}</p>
    <Facts values={[[t('对象'), item.name || item.id], [t('操作'), action]]} />
    {result ? <><AccountStatus item={result} />{status === 'authorizing' && /^https:\/\//.test(result.authorization_url || '') ? <><p className="modal-copy">{t('在供应商官方页面完成授权后，这里会读取最新账号状态。授权取消或过期不会自动重试。')}</p><a className="button button-primary" href={result.authorization_url} target="_blank" rel="noopener noreferrer">{t('前往官方授权页面')}</a></> : null}{status === 'expired' ? <p className="modal-copy">{t('授权已过期。关闭窗口后，可重新发起官方授权；已有凭据可尝试刷新授权。')}</p> : null}<EvidenceDetails value={result} title={t('授权状态与后续步骤')} /></> : null}
    <ErrorNotice error={error} />{error && status === 'authorizing' ? <Button onClick={() => { setError(null); setPoll(value => value + 1); }}>{t('重新读取账号状态')}</Button> : null}
    <footer className="modal-actions"><Button onClick={onClose}>{t('关闭')}</Button><Button variant="primary" disabled={!!result} busy={busy} onClick={run}>{t('确认操作')}</Button></footer>
  </Modal>;
}
