import { useState } from 'react';
import { Button, ErrorNotice } from './ui.jsx';
import { mutate } from '../lib/api.js';
import { downloadText } from '../lib/downloadText.js';
import { t } from '../lib/i18n.jsx';

export function MethodLibraryActions({ asset, revision, dirty, navigate, onChanged }) {
  const [review, setReview] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [savedId, setSavedId] = useState(null);
  const ready = !dirty && [asset.inputs, asset.steps, asset.outputs].every(text => text?.trim());
  const selection = {asset_id: asset.id, profile_revision: revision};
  async function preview(download = false) {
    setBusy(true);
    setError(null);
    setSavedId(null);
    try {
      const next = await mutate('/api/workbench/methods/preview', 'POST', selection);
      if (download) downloadText('SKILL.md', next.content);
      else setReview(next);
    } catch (failure) {
      setReview(null);
      setError(failure);
    } finally { setBusy(false); }
  }
  async function publishToLibrary() {
    setBusy(true);
    setError(null);
    try {
      const saved = await mutate('/api/workbench/methods/catalog', 'POST', {...selection, review_token: review.review_token});
      setSavedId(saved.id);
      setReview(null);
      onChanged?.();
    } catch (failure) {
      setReview(null);
      setError(failure);
    } finally { setBusy(false); }
  }
  const openLibrary = () => navigate({page: 'agents', agent: null, section: 'library', view: 'skill', record: savedId || review?.catalog_id});
  return <div className="method-library-actions">
    <div className="toolbar-actions">
      <Button data-export-kind="skill" disabled={!ready || busy} onClick={() => preview(true)}>{t('导出私有 Skill 草稿')}</Button>
      <Button disabled={!ready || busy} onClick={() => preview()}>{t('预览全局 Skill')}</Button>
    </div>
    {error && <ErrorNotice error={error} />}
    {review && ready && <section aria-label={t('预览全局 Skill')}>
      <p>{t('核对将写入资源库的 SKILL.md。分发给哪些 Agent 可以在保存后选择。')}</p>
      {review.state === 'update' && <details><summary>{t('对照资源库中的现有版本')}</summary><pre>{review.previous}</pre></details>}
      <pre>{review.content}</pre>
      <div className="toolbar-actions">
        {review.state === 'current' ? <Button onClick={openLibrary}>{t('打开全局资源库')}</Button>
          : <Button variant="primary" disabled={busy} onClick={publishToLibrary}>{review.state === 'update' ? t('更新资源库中的 Skill') : t('加入全局资源库')}</Button>}
        <Button disabled={busy} onClick={() => setReview(null)}>{t('关闭')}</Button>
      </div>
    </section>}
    {savedId && <div role="status">{t('Skill 已保存到全局资源库。')} <Button onClick={openLibrary}>{t('打开全局资源库')}</Button></div>}
  </div>;
}
