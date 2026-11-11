import { useRef, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { list } from '../../lib/management.js';
import { Button, ErrorNotice, Field } from '../ui.jsx';
import { SkillInstallMode } from '../AssetTools.jsx';
import { BundleAssetSelection, CatalogChoice } from './CatalogControls.jsx';
import { apiDefaults, presetApi, customApi, ProviderApiFields } from './ProviderApiFields.jsx';

const record = value => value && typeof value === 'object' && !Array.isArray(value);
function parseDraft(text) {
  const value = JSON.parse(text);
  if (!record(value) || typeof value.name !== 'string') throw new Error();
  for (const key of ['content', 'base_url', 'protocol', 'kind']) if (value[key] !== undefined && typeof value[key] !== 'string') throw new Error();
  for (const key of ['asset_ids', 'host_ids', 'agent_ids']) if (value[key] !== undefined && (!Array.isArray(value[key]) || value[key].some(item => typeof item !== 'string'))) throw new Error();
  if (value.files !== undefined && (!record(value.files) || Object.values(value.files).some(item => typeof item !== 'string'))) throw new Error();
  if (value.config !== undefined) {
    if (!record(value.config)) throw new Error();
    for (const key of ['command', 'url']) if (value.config[key] !== undefined && typeof value.config[key] !== 'string') throw new Error();
    if (value.config.args !== undefined && (!Array.isArray(value.config.args) || value.config.args.some(item => typeof item !== 'string'))) throw new Error();
  }
  return value;
}

// The raw editor and ordinary fields edit the same draft. Invalid raw input stays
// visible until corrected; switching back never silently drops it.
export function DefinitionFields({ kind, draft, onChange, onValidityChange, initialRaw = null, onRawChange, providers, accounts, assets, projects = [], hosts, selectedAgent, presets = [], isEditing = false, catalogContext }) {
  const [rawValue, setRawValue] = useState(initialRaw);
  const [rawError, setRawError] = useState(null);
  const [fileError, setFileError] = useState(null);
  const latestDraft = useRef(draft);
  latestDraft.current = draft;
  const fileGeneration = useRef(0);
  const connectionCache = useRef({});
  const raw = rawValue;
  function setRaw(value) { fileGeneration.current += 1; setRawValue(value); onRawChange?.(value); }
  const update = patch => onChange({ ...draft, ...patch });
  const config = draft.config || {};
  const currentPreset = presets.find(item => item.base_url === draft.base_url && item.protocol === draft.protocol);
  function changeConnection(mode) {
    const next = { ...config };
    if (mode === 'url') {
      connectionCache.current.command = { command: config.command || '', args: config.args || [] };
      delete next.command; delete next.args;
      Object.assign(next, connectionCache.current.url || { url: '', transport: 'http' });
    } else {
      connectionCache.current.url = { url: config.url || '', transport: config.transport || 'http' };
      delete next.url;
      Object.assign(next, connectionCache.current.command || { command: '', args: [] }, { transport: 'stdio' });
    }
    update({ config: next });
  }
  async function readTextFile(file) {
    if (!file) return;
    const generation = ++fileGeneration.current;
    setFileError(null);
    if (file.size > (kind === 'prompt' ? 200000 : 500000)) { setFileError(new Error(t('这个文本文件太大，请缩短内容后导入。'))); return; }
    try {
      const text = await file.text();
      if (generation !== fileGeneration.current) return;
      const current = latestDraft.current;
      onChange({ ...current, ...(kind === 'prompt' ? { content: text, name: current.name || file.name } : { files: { ...current.files, 'SKILL.md': text }, name: current.name || file.name }) });
    } catch { setFileError(new Error(t('文件无法读取，请重新选择。'))); }
  }
  function leaveRaw() {
    try {
      const parsed = parseDraft(raw);
      onChange(parsed); setRaw(null); setRawError(null); onValidityChange?.(true);
    } catch { setRawError(new Error(t('JSON 格式不正确，请检查后再预览。'))); }
  }
  function editRaw(value) {
    setRaw(value);
    try {
      const parsed = parseDraft(value);
      onChange(parsed); setRawError(null); onValidityChange?.(true);
    } catch { setRawError(new Error(t('JSON 格式不正确，请检查后再预览。'))); onValidityChange?.(false); }
  }
  return <>
    <div className="agent-editor-mode"><Button onClick={() => raw === null ? setRaw(JSON.stringify(draft, null, 2)) : leaveRaw()}>{raw === null ? t('高级：编辑完整定义') : t('返回表单')}</Button></div>
    {raw !== null ? <><Field label={t('完整定义 JSON')} hint={t('保留已有的额外字段。凭据只使用引用，不填写密钥。')}><textarea rows={18} value={raw} onChange={event => editRaw(event.target.value)} spellCheck={false} /></Field><ErrorNotice error={rawError} /></> : <>
      <Field label={t('名称')} hint={t('起一个自己能认出来的名字，之后可以修改。')}><input required maxLength={200} value={draft.name || ''} onChange={event => update({ name: event.target.value })} /></Field>
      {['prompt', 'skill'].includes(kind) ? <><Field label={t('从文本文件读取（可选）')}><input type="file" accept=".md,.txt,text/plain,text/markdown" onChange={event => readTextFile(event.target.files?.[0])} /></Field><ErrorNotice error={fileError} /></> : null}
      {kind === 'providers' ? <>
        <Field label={t('选择服务商')} hint={t('服务商列表不代表你的账号有调用权限。')}><select value={currentPreset?.id || ''} onChange={event => { const item = presets.find(value => value.id === event.target.value); if (item) update({ name: !draft.name || draft.name === currentPreset?.name ? item.name : draft.name, base_url: item.base_url, ...presetApi(item, catalogContext?.agentId) }); else update({ base_url: '' }); }}><option value="">{t('自定义或选择已知服务商')}</option>{presets.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        <Field label={t('服务地址')} hint={t('选择服务商后会自动填写；自定义服务请粘贴对方提供的 API 地址。')}><input required type="url" value={draft.base_url || ''} onChange={event => update(customApi(draft, event.target.value))} placeholder="https://api.example.com/v1" /></Field>
        {catalogContext ? <CatalogChoice collection="accounts" label={t('绑定授权账号')} value={draft.account_id} onChange={account_id => update({ account_id: account_id || null, credential_source: account_id ? 'account' : 'provider' })} providerId={draft.id || ''} includeUnbound scope="all" revision={catalogContext.revision} /> : <Field label={t('绑定授权账号')}><select value={draft.account_id || ''} onChange={event => update({ account_id: event.target.value || null, credential_source: event.target.value ? 'account' : 'provider' })}><option value="">{t('未绑定')}</option>{list(accounts).filter(item => !item.provider_id || item.provider_id === draft.id).map(item => <option key={item.id} value={item.id}>{item.name} · {item.kind}</option>)}</select></Field>}
        <Field label={t('凭据来源')} hint={t('明确选择本次使用的密钥。账号不可用时不会自动换用另一份密钥。')}><select value={draft.credential_source || (draft.has_credential || !draft.account_id ? 'provider' : 'account')} onChange={event => update({ credential_source: event.target.value })}><option value="provider">{t('服务商单独保存的密钥')}</option><option value="account" disabled={!draft.account_id}>{t('绑定账号的凭据')}</option></select></Field>
        <details className="agent-advanced"><summary>{t('高级：协议、模型映射与官方登录')}</summary><Field label={t('协议')}><select value={draft.protocol || 'openai'} onChange={event => update(apiDefaults(event.target.value, catalogContext?.agentId))}><option value="openai">OpenAI</option><option value="anthropic">Anthropic</option><option value="gemini">Gemini</option></select></Field><ProviderApiFields value={draft} onChange={update} /><label className="inline-checkbox"><input type="checkbox" checked={draft.enabled !== false} onChange={event => update({ enabled: event.target.checked })} />{t('启用供应商')}</label><p>{t('模型映射与已有官方 public client 配置可在完整定义中编辑。没有自己的官方客户端时，请使用 API Key 或在助手内登录。')}</p></details>
      </> : kind === 'accounts' ? <>
        <Field label={t('授权类型')} hint={isEditing ? t('已保存账号的授权类型不能修改。') : undefined}><select disabled={isEditing} value={draft.kind} onChange={event => update({ kind: event.target.value })}><option value="api_key">API Key</option><option value="oauth">OAuth</option><option value="host_owned">{t('宿主自有')}</option></select></Field>
        {catalogContext ? <CatalogChoice collection="providers" label={t('供应商')} value={draft.provider_id} onChange={provider_id => update({ provider_id })} scope="all" revision={catalogContext.revision} /> : <ProviderChoice providers={providers} value={draft.provider_id} onChange={value => update({ provider_id: value })} />}
        <p>{draft.kind === 'api_key' ? t('保存账号后，用“设置密钥”把密钥单独存入本机凭据库。') : t('只支持你有资格使用的官方登录。没有官方客户端配置时，请在助手内登录；这里不会借用其他应用的身份。')}</p>
      </> : kind === 'mcp' ? <>
        <Field label={t('工具连接方式')}><select value={Object.prototype.hasOwnProperty.call(config, 'url') ? 'url' : 'command'} onChange={event => changeConnection(event.target.value)}><option value="url">{t('服务网址')}</option><option value="command">{t('本机程序')}</option></select></Field>
        {Object.prototype.hasOwnProperty.call(config, 'url') ? <Field label={t('服务网址')} hint={t('粘贴工具提供的 MCP 网址，不是普通网站首页。')}><input type="url" required value={config.url || ''} onChange={event => update({ config: { ...config, url: event.target.value } })} /></Field> : <><Field label={t('程序名称或路径')}><input required value={config.command || ''} onChange={event => update({ config: { ...config, command: event.target.value } })} placeholder="npx" /></Field><Field label={t('启动参数（每行一项）')}><textarea rows={4} value={(config.args || []).join('\n')} onChange={event => update({ config: { ...config, args: event.target.value.split('\n') } })} /></Field></>}
        <p>{t('保存和应用工具配置不会运行工具。连接检查会另行征求确认。')}</p><details className="agent-advanced"><summary>{t('高级：协议、请求头与环境变量')}</summary><p>{t('在完整定义中保留或编辑 headers、env 和协议设置。敏感值必须使用凭据引用，不能写入明文密钥。')}</p></details>
      </> : kind === 'skill' ? <>
        <SkillInstallMode value={draft.install_mode} onChange={value => update({ install_mode: value })} />
        <Field label={t('技能说明（SKILL.md）')} hint={t('其他文件保持不变；安装不会执行脚本。')}><textarea required rows={14} value={draft.files?.['SKILL.md'] || ''} onChange={event => update({ files: { ...draft.files, 'SKILL.md': event.target.value } })} /></Field>
        <details className="agent-advanced"><summary>{t('技能中的其他文件')}</summary><p>{Object.keys(draft.files || {}).filter(path => path !== 'SKILL.md').join(' · ') || t('还没有记录')}</p><p>{t('需要编辑其他文件时，可使用完整定义；也可以从文件或目录导入。')}</p></details>
      </> : kind === 'prompt' ? <Field label={t('使用规则正文')} hint={t('例如：请用中文回答，先给结论；涉及删除文件时先问我。')}><textarea required rows={14} value={draft.content || ''} onChange={event => update({ content: event.target.value })} /></Field> : <>
        <Field label={t('项目')}><select value={draft.project_id || ''} onChange={event => update({ project_id: event.target.value })}><option value="">{t('全局')}</option>{projects.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        {catalogContext ? <CatalogChoice collection="providers" label={t('供应商')} value={draft.provider_id} onChange={provider_id => update({ provider_id })} scope="all" revision={catalogContext.revision} /> : <ProviderChoice providers={providers} value={draft.provider_id} onChange={value => update({ provider_id: value })} />}
        {catalogContext ? <BundleAssetSelection value={draft.asset_ids || []} onChange={asset_ids => update({ asset_ids })} revision={catalogContext.revision} /> : <div className="field"><span>{t('组合中的资产')}</span><div>{list(assets).map(item => <label key={item.id} className="inline-checkbox"><input type="checkbox" checked={draft.asset_ids?.includes(item.id) || false} onChange={event => update({ asset_ids: event.target.checked ? [...(draft.asset_ids || []), item.id] : draft.asset_ids.filter(id => id !== item.id) })} />{item.name} · {item.kind}</label>)}</div></div>}
        <p>{t('本次应用只会影响 {agent}。', { agent: selectedAgent })}</p><details className="agent-advanced"><summary>{t('高级：允许使用这个组合的助手')}</summary><p>{t('这里仅保存组合的可用范围，不会批量应用。')}</p><p>{t('已选择 {count} 个助手', { count: draft.host_ids?.length || 0 })}</p>{list(hosts).map(item => <label key={item.id} className="inline-checkbox"><input type="checkbox" checked={draft.host_ids?.includes(item.id) || false} onChange={event => update({ host_ids: event.target.checked ? [...(draft.host_ids || []), item.id] : draft.host_ids.filter(id => id !== item.id) })} />{item.name}</label>)}</details>
      </>}
    </>}
  </>;
}
function ProviderChoice({ providers, value, onChange }) { return <Field label={t('供应商')}><select value={value || ''} onChange={event => onChange(event.target.value)}><option value="">{t('未绑定')}</option>{list(providers).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>; }
