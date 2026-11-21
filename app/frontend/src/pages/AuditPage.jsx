import { useState } from 'react';
import { SlidersHorizontal } from 'lucide-react';
import { t } from '../lib/i18n.jsx';
import { queryString, useResource } from '../lib/useResource.js';
import { Badge, Button, EmptyState, Field, PageHeader, ResourceState, formatTime } from '../components/ui.jsx';
import '../styles/operation-preferences.css';

const EMPTY_FILTERS = { actor: '', action: '', since: '', until: '' };
const FILTERS = [
  ['actor', '操作主体', 'text'],
  ['action', '动作', 'text'],
  ['since', '起始日期', 'date'],
  ['until', '截止日期', 'date']
];

export function AuditPage({ revision, embedded = false }) {
  const [draft, setDraft] = useState(EMPTY_FILTERS);
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [filterOpen, setFilterOpen] = useState(false);
  const resource = useResource('/api/audit' + queryString(filters), revision);
  const activeCount = Object.values(filters).filter(Boolean).length;

  function resetFilters() {
    setDraft(EMPTY_FILTERS);
    setFilters(EMPTY_FILTERS);
  }

  return <section className="audit-studio">
    <PageHeader headingLevel={embedded ? 2 : 1} title={t('变更记录')}
      description={t('记录谁改变了什么，不复制整篇聊天或凭据，也不逐次记录普通读取。')}
      actions={<Button icon={SlidersHorizontal} aria-expanded={filterOpen} onClick={() => setFilterOpen(!filterOpen)}>
        {activeCount ? t('筛选条件 · {count}', { count: activeCount }) : t('筛选条件')}
      </Button>} />
    {filterOpen ? <form className="audit-search" onSubmit={event => {
      event.preventDefault();
      setFilters({ ...draft, actor: draft.actor.trim(), action: draft.action.trim() });
    }}>
      <div className="filter-grid audit-filters">{FILTERS.map(([key, label, type]) =>
        <Field label={t(label)} key={key}><input type={type} value={draft[key]}
          min={key === 'until' && draft.since || undefined} max={key === 'since' && draft.until || undefined}
          onChange={event => setDraft({ ...draft, [key]: event.target.value })} /></Field>)}
      </div>
      <div className="audit-filter-actions"><Button type="submit">{t('应用筛选')}</Button>
        {activeCount || Object.values(draft).some(Boolean) ? <Button onClick={resetFilters}>{t('清除筛选')}</Button> : null}
      </div>
    </form> : null}
    <ResourceState resource={resource}>{resource.data?.length ? <>
      <div className="table-scroll">
        <table className="audit-table">
          <thead><tr>{['时间', '主体', '动作', '目标', '结果'].map(label => <th key={label} scope="col">{t(label)}</th>)}</tr></thead>
          <tbody>{resource.data.map(item => <tr key={item.id}>
            <td><time dateTime={item.created_at}>{formatTime(item.created_at)}</time></td>
            <td>{item.actor}</td><td>{item.action}</td><td className="audit-target">{item.target}</td><td><Badge>{item.result}</Badge></td>
          </tr>)}</tbody>
        </table>
      </div>
      {resource.data.length === 100 ? <p className="audit-limit">{t('显示最近 100 条匹配记录，可按日期缩小范围。')}</p> : null}
    </> : <EmptyState title={t('暂无匹配的变更记录')}
      body={activeCount ? t('试试其他条件，或清除筛选查看最近记录。') : t('内容维护、项目整理和接入变更发生后，会在这里留下记录。')}
      action={activeCount ? <Button onClick={resetFilters}>{t('清除筛选')}</Button> : null} />}</ResourceState>
  </section>;
}
