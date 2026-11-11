import { useEffect, useRef, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { mutate, request } from '../../lib/api.js';
import { useResource } from '../../lib/useResource.js';
import { Button, ErrorNotice, Field, Modal, ResourceState } from '../ui.jsx';
import { AgentPlanDialog, planMatchesAgent } from './AgentPlanDialog.jsx';
import { readbackResources } from './ConfigurationState.jsx';
import { CatalogChoice } from './CatalogControls.jsx';
import { ResourcePagination } from './ResourcePagination.jsx';

const modes = [['direct', '直连'], ['routing', '本地转发'], ['aggregation', '模型聚合']];
const modeName = mode => t(modes.find(([id]) => id === mode)?.[1] || '直连');
const entry = row => ({ provider_id: row?.provider_id || '', model_id: row?.model_id || '' });
function initialDraft(mode, state) {
  const current = state.mode === mode;
  return { expected_version: state.version, mode, endpoint: state.endpoint || state.supported_endpoints?.[0] || 'responses', request_timeout_seconds: state.request_timeout_seconds || 120, circuit_threshold: state.circuit_threshold || 3, circuit_cooldown_seconds: state.circuit_cooldown_seconds || 60,
    ...(mode === 'routing' ? { target: entry(current ? state.target : null), fallbacks: current ? (state.fallbacks || []).map(entry) : [] } : { members: current ? (state.members || []).map(row => ({ ...entry(row), alias: row.alias })) : [{ ...entry(), alias: 'everyday' }], default_alias: current ? state.default_alias : 'everyday' }) };
}

function RouteChoice({ value, label, onChange, agentId, revision }) {
  return <fieldset className="agent-route-choice"><legend>{label}</legend>
    <CatalogChoice collection="providers" label={t('服务商')} value={value.provider_id} agentId={agentId} scope="all" revision={revision} allowNone={false} onChange={provider_id => onChange({ ...value, provider_id, model_id: '' })} />
    {value.provider_id ? <CatalogChoice key={value.provider_id} collection="models" label={t('请求模型')} value={value.model_id} providerId={value.provider_id} scope="all" revision={revision} allowNone={false} onChange={model_id => onChange({ ...value, model_id })} /> : <p>{t('先选择服务商，再选择它的已保存模型。')}</p>}
  </fieldset>;
}

export function AssistantConnectionModes({ agentId, agent, revision, onChanged, busy: externalBusy, onBusyChange, drafts }) {
  const resource = useResource(`/api/manage/hosts/${encodeURIComponent(agentId)}/mode`, revision);
  const state = resource.data;
  const [view, setView] = useState(null);
  const mode = view || state?.mode || 'direct';
  const [draft, setDraft] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [plan, setPlan] = useState(null);
  const [result, setResult] = useState(null);
  const [hostReadback, setHostReadback] = useState(null);
  const [recover, setRecover] = useState(false);
  const [memberOffset, setMemberOffset] = useState(0);
  const alive = useRef(true);
  const locked = useRef(false);
  const localDrafts = useRef(new Map());
  const cache = drafts || localDrafts;
  const cacheKey = value => `${agentId}:connection:${value}`;
  const cached = cache.current.get(cacheKey(mode));
  const value = draft || cached || (state && mode !== 'direct' ? initialDraft(mode, state) : null);
  const routeIds = mode === 'routing' && value ? [value.target, ...value.fallbacks].map(row => row.provider_id).filter(Boolean) : [];
  const aliases = mode === 'aggregation' && value ? value.members.map(row => row.alias.trim()).filter(Boolean) : [];
  const formProblem = new Set(routeIds).size !== routeIds.length ? t('备用服务不能与常用服务或其他备用项重复。请保留一次。') : new Set(aliases).size !== aliases.length ? t('每个聚合模型需要不同的显示名称。') : '';
  const disabled = busy || externalBusy;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    onBusyChange?.(busy);
    return () => onBusyChange?.(false);
  }, [busy, onBusyChange]);
  useEffect(() => {
    if (!draft) return;
    const warn = event => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [draft]);
  function update(patch) {
    const next = { ...value, ...patch };
    setDraft(next); cache.current.set(cacheKey(mode), next); setError(null);
  }
  function selectMode(next) { if (!disabled) { setView(next); setDraft(null); setMemberOffset(0); setError(null); } }
  function discard() { cache.current.delete(cacheKey(mode)); setDraft(null); setError(null); }
  async function execute(action) {
    if (locked.current || externalBusy) return;
    locked.current = true; setBusy(true); setError(null);
    try { await action(); } catch (failure) { if (alive.current) setError(failure); }
    finally { locked.current = false; if (alive.current) setBusy(false); }
  }
  async function preview() {
    await execute(async () => {
      const next = await mutate(`/api/manage/hosts/${encodeURIComponent(agentId)}/mode/preview`, 'POST', value);
      if (!alive.current) return;
      if (!planMatchesAgent(next, agentId)) throw new Error(t('这个预览不属于当前助手，已禁止应用。请关闭后重新预览。'));
      setPlan(next); setResult(null); setHostReadback(null);
    });
  }
  async function returnDirect() {
    await execute(async () => {
      const next = await mutate('/api/manage/plans', 'POST', { host_id: agentId, operation: 'restore' });
      if (alive.current) { setPlan(next); setResult(null); setHostReadback(null); }
    });
  }
  async function readHost(source) {
    try {
      const next = await request(`/api/manage/hosts/${encodeURIComponent(agentId)}/state`);
      if (!alive.current || next?.host_id !== agentId) return null;
      return { state: next, resources: readbackResources(source, next) };
    } catch {
      return null;
    }
  }
  async function apply() {
    await execute(async () => {
      try {
        const next = await mutate(`/api/manage/plans/${encodeURIComponent(plan.id)}/apply`, 'POST', { expected_version: plan.version, confirmed: true });
        if (!alive.current) return;
        const readback = await readHost(plan);
        if (!alive.current) return;
        setResult(next); setHostReadback(readback); discard(); resource.reload(); onChanged?.();
      } catch (failure) {
        if (alive.current) {
          const current = await request(`/api/manage/plans/${encodeURIComponent(plan.id)}`).catch(() => null);
          if (alive.current && current && planMatchesAgent(current, agentId)) setPlan(current);
          resource.reload();
        }
        throw failure;
      }
    });
  }
  function validForm() {
    if (!value || formProblem) return false;
    const rows = mode === 'routing' ? [value.target, ...value.fallbacks] : value.members;
    return rows.length > 0 && rows.every(row => row.provider_id && row.model_id && (mode !== 'aggregation' || row.alias?.trim())) && (mode !== 'aggregation' || value.members.some(row => row.alias === value.default_alias));
  }
  const effect = plan?.payload?.metadata?.connection_change;
  return <section className="management-panel agent-connection-modes" aria-label={t('连接方式')}>
    <div className="cc-software-row">
      <div className="cc-software-row-main">
        <span className="badge">{t('连接方式')}</span>
        {state ? <strong className="cc-software-row-title">{t('当前已应用：{mode}', { mode: modeName(state.mode) })}</strong> : <h2>{t('连接方式')}</h2>}
        {state?.definition_state === 'update_available' ? <span className="badge warning" role="status">{t('使用中的定义已更新；重新预览应用后才恢复转发。')}</span> : null}
      </div>
      {state ? <div className="toolbar-actions cc-software-row-actions">
        {mode === 'direct' && state.mode !== 'direct' ? <Button disabled={disabled || !!state.pending} onClick={returnDirect}>{t('预览返回直连')}</Button> : null}
        <nav className="agent-categories cc-mode-segmented" aria-label={t('查看连接方式')}>{modes.map(([id, label]) => <Button key={id} variant={mode === id ? 'primary' : 'secondary'} disabled={disabled} aria-pressed={mode === id} onClick={() => selectMode(id)}>{t(label)}</Button>)}</nav>
      </div> : null}
    </div>
    <div className="cc-sr-prose">
      <p>{t('直连使用服务商地址；转发与聚合通过本机服务。先预览，再确认修改这个助手。')}</p>
      {state ? <p>{t('已应用的连接方式只说明本机配置。它不证明助手已重新加载，也不证明服务商可达、密钥可用或模型可调用。')}</p> : null}
      {state && mode === 'direct' ? <p>{t('在下方选择并启用模型，助手将直接连接服务商。浏览这个选项不会切换连接。')}</p> : null}
    </div>
    <ResourceState resource={resource}>{state ? <>
      {state.pending ? <div className="agent-draft-notice"><p>{t('有未完成的连接更改。转发已暂停，请先检查并恢复。')}</p><Button disabled={disabled} onClick={() => setRecover(true)}>{t('检查连接恢复')}</Button></div> : null}
      {mode === 'direct' ? null : !state.supported ? <p className="management-card-note">{t('这个助手尚不支持由续境设置本地转发，请使用它已支持的直连方式。')}</p> : <form className="management-form agent-form cc-mode-drawer" onSubmit={event => { event.preventDefault(); preview(); }}>
        <div className="cc-sr-prose">
          <p>{mode === 'routing' ? t('选择常用模型，再按顺序添加你允许使用的备用服务。只在连接未建立或收到限流拒绝时尝试下一项；结果未知时不会重发。') : t('给模型起一个好记的名字。助手选择哪个名字，就发给对应服务商；聚合不会自动使用备用服务。')}</p>
          <p>{t('本机服务需要保持运行。这个转发连接使用独立凭据，服务商密钥由本机服务读取。')}</p>
        </div>
        <fieldset disabled={disabled || !!state.pending} className="agent-import-fields">
          <Field label={t('助手使用的接口')}><select value={value.endpoint} onChange={event => update({ endpoint: event.target.value })}>{state.supported_endpoints.map(endpoint => <option key={endpoint} value={endpoint}>{endpoint === 'responses' ? 'OpenAI Responses' : endpoint === 'messages' ? 'Anthropic Messages' : 'OpenAI Chat Completions'}</option>)}</select></Field>
          {mode === 'routing' ? <>
            <RouteChoice value={value.target} label={t('常用服务与模型')} agentId={agentId} revision={revision} onChange={target => update({ target })} />
            <section className="agent-fallbacks"><div className="cc-software-subhead"><h3>{t('允许使用的备用服务')}</h3><span className="muted">{t('按从上到下的顺序尝试，最多 8 项。')}</span><Button disabled={value.fallbacks.length >= 8} onClick={() => update({ fallbacks: [...value.fallbacks, entry()] })}>{t('添加备用服务')}</Button></div>
              {value.fallbacks.map((row, index) => <div className="agent-route-row" key={index}><RouteChoice value={row} label={t('备用服务 {index}', { index: index + 1 })} agentId={agentId} revision={revision} onChange={next => update({ fallbacks: value.fallbacks.map((old, i) => i === index ? next : old) })} /><div className="toolbar-actions"><Button disabled={index === 0} onClick={() => { const rows = [...value.fallbacks]; [rows[index - 1], rows[index]] = [rows[index], rows[index - 1]]; update({ fallbacks: rows }); }}>{t('上移')}</Button><Button disabled={index === value.fallbacks.length - 1} onClick={() => { const rows = [...value.fallbacks]; [rows[index + 1], rows[index]] = [rows[index], rows[index + 1]]; update({ fallbacks: rows }); }}>{t('下移')}</Button><Button onClick={() => update({ fallbacks: value.fallbacks.filter((_, i) => i !== index) })}>{t('移除备用服务')}</Button></div></div>)}
            </section>
          </> : <section className="agent-aggregation"><div className="cc-software-subhead"><h3>{t('聚合模型')}</h3><Button disabled={value.members.length >= 200} onClick={() => { setMemberOffset(Math.floor(value.members.length / 10) * 10); update({ members: [...value.members, { ...entry(), alias: '' }] }); }}>{t('添加聚合模型')}</Button></div><ResourcePagination label={t('聚合模型分页')} resource={{ data: { summary: true, items: value.members.slice(memberOffset, memberOffset + 10), total: value.members.length, offset: memberOffset, limit: 10, has_more: memberOffset + 10 < value.members.length } }} onPage={setMemberOffset} busy={disabled} />{value.members.map((row, index) => ({ row, index })).slice(memberOffset, memberOffset + 10).map(({ row, index }) => <div className="agent-route-row" key={index}>
            <Field label={t('模型显示名称 {index}', { index: index + 1 })}><input required maxLength={200} value={row.alias} onChange={event => update({ members: value.members.map((old, i) => i === index ? { ...old, alias: event.target.value } : old), default_alias: value.default_alias === row.alias ? event.target.value : value.default_alias })} /></Field>
            <RouteChoice value={row} label={t('模型来源 {index}', { index: index + 1 })} agentId={agentId} revision={revision} onChange={next => update({ members: value.members.map((old, i) => i === index ? next : old) })} />
            <div className="toolbar-actions"><label className="inline-checkbox"><input type="radio" name={`default-${agentId}`} checked={value.default_alias === row.alias} onChange={() => update({ default_alias: row.alias })} />{t('默认模型')}</label><Button disabled={value.default_alias === row.alias} onClick={() => update({ members: value.members.filter((_, i) => i !== index) })}>{t('移除此模型')}</Button></div>
          </div>)}<p className="cc-sr-prose">{t('删除默认模型前，请先选择另一个默认模型。')}</p></section>}
          <details className="agent-advanced"><summary>{t('超时与暂停策略')}</summary><Field label={t('请求超时（秒）')}><input type="number" min="1" max="600" step="1" value={value.request_timeout_seconds} onChange={event => update({ request_timeout_seconds: Number(event.target.value) })} /></Field><Field label={t('连续失败后暂停（次数）')}><input type="number" min="1" max="100" step="1" value={value.circuit_threshold} onChange={event => update({ circuit_threshold: Number(event.target.value) })} /></Field><Field label={t('暂停后再尝试（秒）')}><input type="number" min="1" max="3600" step="1" value={value.circuit_cooldown_seconds} onChange={event => update({ circuit_cooldown_seconds: Number(event.target.value) })} /></Field></details>
          {draft || cached ? <p className="management-card-note" role="status">{t('这份连接草稿尚未应用，可以切换查看后再回来继续编辑。')}</p> : null}
          {formProblem ? <p className="management-card-warning" role="alert">{formProblem}</p> : null}
          <div className="toolbar-actions"><Button type="submit" variant="primary" disabled={!validForm()}>{t('预览连接更改')}</Button>{draft || cached ? <Button onClick={discard}>{t('放弃连接草稿')}</Button> : null}<Button onClick={resource.reload}>{t('重新读取')}</Button></div>
        </fieldset>
      </form>}
    </> : null}</ResourceState>
    {!plan && !recover ? <ErrorNotice error={error} /> : null}
    {plan ? <AgentPlanDialog key={plan.id} plan={plan} result={result} hostReadback={hostReadback} definition={effect ? t('从{before}切换为{after}', { before: modeName(effect.before), after: modeName(effect.after) }) : ''} agent={agent} category={t('连接方式')} valid={planMatchesAgent(plan, agentId)} busy={busy} error={error} onApply={apply} onClose={() => { setPlan(null); setResult(null); setHostReadback(null); setError(null); }} /> : null}
    {recover ? <Modal title={t('检查连接恢复')} busy={busy} onClose={() => { setRecover(false); setError(null); }}><p className="modal-copy">{t('恢复会撤回这次未完成的助手文件更改，保留此前已应用的连接方式。检测到外部修改时会停止。')}</p><ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setRecover(false)}>{t('取消')}</Button><Button variant="primary" busy={busy} onClick={() => execute(async () => { await mutate(`/api/manage/plans/${encodeURIComponent(state.pending.plan_id)}/recover`, 'POST', { confirmed: true }); if (alive.current) { setRecover(false); resource.reload(); onChanged?.(); } })}>{t('确认恢复连接')}</Button></footer></Modal> : null}
  </section>;
}
