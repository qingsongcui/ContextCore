import { useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { configurationDiffEntries, hostNotice } from '../../lib/management.js';
import { Button, ErrorNotice, Modal } from '../ui.jsx';
import { EvidenceDetails, Facts, StatusBadge, statusLabel } from '../ManagementUI.jsx';
import { configuredLabel, effectFacts } from './ConfigurationState.jsx';
import { ResourcePagination } from './ResourcePagination.jsx';

export function planMatchesAgent(plan, agentId) {
  return Boolean(plan && plan.payload?.metadata?.host_id === agentId);
}
export function AgentPlanDialog({ plan, definition, result, hostReadback, agent, category, busy, error, onApply, onClose, valid }) {
  const [confirmed, setConfirmed] = useState(false);
  const [routeOffset, setRouteOffset] = useState(0);
  const [changeOffset, setChangeOffset] = useState(0);
  const [showFiles, setShowFiles] = useState(false);
  const blocked = !valid || Boolean(plan.blocked) || ['blocked', 'conflict', 'failed', 'recovery_required', 'applying'].includes(plan.status);
  const files = result?.payload?.files || plan.payload?.files || [];
  const operation = plan.payload?.metadata?.operation;
  const connection = plan.payload?.metadata?.connection_change;
  const routes = connection?.routes || [];
  const changes = plan.payload?.metadata?.configuration_changes || [];
  const selection = plan.payload?.metadata?.model_selection;
  const sharedConfiguration = plan.payload?.metadata?.shared_configuration === true;
  const kindLabel = kind => ({ provider: t('模型与账号'), mcp: t('工具'), skill: t('技能'), prompt: t('使用规则') })[kind] || kind;
  return <Modal wide title={`${agent} · ${category} · ${result ? t('配置应用结果') : t('检查本次更改')}`} busy={busy} onClose={onClose}>
    <section className="agent-change-summary"><h3>{sharedConfiguration ? t('共享配置文件更改') : t('只更改 {agent}', { agent })}</h3><p>{operation === 'uninstall' ? t('只移除由续境管理且未被外部修改的条目；不会删除不属于续境的文件。') : operation === 'restore' ? t('恢复此前保留的配置和模型选择，同时移除之后应用的组合条目；外部更改会阻止应用。') : sharedConfiguration ? t('将所选定义写入所列的共享配置文件；共用这些文件的入口也会受到影响。') : t('将所选定义写入这个助手的配置。其他助手保持不变。')}</p><p>{t('配置写入不代表助手已加载，也不会授权读取聊天、后台算力或执行工具。')}</p><StatusBadge value={result?.status || plan.status} />
      {definition ? <p><strong>{definition}</strong></p> : null}
      {plan.payload?.metadata?.configuration_notice ? <p role="status">{hostNotice(plan.payload.metadata.host_id, 'configuration', plan.payload.metadata.configuration_notice)}</p> : null}
      {plan.payload?.metadata?.prompt_notice ? <p role="status">{hostNotice(plan.payload.metadata.host_id, 'prompt', plan.payload.metadata.prompt_notice)}</p> : null}
      {selection && !connection?.gateway_url ? <p>{plan.payload?.metadata?.selection_config_only ? t('本次将写入默认模型配置：{provider} · {model}；宿主是否采用仍需核验。', { provider: selection.provider_name, model: selection.native_model }) : t('本次将使用 {provider} 的 {model}。', { provider: selection.provider_name, model: selection.native_model })}</p> : null}
      {changes.length ? <div className="agent-resource-changes"><h4>{t('本次将更改的资料')}</h4><ResourcePagination label={t('资料更改分页')} resource={{ data: { summary: true, items: changes.slice(changeOffset, changeOffset + 10), total: changes.length, offset: changeOffset, limit: 10, has_more: changeOffset + 10 < changes.length } }} onPage={setChangeOffset} busy={busy} /><ul>{changes.slice(changeOffset, changeOffset + 10).map((entry, index) => <li key={changeOffset + index}>{kindLabel(entry.kind)} · <strong>{entry.name}</strong> · {entry.operation === 'uninstall' ? t('移除自有条目') : t('安装或更新')}</li>)}</ul></div> : null}
      {connection?.gateway_url ? <><p>{t('助手将连接到：{url}', { url: connection.gateway_url })}</p><p>{t('应用后需要续境本机服务运行。新连接使用独立凭据，不含服务商密钥。')}</p></> : null}
      {connection?.gateway_credential_revoked ? <p>{t('应用后撤销本机连接凭据，并恢复直连选择。')}</p> : null}
      {routes.length ? <><ResourcePagination label={t('连接预览分页')} resource={{ data: { summary: true, items: routes.slice(routeOffset, routeOffset + 10), total: routes.length, offset: routeOffset, limit: 10, has_more: routeOffset + 10 < routes.length } }} onPage={setRouteOffset} busy={busy} /><ol>{routes.slice(routeOffset, routeOffset + 10).map((row, i) => <li key={routeOffset + i}>{row.alias ? <strong>{row.alias} · </strong> : null}{row.provider_name} · {row.model_name} · {row.native_model}{row.alias === connection.default_alias ? ` · ${t('默认模型')}` : ''}</li>)}</ol></> : null}
      {files.length ? <details className="agent-file-details" onToggle={event => setShowFiles(event.currentTarget.open)}><summary>{t('文件位置与条目（高级）')} · {files.length}</summary>{showFiles ? <ul>{files.map((file, index) => <li key={file.path || index}><strong>{file.path || file.target_path}</strong>{file.install_mode ? <span> · {file.install_mode === 'symlink' ? t('共享版本链接') : t('独立副本')}</span> : null}{configurationDiffEntries(file.diff).map((entry, i) => <p key={i}>{entry.entry} · {entry.operation === 'uninstall' ? t('移除自有条目') : t('安装或更新')}</p>)}</li>)}</ul> : null}</details> : <p>{t('没有列出文件更改，请检查计划状态和说明后再决定。')}</p>}
      {plan.summary ? <p>{plan.summary}</p> : null}
    </section>
    {result ? <><Facts values={[[t('文件读回'), statusLabel(result.payload?.readback)], [t('宿主重载'), statusLabel(result.payload?.host_reload)], [t('真实请求'), statusLabel(result.payload?.real_request)]]} />{hostReadback?.state ? <section className="agent-host-readback" aria-label={t('应用后的配置读回')}><h3>{t('应用后的配置读回')}</h3><Facts values={effectFacts(hostReadback.state, agent)} />{hostReadback.resources?.length ? <ul>{hostReadback.resources.map(item => <li key={item.id}><strong>{item.name || t('名称未读到的条目')}</strong> · {configuredLabel(item.status)}</li>)}</ul> : null}{hostReadback.state.coverage === 'partial' ? <p>{t('本次只检查了部分条目，请查看更改记录核对完整范围。')}</p> : null}</section> : <p role="status">{t('应用后的本机配置还没有读回。请重新读取配置状态，不要把文件回执当成当前选择或已经移除。')}</p>}<p className="modal-copy">{connection?.client_configuration_changed === false ? t('助手连接地址和模型清单未改变。新请求使用新线路；已经发出的请求继续使用原线路。') : t('如果文件读回已验证，请重新打开助手并检查实际加载；尚未验证的项目仍然未知。')}</p></> : null}
    {!result ? <p>{t('预览有效期为 30 分钟；过期后请关闭并重新预览。')}</p> : null}
    <EvidenceDetails value={result || plan} title={t('高级：完整差异与回读')} />
    {!valid ? <p role="alert" className="modal-copy">{t('这个预览不属于当前助手，已禁止应用。请关闭后重新预览。')}</p> : null}
    <ErrorNotice error={error} />
    {!result && !blocked ? <label className="checkbox-label management-consent"><input type="checkbox" checked={confirmed} onChange={event => setConfirmed(event.target.checked)} />{sharedConfiguration ? t('我已核对共享文件及受影响的入口，确认执行当前计划。') : t('我确认只更改 {agent} 的{category}。', { agent, category })}</label> : null}
    <footer className="modal-actions"><Button onClick={onClose}>{result ? t('关闭') : t('取消')}</Button>{!result ? <Button variant="primary" busy={busy} disabled={!confirmed || blocked} onClick={onApply}>{t('确认应用到 {agent}', { agent })}</Button> : null}</footer>
  </Modal>;
}
