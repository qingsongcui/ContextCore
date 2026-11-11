import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowLeft, Plus, RefreshCw, FileDiff, Download, Upload, KeyRound, Copy, Pencil, Trash2 } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { mutate, request, errorFeedback } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { list, display, safeEvidence, redactEvidence, hostNotice } from '../lib/management.js';
import { Button, EmptyState, ErrorNotice, Field, Modal, PageHeader, ResourceState } from '../components/ui.jsx';
import { DataTable, EvidenceDetails, Facts, StatusBadge } from '../components/ManagementUI.jsx';
import { CredentialRemoval } from '../components/CredentialRemoval.jsx';
import { AssetTools, AssetDiscovery } from '../components/AssetTools.jsx';
import { ProviderChecks } from '../components/ProviderChecks.jsx';
import { ManagementMigration } from '../components/ManagementMigration.jsx';
import { RoutingSettings } from '../components/RoutingSettings.jsx';
import { AssistantConnectionModes } from '../components/agent-management/AssistantConnectionModes.jsx';
import { DefinitionFields } from '../components/agent-management/DefinitionFields.jsx';
import { ImportDialog } from '../components/agent-management/ImportDialog.jsx';
import { AgentPlanDialog, planMatchesAgent } from '../components/agent-management/AgentPlanDialog.jsx';
import { AccountOperation } from '../components/agent-management/AccountOperation.jsx';
import { AccountStatus } from '../components/agent-management/AccountStatus.jsx';
import { ModelCatalogInput } from '../components/agent-management/ModelCatalogInput.jsx';
import { ProviderSetup } from '../components/agent-management/ProviderSetup.jsx';
import { ConfigurationState, ConfiguredResource, readbackResources } from '../components/agent-management/ConfigurationState.jsx';
import { apiLabel, apiDefaults, directApiMismatch } from '../components/agent-management/ProviderApiFields.jsx';
import { ModelCapacityField, capacityIsValid } from '../components/agent-management/ModelCapacityField.jsx';
import { ResourcePagination } from '../components/agent-management/ResourcePagination.jsx';
import { SharedLibrary, CatalogChoice, catalogPath } from '../components/agent-management/CatalogControls.jsx';
import { ConsolePage } from './ConsolePage.jsx';
import '../styles/agent-experience.css';

const PRIMARY = [['providers', '模型与账号'], ['mcp', '工具'], ['skill', '技能'], ['prompt', '使用规则']];
const SECONDARY = [['accounts', '账号授权'], ['bundles', '项目配置组合'], ['plans', '更改记录'], ['connections', '历史采集与来源授权'], ['advanced', '高级管理']];
const ASSET_KINDS = new Set(['mcp', 'skill', 'prompt']);
const cacheableDraft = value => value && value.draftValid !== false && JSON.stringify(redactEvidence(value)) === JSON.stringify(value);
const formatBackendWarning = (detail, fallbackStatus = 409) => {
  const text = typeof detail === 'string' ? detail.trim() : '';
  if (!text) return '';
  return /[\u4e00-\u9fff]/.test(text) ? safeEvidence(text) : t(errorFeedback(fallbackStatus, text));
};
function normalizeSyncPreview(res) {
  const rawResults = res?.results && typeof res.results === 'object' ? { ...res.results } : {};
  const failedMap = {};
  if (Array.isArray(res?.failed)) {
    for (const item of res.failed) {
      if (typeof item === 'string' && item) failedMap[item] = rawResults[item]?.error || '';
      else if (item && typeof item === 'object' && (item.host_id || item.id)) failedMap[item.host_id || item.id] = item.error || '';
    }
  } else if (res?.failed && typeof res.failed === 'object') {
    for (const [id, err] of Object.entries(res.failed)) {
      failedMap[id] = typeof err === 'string' ? err : err?.error || '';
    }
  }
  for (const [id, entry] of Object.entries(rawResults)) {
    if (entry?.ok === false || entry?.status === 'failed' || entry?.error) {
      failedMap[id] = entry.error || failedMap[id] || '';
    }
  }
  for (const [id, err] of Object.entries(failedMap)) {
    if (!rawResults[id]) rawResults[id] = { ok: false, status: 'failed', error: err };
  }
  const plannedIds = Array.isArray(res?.planned)
    ? res.planned.filter(Boolean)
    : Object.entries(rawResults).filter(([id, entry]) => !Object.prototype.hasOwnProperty.call(failedMap, id) && (entry?.plan || entry?.ok)).map(([id]) => id);
  const failedIds = Object.keys(failedMap);
  const conflictItems = Object.entries(rawResults)
    .filter(([, entry]) => entry?.status === 'conflict_preview' && entry?.plan)
    .map(([id, entry]) => ({ hostId: id, plan: entry.plan, result: null }));
  return { ...res, results: rawResults, plannedIds, failedIds, conflictItems };
}
const aliases = { models: 'providers', tools: 'mcp', skills: 'skill', instructions: 'prompt' };
const tabTitle = tab => t([...PRIMARY, ...SECONDARY].find(([id]) => id === tab)?.[1] || '共享资料库');
const pathFor = tab => `/api/manage/${ASSET_KINDS.has(tab) ? 'assets' : tab}`;
export const agentName = host => host?.id === 'omp' ? 'Oh My Pi (OMP)' : host?.label || host?.name || host?.id || '';
const sourceTypeLabel = type => type === 'cc-switch' || type === 'cc-switch-migration' ? t('本地配置') : type;
const newEntity = (tab, hostId) => tab === 'providers' ? { name: '', base_url: '', ...apiDefaults(hostId === 'claude-code' ? 'anthropic' : 'openai', hostId), enabled: true, model_map: {} } : tab === 'accounts' ? { name: '', kind: 'api_key', provider_id: '' } : tab === 'mcp' ? { name: '', kind: 'mcp', config: { url: '', transport: 'http' }, source: { type: 'manual' } } : tab === 'skill' ? { name: '', kind: 'skill', install_mode: 'copy', files: { 'SKILL.md': '' }, source: { type: 'manual' } } : tab === 'prompt' ? { name: '', kind: 'prompt', content: '', source: { type: 'manual' } } : { name: '', project_id: '', provider_id: '', asset_ids: [], host_ids: [hostId] };
const syncWriteCounts = res => ({
  p: res?.providers_synced ?? 0,
  m: res?.models_synced ?? 0,
  a: (res?.mcp_synced ?? 0) + (res?.prompts_synced ?? 0) + (res?.skills_synced ?? 0),
});
function rejectIncompleteSync(res) {
  if (res?.ok === false || Number(res?.failure_count) > 0) {
    const count = Number(res?.failure_count ?? res?.failures?.length ?? 0);
    throw new Error(t('本机配置没有全部写入：{n} 处失败。已有资料还在，失败项不会显示成已同步。', { n: count }));
  }
}

export function AgentManagementPage({ agentId = '', section = '', defaultToFirstHost = false, onAgentChange, navigate, revision, onChanged, projects = [], draftStore }) {
  const hosts = useResource('/api/manage/hosts', revision);
  const hostList = list(hosts.data);
  const effectiveAgentId = agentId || (defaultToFirstHost ? (hostList.find(item => item.id === 'claude-code')?.id || hostList[0]?.id || '') : '');
  const selected = hostList.find(item => item.id === effectiveAgentId);
  const requestedTab = aliases[section] || section || 'providers';
  const tab = [...PRIMARY, ...SECONDARY, ['library']].some(([id]) => id === requestedTab) ? requestedTab : 'providers';
  const scope = `${effectiveAgentId}:${tab}`;
  // Updated during render, so even a retained old event handler cannot apply to
  // the previous Agent between a URL update and effect cleanup.
  const scopeRef = useRef(scope);
  scopeRef.current = scope;
  const localDrafts = useRef(new Map());
  const drafts = draftStore || localDrafts;
  const [syncingLocal, setSyncingLocal] = useState(false);
  const [syncNotice, setSyncNotice] = useState('');
  const [syncError, setSyncError] = useState(null);
  const choose = (id, category = 'providers') => onAgentChange?.(id, category);
  async function syncLocalEnv() {
    if (syncingLocal) return;
    setSyncingLocal(true);
    setSyncError(null);
    setSyncNotice('');
    try {
      const res = await mutate('/api/manage/sync-local', 'POST', {});
      rejectIncompleteSync(res);
      setSyncNotice(t('已写入本机配置：服务商 {p} 个、模型 {m} 个、工具与技能 {a} 项。没有失败项。', syncWriteCounts(res)));
    } catch (failure) {
      setSyncError(failure);
    } finally {
      setSyncingLocal(false);
      hosts.reload();
      onChanged?.();
    }
  }
  return <div className="agent-experience">
    {!effectiveAgentId ? <><PageHeader title={t('Agent 管理')} description={t('先选一个助手，再调整它的模型、工具和使用规则。不会默认应用到所有助手。')} actions={<Button icon={RefreshCw} disabled={syncingLocal} onClick={syncLocalEnv}>{syncingLocal ? t('正在同步本机配置…') : t('同步本机现有配置')}</Button>} /><ErrorNotice error={syncError} />{syncNotice ? <p className="operation-notice" role="status">{syncNotice}</p> : null}<ResourceState resource={hosts}><div className="agent-chooser">{list(hosts.data).filter(host => ['codex', 'codex-desktop', 'omp', 'cursor', 'antigravity', 'claude-code', 'opencode'].includes(host.id)).map(host => <button className="agent-choice" key={host.id} onClick={() => choose(host.id)}><span className="agent-choice-name">{agentName(host)}</span><StatusBadge value={host.configuration_status} /><span>{t('本机加载尚需单独验证')}</span><span className="agent-choice-action">{t('管理这个助手')} →</span></button>)}</div><details className="agent-secondary"><summary>{t('其他助手与配置适配')}</summary><p>{t('这些选项的配置管理与聊天接入支持范围不同，请先查看对应能力。')}</p><div className="agent-chooser">{list(hosts.data).filter(host => !['codex', 'codex-desktop', 'omp', 'cursor', 'antigravity', 'claude-code', 'opencode'].includes(host.id)).map(host => <button className="agent-choice" key={host.id} onClick={() => choose(host.id)}><span className="agent-choice-name">{agentName(host)}</span><StatusBadge value={host.configuration_status} /><span>{t('本机加载尚需单独验证')}</span><span className="agent-choice-action">{t('管理这个助手')} →</span></button>)}</div></details>{!hosts.loading && !hosts.error && !list(hosts.data).length ? <EmptyState title={t('还没有可管理的助手')} body={t('重新读取本机服务中的助手列表。')} action={<Button onClick={hosts.reload}>{t('重新读取')}</Button>} /> : null}</ResourceState></> : <ResourceState resource={hosts}>{selected ? <AgentWorkspace key={scope} host={selected} allHosts={list(hosts.data)} tab={tab} revision={revision} onChanged={onChanged} projects={projects} navigate={navigate} choose={choose} scope={scope} scopeRef={scopeRef} drafts={drafts} /> : !hosts.loading && !hosts.error ? <EmptyState title={t('找不到这个助手')} body={t('地址中的助手不在当前列表中，请重新选择。')} action={<Button onClick={() => choose('', '')}>{t('返回助手列表')}</Button>} /> : null}</ResourceState>}
  </div>;
}

