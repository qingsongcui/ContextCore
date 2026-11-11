import { t } from '../../lib/i18n.jsx';
import { Field } from '../ui.jsx';

export function apiDefaults(protocol, agentId) {
  const wire_api = protocol === 'openai' ? ['codex', 'codex-desktop', 'grok-build'].includes(agentId) ? 'responses' : 'chat' : null;
  return { protocol, wire_api, supported_endpoints: protocol === 'openai' ? [wire_api === 'responses' ? 'responses' : 'chat/completions'] : protocol === 'anthropic' ? ['messages'] : ['generateContent'] };
}

export function presetApi(preset, agentId) {
  const defaults = apiDefaults(preset.protocol, agentId);
  const endpoints = preset.supported_endpoints?.length ? preset.supported_endpoints : defaults.supported_endpoints;
  const preferred = defaults.wire_api === 'responses' ? 'responses' : 'chat/completions';
  return { ...defaults, supported_endpoints: endpoints, ...(preset.protocol === 'openai' ? { wire_api: endpoints.includes(preferred) ? defaults.wire_api : endpoints.includes('responses') ? 'responses' : 'chat' } : {}) };
}

export function customApi(value, base_url) {
  return { base_url, supported_endpoints: value.protocol === 'openai' ? value.wire_api ? [value.wire_api === 'responses' ? 'responses' : 'chat/completions'] : null : apiDefaults(value.protocol).supported_endpoints };
}

export function apiLabel(provider) {
  if (provider.protocol === 'anthropic') return 'Anthropic Messages';
  if (provider.protocol === 'gemini') return 'Gemini generateContent';
  if (provider.wire_api === 'responses') return 'OpenAI Responses';
  if (provider.wire_api === 'chat') return 'OpenAI Chat Completions';
  return t('接口格式尚未指定');
}

export function ProviderApiFields({ value, onChange }) {
  if (value.protocol !== 'openai') return <p>{t('接口格式')} · {apiLabel(value)}</p>;
  const both = ['responses', 'chat/completions'].every(endpoint => value.supported_endpoints?.includes(endpoint));
  return <>
    <Field label={t('OpenAI 接口格式')} hint={t('Responses 与 Chat Completions 不一定通用。自定义服务请按对方文档选择；保存不会发起请求。')}><select value={value.wire_api || ''} onChange={event => {
      const wire_api = event.target.value;
      onChange({ wire_api: wire_api || null, supported_endpoints: wire_api ? both ? ['chat/completions', 'responses'] : [wire_api === 'responses' ? 'responses' : 'chat/completions'] : null });
    }}><option value="">{t('沿用旧定义（未指定）')}</option><option value="responses">OpenAI Responses</option><option value="chat">OpenAI Chat Completions</option></select></Field>
    <label className="inline-checkbox"><input type="checkbox" disabled={!value.wire_api} checked={both} onChange={event => onChange({ supported_endpoints: event.target.checked ? ['chat/completions', 'responses'] : [value.wire_api === 'responses' ? 'responses' : 'chat/completions'] })} />{t('服务商文档明确支持两种 OpenAI 接口')}</label>
  </>;
}

export function directApiMismatch(agentId, provider) {
  const required = ['codex', 'codex-desktop'].includes(agentId) ? 'responses' : agentId === 'claude-code' ? 'messages' : null;
  if (!required) return false;
  if ((required === 'responses' && provider.protocol !== 'openai') || (required === 'messages' && provider.protocol !== 'anthropic')) return true;
  const declared = provider.supported_endpoints || (provider.wire_api ? [provider.wire_api === 'responses' ? 'responses' : 'chat/completions'] : null);
  return declared ? !declared.includes(required) : false;
}
