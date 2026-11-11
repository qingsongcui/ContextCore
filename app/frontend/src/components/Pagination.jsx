import { t } from '../lib/i18n.jsx';
import { Button } from './ui.jsx';
export function Pagination({ total = 0, offset, limit, loading, onChange, label }) {
  return <div className="management-pagination"><span>{label || t('共 {count} 条', { count: total })}{!label && total > 0 ? ` · ${Math.min(offset + 1, total)}–${Math.min(offset + limit, total)}` : ''}</span><div className="toolbar-actions"><Button disabled={offset === 0 || loading} onClick={() => onChange(Math.max(0, offset - limit))}>{t('上一页')}</Button><Button disabled={loading || offset + limit >= total} onClick={() => onChange(offset + limit)}>{t('下一页')}</Button></div></div>;
}