export function AgentWorkspace({ host, allHosts = [], tab, revision, onChanged, projects, navigate, choose, scope, scopeRef, drafts }) {
  const hostId = host.id;
  const connId = host.registry_host_id || hostId;
  const name = agentName(host);
  const [localRevision, setLocalRevision] = useState(0);
  const tick = `${revision}:${localRevision}`;
  const [showShared, setShowShared] = useState(false);
  const [search, setSearch] = useState('');
  const [assetOffset, setAssetOffset] = useState(0);
  const [sharedOffset, setSharedOffset] = useState(0);
  const assetListPath = (kind, offset) => '/api/manage/assets?' + new URLSearchParams({ summary: 'true', agent_id: hostId, kind: tab, scope: kind, limit: '50', offset: String(offset), q: search.trim() });
  const [modelOffset, setModelOffset] = useState(0);
  const [modelSearch, setModelSearch] = useState('');
  const [bindingOffset, setBindingOffset] = useState(0);
  const [showBindings, setShowBindings] = useState(false);
  const pagedTab = ASSET_KINDS.has(tab) || ['providers', 'accounts', 'bundles'].includes(tab);
  const providers = useResource(tab === 'providers' ? catalogPath('providers', { agentId: hostId, query: search, offset: assetOffset }) : null, tick);
  const presets = useResource(tab === 'providers' ? '/api/manage/provider-presets' : null, tick);
  const assets = useResource(ASSET_KINDS.has(tab) ? assetListPath('assigned', assetOffset) : null, tick);
  const availableAssets = useResource(pagedTab && showShared ? ASSET_KINDS.has(tab) ? assetListPath('available', sharedOffset) : catalogPath(tab, { agentId: hostId, query: search, scope: 'available', offset: sharedOffset }) : null, tick);
  const accounts = useResource(tab === 'accounts' ? catalogPath('accounts', { agentId: hostId, query: search, offset: assetOffset }) : null, tick);
  const models = useResource(tab === 'providers' ? catalogPath('models', { agentId: hostId, query: modelSearch, offset: modelOffset }) : null, tick);
  const bindings = useResource(tab === 'providers' && showBindings ? catalogPath('bindings', { agentId: hostId, offset: bindingOffset }) : null, tick);
  const configuration = useResource(`/api/manage/hosts/${encodeURIComponent(hostId)}/state`, tick);
  const connections = useResource('/api/connections', tick);
  const currentConnection = list(connections.data).find(item => item.id === connId || item.id === hostId);
  const status = useResource(tab === 'advanced' ? '/api/manage/status' : null, tick);
  const hosts = useResource(tab === 'bundles' ? '/api/manage/hosts' : null, tick);
  const resource = useResource(tab === 'bundles' ? catalogPath('bundles', { agentId: hostId, query: search, offset: assetOffset }) : tab === 'plans' ? catalogPath('plans', { agentId: hostId, offset: assetOffset }) : null, tick);
  const [editor, setEditor] = useState(null);
  const [draft, setDraft] = useState(null);
  const [draftValid, setDraftValid] = useState(true);
  const [rawDraft, setRawDraft] = useState(null);
  const [preview, setPreview] = useState(false);
  const [error, setError] = useState(null);
  const [localBusy, setBusy] = useState(false);
  const [modeBusy, setModeBusy] = useState(false);
  const busy = localBusy || modeBusy;
  const [notice, setNotice] = useState('');
  const [setupOpen, setSetupOpen] = useState(false);
  const [connectionEdit, setConnectionEdit] = useState(null);
  const [justSaved, setJustSaved] = useState(null);
  const [plan, setPlan] = useState(null);
  const [planDescription, setPlanDescription] = useState('');
  const [result, setResult] = useState(null);
  const [hostReadback, setHostReadback] = useState(null);
  const [activation, setActivation] = useState(null);
  const [activationModel, setActivationModel] = useState('');
  const [replacement, setReplacement] = useState('');
  const [secretTarget, setSecretTarget] = useState(null);
  const [secret, setSecret] = useState('');
  const [remove, setRemove] = useState(null);
  const [importing, setImporting] = useState(null);
  const [modelDraft, setModelState] = useState(null);
  const [bindingDraft, setBindingState] = useState(null);
  const modelProvider = useResource(modelDraft?.provider_id ? catalogPath('providers', { scope: 'all', ids: [modelDraft.provider_id], limit: 1 }) : null, tick);
  const [accountAction, setAccountAction] = useState(null);
  const [syncTarget, setSyncTarget] = useState(null);
  const [syncReview, setSyncReview] = useState(null);
  const [syncConflictQueue, setSyncConflictQueue] = useState([]);
  const [leave, setLeave] = useState(null);
  const [savedDraft, setSavedDraft] = useState(() => drafts.current.get(scope));
  const alive = useRef(true);
  const lock = useRef(false);
  const isCurrent = useCallback(() => alive.current && scopeRef.current === scope, [scope, scopeRef]);
  const refresh = useCallback(() => { setLocalRevision(value => value + 1); }, []);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => {
    const current = editor && draft ? { type: 'definition', editor, draft, rawDraft, draftValid } : modelDraft ? { type: 'model', draft: modelDraft } : bindingDraft ? { type: 'binding', draft: bindingDraft } : null;
    if (current) {
      if (cacheableDraft(current)) drafts.current.set(scope, current);
      else { drafts.current.delete(scope); setSavedDraft(null); }
    }
    if (!current && !secretTarget && !importing) return;
    const warn = event => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [editor, draft, rawDraft, draftValid, modelDraft, bindingDraft, secretTarget, importing, drafts, scope]);
  function clearDraft() { drafts.current.delete(scope); setSavedDraft(null); }
  function canStartDraft() {
    if (!savedDraft || editor || modelDraft || bindingDraft) return true;
    setError(new Error(`${t('继续编辑草稿')} / ${t('放弃草稿')}`));
    return false;
  }
  function setModelDraft(value) { if (value && !modelDraft && !canStartDraft()) return; setModelState(value); if (!value) clearDraft(); }
  function setBindingDraft(value) { if (value && !bindingDraft && !canStartDraft()) return; setBindingState(value); if (!value) clearDraft(); }
  function resumeDraft() {
    setSavedDraft(null); setError(null);
    if (savedDraft.type === 'model') setModelState(savedDraft.draft);
    else if (savedDraft.type === 'binding') setBindingState(savedDraft.draft);
    else { setEditor(savedDraft.editor); setDraft(savedDraft.draft); setRawDraft(savedDraft.rawDraft ?? null); setPreview(false); setDraftValid(savedDraft.draftValid !== false); }
  }
  function changeScope(id, category) {
    if (busy) return;
    if (editor || modelDraft || bindingDraft || secretTarget || importing || setupOpen || connectionEdit) { setLeave({ id, category }); return; }
    choose(id, category);
  }
  async function execute(fn) {
    if (lock.current || !isCurrent()) return null;
    lock.current = true; setBusy(true); setError(null); setNotice('');
    try { return await fn(); } catch (failure) { if (isCurrent()) setError(failure); return null; }
    finally { lock.current = false; if (isCurrent()) setBusy(false); }
  }
  function isDraftDirty() {
    if (!draft) return false;
    if (savedDraft) return true;
    if (editor?.original) {
      return JSON.stringify(draft) !== JSON.stringify(editor.original) || rawDraft !== null;
    }
    const initial = newEntity(editor?.tab || tab, hostId);
    return JSON.stringify(draft) !== JSON.stringify(initial) || rawDraft !== null;
  }
  function closeEditor() {
    if (isDraftDirty()) {
      const current = { type: 'definition', editor, draft, rawDraft, draftValid };
      if (cacheableDraft(current)) setSavedDraft(current);
      else clearDraft();
    } else {
      clearDraft();
    }
    setEditor(null);
    setDraft(null);
    setRawDraft(null);
    setPreview(false);
    setError(null);
  }
  function discardEditor() {
    clearDraft();
    setEditor(null);
    setDraft(null);
    setRawDraft(null);
    setPreview(false);
    setError(null);
  }
  function isModelDraftDirty(d) {
    if (!d) return false;
    if (savedDraft) return true;
    return Boolean(d.name?.trim() || d.model_id?.trim() || d.provider_id);
  }
  function isBindingDraftDirty(d) {
    if (!d) return false;
    if (savedDraft) return true;
    return Boolean(d.provider_id || d.model_id || d.project_id);
  }
  function closeModelModal() {
    if (modelDraft && isModelDraftDirty(modelDraft)) {
      setSavedDraft(drafts.current.get(scope) || { type: 'model', draft: modelDraft });
    } else {
      clearDraft();
    }
    setModelState(null);
    setError(null);
  }
  function closeBindingModal() {
    if (bindingDraft && isBindingDraftDirty(bindingDraft)) {
      setSavedDraft(drafts.current.get(scope) || { type: 'binding', draft: bindingDraft });
    } else {
      clearDraft();
    }
    setBindingState(null);
    setError(null);
  }
  function discardModelOrBinding() {
    clearDraft();
    setModelState(null);
    setBindingState(null);
    setError(null);
  }
  async function edit(item = null, duplicate = false) {
    if (busy || !canStartDraft()) return;
    if (item?._summary) {
      await execute(async () => {
        const full = await request(`${pathFor(tab)}/${encodeURIComponent(item.id)}`);
        if (!isCurrent()) return;
        if (full?.id !== item.id || full?._summary || (ASSET_KINDS.has(tab) && full.kind !== tab)) throw new Error(t('资料详情与当前选择不一致，请重新读取列表。'));
        beginEdit(full, duplicate);
      });
      return;
    }
    beginEdit(item, duplicate);
  }
  function beginEdit(item, duplicate) {
    const value = item ? structuredClone(item) : newEntity(tab, hostId);
    if (duplicate) {
      if (tab === 'providers' && (value.has_credential || value.account_id || value.credential_reattach_required)) value.credential_reattach_required = true;
      if (tab === 'providers' && value.credential_source === 'account') value.credential_source = 'provider';
      delete value.id;
      delete value.version;
      value.name += ` ${t('副本')}`;
      delete value.credential_ref;
      delete value.has_credential;
      delete value.account_id;
      delete value.status;
      value.agent_ids = [hostId];
      if (tab === 'bundles') value.host_ids = [hostId];
    }
    setEditor({ tab, original: duplicate ? null : item }); setDraft(value); setRawDraft(null); setDraftValid(true); setPreview(false); setError(null);
  }
  async function editCatalog(item, collection) {
    const begin = full => {
      if (collection === 'models') setModelDraft({ ...full, context_window: full.context_window ?? '' });
      else setBindingDraft({ ...full });
      setError(null);
    };
    if (!item._summary) { begin(item); return; }
    await execute(async () => {
      const full = await request(`/api/manage/${collection}/${encodeURIComponent(item.id)}`);
      if (!isCurrent()) return;
      if (full?.id !== item.id || full._summary || (collection === 'bindings' && full.host_id !== hostId)) throw new Error(t('资料详情与当前选择不一致，请重新读取列表。'));
      begin(full);
    });
  }
  async function saveDefinition() {
    await execute(async () => {
      // PATCH already merges existing data server-side. Preserve unknown editable
      // definition fields instead of rebuilding only the ordinary form fields.
      const body = { ...draft, ...(!editor.original ? { agent_ids: [hostId] } : {}) };
      for (const key of ['id', 'version', 'created_at', 'updated_at', 'has_credential', 'credential_ref', 'quota']) delete body[key];
      if (editor.tab === 'accounts') delete body.status;
      const old = editor.original;
      if (old && editor.tab === 'accounts' && body.kind === old.kind) delete body.kind;
      const saved = await mutate(`${pathFor(editor.tab)}${old ? `/${encodeURIComponent(old.id)}` : ''}`, old ? 'PATCH' : 'POST', { ...body, ...(old ? { expected_version: old.version } : {}) });
      if (!isCurrent()) return;
      clearDraft();
      setEditor(null);
      setDraft(null);
      setRawDraft(null);
      setPreview(false);
      refresh();
      setJustSaved(saved);
      setNotice(t('已加入 {agent} 的清单。下一步：预览并确认应用，才会修改助手配置。', { agent: name }));
    });
  }
  async function makePlan(item, operation = 'install', selection = null) {
    return execute(async () => {
      const body = operation === 'restore' ? { host_id: hostId, operation } : { host_id: hostId, operation, asset_ids: ASSET_KINDS.has(tab) ? [item.id] : [], ...(tab === 'providers' ? { provider_id: item.id } : {}), ...(tab === 'bundles' ? { bundle_id: item.id } : {}), ...(selection && selection.activate !== false ? { activate_provider: true, model_id: selection.model } : {}), ...(selection?.replace ? { replace_provider_id: selection.replace } : {}) };
      const output = await mutate('/api/manage/plans', 'POST', body);
      if (!isCurrent()) return null;
      if (!planMatchesAgent(output, hostId)) throw new Error(t('这个预览不属于当前助手，已禁止应用。请关闭后重新预览。'));
      setPlanDescription([item?.name || t('预览恢复原模型选择'), selection?.model ? selection.name || list(models.data).find(model => model.id === selection.model)?.name || selection.model : ''].filter(Boolean).join(' · '));
      setPlan(output); setResult(null); setHostReadback(null); return output;
    });
  }
  async function previewApplyAfterSetup(saved) {
    const provider = { ...saved.provider, setupModel: saved.model };
    setSetupOpen(false);
    setConnectionEdit(null);
    refresh();
    const canNativeApply = host.capabilities?.native_activation === true && Boolean(saved.model?.id) && !directApiMismatch(hostId, saved.provider);
    if (!canNativeApply) {
      setJustSaved(provider);
      setNotice(t('连接与默认模型已保存。这个助手暂不能从这里直接写入，请导出或按助手说明配置；确认前不会改助手文件。'));
      return;
    }
    setJustSaved(null);
    const output = await mutate('/api/manage/plans', 'POST', {
      host_id: hostId,
      operation: 'install',
      provider_id: saved.provider.id,
      activate_provider: true,
      model_id: saved.model.id,
    });
    if (!isCurrent()) return;
    if (!planMatchesAgent(output, hostId)) throw new Error(t('这个预览不属于当前助手，已禁止应用。请关闭后重新预览。'));
    setPlanDescription([saved.provider.name, saved.model.model_id || saved.model.name || ''].filter(Boolean).join(' · '));
    setPlan(output);
    setResult(null);
    setHostReadback(null);
    setNotice(t('已保存连接与模型。确认更改后才会写入 {agent}。', { agent: name }));
  }
  async function saveProviderSetup(body) {
    return execute(async () => {
      const saved = await mutate('/api/manage/providers/setup', 'POST', body);
      if (!isCurrent()) return;
      await previewApplyAfterSetup(saved);
    });
  }
  function openAddProvider() { setConnectionEdit(null); setSetupOpen(true); setError(null); }
  async function openConnectionEdit(item) {
    if (busy) return;
    await execute(async () => {
      let provider = item;
      if (item?._summary) {
        provider = await request(`/api/manage/providers/${encodeURIComponent(item.id)}`);
        if (!isCurrent()) return;
        if (provider?.id !== item.id || provider._summary) throw new Error(t('资料详情与当前选择不一致，请重新读取列表。'));
      }
      const page = await request(catalogPath('models', { providerId: provider.id, scope: 'all', limit: 100 }));
      if (!isCurrent()) return;
      const models = list(page).filter(model => !model.provider_id || model.provider_id === provider.id);
      if (page?.has_more) throw new Error(t('这个服务商的模型超过一页。请用完整定义分别修改，避免漏改。'));
      if (!models.length) throw new Error(t('这个服务商还没有可一起保存的模型。请用完整定义添加模型。'));
      setSetupOpen(false);
      setConnectionEdit({ provider, models });
    });
  }
  async function saveProviderRevision(body) {
    return execute(async () => {
      const saved = await mutate('/api/manage/providers/setup/revise', 'POST', body);
      if (!isCurrent()) return;
      await previewApplyAfterSetup(saved);
    });
  }
  async function openActivation(item) {
    const providerId = tab === 'bundles' ? item.provider_id : item.id;
    function begin(provider, seed) {
      const aliases = Object.entries(provider?.model_map || {}).map(([id, model_id]) => ({ id, name: id, model_id }));
      setActivation({ item, providerId, provider, aliases, seedModel: seed, chosenModel: seed || aliases[0] });
      setActivationModel(seed?.id || aliases[0]?.id || ''); setReplacement(''); setError(null);
    }
    if (!item._summary) {
      begin(list(providers.data).find(provider => provider.id === providerId) || item, item.setupModel || list(models.data).find(model => model.provider_id === providerId));
      return;
    }
    await execute(async () => {
      const provider = await request(`/api/manage/providers/${encodeURIComponent(providerId)}`);
      if (!isCurrent()) return;
      if (provider?.id !== providerId || provider._summary) throw new Error(t('资料详情与当前选择不一致，请重新读取列表。'));
      const saved = await request(catalogPath('models', { providerId, scope: 'all', limit: 1 }));
      if (isCurrent()) begin(provider, list(saved)[0]);
    });
  }
  async function loadHostReadback(source) {
    try {
      const state = await request(`/api/manage/hosts/${encodeURIComponent(hostId)}/state`);
      if (!isCurrent() || state?.host_id !== hostId) return null;
      const names = {};
      for (const item of [...list(providers.data), ...list(assets.data), ...list(models.data), ...list(accounts.data)]) {
        if (item?.id && item.name) names[item.id] = item.name;
      }
      return { state, resources: readbackResources(source, state, names) };
    } catch {
      return null;
    }
  }
  async function applyPlan() {
    if (!isCurrent() || !planMatchesAgent(plan, hostId) || plan.status !== 'planned' || result) return;
    await execute(async () => {
      const id = plan.id;
      try {
        await mutate(`/api/manage/plans/${encodeURIComponent(id)}/apply`, 'POST', { confirmed: true, expected_version: plan.version });
        const output = await request(`/api/manage/plans/${encodeURIComponent(id)}`);
        if (output.id !== id || !planMatchesAgent(output, hostId)) throw new Error(t('应用读回与当前助手不匹配，请重新读取更改记录。'));
        const readback = await loadHostReadback(output);
        if (isCurrent()) { setResult(output); setHostReadback(readback); }
      } catch (failure) {
        let output;
        try {
          output = await request(`/api/manage/plans/${encodeURIComponent(id)}`);
          if (output.id !== id || !planMatchesAgent(output, hostId)) throw new Error();
        } catch { output = { id, status: 'unknown', note: t('应用结果未知，请重新读取事务状态；不要重复提交。') }; }
        const readback = await loadHostReadback(planMatchesAgent(output, hostId) ? output : plan);
        if (isCurrent()) { setResult(output); setHostReadback(readback); setError(failure); }
      }
      if (isCurrent()) refresh();
    });
  }
  async function exportAssets() {
    await execute(async () => {
      const data = await request(tab === 'providers' ? '/api/manage/providers/export' : ASSET_KINDS.has(tab) ? `/api/manage/assets/export?kind=${tab}` : '/api/manage/export');
      if (!isCurrent()) return;
      const json = JSON.stringify(data, null, 2);
      const url = URL.createObjectURL(new Blob([json], { type: 'application/json' }));
      const link = document.createElement('a');
      link.href = url;
      link.download = `senrelay-${tab === 'providers' ? 'providers' : ASSET_KINDS.has(tab) ? tab : 'management'}.json`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
      setNotice(t('已导出共享定义文件。已排除保存的凭据，但技能、提示词与配置中的自由文本未经核验，对外共享前请自行检查。'));
    });
  }
  const activeResource = tab === 'providers' || tab === 'library' ? providers : tab === 'accounts' ? accounts : ASSET_KINDS.has(tab) ? assets : resource;
  const selectedForAgent = item => Boolean(configuration.data?.resources?.[item.id]?.paths) || (Array.isArray(item.agent_ids) ? item.agent_ids.includes(hostId) : (item.host_ids || []).includes(hostId) || list(bindings.data).some(binding => binding.host_id === hostId && binding.provider_id === item.id));
  const agentProviders = providers.data?.summary ? list(providers.data) : list(providers.data).filter(selectedForAgent);
  const allRows = list(activeResource.data).filter(item => !ASSET_KINDS.has(tab) || item.kind === tab);
  const assignedRows = tab === 'plans' || (activeResource.data?.summary && activeResource.data.agent_id === hostId) ? allRows : allRows.filter(selectedForAgent);
  const searchText = search.trim().toLocaleLowerCase();
  const rows = activeResource.data?.summary ? assignedRows : assignedRows.filter(item => !searchText || [item.name, item.base_url, item.protocol, item.source?.url, item.id].filter(Boolean).join(' ').toLocaleLowerCase().includes(searchText));
  const sharedRows = availableAssets.data?.summary ? list(availableAssets.data) : (ASSET_KINDS.has(tab) ? list(availableAssets.data).filter(item => item.kind === tab) : allRows).filter(item => !selectedForAgent(item));
  function changeSearch(value) { setSearch(value); setAssetOffset(0); setSharedOffset(0); }
  async function selectShared(item, selected) {
    await execute(async () => {
      await mutate(`${pathFor(tab)}/${encodeURIComponent(item.id)}/assignment`, 'POST', { host_id: hostId, selected, expected_version: item.version });
      if (!isCurrent()) return;
      refresh();
      setNotice(t('清单已更新。助手中的配置没有改变；应用或移除配置需要另行确认。'));
    });
  }
  async function quickSwitchProvider(item) {
    const knownModel = item?.default_model || item?.setupModel?.model_id || item?.setupModel?.id || Object.values(item?.model_map || {})[0] || list(models.data).find(m => m.provider_id === item?.id)?.model_id || list(models.data).find(m => m.provider_id === item?.id)?.id;
    if (!knownModel || host.selection_config_only) {
      await openActivation(item);
      return;
    }
    await execute(async () => {
      const res = await mutate(`/api/manage/hosts/${encodeURIComponent(hostId)}/quick-switch-provider`, 'POST', {
        provider_id: item.id,
        model_id: knownModel,
        auto_apply: true
      });
      if (!isCurrent()) return;
      if (res?.status === 'conflict_preview' && res?.plan) {
        setPlanDescription(item.name);
        setPlan(res.plan);
        setResult(null);
        setHostReadback(null);
        return;
      }
      refresh();
      onChanged?.();
      setNotice(t('已一键切换服务商并写入配置（已自动创建备份快照）。'));
    });
  }
  async function quickToggleAsset(item, enabled) {
    await execute(async () => {
      const res = await mutate(`/api/manage/hosts/${encodeURIComponent(hostId)}/quick-toggle-asset`, 'POST', {
        asset_id: item.id,
        enabled,
        auto_apply: true
      });
      if (!isCurrent()) return;
      if (res?.status === 'conflict_preview' && res?.plan) {
        setPlanDescription(item.name);
        setPlan(res.plan);
        setResult(null);
        setHostReadback(null);
        return;
      }
      refresh();
      onChanged?.();
      setNotice(enabled ? t('已一键启用并写入宿主配置（已自动创建备份快照）。') : t('已一键停用并更新宿主配置（已自动创建备份快照）。'));
    });
  }
  async function oneClickConnect() {
    await execute(async () => {
      const res = await mutate(`/api/connections/${encodeURIComponent(connId)}/one-click-connect`, 'POST', {});
      if (!isCurrent()) return;
      refresh();
      onChanged?.();
      if (res && (res.ok === false || res.warning)) {
        const friendlyWarning = formatBackendWarning(res.warning, 409) || t('请检查宿主配置冲突');
        setError(new Error(`${t('历史采集已开启，但宿主配置未完全自动写入：')}${friendlyWarning}`));
      } else {
        setNotice(t('已完成一键接入续境：历史采集、MCP、斜杠命令与静默 Hook 已配置就绪。'));
      }
    });
  }
  async function togglePauseCapture() {
    if (!currentConnection) return;
    await execute(async () => {
      const nextPaused = !currentConnection.paused;
      await mutate(`/api/connections/${encodeURIComponent(connId)}`, 'PATCH', { paused: nextPaused, confirmed: true });
      if (!isCurrent()) return;
      refresh();
      onChanged?.();
      setNotice(nextPaused ? t('已暂停此助手的聊天记录采集。') : t('已恢复此助手的聊天记录采集。'));
    });
  }
  async function oneClickDisconnect() {
    await execute(async () => {
      await mutate(`/api/connections/${encodeURIComponent(connId)}/one-click-disconnect`, 'POST', {});
      if (!isCurrent()) return;
      refresh();
      onChanged?.();
      setNotice(t('已断开此助手的续境接入并卸载受管组件。'));
    });
  }
  async function moveProvider(index, delta) {
    const nextIndex = index + delta;
    if (nextIndex < 0 || nextIndex >= rows.length) return;
    const ordered = rows.map(row => row.id);
    const [moved] = ordered.splice(index, 1);
    ordered.splice(nextIndex, 0, moved);
    await execute(async () => {
      await mutate('/api/manage/providers/reorder', 'POST', { ordered_ids: ordered });
      if (!isCurrent()) return;
      refresh();
      setNotice(t('已更新服务商排序。'));
    });
  }
  function syncHostLabel(id) {
    return agentName((allHosts?.length ? allHosts : [host]).find(item => item.id === id) || { id });
  }
  function closeSyncReview() {
    setError(syncTarget?.partialError || null);
    if (syncConflictQueue.length > 0) {
      const [nextReview, ...rest] = syncConflictQueue;
      setSyncConflictQueue(rest);
      setSyncReview(nextReview);
    } else {
      setSyncReview(null);
    }
  }
  async function submitSyncToHosts(event) {
    event.preventDefault();
    if (!syncTarget || !syncTarget.hostIds?.length) return;
    await execute(async () => {
      const res = await mutate('/api/manage/sync-hosts', 'POST', {
        kind: syncTarget.kind, entity_id: syncTarget.item.id, host_ids: syncTarget.hostIds,
        operation: 'install', ...(syncTarget.kind === 'provider' ? { model_id: syncTarget.modelId || '' } : {})
      });
      if (!isCurrent()) return;
      const normalized = normalizeSyncPreview(res);
      let partialError = null;
      if (normalized.failedIds.length > 0) {
        const failedNames = normalized.failedIds.map(syncHostLabel).join(', ');
        if (normalized.plannedIds.length > 0) {
          const plannedNames = normalized.plannedIds.map(syncHostLabel).join(', ');
          partialError = new Error(t('部分宿主已生成预览（{planned}），但以下宿主失败：{failed}。', { planned: plannedNames, failed: failedNames }));
        } else {
          partialError = new Error(t('以下宿主未能生成预览：{failed}。', { failed: failedNames }));
        }
        setError(partialError);
      }
      const conflictNote = normalized.conflictItems.length > 1
        ? t('检测到 {count} 个宿主存在配置冲突（{hosts}），请依次核对差异。', { count: normalized.conflictItems.length, hosts: normalized.conflictItems.map(item => syncHostLabel(item.hostId)).join(', ') })
        : '';
      if (conflictNote) setNotice(conflictNote);
      setSyncTarget(current => ({ ...current, preview: normalized, partialError, conflictNote }));
      if (normalized.conflictItems.length > 0) {
        setSyncReview(normalized.conflictItems[0]);
        setSyncConflictQueue(normalized.conflictItems.slice(1));
      } else {
        setSyncConflictQueue([]);
      }
    });
  }
  async function applySyncPlan() {
    if (!syncReview || syncReview.result || !planMatchesAgent(syncReview.plan, syncReview.hostId)) return;
    await execute(async () => {
      const selected = syncReview;
      let receipt;
      let applyError;
      try {
        await mutate(`/api/manage/plans/${encodeURIComponent(selected.plan.id)}/apply`, 'POST', { confirmed: true, expected_version: selected.plan.version });
      } catch (failure) { applyError = failure; }
      try {
        receipt = await request(`/api/manage/plans/${encodeURIComponent(selected.plan.id)}`);
        if (receipt.id !== selected.plan.id || !planMatchesAgent(receipt, selected.hostId)) throw new Error(t('应用读回与当前助手不匹配，请重新读取更改记录。'));
      } catch (failure) {
        receipt = { id: selected.plan.id, status: 'unknown', payload: { metadata: { host_id: selected.hostId } } };
        applyError = failure;
      }
      if (!isCurrent()) return;
      if (applyError) setError(applyError);
      setSyncReview(current => ({ ...current, result: receipt }));
      setSyncTarget(current => ({ ...current, applied: { ...current.applied, [selected.hostId]: receipt.status } }));
      try {
        const state = await request(`/api/manage/hosts/${encodeURIComponent(selected.hostId)}/state`);
        if (!isCurrent() || state.host_id !== selected.hostId) return;
        setSyncReview(current => ({ ...current, hostReadback: { state, resources: readbackResources(receipt, state, {}) } }));
      } catch {
        if (isCurrent()) setError(new Error(t('配置已提交，但回读失败。请重新读取宿主状态。')));
      }
      if (isCurrent()) refresh();
    });
  }

  async function syncLocalInWorkspace() {
    await execute(async () => {
      const res = await mutate('/api/manage/sync-local', 'POST', {});
      if (!isCurrent()) return;
      refresh();
      rejectIncompleteSync(res);
      setNotice(t('已写入本机配置：服务商 {p} 个、模型 {m} 个、工具与技能 {a} 项。没有失败项。', syncWriteCounts(res)));
    });
  }
  const supported = tab === 'providers' ? host.capabilities?.providers === true : tab === 'mcp' ? host.capabilities?.mcp === true : tab === 'skill' ? host.capabilities?.skills === true : tab === 'prompt' ? host.capabilities?.prompts === true : true;
  const category = tabTitle(tab);
  const hasDialog = editor || secretTarget || plan || importing || modelDraft || bindingDraft || accountAction || remove || activation || setupOpen || connectionEdit || syncTarget;
  const switcherHosts = (allHosts?.length ? allHosts : [host]).filter(item => ['claude-code', 'codex', 'codex-desktop', 'cursor', 'antigravity', 'omp', 'opencode', 'gemini-cli'].includes(item.id) || item.id === hostId);
  const activeLocalHosts = item => Array.isArray(item?.local_active_hosts) ? item.local_active_hosts : Array.isArray(item?.cc_switch_current_hosts) ? item.cc_switch_current_hosts : [];
  const activeCcSwitchProvider = tab === 'providers' ? rows.find(item => activeLocalHosts(item).includes(hostId)) || null : null;
  return <>
    <div className="cc-top-switcher-bar toolbar-actions" role="navigation" aria-label={t('宿主切换栏')}>
      {switcherHosts.map(item => <Button key={item.id} disabled={busy} variant={item.id === hostId ? 'primary' : 'secondary'} aria-current={item.id === hostId ? 'true' : undefined} onClick={() => item.id !== hostId && changeScope(item.id, tab)}>{agentName(item)}</Button>)}
      <div className="cc-top-switcher-actions">
        <Button disabled={busy} onClick={syncLocalInWorkspace}>{t('同步本机配置')}</Button>
        <Button icon={RefreshCw} disabled={busy} onClick={refresh}>{t('刷新')}</Button>
        <Button icon={ArrowLeft} className="cc-all-hosts-btn" disabled={busy} onClick={() => changeScope('', '')}>{t('所有助手')}</Button>
      </div>
    </div>
    <PageHeader title={name} description={tab === 'connections' ? t('读历史与新增聊天、写入助手配置与安装受管组件各自独立确认。每项能力都要单独核对。') : t('为 {agent} 选择模型、添加工具，或告诉它你的使用习惯。', { agent: name })} />
    <div className="cc-workspace-nav-bar">
      <nav className="agent-categories" aria-label={t('助手设置分类')}>{PRIMARY.map(([id, label]) => <Button key={id} disabled={busy} variant={tab === id ? 'primary' : 'secondary'} aria-current={tab === id ? 'page' : undefined} onClick={() => changeScope(hostId, id)}>{t(label)}</Button>)}</nav>
      <div className="cc-workspace-nav-right">
        <details className="agent-status" onToggle={event => { if (event.currentTarget.open) event.currentTarget.parentElement?.querySelectorAll(':scope > details[open]').forEach(node => { if (node !== event.currentTarget) node.removeAttribute('open'); }); }}><summary>{t('支持范围与验证')}</summary><div className="cc-popover-sheet"><div><strong>{t('配置格式')}</strong><StatusBadge value={host.configuration_status} /></div><div><strong>{t('本机实际加载')}</strong><StatusBadge value={host.verification_status} /></div><p>{t('管理能力与上下文能力分别验证；配置成功不等于已经登录、加载或调用成功。')}</p>{host.configuration_notice ? <p className="management-host-note" role="status">{hostNotice(hostId, 'configuration', host.configuration_notice)}</p> : null}<div className="toolbar-actions"><Button disabled={busy || tab === 'connections'} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); changeScope(hostId, 'connections'); }}>{t('历史采集与来源授权')}</Button></div><EvidenceDetails value={host} title={t('此宿主的能力与验证证据')} /></div></details>
        <details className={`agent-secondary ${SECONDARY.some(([id]) => id === tab) || tab === 'library' ? 'is-active' : ''}`} onToggle={event => { if (event.currentTarget.open) event.currentTarget.parentElement?.querySelectorAll(':scope > details[open]').forEach(node => { if (node !== event.currentTarget) node.removeAttribute('open'); }); }}><summary>{t('更多设置与共享资料库')}{SECONDARY.some(([id]) => id === tab) || tab === 'library' ? ` · ${category}` : ''}</summary><div className="toolbar-actions">{SECONDARY.map(([id, label]) => <Button key={id} disabled={busy} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); changeScope(hostId, id); }} aria-current={tab === id ? 'page' : undefined}>{t(label)}</Button>)}<Button disabled={busy} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); changeScope(hostId, 'library'); }}>{t('共享资料库')}</Button><Button onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); navigate?.('usage', { agent: host.registry_host_id || hostId }); }}>{t('使用情况')}</Button></div></details>
      </div>
    </div>
    {tab === 'providers' ? <ConfigurationState resource={configuration} host={host} agent={name} ccSwitchCurrent={activeCcSwitchProvider} busy={busy} onRestore={() => makePlan(null, 'restore')} onRecords={() => changeScope(hostId, 'plans')} /> : null}
    <details className="agent-advanced cc-provider-advanced">
      <summary>{t('高级：续境接入与本机转发')}</summary>
      <div className="cc-software-control-stack">
        <div className="cc-connection-status-bar cc-software-row" role="region" aria-label={t('续境接入状态')}>
          <div className="cc-software-row-main">
            <span className="badge">{t('续境接入状态')}</span>
            <StatusBadge value={currentConnection?.enabled ? (currentConnection.paused ? 'paused' : 'enabled') : 'not_connected'} />
            <span className="muted">{currentConnection?.enabled ? (currentConnection.paused ? t('已暂停自动采集新对话，已有上下文与 MCP 仍可使用。') : t('已启用对话采集与续境上下文协同。')) : t('一键完成历史路径授权、MCP 工具、/contextcore 斜杠命令与静默 Hook 安装。')}</span>
          </div>
          <div className="toolbar-actions cc-software-row-actions">
            <Button variant={currentConnection?.enabled ? 'secondary' : 'primary'} disabled={busy} onClick={oneClickConnect}>{t('一键接入续境')}</Button>
            {currentConnection?.enabled ? <Button disabled={busy} onClick={togglePauseCapture}>{currentConnection.paused ? t('恢复采集') : t('暂停采集')}</Button> : null}
            {currentConnection?.enabled ? <Button disabled={busy} onClick={oneClickDisconnect}>{t('断开接入')}</Button> : null}
          </div>
        </div>
        {tab === 'providers' ? <AssistantConnectionModes agentId={hostId} agent={name} revision={tick} onChanged={refresh} busy={localBusy} onBusyChange={setModeBusy} drafts={drafts} /> : null}
      </div>
    </details>
    {tab === 'prompt' && host.prompt_notice ? <p className="management-host-note" role="status">{hostNotice(hostId, 'prompt', host.prompt_notice)}</p> : null}
    <ErrorNotice error={!hasDialog ? error : null} />{notice ? <p className="operation-notice" role="status">{notice}</p> : null}{justSaved?.id && supported && (ASSET_KINDS.has(tab) || tab === 'providers' || tab === 'bundles') ? <div className="agent-next-step"><strong>{justSaved.name}</strong><Button variant="primary" disabled={busy || (tab === 'providers' && directApiMismatch(hostId, justSaved))} onClick={() => tab === 'providers' && host.capabilities?.native_activation ? justSaved.setupModel ? makePlan(justSaved, 'install', { model: justSaved.setupModel.id }) : openActivation(justSaved) : makePlan(justSaved)}>{t('预览应用到 {agent}', { agent: name })}</Button><Button onClick={() => setJustSaved(null)}>{t('稍后再应用')}</Button></div> : null}
    {savedDraft && !editor && !modelDraft && !bindingDraft ? <div className="agent-draft-notice" role="status"><p>{t('这个助手有一份尚未保存的草稿。没有应用任何配置。')}</p><Button onClick={resumeDraft}>{t('继续编辑草稿')}</Button><Button onClick={clearDraft}>{t('放弃草稿')}</Button></div> : null}
    {!supported ? <div className="agent-unsupported" role="status"><h2>{t('{agent} 暂不能直接应用这类配置', { agent: name })}</h2><p>{hostNotice(hostId, tab === 'providers' ? 'provider' : tab, host.operation_limits?.[tab]?.reason || (tab === 'providers' ? host.provider_unsupported_reason : null) || host.unsupported_reason || t('可以先保存共享定义，再导出并按照助手官方说明配置；本页不会把未支持的操作标为成功。'))}</p></div> : null}
    {tab === 'connections' ? <ConsolePage embedded agentId={host.registry_host_id || hostId} returnAgent={hostId} navigate={navigate} revision={revision} onChanged={onChanged} /> : tab === 'advanced' ? <><section className="management-panel"><h2>{t('共享管理与迁移')}</h2><p>{t('下列操作作用于共享资料或路由服务，不会自动应用到所有助手。导出的定义不含已存凭据，但包含的文本内容未经核验，共享前需人工核对。')}</p><div className="toolbar-actions"><ManagementMigration onChanged={refresh} /><Button icon={Upload} disabled={busy} onClick={() => setImporting({})}>{t('导入共享定义')}</Button><Button icon={Download} disabled={busy} onClick={exportAssets}>{t('导出共享定义')}</Button></div></section><RoutingSettings revision={tick} providers={providers.data} catalogContext /><ResourceState resource={status}><EvidenceDetails value={status.data} title={t('管理服务与凭据库状态')} /></ResourceState></> : tab === 'plans' ? <section className="management-panel"><h2>{t('{agent} 的更改记录', { agent: name })}</h2><ResourcePagination label={t('更改记录分页')} resource={resource} onPage={setAssetOffset} busy={busy} /><ResourceState resource={resource}><DataTable rows={rows.filter(item => planMatchesAgent(item, hostId))} onRow={item => execute(async () => { const value = await request(`/api/manage/plans/${encodeURIComponent(item.id)}`); if (isCurrent() && planMatchesAgent(value, hostId)) { setPlanDescription(''); setPlan(value); const applied = value.status !== 'planned'; setResult(applied ? value : null); setHostReadback(applied ? await loadHostReadback(value) : null); } })} columns={[{ key: 'id', label: t('计划 ID') }, { key: 'status', label: t('状态'), render: item => <StatusBadge value={item.status} /> }, { key: 'created_at', label: t('创建时间') }, { key: 'recovery', label: t('恢复'), render: item => ['recovery_required', 'applying'].includes(item.status) ? <Button disabled={busy} onClick={() => setAccountAction({ type: 'recover', item })}>{t('检查恢复')}</Button> : display(null) }]} /></ResourceState></section> : tab === 'library' ? <section className="management-panel"><h2>{t('共享资料库')}</h2><p>{t('这里的资料可以被多个助手引用；保存、删除或恢复定义不会自动重写助手文件。要应用，请返回对应分类并选择单个助手。')}</p><div className="toolbar-actions">{[...PRIMARY, ['accounts', '账号授权'], ['bundles', '项目配置组合']].map(([id, label]) => <Button key={id} onClick={() => changeScope(hostId, id)}>{t(label)}</Button>)}<Button icon={Upload} disabled={busy} onClick={() => setImporting({})}>{t('导入共享定义')}</Button><Button icon={Download} onClick={exportAssets}>{t('导出共享定义')}</Button></div><SharedLibrary revision={tick} onManage={category => changeScope(hostId, category)} /></section> : <>
      <section className="management-panel">
        <header className="cc-panel-header-bar">
          <div className="cc-panel-header-title">
            <h2>{name} · {category}</h2>
            <p className="agent-panel-hint">{t('只看这个助手的资料。常用路径：添加或一键切换 → 确认更改。')}</p>
          </div>
          <div className="toolbar-actions agent-library-actions">
            <div className="cc-toolbar-search">
              <Field label={t("搜索当前助手的资料")}><input type="search" maxLength={200} value={search} onChange={event => changeSearch(event.target.value)} placeholder={t("按名称、服务地址或来源查找")} /></Field>
            </div>
            <Button disabled={busy} aria-expanded={showShared} onClick={() => setShowShared(value => !value)}>{showShared ? t('收起共享资料') : t('从共享资料库选用')}</Button>
            <Button icon={Upload} disabled={busy} onClick={() => setImporting({})}>{tab === 'skill' ? t('导入技能') : t('导入共享定义')}</Button>
            {tab === 'mcp' ? <AssetDiscovery host={supported ? host : null} isCurrent={isCurrent} onImport={candidate => { if (!canStartDraft()) return; setEditor({ tab: 'mcp', original: null }); setDraft({ name: candidate.name, kind: 'mcp', config: candidate.config_preview, source: candidate.source }); setRawDraft(null); setPreview(false); setDraftValid(true); setError(null); }} /> : null}
            <details className="agent-advanced"><summary>{t('更多资料操作')}</summary><div className="cc-popover-sheet">{tab === 'providers' ? <><p>{t('已有模型目录、账号绑定或特殊配置时，可编辑完整定义。')}</p><div className="toolbar-actions"><Button disabled={busy} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); edit(); }}>{t('新建')}</Button><Button icon={Download} disabled={busy} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); exportAssets(); }}>{t('导出共享定义')}</Button></div></> : <div className="toolbar-actions"><Button icon={Download} disabled={busy} onClick={event => { event.currentTarget.closest('details')?.removeAttribute('open'); exportAssets(); }}>{t('导出共享定义')}</Button></div>}</div></details>
            <Button icon={Plus} variant="primary" disabled={busy} onClick={() => tab === 'providers' ? openAddProvider() : edit()}>{tab === 'providers' ? t('添加服务商') : t('新建')}</Button>
          </div>
        </header>
        {showShared ? <div className="agent-shared-picker"><div><p>{t('选用会把同一份资料加入当前助手清单，不会自动应用。修改共享资料会影响其他引用；需要独立修改时，请复制。')}</p><ResourceState resource={pagedTab ? availableAssets : activeResource}>{sharedRows.length ? sharedRows.map(item => <div className="agent-shared-row" key={item.id}><span>{item.name}</span>{ASSET_KINDS.has(tab) && supported ? <Button variant="primary" disabled={busy} onClick={() => quickToggleAsset(item, true)}>{t('一键启用')}</Button> : tab === 'providers' && host.capabilities?.native_activation === true ? <Button variant="primary" disabled={busy || item.enabled === false || directApiMismatch(hostId, item)} onClick={() => quickSwitchProvider(item)}>{t('一键切换')}</Button> : null}<Button disabled={busy} onClick={() => selectShared(item, true)}>{t('为这个助手选用')}</Button><Button disabled={busy} onClick={() => edit(item, true)}>{t('复制后单独修改')}</Button></div>) : <p>{t('没有其他可选资料')}</p>}</ResourceState>{pagedTab ? <ResourcePagination shared resource={availableAssets} onPage={setSharedOffset} busy={busy} /> : null}</div></div> : null}
        <ResourcePagination resource={activeResource} onPage={setAssetOffset} busy={busy} /><ResourceState resource={activeResource}>{rows.length ? <div className="management-grid">{rows.map((item, rowIndex) => {
          const hasConfigState = (Boolean(configuration.data) || !configuration.loading) && !configuration.error;
          const isSelectedProvider = tab === 'providers' && hasConfigState && ['verified', 'update_available'].includes(configuration.data?.selection?.status) && configuration.data?.selection?.provider_id === item.id;
          const isCcSwitchCurrent = tab === 'providers' && !isSelectedProvider && activeLocalHosts(item).includes(hostId);
          const isAppliedResource = Boolean(configuration.data?.resources?.[item.id]?.paths);
          const providerModelLabel = tab === 'providers' ? (item.default_model || Object.values(item.model_map || {})[0] || list(models.data).find(m => m.provider_id === item.id)?.model_id || '') : '';
          return <article className={`management-card ${isSelectedProvider || isCcSwitchCurrent || isAppliedResource ? 'is-active-card' : ''}`} key={item.id}>
            <header>
              <div className="management-card-heading">
                <span className={`cc-provider-dot ${isSelectedProvider || isCcSwitchCurrent || isAppliedResource ? 'is-active' : ''}`} aria-hidden="true" />
                <h3>{item.name}</h3>
                {providerModelLabel ? <span className="cc-model-pill" title={t('默认模型')}>{providerModelLabel}</span> : null}
                {isSelectedProvider ? <span className="status-badge is-active">{t('正在使用')}</span> : isCcSwitchCurrent ? <span className="status-badge is-active">{t('本机当前生效')}</span> : null}
                {item.status ? tab === 'accounts' ? <AccountStatus item={item} /> : <StatusBadge value={item.status} /> : null}
                <span>
                  {tab === 'accounts' || tab === 'bundles' ? <p className="agent-library-label">{t('已加入此助手清单')}</p> : <ConfiguredResource state={hasConfigState ? (configuration.data?.resources?.[item.id] || (configuration.data ? { status: configuration.data.coverage === 'complete_owned_entries' ? 'not_managed' : 'not_checked' } : null)) : null} selected={isSelectedProvider} />}
                </span>
              </div>
            </header>
            <Facts values={tab === 'providers' ? [[t('服务地址'), item.base_url], [t('接口格式'), apiLabel(item)], ...(item.credential_source === 'account' || (!item.credential_source && !item.has_credential && item.account_id) ? [[t('绑定授权账号'), item.account_name || t('账号名称待读取')]] : [[t('凭据已保存'), item.has_credential]])] : tab === 'accounts' ? [[t('授权类型'), item.kind]] : [[t('来源'), sourceTypeLabel(item.source?.type)], ...(tab === 'skill' ? [[t('技能安装方式'), item.install_mode === 'symlink' ? t('共享版本链接') : t('独立副本')]] : [])]} />
            {tab === 'providers' && item.credential_reattach_required && !item.has_credential && !item.account_id ? <p className="management-card-warning">{t('当前定义尚未绑定可用密钥，请设置本机密钥或绑定账号。')}</p> : null}
            {tab === 'providers' && directApiMismatch(hostId, item) ? <p className="management-card-warning">{t('这份定义的接口与 {agent} 的直连要求不同。请编辑定义并核对服务商支持的接口格式。', { agent: name })}</p> : null}
            {tab === 'providers' && host.capabilities?.native_activation !== true ? <p className="management-card-note">{t('这个助手暂不支持从这里切换当前模型。可以保存定义并导出，再在助手内选择；不会把写入定义当成已启用。')}</p> : null}
            <div className="toolbar-actions management-card-footer">
              <div className="management-card-primary-actions">
                {tab === 'accounts' ? <Button icon={KeyRound} disabled={busy} onClick={() => item.kind === 'api_key' ? (setSecretTarget({ collection: 'accounts', item }), setSecret('')) : setAccountAction({ type: 'authorize', item })}>{item.kind === 'api_key' ? t('设置密钥') : t('官方授权')}</Button> : <>
                  {tab === 'providers' ? <Button variant="primary" disabled={host.capabilities?.native_activation !== true || busy || item.enabled === false || directApiMismatch(hostId, item)} onClick={() => quickSwitchProvider(item)}>{host.selection_config_only ? t('预览默认模型配置') : t('一键切换')}</Button> : null}
                  {ASSET_KINDS.has(tab) ? <Button variant={isAppliedResource ? 'secondary' : 'primary'} disabled={!supported || busy} onClick={() => quickToggleAsset(item, !isAppliedResource)}>{isAppliedResource ? t('一键停用') : t('一键启用')}</Button> : null}
                  {tab !== 'providers' ? <Button icon={FileDiff} disabled={!supported || busy || item.enabled === false} onClick={() => makePlan(item)}>{t('预览应用到 {agent}', { agent: name })}</Button> : null}
                  {tab === 'bundles' || (tab === 'providers' && !providerModelLabel) ? <Button disabled={host.capabilities?.native_activation !== true || busy || (tab === 'providers' && directApiMismatch(hostId, item)) || (tab === 'bundles' && !item.provider_id)} onClick={() => openActivation(item)}>{t('选择并启用模型')}</Button> : null}
                  {tab === 'providers' ? <Button icon={KeyRound} disabled={busy} onClick={() => { setSecretTarget({ collection: 'providers', item }); setSecret(''); setError(null); }}>{t('设置密钥')}</Button> : null}
                </>}
              </div>
              <div className="management-card-icon-actions">
                <Button icon={Pencil} disabled={busy} onClick={() => edit(item)}>{t('编辑')}</Button>
                <Button icon={Copy} disabled={busy} onClick={() => edit(item, true)} aria-label={`${name} · ${category} · ${t('复制定义')} · ${item.name}`} />
                <Button icon={Trash2} disabled={busy || isAppliedResource} title={isAppliedResource ? t('先从助手移除配置或恢复原选择，再删除资料。') : t('删除定义')} onClick={() => { setRemove({ item, collection: tab }); setError(null); }} aria-label={`${name} · ${category} · ${t('删除定义')} · ${item.name}`} />
                <details className="agent-advanced cc-card-more-menu" onToggle={event => { if (event.currentTarget.open) document.querySelectorAll('.cc-card-more-menu[open]').forEach(node => { if (node !== event.currentTarget) node.removeAttribute('open'); }); }}>
                  <summary title={tab === 'accounts' ? t('更多账号操作') : t('版本、检查与高级详情')}>⋯<span className="cc-sr-prose">{tab === 'accounts' ? t('更多账号操作') : t('版本、检查与高级详情')}</span></summary>
                  <div className="cc-popover-sheet cc-card-more-sheet">
                    <div className="toolbar-actions agent-advanced-actions">
                      {tab === 'providers' ? <Button disabled={busy} onClick={() => openConnectionEdit(item)}>{t('编辑连接与模型')}</Button> : null}
                      {tab === 'providers' && providerModelLabel ? <Button disabled={host.capabilities?.native_activation !== true || busy || directApiMismatch(hostId, item)} onClick={() => openActivation(item)}>{t('选择并启用模型')}</Button> : null}
                      {tab === 'providers' && host.capabilities?.native_activation !== true ? <Button icon={FileDiff} disabled={!supported || busy || item.enabled === false} onClick={() => makePlan(item)}>{t('预览应用到 {agent}', { agent: name })}</Button> : null}
                      {tab === 'skill' ? <Button disabled={busy} onClick={() => setImporting({ target: item })}>{t('更新技能来源')}</Button> : null}
                      {ASSET_KINDS.has(tab) || (tab === 'providers' && isAppliedResource && configuration.data?.selection?.provider_id !== item.id) ? <Button disabled={!supported || busy} onClick={() => makePlan(item, 'uninstall')}>{t('预览从 {agent} 移除', { agent: name })}</Button> : null}
                      {tab === 'providers' || ASSET_KINDS.has(tab) ? <Button disabled={busy} onClick={() => { setSyncTarget({ kind: tab === 'providers' ? 'provider' : 'asset', item, hostIds: [hostId] }); setError(null); }}>{t('同步到多宿主')}</Button> : null}
                      {tab === 'providers' ? <><Button disabled={busy || rowIndex === 0} onClick={() => moveProvider(rowIndex, -1)}>{t('上移')}</Button><Button disabled={busy || rowIndex === rows.length - 1} onClick={() => moveProvider(rowIndex, 1)}>{t('下移')}</Button></> : null}
                      <Button disabled={busy} onClick={() => selectShared(item, false)}>{t('从助手清单移出')}</Button>
                      {tab === 'accounts' ? [['cancel', '取消授权流程'], ['refresh', '刷新授权'], ['logout', '退出授权'], ...(item.kind === 'oauth' ? [['revoke', '撤销官方授权']] : [])].map(([type, label]) => <Button key={type} onClick={() => setAccountAction({ type, item })}>{t(label)}</Button>) : null}
                    </div>
                    {tab === 'providers' && isAppliedResource && configuration.data?.selection?.provider_id === item.id ? <p className="management-card-note">{t('这是当前模型选择。先恢复原选择或改选其他模型，再移除这个服务商；不能在仍被选中时直接卸下。')}</p> : null}
                    <p>{t('移出清单只整理资料，不会卸载助手中的配置。')}</p>
                    {tab === 'providers' ? <ProviderChecks provider={item} contextTitle={`${name} · ${category}`} /> : null}
                    {tab === 'providers' && hostId === 'claude-code' ? <Button onClick={() => { setActivation({ item, providerId: item.id, switchOnly: true }); setActivationModel(''); setReplacement(''); setError(null); }}>{t('切换已托管供应商')}</Button> : null}
                    {['providers', 'accounts'].includes(tab) ? <CredentialRemoval collection={tab} item={item} contextTitle={`${name} · ${category}`} onChanged={refresh} /> : null}
                    {ASSET_KINDS.has(tab) ? <AssetTools selectedAgent={`${name} · ${category}`} isCurrent={isCurrent} item={item} onChanged={refresh} /> : null}
                    <EvidenceDetails value={item} />
                  </div>
                </details>
              </div>
            </div>
          </article>; })}</div> : searchText ? <EmptyState title={t('没有匹配的资料')} body={t('试试其他名称，或清空搜索查看这个助手的全部资料。')} action={<Button onClick={() => changeSearch('')}>{t('清空搜索')}</Button>} /> : <EmptyState title={t('还没有为这个助手添加资料')} body={t('可以新建、导入，或从共享资料库选用。其他助手的资料不会自动出现在这里。')} action={<Button onClick={() => tab === 'providers' ? openAddProvider() : edit()}>{tab === 'providers' ? t('添加服务商') : t('新建')}</Button>} />}</ResourceState>
      </section>
      {tab === 'providers' ? <details className="agent-advanced cc-models-drawer">
        <summary>{t('可选模型')} · {t('管理账号与密钥')}</summary>
        <div className="toolbar-actions agent-account-link"><Button onClick={() => changeScope(hostId, 'accounts')}>{t('管理账号与密钥')}</Button><Button disabled={busy || host.capabilities?.native_activation !== true} onClick={() => makePlan(null, 'restore')}>{t('预览恢复原模型选择')}</Button></div>
        <section className="management-panel"><header className="cc-panel-header-bar"><div className="cc-panel-header-title"><h2>{t('可选模型')}</h2><p className="cc-sr-prose">{t('模型目录与真实请求验证分别记录，不会因添加模型而发起调用。')}</p></div><div className="toolbar-actions agent-library-actions"><div className="cc-toolbar-search"><Field label={t('搜索可选模型')}><input type="search" placeholder={t('搜索可选模型')} value={modelSearch} maxLength={200} onChange={event => { setModelSearch(event.target.value); setModelOffset(0); }} /></Field></div><Button icon={Plus} disabled={busy} onClick={() => { setModelDraft({ name: '', provider_id: '', model_id: '', context_window: '' }); setError(null); }}>{t('添加模型')}</Button></div></header><ResourcePagination label={t('模型目录分页')} resource={models} onPage={setModelOffset} busy={busy} /><ResourceState resource={models}><DataTable rows={models.data?.summary ? list(models.data) : list(models.data).filter(item => agentProviders.some(provider => provider.id === item.provider_id))} columns={[{ key: 'name', label: t('名称') }, { key: 'provider_id', label: t('供应商'), render: item => list(providers.data).find(provider => provider.id === item.provider_id)?.name || item.provider_id }, { key: 'model_id', label: t('请求模型') }, { key: 'actions', label: t('操作'), render: item => <div className="toolbar-actions"><Button onClick={() => editCatalog(item, 'models')}>{t('编辑')}</Button><Button onClick={() => setRemove({ item, collection: 'models' })}>{t('删除定义')}</Button></div> }]} /></ResourceState></section>
        <details className="agent-advanced" onToggle={event => setShowBindings(event.currentTarget.open)}><summary>{t('高级：这个助手的模型绑定')}</summary><p>{t('绑定保存选择意图；文件生效仍需应用供应商配置计划。')}</p><Button disabled={busy} onClick={() => { setBindingDraft({ host_id: hostId, provider_id: '', model_id: '', project_id: '' }); setError(null); }}>{t('添加绑定')}</Button><ResourcePagination label={t('模型绑定分页')} resource={bindings} onPage={setBindingOffset} busy={busy} /><ResourceState resource={bindings}><DataTable rows={list(bindings.data).filter(item => item.host_id === hostId)} columns={[{ key: 'provider_id', label: t('供应商') }, { key: 'model_id', label: t('模型') }, { key: 'project_id', label: t('项目') }, { key: 'actions', label: t('操作'), render: item => <><Button onClick={() => editCatalog(item, 'bindings')}>{t('编辑')}</Button><Button onClick={() => setRemove({ item: { ...item, name }, collection: 'bindings' })}>{t('删除定义')}</Button></> }]} /></ResourceState></details>
      </details> : null}
    </>}
    {(setupOpen || connectionEdit) ? <ProviderSetup agent={name} agentId={hostId} existing={connectionEdit} presets={presets.data?.presets || []} protocols={host.provider_protocols?.length ? host.provider_protocols : hostId === 'codex' ? ['openai'] : hostId === 'claude-code' ? ['anthropic'] : ['openai', 'anthropic', 'gemini']} busy={busy} error={error} onSave={connectionEdit ? saveProviderRevision : saveProviderSetup} onClose={() => { setSetupOpen(false); setConnectionEdit(null); setError(null); refresh(); }} /> : null}
    {editor && draft ? <Modal wide title={`${name} · ${tabTitle(editor.tab)} · ${preview ? t('检查共享定义') : editor.original ? t('编辑定义') : t('新建定义')}`} busy={busy} onClose={closeEditor}><form className="management-form agent-form" onSubmit={event => { event.preventDefault(); if (draftValid) preview ? saveDefinition() : setPreview(true); }}>
      <p>{t('保存只更新共享定义，不写助手文件、不调用模型。已有其他引用也会看到新版定义；应用仍需分别确认。')}</p>
      {!cacheableDraft({ editor, draft, rawDraft, draftValid }) ? <p role="status">{t('含敏感内容或未通过校验的草稿不会跨页面保留。')}</p> : null}
      {preview ? <section className="agent-change-summary"><h3>{draft.name}</h3><p>{t('将保存{category}，不会更改 {agent}。', { category: tabTitle(editor.tab), agent: name })}</p>{draft.content ? <p className="agent-readable-text">{draft.content}</p> : null}{draft.files?.['SKILL.md'] ? <p className="agent-readable-text">{draft.files['SKILL.md']}</p> : null}<EvidenceDetails value={{ before: editor.original, after: draft }} title={t('高级：完整定义差异')} /></section> : <><DefinitionFields catalogContext={{ agentId: hostId, revision: tick }} kind={editor.tab} draft={draft} isEditing={Boolean(editor.original)} onChange={setDraft} initialRaw={rawDraft} onRawChange={setRawDraft} onValidityChange={setDraftValid} providers={providers.data} accounts={accounts.data} assets={assets.data} hosts={hosts.data} projects={projects} selectedAgent={name} presets={presets.data?.presets || []} />{editor.tab === 'providers' ? <ErrorNotice error={presets.error} /> : null}</>}
      <ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => preview ? setPreview(false) : discardEditor()}>{preview ? t('返回编辑') : t('取消')}</Button><Button type="submit" variant="primary" busy={busy} disabled={!draft.name?.trim() || !draftValid}>{preview ? t('保存共享定义') : t('预览保存')}</Button></footer>
    </form></Modal> : null}
    {activation ? <Modal title={`${name} · ${activation.switchOnly ? t('切换已托管供应商') : t('选择并启用模型')}`} busy={busy} onClose={() => setActivation(null)}><form className="management-form agent-form" onSubmit={async event => { event.preventDefault(); const next = await makePlan(activation.item, 'install', { model: activationModel, name: activation.chosenModel?.name, replace: replacement, activate: !activation.switchOnly }); if (next && isCurrent()) setActivation(null); }}>
      <p>{activation.switchOnly ? t('只替换 Claude Code 中属于 Senrelay 的旧供应商字段。新供应商没有凭据时不会沿用旧密钥；先预览，再确认应用。') : host.selection_config_only ? t('这里只预览默认模型配置，不会切换正在运行的对话；实际采用情况和账号模型权限仍需在宿主内核验。') : t('将预览这个助手当前模型选择的更改；其他助手不变。目录中的模型不代表账号有调用权限。')}</p>
      {!activation.switchOnly ? <><CatalogChoice collection="models" label={t('请求模型')} value={activationModel} allowNone={false} providerId={activation.providerId} scope="all" revision={tick} extraOptions={[...(activation.seedModel ? [activation.seedModel] : []), ...(activation.aliases || [])]} onChange={(value, chosenModel) => { setActivationModel(value); setActivation({ ...activation, chosenModel }); }} /><p>{t('没有可选模型？先返回“添加模型”，可读取本机缓存或明确刷新服务商目录。')}</p></> : null}
      {hostId === 'claude-code' && tab === 'providers' ? <CatalogChoice collection="providers" label={t('要替换的旧供应商')} value={replacement} onChange={setReplacement} scope="all" revision={tick} excludeIds={[activation.providerId]} noneLabel={t('不替换旧定义')} allowNone={!activation.switchOnly} /> : null}
      <ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setActivation(null)}>{t('取消')}</Button><Button type="submit" variant="primary" busy={busy} disabled={activation.switchOnly ? !replacement : !activationModel}>{t('预览应用到 {agent}', { agent: name })}</Button></footer></form></Modal> : null}
    {plan ? <AgentPlanDialog key={plan.id} plan={plan} definition={planDescription || Object.keys(plan.payload?.metadata?.entity_versions || {}).map(id => [...list(providers.data), ...list(assets.data), ...list(resource.data)].find(item => item.id === id)?.name).filter(Boolean).join(' · ')} result={result} hostReadback={hostReadback} agent={name} category={category} busy={busy} error={error} valid={isCurrent() && planMatchesAgent(plan, hostId)} onApply={applyPlan} onClose={() => { setPlan(null); setPlanDescription(''); setResult(null); setHostReadback(null); setError(null); }} /> : null}
    {secretTarget ? <Modal title={`${name} · ${category} · ${t('设置密钥')} · ${secretTarget.item.name}`} busy={busy} onClose={() => { setSecretTarget(null); setSecret(''); }}><form className="management-form agent-form" onSubmit={event => { event.preventDefault(); execute(async () => { await mutate(`/api/manage/${secretTarget.collection}/${encodeURIComponent(secretTarget.item.id)}/credential`, 'POST', { secret }); if (!isCurrent()) return; setSecret(''); setSecretTarget(null); refresh(); setNotice(t('凭据已提交到本机凭据库；是否可用以状态读回为准。')); }); }}><p>{t('密钥只提交给本机凭据库，不保存在浏览器草稿、导出文件或管理定义中。')}</p><p>{t('这个密钥属于共享定义；引用它的其他助手也可能使用它。不会自动应用配置或测试调用。')}</p><Field label={secretTarget.item.name}><input type="password" required value={secret} autoComplete="new-password" onChange={event => setSecret(event.target.value)} /></Field><ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => { setSecretTarget(null); setSecret(''); }}>{t('取消')}</Button><Button type="submit" variant="primary" busy={busy} disabled={!secret.trim()}>{t('保存到凭据库')}</Button></footer></form></Modal> : null}
    {remove ? <Modal title={`${name} · ${tabTitle(remove.collection)} · ${t('删除共享定义')}`} busy={busy} onClose={() => setRemove(null)}><p className="modal-copy">{t('删除 {name} 的管理定义。宿主文件中的自有条目需要先通过卸载计划移除。', { name: remove.item.name })}</p><p className="modal-copy">{t('这是共享资料库中的定义，删除也会影响其他引用；不会擅自删除助手里的非托管文件。')}</p><ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setRemove(null)}>{t('取消')}</Button><Button variant="danger" busy={busy} onClick={() => execute(async () => { await mutate(`${pathFor(remove.collection)}/${encodeURIComponent(remove.item.id)}?expected_version=${remove.item.version}`, 'DELETE'); if (isCurrent()) { setRemove(null); refresh(); } })}>{t('确认删除定义')}</Button></footer></Modal> : null}
    {importing ? <ImportDialog agent={name} agentId={['advanced', 'library'].includes(tab) ? undefined : hostId} category={tab} target={importing.target} isCurrent={isCurrent} onClose={() => setImporting(null)} onChanged={refresh} /> : null}
    {modelDraft || bindingDraft ? <Modal title={`${name} · ${modelDraft ? t('可选模型') : t('宿主模型绑定')}`} busy={busy} onClose={() => modelDraft ? closeModelModal() : closeBindingModal()}><form className="management-form agent-form" onSubmit={event => { event.preventDefault(); execute(async () => { const current = modelDraft || bindingDraft; const body = modelDraft ? { ...current, context_window: current.context_window ? Number(current.context_window) : null } : { ...current, host_id: hostId }; await mutate(`/api/manage/${modelDraft ? 'models' : 'bindings'}${current.id ? `/${encodeURIComponent(current.id)}` : ''}`, current.id ? 'PATCH' : 'POST', { ...body, ...(current.id ? { expected_version: current.version } : {}) }); if (isCurrent()) { clearDraft(); setModelState(null); setBindingState(null); refresh(); } }); }}>
      <CatalogChoice collection="providers" label={t('供应商')} agentId={hostId} value={(modelDraft || bindingDraft).provider_id} allowNone={false} noneLabel={t('选择服务商')} revision={tick} onChange={value => modelDraft ? setModelDraft({ ...modelDraft, provider_id: value, model_id: '' }) : setBindingDraft({ ...bindingDraft, provider_id: value, model_id: '' })} />
      {modelDraft ? <><Field label={t('名称')}><input required value={modelDraft.name} onChange={event => setModelDraft({ ...modelDraft, name: event.target.value })} /></Field><ModelCatalogInput key={modelDraft.provider_id} supported={['openai', 'anthropic'].includes(list(modelProvider.data)[0]?.protocol)} providerId={modelDraft.provider_id} value={modelDraft.model_id} onChange={value => setModelDraft({ ...modelDraft, model_id: value })} />{hostId === 'kimi-code' ? <ModelCapacityField required value={modelDraft.context_window} onChange={context_window => setModelDraft({ ...modelDraft, context_window })} /> : <details className="agent-advanced"><summary>{t('高级：上下文容量')}</summary><ModelCapacityField value={modelDraft.context_window} onChange={context_window => setModelDraft({ ...modelDraft, context_window })} /></details>}</> : <><CatalogChoice collection="models" label={t('模型')} value={bindingDraft.model_id} providerId={bindingDraft.provider_id || ''} scope="all" revision={tick} onChange={model_id => setBindingDraft({ ...bindingDraft, model_id })} /><ProjectSelect projects={projects} value={bindingDraft.project_id} onChange={value => setBindingDraft({ ...bindingDraft, project_id: value })} /></>}
      <ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={discardModelOrBinding}>{t('取消')}</Button><Button type="submit" variant="primary" busy={busy} disabled={!(modelDraft || bindingDraft).provider_id || Boolean(modelDraft && !capacityIsValid(modelDraft.context_window, hostId === 'kimi-code'))}>{t('保存共享定义')}</Button></footer>
    </form></Modal> : null}
    {accountAction ? <AccountOperation key={`${accountAction.item.id}:${accountAction.type}`} agent={name} action={accountAction.type} item={accountAction.item} isCurrent={isCurrent} onChanged={refresh} onClose={() => setAccountAction(null)} /> : null}
    {syncTarget && !syncReview ? <Modal title={t('同步到多宿主')} busy={busy} onClose={() => { setSyncTarget(null); setSyncConflictQueue([]); setError(null); }}><form className="management-form agent-form" onSubmit={submitSyncToHosts}>
      <p>{t('先为每个宿主生成差异预览；逐项确认后才会写入配置。')}</p>
      {syncTarget.kind === 'provider' ? <Field label={t('模型')}><input required value={syncTarget.modelId || ''} onChange={event => setSyncTarget(current => ({ ...current, modelId: event.target.value, preview: null, partialError: null, conflictNote: '', applied: {} }))} /></Field> : null}
      <div className="agent-resource-checklist">{switcherHosts.map(item => <label key={item.id} className="checkbox-label"><input type="checkbox" checked={syncTarget.hostIds.includes(item.id)} onChange={event => setSyncTarget(current => ({ ...current, preview: null, partialError: null, conflictNote: '', applied: {}, hostIds: event.target.checked ? Array.from(new Set([...current.hostIds, item.id])) : current.hostIds.filter(id => id !== item.id) }))} />{agentName(item)}</label>)}</div>
      {syncTarget.conflictNote ? <p className="operation-notice" role="status">{syncTarget.conflictNote}</p> : null}
      {syncTarget.preview ? <div>{Object.entries(syncTarget.preview.results || {}).map(([id, entry]) => <section key={id}><h3>{syncHostLabel(id)}</h3>{entry.plan ? <><StatusBadge value={syncTarget.applied?.[id] || entry.plan.status} /><Button disabled={busy || Boolean(syncTarget.applied?.[id])} onClick={() => { setError(null); setSyncReview({ hostId: id, plan: entry.plan, result: null }); }}>{t('查看差异并确认')}</Button></> : <><StatusBadge value="failed" /><p role="alert">{formatBackendWarning(entry.error, 422) || t('生成预览失败，请检查该宿主支持范围。')}</p></>}</section>)}</div> : null}
      <ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => { setSyncTarget(null); setSyncConflictQueue([]); setError(null); }}>{t('关闭')}</Button><Button type="submit" variant="primary" busy={busy} disabled={!syncTarget.hostIds.length}>{t('生成各宿主预览')}</Button></footer>
    </form></Modal> : null}
    {syncReview ? <AgentPlanDialog key={`${syncReview.hostId}:${syncReview.plan?.id || ''}`} plan={syncReview.plan} definition={[syncTarget?.item.name, syncConflictQueue.length > 0 ? t('剩余 {count} 个冲突宿主待确认', { count: syncConflictQueue.length }) : ''].filter(Boolean).join(' · ')} result={syncReview.result} hostReadback={syncReview.hostReadback} agent={syncHostLabel(syncReview.hostId)} category={category} busy={busy} error={error} valid={isCurrent() && planMatchesAgent(syncReview.plan, syncReview.hostId)} onApply={applySyncPlan} onClose={closeSyncReview} /> : null}
    {leave ? <Modal title={t('离开当前编辑？')} onClose={() => setLeave(null)}><p className="modal-copy">{t('已保存的定义不会丢失。定义与模型表单会保留为这个助手的临时草稿；未保存的密钥和导入内容会清空。旧的应用预览不会带到新页面。')}</p><footer className="modal-actions"><Button onClick={() => setLeave(null)}>{t('继续编辑')}</Button><Button onClick={() => choose(leave.id, leave.category)}>{t('确认离开')}</Button></footer></Modal> : null}
  </>;
}
function ProjectSelect({ projects, value, onChange }) { return <Field label={t('项目')}><select value={value || ''} onChange={event => onChange(event.target.value)}><option value="">{t('全局')}</option>{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>; }
