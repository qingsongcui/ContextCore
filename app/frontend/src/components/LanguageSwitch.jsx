import { Languages } from 'lucide-react';
import { setLang, useI18n } from '../lib/i18n.jsx';

export function LanguageSwitch({ compact = false }) {
  const { lang } = useI18n();
  return <div className={`language-switch ${compact ? 'language-switch-compact' : ''}`} role="group" aria-label={lang === 'en' ? 'Language' : '语言'}>
    <Languages size={14} aria-hidden="true" />
    <button type="button" lang="zh-CN" aria-pressed={lang === 'zh'} title="中文" onClick={() => setLang('zh')}>中</button>
    <button type="button" lang="en" aria-pressed={lang === 'en'} title="English" onClick={() => setLang('en')}>EN</button>
  </div>;
}
