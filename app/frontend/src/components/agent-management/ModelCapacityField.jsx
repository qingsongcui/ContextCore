import { t } from '../../lib/i18n.jsx';
import { Field } from '../ui.jsx';

export function capacityIsValid(value, required = false) {
  if (value === '' || value === null || value === undefined) return !required;
  return Number.isSafeInteger(Number(value)) && Number(value) > 0;
}

export function ModelCapacityField({ value, onChange, required = false }) {
  return <Field label={t('上下文窗口')} hint={required ? t('这个助手需要模型的上下文容量，请按服务商说明填写 token 数量。') : t('可留空，不把未知写成零。')}><input type="number" required={required} min="1" max={Number.MAX_SAFE_INTEGER} step="1" value={value ?? ''} onChange={event => onChange(event.target.value)} /></Field>;
}
