import { useEffect } from 'react';
import { t } from '../../lib/i18n.jsx';
import { number } from '../../lib/management.js';
import { Button } from '../ui.jsx';

export function ResourcePagination({ resource, onPage, shared = false, busy = false, label }) {
  const page = resource.data;
  const { summary, offset, total, limit } = page || {};
  const disabled = busy || resource.loading || Boolean(resource.error);
  useEffect(() => {
    if (!summary || disabled || !limit) return;
    const last = Math.max(0, Math.floor((total - 1) / limit) * limit);
    if (offset > last) onPage(last);
  }, [summary, offset, total, limit, disabled, onPage]);
  if (!page?.summary || (page.total <= page.limit && page.offset === 0)) return null;
  return <nav className="agent-resource-pagination" aria-label={label || (shared ? t('共享资料分页') : t('助手资料分页'))}>
    <span>{t('第 {start}–{end} 条，共 {total} 条', {
      start: number(page.items.length ? page.offset + 1 : 0), end: number(page.items.length ? page.offset + page.items.length : 0), total: number(page.total)
    })}</span>
    <div className="toolbar-actions"><Button disabled={disabled || page.offset === 0} onClick={() => onPage(Math.max(0, page.offset - page.limit))}>{t('上一页')}</Button><Button disabled={disabled || !page.has_more} onClick={() => onPage(page.offset + page.limit)}>{t('下一页')}</Button></div>
  </nav>;
}
