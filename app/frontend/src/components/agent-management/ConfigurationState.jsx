import { t } from '../../lib/i18n.jsx';
import { Button, ErrorNotice } from '../ui.jsx';
import { Facts } from '../ManagementUI.jsx';

export function configuredLabel(status) {
  const labels = {
    verified: t('配置已写入'), update_available: t('定义已更新，待应用'), externally_changed: t('配置已被外部修改'),
    missing: t('配置条目已缺失'), removed: t('已从助手移除'), unreadable: t('配置暂无法读取'),
    not_checked: t('尚未检查全部条目'), not_managed: t('尚未应用')
  };
  return labels[status] || t('配置状态尚未读取');
}

export function resourceNextStep(status) {
  const steps = {
    update_available: t('下一步：重新预览并应用，文件才会使用新定义。这仍不表示助手已加载。'),
    externally_changed: t('下一步：先查看差异再决定是否恢复，不要直接覆盖外部修改。'),
    missing: t('下一步：重新应用或恢复原选择。条目缺失不是删除成功。'),
    unreadable: t('下一步：先确认文件仍可读取，不要把读失败当成已经卸载。'),
    not_checked: t('下一步：重新读取配置状态。这次没有检查全部条目。')
  };
  return steps[status] || '';
}

export function resourceNextStepShort(status) {
  const steps = {
    update_available: t('待重新应用'),
    externally_changed: t('外部已改动'),
    missing: t('配置条目缺失'),
    unreadable: t('暂时读不到'),
    not_checked: t('尚未完整检查')
  };
  return steps[status] || '';
}

export function readbackResources(plan, state, names = {}) {
  const metadata = plan?.payload?.metadata || {};
  if (!state || state.host_id !== metadata.host_id) return [];
  const ids = [];
  for (const id of [metadata.provider_id, metadata.bundle_id, ...Object.keys(metadata.entity_versions || {})]) {
    if (id && !ids.includes(id)) ids.push(id);
  }
  return ids.filter(id => state.resources?.[id] && !['model', 'account'].includes(state.resources[id].kind)).map(id => ({
    id,
    name: names[id] || (state.selection?.provider_id === id ? state.selection.provider_name : '') || '',
    status: state.resources[id].status,
  }));
}

const PENDING_EFFECTS = {
  host_loaded: { not_verified: agent => t('尚未验证。重新打开 {agent} 后才能知道它是否加载了这份配置。', { agent }) },
  reachability: { not_checked: () => t('尚未检查。写入配置不能说明网络可达。') },
  authentication: { not_checked: () => t('尚未验证。读取配置时没有读取密钥，也不能说明密钥可用。') },
  invocation: { not_checked: () => t('尚未验证。没有发起模型调用，也不会把配置写入当成真实调用成功。') }
};

function pendingEffect(kind, value, agent) {
  const pending = { host_loaded: 'not_verified', reachability: 'not_checked', authentication: 'not_checked', invocation: 'not_checked' }[kind];
  const label = PENDING_EFFECTS[kind]?.[value || pending];
  if (label) return label(agent);
  return kind === 'host_loaded' ? t('加载状态无法识别，不能当成已经加载。') : t('这项状态无法识别，不能当成已经通过。');
}

export function effectFacts(state, agent) {
  const selection = state?.selection || {};
  const effects = state?.effects || {};
  const recorded = effects.selection ? effects.selection === 'recorded' : Boolean(selection.provider_id);
  return [
    [t('当前选择'), recorded ? [selection.provider_name || t('服务商定义已移除'), selection.model_id || t('模型未记录')].join(' · ') : t('尚未由续境选择当前模型')],
    [t('配置文件'), configuredLabel(effects.configuration || selection.status)],
    [t('宿主加载'), pendingEffect('host_loaded', effects.host_loaded || selection.host_loaded, agent)],
    [t('连接可达'), pendingEffect('reachability', effects.reachability, agent)],
    [t('鉴权'), pendingEffect('authentication', effects.authentication, agent)],
    [t('模型调用'), pendingEffect('invocation', effects.invocation, agent)]
  ];
}

