import { useRef, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { Button, ErrorNotice, Field, Modal } from '../ui.jsx';
import { apiDefaults, presetApi, customApi, ProviderApiFields } from './ProviderApiFields.jsx';
import { capacityIsValid, ModelCapacityField } from './ModelCapacityField.jsx';

/** Secrets live only in this form; never in the cross-page draft store. */
function initialDraft(existing, protocols, agentId) {
  if (!existing) return { name: '', base_url: '', ...apiDefaults(protocols[0] || 'openai', agentId), model_id: '', context_window: '', secret: '' };
  const model = existing.models[0];
  return { name: existing.provider.name || '', base_url: existing.provider.base_url || '', protocol: existing.provider.protocol, wire_api: existing.provider.wire_api ?? null, supported_endpoints: existing.provider.supported_endpoints ?? null, model_entity_id: model.id, expected_model_version: model.version, model_id: model.model_id || '', context_window: model.context_window ?? '', secret: '' };
}

export function ProviderSetup({ agent, agentId, presets = [], protocols = ['openai', 'anthropic', 'gemini'], existing = null, busy, error, onSave, onClose }) {
  const [draft, setDraft] = useState(() => initialDraft(existing, protocols, agentId));
  const submitted = useRef(null);
  const uncertain = Boolean(submitted.current && error && ![400, 401, 403, 409, 422].includes(error.status));
  const frozen = busy || uncertain;
  const preset = presets.find(item => item.base_url === draft.base_url && item.protocol === draft.protocol);
  const stored = existing?.provider;
  const accountCredential = Boolean(stored) && (stored.credential_source === 'account' || (!stored.credential_source && stored.account_id && !stored.credential_ref && !stored.has_credential));
  function update(patch) { if (!frozen) { submitted.current = null; setDraft(value => ({ ...value, ...patch })); } }
  function submit(event) {
    event.preventDefault();
    if (busy) return;
    if (!submitted.current) submitted.current = existing ? {
      request_id: crypto.randomUUID(), host_id: agentId, provider_id: existing.provider.id, model_entity_id: draft.model_entity_id,
      expected_provider_version: existing.provider.version, expected_model_version: draft.expected_model_version,
      provider: { name: draft.name.trim(), base_url: draft.base_url.trim(), protocol: draft.protocol, wire_api: draft.wire_api ?? null, supported_endpoints: draft.supported_endpoints ?? null },
      model_id: draft.model_id.trim(), ...(draft.context_window ? { context_window: Number(draft.context_window) } : {}), ...(!accountCredential && draft.secret ? { secret: draft.secret } : {})
    } : {
      request_id: crypto.randomUUID(), host_id: agentId,
      provider: { name: draft.name.trim(), base_url: draft.base_url.trim(), protocol: draft.protocol, ...(draft.wire_api ? { wire_api: draft.wire_api } : {}), ...(draft.supported_endpoints ? { supported_endpoints: draft.supported_endpoints } : {}) },
      model_id: draft.model_id.trim(), ...(draft.context_window ? { context_window: Number(draft.context_window) } : {}), ...(draft.secret ? { secret: draft.secret } : {})
    };
    onSave(submitted.current);
  }
  return <Modal wide title={existing ? t('编辑 {agent} 的连接与模型', { agent }) : t('为 {agent} 添加服务商', { agent })} busy={busy} onClose={onClose}>
    <form className="management-form agent-form" onSubmit={submit}>
      <ol className="agent-setup-steps" aria-label={t('添加服务商的步骤')}><li aria-current="step">{t('填写连接')}</li><li>{t('选择模型')}</li><li>{t('确认写入助手')}</li></ol>
      <p>{existing ? t('修改会一起保存服务地址和这个模型。保存后若此助手支持直接应用，会立刻打开更改预览；确认前不改助手文件，也不发起模型调用。') : t('把服务地址、密钥和模型一次填完。保存后会打开写入预览，确认前不会改助手文件。')}</p>
      <fieldset disabled={frozen} className="agent-import-fields">
        {presets.filter(item => protocols.includes(item.protocol)).length > 0 ? <div className="cc-preset-pills toolbar-actions" aria-label={t('常用服务商预设')}>{presets.filter(item => protocols.includes(item.protocol)).map(item => <Button key={item.id} variant={preset?.id === item.id ? 'primary' : 'secondary'} disabled={frozen} onClick={() => update({ base_url: item.base_url, ...presetApi(item, agentId), name: !draft.name || draft.name === preset?.name ? item.name : draft.name })}>{item.name}</Button>)}</div> : null}
        <Field label={t('选择服务商')}><select value={preset?.id || ''} onChange={event => {
          const next = presets.find(item => item.id === event.target.value);
          update(next ? { base_url: next.base_url, ...presetApi(next, agentId), name: !draft.name || draft.name === preset?.name ? next.name : draft.name } : { base_url: '' });
        }}><option value="">{t('自定义或选择已知服务商')}</option>{presets.filter(item => protocols.includes(item.protocol)).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
        <Field label={t('名称')}><input required autoFocus maxLength={200} value={draft.name} onChange={event => update({ name: event.target.value })} placeholder={t('例如：我的常用模型服务')} /></Field>
        <Field label={t('服务地址')} hint={t('使用服务商提供的 API 地址，预设会自动填写。')}><input type="url" required value={draft.base_url} onChange={event => update(existing ? { base_url: event.target.value } : customApi(draft, event.target.value))} placeholder="https://api.example.com/v1" /></Field>
        {accountCredential ? <p>{t('这个服务商使用绑定账号的凭据。请到账号授权页修改，不要在这里另存一把密钥。')}</p> : <Field label={t('API Key（可选）')} hint={existing ? t('留空表示继续使用已保存的密钥。填写新密钥才会替换；不会读取旧密钥。') : t('密钥单独存入系统钥匙串。已有官方订阅请在助手内登录，或到账号授权页管理。')}><input type="password" maxLength={16384} value={draft.secret} autoComplete="new-password" onChange={event => update({ secret: event.target.value })} /></Field>}
        {existing ? existing.models.length > 1 ? <Field label={t('要一起保存的模型')}><select value={draft.model_entity_id} onChange={event => {
          const model = existing.models.find(item => item.id === event.target.value);
          if (model) update({ model_entity_id: model.id, expected_model_version: model.version, model_id: model.model_id || '', context_window: model.context_window ?? '' });
        }}>{existing.models.map(model => <option key={model.id} value={model.id}>{model.name || model.model_id}</option>)}</select></Field> : <p>{t('要一起保存的模型')}{`: ${existing.models[0].name || existing.models[0].model_id}`}</p> : null}
        <Field label={t('默认请求模型')} hint={t('填写服务商提供的模型名称；保存时不会发起模型调用。')}><input required maxLength={200} value={draft.model_id} onChange={event => update({ model_id: event.target.value })} placeholder={preset?.default_model || t('服务商文档中的模型名称')} /></Field>
        {agentId === 'kimi-code' ? <ModelCapacityField required value={draft.context_window} onChange={context_window => update({ context_window })} /> : null}
        <details className="agent-advanced"><summary>{t('高级：API 协议')}</summary><Field label={t('协议')}><select value={draft.protocol} onChange={event => update(apiDefaults(event.target.value, agentId))}>{protocols.map(protocol => <option key={protocol} value={protocol}>{({ openai: 'OpenAI', anthropic: 'Anthropic', gemini: 'Gemini' })[protocol]}</option>)}</select></Field><ProviderApiFields value={draft} onChange={update} /><p>{t('这里只列出可直接使用的协议；跨协议转发请到本地路由设置。')}</p></details>
      </fieldset>
      {uncertain ? <><p role="status">{existing ? t('保存结果尚未确认。重试会核对同一笔修改，不会另建服务商。') : t('保存结果尚未确认。重试会核对同一笔保存，不会重复新建服务商。')}</p><p>{existing ? t('取消只关闭窗口。如果修改其实已经完成，列表会显示新内容；取消不会撤回这次修改。') : t('取消只关闭窗口。如果保存其实已经完成，服务商会留在列表里；取消不会删除它。')}</p></> : null}
      <ErrorNotice error={error} />
      <footer className="modal-actions"><Button disabled={busy} onClick={onClose}>{t('取消')}</Button><Button type="submit" variant="primary" busy={busy} disabled={!draft.name.trim() || !draft.base_url.trim() || !draft.model_id.trim() || !capacityIsValid(draft.context_window, agentId === 'kimi-code')}>{uncertain ? t('核对并重试保存') : existing ? t('保存连接与模型的修改') : t('保存连接与模型')}</Button></footer>
    </form>
  </Modal>;
}
