import { useRef, useState } from 'react';
import { t } from '../lib/i18n.jsx';
import { mutate, request } from '../lib/api.js';
import { Button, ErrorNotice, Field, Modal } from './ui.jsx';

const OPTIONS = [
  ['context_tools', 'mcp', '上下文工具（MCP）'],
  ['continuation', 'skill', '接续命令或技能'],
  ['lifecycle', 'hooks', '生命周期通知（可选）'],
];
const OPERATIONS = { install: '启用所选能力', update: '更新所选配置', uninstall: '撤销所选能力', pause: '暂停所选权限', resume: '恢复所选权限' };
const SURFACES = { cli: '命令行', desktop: '桌面端', ide: 'IDE' };
const EFFECTS = {
  context_tools: '配置上下文工具连接，让助手在获得对应权限后访问续境；不会读取历史或调用模型。',
  continuation: '安装接续命令或技能，供你主动发起接棒；不会自动发送消息，也不会自动启用上下文工具。',
  lifecycle: '配置生命周期通知入口，将所选事件送入本机队列；通知不会改写输入或注入聊天消息。',
};

export function ConnectionPlanDialog({ setup, onClose, onChanged }) {
  const [setupData, setSetupData] = useState(setup.data);
  const [selected, setSelected] = useState([]);
  const [surface, setSurface] = useState(setupData.surfaces?.length === 1 ? setupData.surfaces[0] : '');
  const [operation, setOperation] = useState('install');
  const [plan, setPlan] = useState(null);
  const [consent, setConsent] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [unknown, setUnknown] = useState(false);
  const lock = useRef(false);
  const prefix = `/api/connections/${setup.id}/plans`;
  const applied = plan?.status === 'applied' && plan?.readback === 'verified';
  const recovery = ['applying', 'recovery_required'].includes(plan?.status);
  const valid = setupData.plan_supported && surface && selected.length > 0;

  async function run(fn) {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    setError(null);
    try { await fn(); } catch (failure) { setError(failure); }
    finally { lock.current = false; setBusy(false); }
  }
  async function readback(id) {
    const result = await request(`${prefix}/${id}`);
    setPlan(result);
    setUnknown(false);
    if (result.status === 'applied' || result.status === 'recovered') onChanged();
    return result;
  }
  async function reset() {
    await run(async () => {
      const fresh = await request(`/api/connections/${setup.id}/setup`);
      setSetupData(fresh);
      setSelected([]);
      setPlan(null);
      setConsent(false);
      setUnknown(false);
    });
  }
  async function submit(action) {
    await run(async () => {
      setUnknown(true);
      setConsent(false);
      try {
        await mutate(`${prefix}/${plan.id}/${action}`, 'POST', { version: plan.version, confirmed: true });
      } catch (failure) {
        try { await readback(plan.id); } catch { /* Keep unknown until a successful readback. */ }
        throw failure;
      }
      await readback(plan.id);
    });
  }
  return <Modal title={t('{name} 的接入计划', { name: setup.label })} busy={busy} onClose={onClose} wide>
    <div className="editor-form connection-plan-form">
      <p>{t('只处理你选中的能力。读取历史、修改模型和使用模型额度需要另外授权。')}</p>
      {!plan ? <>
        <Field label={t('目标入口')}><select value={surface} disabled={busy} onChange={e => setSurface(e.target.value)}>
          <option value="">{t('请选择入口')}</option>
          {(setupData.surfaces || []).map(value => <option key={value} value={value}>{t(SURFACES[value] || value)}</option>)}
        </select></Field>
        <Field label={t('操作')}><select value={operation} disabled={busy} onChange={e => { setOperation(e.target.value); setSelected([]); }}>
          <option value="install">{t('启用所选能力')}</option><option value="update">{t('更新所选配置')}</option><option value="uninstall">{t('撤销所选能力')}</option>
          <option value="pause">{t('暂停所选权限')}</option><option value="resume">{t('恢复所选权限')}</option>
        </select></Field>
        <fieldset disabled={busy}><legend>{t('选择能力')}</legend>{OPTIONS.map(([id, component, label]) => <label className="check-label" key={id}>
          <input type="checkbox" checked={selected.includes(id)} disabled={operation !== 'install' && !setupData.installed_components?.includes(component)}
            onChange={e => setSelected(old => e.target.checked ? [...old, id] : old.filter(c => c !== id))} />
          <span>{t(label)}</span>
        </label>)}</fieldset>
        {setupData.installation_review === 'legacy_review_required' ? <p className="muted">{t('发现旧接入记录。预览会检查归属，不会自动迁移或覆盖。')}</p> : null}
        {(setupData.pending_plans || []).map(item => <Button key={item.id} disabled={busy} onClick={() => run(() => readback(item.id))}>{t('查看待恢复的接入计划')}</Button>)}
        <Button variant="primary" disabled={!valid || busy} onClick={() => run(async () => {
          const result = await mutate(prefix, 'POST', { operation, capabilities: selected, surface });
          setPlan(result); setConsent(false);
        })}>{t('预览所选改动')}</Button>
      </> : <>
        <p><strong>{t('操作')}</strong>：{t(OPERATIONS[plan.operation] || plan.operation)} · <strong>{t('目标入口')}</strong>：{t(SURFACES[plan.surface] || plan.surface)}</p>
        <p>{t('已选能力：')}{plan.capabilities.map(id => t(OPTIONS.find(([key]) => key === id)?.[2] || id)).join(' / ')}</p>
        <section className="agent-change-summary"><h3>{t('本次改动的影响')}</h3>
          <ul>{plan.capabilities.map(id => <li key={id}><strong>{t(OPTIONS.find(([key]) => key === id)?.[2] || id)}</strong>：{t(OPERATIONS[plan.operation] || plan.operation)}。{['install', 'update'].includes(plan.operation) && EFFECTS[id] ? t(EFFECTS[id]) : null}</li>)}</ul>
          {plan.operation === 'uninstall' ? <p>{t('撤销会移除所选的自有接入条目并收回对应权限；外部修改会阻止覆盖。')}</p> : null}
          <p>{t('风险与验证：配置写入不能证明助手已加载。接续仍须目标助手回读并等待用户确认，模型请求可能产生费用且须另外授权。')}</p>
        </section>
        {['pause', 'resume'].includes(plan.operation) ? <p>{t('本次只改变所选权限，保留助手配置文件。')}</p> : null}
        {plan.dependencies?.length ? <p className="muted">{t('接续命令需要可用的上下文工具；单独安装命令不会自动启用 MCP。')}</p> : null}
        <section className="setup-targets"><h3>{t('目标文件位置')}</h3><ul>{plan.files.map(item => <li key={item.path}><code>{item.path}</code></li>)}</ul></section>
        {applied ? <p className="operation-notice" role="status">{t('所选改动已完成并读回。助手实际加载与真实调用仍待验证。')}</p> : null}
        {unknown ? <p role="alert">{t('操作结果尚不确定。先重新读取状态，不要重复提交。')}</p> : null}
        {recovery ? <p role="alert">{t('写入中断，配置待恢复。恢复会检查文件，外部改动不会被覆盖。')}</p> : null}
        {plan.status === 'conflict' ? <p role="alert">{t('配置已变化，请重新预览。')}</p> : null}
        {plan.status === 'recovered' ? <p role="status">{t('已恢复操作前的配置，请重新预览。')}</p> : null}
        <details><summary>{t('查看计划详情')}</summary><pre>{JSON.stringify({ id: plan.id, version: plan.version, surface: plan.surface, status: plan.status, files: plan.files }, null, 2)}</pre></details>
        {(plan.status === 'planned' || recovery) && !unknown ? <label className="check-label consent"><input type="checkbox" checked={consent} disabled={busy} onChange={e => setConsent(e.target.checked)} />
          <span>{t('我已核对目标入口、所选能力和文件，确认执行当前计划。')}</span></label> : null}
        <footer className="modal-actions">
          <Button disabled={busy} onClick={() => run(() => readback(plan.id))}>{t('重新读取结果')}</Button>
          {!unknown && !recovery ? <Button disabled={busy} onClick={reset}>{t('重新选择并预览')}</Button> : null}
          {plan.status === 'planned' && !unknown ? <Button variant={plan.operation === 'uninstall' ? 'danger' : 'primary'} busy={busy} disabled={!consent} onClick={() => submit('apply')}>{t('确认执行计划')}</Button> : null}
          {recovery && !unknown ? <Button busy={busy} disabled={!consent} onClick={() => submit('recover')}>{t('恢复本次改动')}</Button> : null}
        </footer>
      </>}
      <ErrorNotice error={error} />
    </div>
  </Modal>;
}