export function ConfiguredResource({ state, selected = false }) {
  const step = resourceNextStep(state?.status);
  const short = resourceNextStepShort(state?.status);
  return <p className={`agent-configured-state ${state?.status === 'verified' ? 'is-configured' : ''}`} title={step || undefined}>
    <span className="agent-state-dot" aria-hidden="true" />
    {selected ? t('当前配置选择') : configuredLabel(state?.status)}
    {selected ? <span> · {configuredLabel(state?.status)}</span> : null}
    {short ? <span className="agent-configured-step"> · {short}</span> : null}
  </p>;
}

export function ConfigurationState({ resource, host, agent, ccSwitchCurrent = null, onRestore, onRecords, busy }) {
  const state = resource.data;
  const selection = state?.selection;
  const initialLoading = resource.loading && !state;
  const checked = (Boolean(state) || !resource.loading) && !resource.error;
  const selected = checked && selection?.provider_id && ['verified', 'update_available'].includes(selection.status);
  const envConflicts = Array.isArray(state?.env_conflicts) ? state.env_conflicts : Array.isArray(state?.env_conflicts?.conflicts) ? state.env_conflicts.conflicts : [];
  return <section className="agent-current-configuration" aria-label={t('当前配置')}>
    <div className="cc-software-row">
      <div className="cc-software-row-main">
        <span className="badge">{t('当前配置')}</span>
        <h2>{selected ? selection.connection_mode && selection.connection_mode !== 'direct' ? t('续境本机连接') : selection.provider_name || t('服务商定义已移除') : ccSwitchCurrent ? ccSwitchCurrent.name : checked && selection?.provider_id ? t('当前选择需重新核对') : t('由助手管理当前选择')}</h2>
        {selected ? <span className="status-badge is-active">{selection.model_id || t('模型未记录')} · {configuredLabel(selection.status)}</span> : ccSwitchCurrent ? <span className="status-badge is-active">{ccSwitchCurrent.default_model ? `${ccSwitchCurrent.default_model} · ${t('本机当前生效')}` : t('本机当前生效')}</span> : null}
      </div>
      <div className="toolbar-actions cc-software-row-actions">
        {selected ? <Button disabled={busy || initialLoading || Boolean(resource.error)} onClick={onRestore}>{t('预览恢复原模型选择')}</Button> : null}
        {state?.recovery?.length ? <Button variant="primary" disabled={busy} onClick={onRecords}>{t('有未完成更改，检查恢复')}</Button> : null}
        {checked && state ? <details className="cc-inline-popover"><summary>{t('此宿主的能力与验证证据')}</summary><div className="cc-inline-popover-body"><Facts values={effectFacts(state, agent)} /></div></details> : null}
        <Button busy={initialLoading} disabled={busy} onClick={resource.reload}>{t('读取配置状态')}</Button>
      </div>
    </div>
    <div className="agent-config-hint">
      {selected ? <p>{selection.model_id || t('模型未记录')} · {configuredLabel(selection.status)}</p> : <p>{t('下方资料卡显示写入状态。助手里已有的登录和模型选择会保留，直到你确认切换。')}</p>}
      <p>{!state || !checked ? t('读取配置后查看当前选择与应用方式。') : state.provider_mode === 'single_selection' ? t('这个助手一次选择一个默认服务商；切换会预览当前选择的变化。') : host.capabilities?.providers ? t('这个助手可以保留多个服务商；添加配置与选择默认模型分别操作。') : t('这个助手的模型选择请在它自己的界面完成。')}</p>
    </div>
    {checked && envConflicts.length > 0 ? <div className="cc-env-conflict-alert" role="status"><strong>{t('检测到环境变量冲突')}</strong><p>{t('检测到进程环境变量可能覆盖当前配置的密钥或地址：')}{envConflicts.map(item => `${item.env_var || item.variable} (${item.masked_value || item.source || 'env'})`).join(', ')}</p></div> : null}
    {checked && selection?.receipt_unknown ? <p className="management-card-warning" role="status">{t('旧版模型选择记录不完整，请先选择并启用当前模型，再继续管理连接。')}</p> : null}
    {initialLoading ? <p className="cc-sr-prose" role="status">{t('正在读取已管理条目…')}</p> : null}<ErrorNotice error={resource.error} />
    {state?.coverage === 'partial' ? <p className="management-card-note">{t('本次只检查了部分条目，请查看更改记录核对完整范围。')}</p> : null}
  </section>;
}
