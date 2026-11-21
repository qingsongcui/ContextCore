import { useEffect, useId, useState } from 'react';
import { ArrowRight, BookOpen, ChevronDown, FileText, History, MessageSquare } from 'lucide-react';
import { Button, ResourceState, formatTime } from '../components/ui.jsx';
import { Pagination } from '../components/Pagination.jsx';
import { t } from '../lib/i18n.jsx';
import { queryString, useResource } from '../lib/useResource.js';
import '../styles/project-workspace.css';

const GROUPS = [
  ['decision', '当前决定'],
  ['background', '项目背景'],
  ['preference', '工作约定'],
  ['lesson', '经验与教训'],
  ['knowledge', '保留的知识'],
  ['personal', '本项目采用的个人约定'],
  ['shared', '跨项目共享的知识'],
  ['other', '其他记忆'],
];
const PAGE_SIZE = 40;

function ReadingEntry({item, onOpen, onSource}) {
  const sources = item.sources || [];
  const heading = item.heading || item.title;
  const body = item.content?.trim();
  return <details className="reader-item reading-entry">
    <summary><span>{heading}</span><ChevronDown size={16} aria-hidden="true"/></summary>
    <div className="reading-evidence">
      {body && body !== heading && !sources.some(source => source.quote === body) && <p>{body}</p>}
      {sources.map((source, index) => {
        const Icon = source.kind === 'document' ? FileText : MessageSquare;
        return <blockquote key={source.kind + String(source.id || index)}>
          {source.quote && <p>{source.quote}</p>}
          <footer><Icon size={14}/>{source.deleted ? t('来源已删除') : onSource && source.id && ['conversation', 'document'].includes(source.kind) ?
            <button type="button" onClick={() => onSource(source)}>{source.title || t('查看来源')}<ArrowRight size={13}/></button> :
            <span>{source.title || t('来源')}</span>}</footer>
        </blockquote>;
      })}
      {item.previous?.length > 0 && <div className="reading-history"><History size={15}/><div>{item.previous.map(old =>
        <button type="button" key={old.id} onClick={() => onOpen(old.id)}>{t('此前采用')} · {old.title}</button>)}</div></div>}
      <div className="reading-actions">
        {item.updated_at && <small>{t('更新于')} {formatTime(item.updated_at)}</small>}
        <Button onClick={() => onOpen(item.id)}>{t('详情与纠正')}<ArrowRight size={14}/></Button>
      </div>
    </div>
  </details>;
}

export function MemoryReader({projectId, revision, onOpen, onManage, onTasks, onAdd, onSource, onConversations}) {
  const [offset, setOffset] = useState(0);
  const prefix = useId();
  const result = useResource('/api/workbench/memories' + queryString({project_id: projectId, offset, limit: PAGE_SIZE}), revision);
  const total = result.data?.total;
  useEffect(() => {
    if (total !== undefined && offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor((total - 1) / PAGE_SIZE) * PAGE_SIZE));
  }, [offset, total]);
  const sections = new Map(GROUPS.map(([key]) => [key, []]));
  for (const item of result.data?.items || []) {
    (sections.get(item.section || item.category) || sections.get('other')).push(item);
  }
  const groups = GROUPS.filter(([key]) => sections.get(key).length);

  return <section className="memory-reader reading-room">
    <div className="reader-heading">
      <div><h2>{projectId ? t('当前工作记忆') : t('我的工作约定')}</h2>
        <p>{projectId ? t('先读现在采用的结论。展开后可以回看依据、查看旧版本或纠正内容。') :
          t('跨项目适用的工作方式。在某个项目有例外时，优先采用项目约定。')}</p></div>
      <div className="toolbar-actions">
        {onTasks && <Button onClick={onTasks}>{t('当前任务')}</Button>}
        {onManage && <Button onClick={onManage}>{t('管理记忆')}</Button>}
        {onAdd && <Button onClick={onAdd}>{t('添加个人约定')}</Button>}
      </div>
    </div>
    <ResourceState resource={result}>
      {!groups.length && <div className="reading-empty"><BookOpen size={23} aria-hidden="true"/>
        <h3>{t('这里还没有生效的记忆。')}</h3>
        {onConversations && <><p>{t('从项目对话里提炼决定和经验，或在管理记忆里手动记录。')}</p><Button onClick={onConversations}>{t('查看原始对话')}<ArrowRight size={15}/></Button></>}
      </div>}
      {groups.length > 0 && <div className={groups.length > 1 ? 'reading-layout has-index' : 'reading-layout'}>
        {groups.length > 1 && <nav className="reading-index" aria-label={t('本页目录')}>
          <h3>{t('本页目录')}</h3>{groups.map(([key, label]) => <button type="button" key={key} onClick={() => {
            const target = document.getElementById(prefix + key);
            target?.focus({preventScroll: true});
            target?.scrollIntoView?.({block: 'start'});
          }}><span>{t(label)}</span><small>{sections.get(key).length}</small></button>)}
        </nav>}
        <div className="reading-sections">{groups.map(([key, label]) => <section className="reader-group reading-chapter" key={key}>
          <h3 id={prefix + key} tabIndex={-1}>{t(label)}</h3>
          {sections.get(key).map(item => <ReadingEntry key={item.id} item={item} onOpen={onOpen} onSource={onSource}/>)}
        </section>)}</div>
      </div>}
      {(offset > 0 || total > PAGE_SIZE) && <Pagination total={total} offset={offset} limit={PAGE_SIZE} loading={result.loading} onChange={setOffset}/>}
    </ResourceState>
  </section>;
}
