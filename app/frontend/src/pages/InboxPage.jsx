import { t } from "../lib/i18n.jsx";
import { useState } from 'react';
import { Check, CircleDot, GitCompare } from 'lucide-react';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { Badge, Button, EmptyState, ErrorNotice, Field, Modal, PageHeader, ResourceState, formatTime } from '../components/ui.jsx';
import { Markdown } from '../components/Markdown.jsx';
const RESOLUTIONS = {
  keep: '保留原结论',
  replace: '采用新结论',
  merge: '融合编辑后采纳',
  both: '两种说法适用范围不同，都保留'
};
function computeLineDiff(oldText = '', newText = '') {
  const oldLines = String(oldText || '').split('\n');
  const newLines = String(newText || '').split('\n');
  const oldSet = new Set(oldLines.map(l => l.trim()).filter(Boolean));
  const newSet = new Set(newLines.map(l => l.trim()).filter(Boolean));
  const ops = [];
  for (const line of oldLines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (!newSet.has(trimmed)) ops.push({ type: 'removed', text: line });
    else ops.push({ type: 'unchanged', text: line });
  }
  for (const line of newLines) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    if (!oldSet.has(trimmed)) ops.push({ type: 'added', text: line });
  }
  return ops;
}
function ConflictItem({
  item,
  revision,
  onChoose
}) {
  const existing = useResource(item.existing_id ? `/api/content/${item.existing_id}` : null, revision);
  const [showDiff, setShowDiff] = useState(false);
  const unavailable = !existing.data || existing.loading || !!existing.error;
  const diffOps = existing.data ? computeLineDiff(existing.data.content, item.proposed?.content) : [];
  return <article className="conflict-panel"><header><div><div className="meta-line"><Badge>{item.status === 'resolved' ? t("已处理") : t("需要你的判断")}</Badge>{existing.data ? <Button icon={GitCompare} onClick={() => setShowDiff(v => !v)}>{showDiff ? t("并排视图") : t("差异对比 (Diff)")}</Button> : null}</div><h2>{item.proposed?.title || t("有一条记忆的更新，需要你确认")}</h2><p>{item.reason}</p></div><time>{formatTime(item.created_at)}</time></header>
    {showDiff && existing.data ? <div className="conflict-diff-box" aria-label={t("差异对比")}>{diffOps.length ? diffOps.map((op, idx) => <div key={idx} className={`diff-line diff-${op.type}`}><span className="diff-marker">{op.type === 'added' ? '+' : op.type === 'removed' ? '−' : ' '}</span><span>{op.text}</span></div>) : <p className="muted">{t("两份内容行级文本一致。")}</p>}</div> : <div className="conflict-comparison"><section><h3>{item.status === 'resolved' ? t("当前内容") : t("已有内容")}</h3><ResourceState resource={existing}>{existing.data ? <><small>{t("版本")} {existing.data.version}</small><Markdown>{existing.data.content}</Markdown></> : <p>{t("原内容看不到了。先核对来源，再决定怎么处理。")}</p>}</ResourceState></section><section><h3>{t("提出的更新")}</h3><Markdown>{item.proposed?.content || t("没有可显示的新内容")}</Markdown></section></div>}
    {item.status === 'resolved' ? <p className="resolution"><Check size={16} />{t("已按你的选择处理：{resolution}", { resolution: t(RESOLUTIONS[item.resolution] || item.resolution) })}</p> : <footer>{Object.entries(RESOLUTIONS).map(([resolution, label]) => <Button key={resolution} variant={resolution === 'replace' ? 'primary' : 'secondary'} disabled={unavailable} onClick={() => onChoose({
        item,
        resolution,
        version: existing.data.version,
        title: existing.data.title,
        content: existing.data.content
      })}>{t(label)}</Button>)}</footer>}
  </article>;
}
function ConflictConfirmation({
  selection,
  revision,
  onClose,
  onChanged
}) {
  const conflict = useResource(`/api/conflicts/${selection.item.id}`, revision);
  const existing = useResource(`/api/content/${selection.item.existing_id}`, revision);
  const [mergedTitle, setMergedTitle] = useState(() => selection.item.proposed?.title || selection.title || '');
  const [mergedContent, setMergedContent] = useState(() => {
    const oldBody = (selection.content || '').trim();
    const newBody = (selection.item.proposed?.content || '').trim();
    return oldBody && newBody && oldBody !== newBody ? `${oldBody}\n\n${newBody}` : (newBody || oldBody);
  });
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const resolved = conflict.data?.status === 'resolved';
  const stale = existing.data && existing.data.version !== selection.version;
  const unavailable = !conflict.data || !existing.data || conflict.loading || existing.loading || !!conflict.error || !!existing.error;
  async function resolve() {
    if (busy || unavailable || stale || resolved) return;
    setBusy(true);
    setError(null);
    try {
      const payload = {
        resolution: selection.resolution,
        expected_version: selection.version,
        ...(selection.resolution === 'merge' ? {
          merged_title: mergedTitle.trim(),
          merged_content: mergedContent.trim()
        } : {})
      };
      const result = await mutate(`/api/conflicts/${selection.item.id}/resolve`, 'POST', payload);
      onChanged(t("已按你的选择处理：{resolution}", { resolution: t(RESOLUTIONS[result.resolution] || result.resolution) }));
      onClose();
    } catch (failure) {
      setError(failure);
      conflict.reload();
      existing.reload();
      onChanged();
    } finally {
      setBusy(false);
    }
  }
  return <Modal title={resolved ? t("这项冲突已处理") : t("确认你的判断")} busy={busy} onClose={onClose} wide><div className="editor-form">
    <p>「{selection.item.proposed?.title}」 · {t(RESOLUTIONS[selection.resolution])}</p>
    {resolved ? <p className="operation-notice" role="status">{t("已经在别处处理过了：{resolution}。这次不会再提交一遍。", { resolution: t(RESOLUTIONS[conflict.data.resolution] || conflict.data.resolution) })}</p> : <>
      <p className="muted">{selection.resolution === 'replace' ? t("新结论将生效，原结论保留为历史。") : selection.resolution === 'merge' ? t("你可以结合新旧两条结论编辑出一份最准确的融合版本，原版本保留为历史。") : selection.resolution === 'keep' ? t("保留原结论，不采用这次更新。") : t("确认两份内容适用范围不同，需要同时保留为有效内容。")}{t("结果由网页与 Agent 共享。")}</p>
      <div className="conflict-comparison"><section><h3>{t("你确认的是版本")} {selection.version}</h3><Markdown>{selection.content}</Markdown></section><section><h3>{t("提出的更新")}</h3><Markdown>{selection.item.proposed?.content}</Markdown></section></div>
      {selection.resolution === 'merge' ? <div className="editor-form"><Field label={t("融合后的标题")}><input required value={mergedTitle} onChange={event => setMergedTitle(event.target.value)} /></Field><Field label={t("融合后的正文")}><textarea required rows={7} value={mergedContent} onChange={event => setMergedContent(event.target.value)} /></Field></div> : null}
      {stale ? <p className="inline-error" role="alert">{t("这条内容已经更新到版本 {version}。请关掉窗口，重新比较后再选。", { version: existing.data.version })}</p> : null}
    </>}
    <ErrorNotice error={error || conflict.error || existing.error} />
    {conflict.error || existing.error ? <Button onClick={() => {
        conflict.reload();
        existing.reload();
      }}>{t("重新读取状态")}</Button> : null}
    <footer className="modal-actions"><Button onClick={onClose}>{resolved || stale ? t("返回查看最新内容") : t("取消")}</Button>{!resolved ? <Button variant="primary" busy={busy} disabled={unavailable || stale || (selection.resolution === 'merge' && (!mergedTitle.trim() || !mergedContent.trim())) || [404, 409, 410].includes(error?.status)} onClick={resolve}>{t("确认并保存")}</Button> : null}</footer>
  </div></Modal>;
}
export function InboxPage({
  revision,
  onChanged
}) {
  const [showResolved, setShowResolved] = useState(false);
  const [selection, setSelection] = useState(null);
  const [notice, setNotice] = useState('');
  const resource = useResource('/api/conflicts', revision);
  const rows = (resource.data || []).filter(item => showResolved || item.status === 'pending');
  function changed(message) {
    if (message) setNotice(message);
    resource.reload();
    onChanged();
  }
  return <><PageHeader title={t("待确认")} description={t("普通记忆会自动生效。这里只处理真的拿不准的冲突，平时不用来批东西。")} actions={<label className="check-label"><input type="checkbox" checked={showResolved} onChange={event => setShowResolved(event.target.checked)} />{t("包含已处理")}</label>} />
    {notice ? <p className="operation-notice" role="status">{notice}</p> : null}
    <ResourceState resource={resource}>{rows.length ? <div className="conflicts-list">{rows.map(item => <ConflictItem key={item.id} item={item} revision={revision} onChoose={setSelection} />)}</div> : <EmptyState title={showResolved ? t("还没有冲突记录") : t("没有等待处理的冲突")} body={t("目前不需要你做判断。你可以返回项目继续工作；只有记忆更新存在分歧时，才会出现在这里。")} action={<span className="quiet-confirm"><CircleDot size={15} /> {t("不打扰，也不擅自决定")}</span>} />}</ResourceState>
    {selection ? <ConflictConfirmation selection={selection} revision={revision} onClose={() => setSelection(null)} onChanged={changed} /> : null}
  </>;
}
