import { t } from "../lib/i18n.jsx";
import { useState } from 'react';
import { queryString, useResource } from '../lib/useResource.js';
import { Badge, EmptyState, Field, PageHeader, ResourceState, formatTime } from '../components/ui.jsx';
export function AuditPage({
  revision
}) {
  const [filters, setFilters] = useState({
    actor: '',
    action: '',
    since: '',
    until: ''
  });
  const resource = useResource('/api/audit' + queryString(filters), revision);
  return <><PageHeader title={t("变更记录")} description={t("记录谁改变了什么，不复制整篇聊天或凭据，也不逐次记录普通读取。")} /><div className="filter-grid audit-filters">{[['actor', t("操作主体"), 'text'], ['action', t("动作"), 'text'], ['since', t("起始日期"), 'date'], ['until', t("截止日期"), 'date']].map(([key, label, type]) => <Field label={label} key={key}><input type={type} value={filters[key]} onChange={event => setFilters({
          ...filters,
          [key]: event.target.value
        })} /></Field>)}</div><ResourceState resource={resource}>{resource.data?.length ? <div className="table-scroll"><table className="audit-table"><thead><tr><th>{t("时间")}</th><th>{t("主体")}</th><th>{t("动作")}</th><th>{t("目标")}</th><th>{t("结果")}</th></tr></thead><tbody>{resource.data.map(item => <tr key={item.id}><td>{formatTime(item.created_at)}</td><td>{item.actor}</td><td>{item.action}</td><td className="audit-target">{item.target}</td><td><Badge>{item.result}</Badge></td></tr>)}</tbody></table></div> : <EmptyState title={t("暂无匹配的变更记录")} body={t("内容维护、项目整理和接入变更发生后，会在这里留下记录。")} />}</ResourceState></>;
}
