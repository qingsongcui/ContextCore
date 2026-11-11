import { t, locale } from './i18n.jsx';

export const number = (value, options = {}) => value === null || value === undefined || value === '' || typeof value === 'boolean' || !Number.isFinite(Number(value)) ? t('未知') : new Intl.NumberFormat(locale(), options).format(Number(value));
export const list = value => Array.isArray(value) ? value : value?.items || [];
export function display(value) {
  if (value === null || value === undefined || value === '') return t('未知');
  if (typeof value === 'boolean') return value ? t('是') : t('否');
  if (typeof value === 'object') return JSON.stringify(value);
  return String(value);
}

export function hostNotice(hostId, kind, fallback) {
  switch (`${hostId}:${kind}`) {
    case 'omp:configuration':
      return t('这里只修改 OMP 默认配置；项目、命令行和当前会话的选择可能优先，写入不会切换正在运行的会话。');
    case 'omp:prompt':
      return t('使用规则会在 OMP 新会话启动时读取；任务子 Agent 不读取这份规则，也不会逐轮自动注入。');
    case 'cursor:prompt':
      return t('使用规则会安装为需要你手动调用的技能；不会改动 Cursor 的全局用户规则。');
    case 'cursor:provider':
      return t('请到 Cursor 设置的 Models 页面管理密钥并选择模型。这里可以保存和导出定义，但不会改写它的私人配置数据库。');
    case 'antigravity:provider':
      return t('请在 Antigravity 对话的模型菜单选择模型。它没有公开的自定义 API 配置接口，这里不会改写或借用账号缓存。');
    case 'codex:configuration':
    case 'codex-desktop:configuration':
      return t('这些配置文件与 Codex CLI 共用，会影响后续配置读取；写入不能证明桌面会话已经采用。');
    case 'codex:prompt':
    case 'codex-desktop:prompt':
      return t('使用规则写入 Codex 用户级 AGENTS.md；自定义目录或覆盖规则可能优先，桌面读取效果尚未验证。');
    default:
      return fallback;
  }
}

export function readSavedValue(key, fallback) {
  try {
    const value = sessionStorage.getItem(`senrelay.${key}`);
    return value === null ? fallback : JSON.parse(value);
  } catch { return fallback; }
}
export function saveValue(key, value) {
  try { sessionStorage.setItem(`senrelay.${key}`, JSON.stringify(value)); } catch { /* Private mode can deny storage. */ }
}

// Diagnostics are not a transcript viewer. Credential-bearing free text is
// withheld as a whole: partial replacements cannot parse every provider's
// nested JSON, shell command, URL or multiline secret format safely.
const SENSITIVE_STRING_RE = /(?:bearer\s+\S+|-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|secret|password|authorization|credential)\s*["']?\s*[:=]\s*(?!null\b|false\b|true\b|"\[redacted\]"|'\[redacted\]')\S+|https?:\/\/[^/\s:@]+:[^/\s@]+@|[?&](?:code|token|access_token|refresh_token|api_key|client_secret)=[^&\s]+|\b(?:sk|ghp|github_pat|xox[baprs])[-_][A-Za-z0-9_-]{12,})/i;
const SENSITIVE_KEY_RE = /(?:^|[_-])(api[_-]?key|secret|access[_-]?token|refresh[_-]?token|authorization|authorization_url|password|client[_-]?secret|code|token)$/i;
const COMPAT_STRING_MAP = {
  'cc-switch': 'local-config',
  'cc-switch-migration': 'local-config',
  'cc-switch-sqlite': 'sqlite',
  'cc-switch-json': 'json',
  'cc-switch.db': 'local-config.db',
};

export function redactEvidence(value) {
  if (typeof value === 'string') return SENSITIVE_STRING_RE.test(value) ? '[redacted]' : value;
  if (Array.isArray(value)) return value.map(redactEvidence);
  if (!value || typeof value !== 'object') return value;
  return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, SENSITIVE_KEY_RE.test(key) ? '[redacted]' : redactEvidence(item)]));
}

export function safeEvidence(value) {
  if (typeof value === 'string') {
    if (SENSITIVE_STRING_RE.test(value)) return '[redacted]';
    return Object.prototype.hasOwnProperty.call(COMPAT_STRING_MAP, value) ? COMPAT_STRING_MAP[value] : value;
  }
  if (Array.isArray(value)) return value.map(safeEvidence);
  if (!value || typeof value !== 'object') return value;
  const entries = [];
  for (const [key, item] of Object.entries(value)) {
    if (key === 'reference_commit') continue;
    if (key === 'cc_switch_current_hosts' && Object.prototype.hasOwnProperty.call(value, 'local_active_hosts')) continue;
    const mappedKey = key === 'cc_switch_current_hosts' ? 'local_active_hosts' : key;
    entries.push([mappedKey, SENSITIVE_KEY_RE.test(mappedKey) ? '[redacted]' : safeEvidence(item)]);
  }
  return Object.fromEntries(entries);
}

export function usageDateFilters(filters) {
  const result = { ...filters };
  for (const key of ['start', 'end']) {
    if (!filters[key]) continue;
    const date = new Date(`${filters[key]}T00:00:00`);
    if (!Number.isFinite(date.getTime())) { delete result[key]; continue; }
    if (key === 'end') date.setDate(date.getDate() + 1);
    result[key] = date.toISOString();
  }
  return result;
}

export function configurationDiffEntries(diff) {
  if (typeof diff !== 'string') return [];
  return diff.split('\n').filter(line => line.startsWith('+') && !line.startsWith('+++')).flatMap(line => {
    try {
      const item = JSON.parse(line.slice(1));
      return item && typeof item === 'object' && typeof item.operation === 'string' && Object.prototype.hasOwnProperty.call(item, 'value') ? [safeEvidence(item)] : [];
    } catch { return []; }
  });
}

export function backendCompatibility(resource) {
  if (!resource) return 'checking';
  if ([404, 405, 410].includes(resource.error?.status)) return 'upgrade_required';
  if (resource.error) return 'unavailable';
  const value = resource.data;
  if (value && typeof value === 'object' && !Array.isArray(value) && value.capabilities && typeof value.capabilities === 'object' && value.counts && typeof value.counts === 'object') return 'ready';
  if (resource.loading) return 'checking';
  return 'upgrade_required';
}
