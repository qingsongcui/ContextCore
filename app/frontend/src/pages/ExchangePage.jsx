import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Download, FileText } from 'lucide-react';
import { Button, ErrorNotice, PageHeader, ResourceState } from '../components/ui.jsx';
import { request } from '../lib/api.js';
import { downloadText } from '../lib/downloadText.js';
import { t } from '../lib/i18n.jsx';
import { useResource } from '../lib/useResource.js';
import '../styles/exchange.css';

const exportPath = '/api/workbench/profile/export';
const newDraft = () => ({ selection: null, deliverable: '', terms: '' });

function selectedAsset(snapshot, selection) {
  if (!selection || snapshot?.revision !== selection.revision) return null;
  const asset = snapshot.assets[selection.index];
  return asset?.title === selection.title && asset.excerpt === selection.excerpt ? asset : null;
}

function briefSections(snapshot, asset, draft) {
  const sections = [];
  if (snapshot.name) sections.push([t('提供方'), snapshot.name]);
  if (asset.excerpt) sections.push([t('能力说明'), asset.excerpt]);
  if (draft.deliverable.trim()) sections.push([t('本次交付'), draft.deliverable.trim()]);
  if (draft.terms.trim()) sections.push([t('合作条件'), draft.terms.trim()]);
  return sections;
}

export default function ExchangePage({ navigate, revision, draftStore }) {
  const resource = useResource(exportPath, revision);
  const snapshot = resource.data;
  const [draft, setDraft] = useState(() => draftStore?.current || newDraft());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState(null);
  const pending = useRef(null);
  const visibleSnapshot = useRef(snapshot);
  useEffect(() => { visibleSnapshot.current = snapshot; }, [snapshot]);

  useEffect(() => () => pending.current?.abort(), []);

  const asset = selectedAsset(snapshot, draft.selection);
  const sections = asset ? briefSections(snapshot, asset, draft) : [];
  const stale = Boolean(snapshot && draft.selection && !asset);
  const toProfile = () => navigate({ page: 'personal', section: 'profile' });
  const toAssets = () => navigate({ page: 'personal', section: 'assets' });

  function updateDraft(next) {
    if (draftStore) draftStore.current = next;
    setDraft(next);
    setNotice('');
    setError(null);
  }

  async function download() {
    if (!asset || !draft.deliverable.trim() || pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setNotice('');
    setError(null);
    try {
      const current = await request(exportPath, { signal: controller.signal, cache: 'no-store' });
      if (controller.signal.aborted) return;
      const currentAsset = selectedAsset(current, draft.selection);
      if (!currentAsset || current.name !== snapshot.name || !selectedAsset(visibleSnapshot.current, draft.selection)) {
        updateDraft({ ...draft, selection: null });
        setNotice('名片已更新。请重新选择能力并核对预览。');
        resource.reload();
        return;
      }
      const body = [currentAsset.title, ...briefSections(current, currentAsset, draft).map(([label, value]) => label + '\n' + value)];
      downloadText('Senrelay-collaboration.txt', body.join('\n\n') + '\n');
      setNotice('已请求导出协作说明。');
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure);
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return <div className="exchange-workspace">
    <PageHeader title={t('协作')} description={t('从已保存的名片中选择一项能力，写清交付内容，导出给合作者。')} />
    <ResourceState resource={resource}>
      {snapshot && (!snapshot.assets.length ? <section className="exchange-empty">
        <div className="exchange-document-icon"><FileText size={26} aria-hidden="true" /></div>
        <h2>{t('从名片中选出一项能力')}</h2>
        <p>{t('先在 Agent 名片中勾选愿意展示的能力或作品并保存，就可以用它起草协作说明。')}</p>
        <div className="toolbar-actions">
          <Button variant="primary" onClick={toProfile}>{t('编辑 Agent 名片')}</Button>
          <Button onClick={toAssets}>{t('整理能力与作品')}</Button>
        </div>
      </section> : <div className="exchange-layout">
        <div className="exchange-editor">
          <div className="exchange-heading">
            <h2>{t('起草协作说明')}</h2>
            <button type="button" onClick={toProfile}>{t('查看名片')} <ArrowUpRight size={14} aria-hidden="true" /></button>
          </div>
          <fieldset className="exchange-fields" disabled={busy}>
            <label>{t('本次提供的能力或作品')}
              <select value={asset ? String(draft.selection.index) : ''} onChange={event => {
                const value = event.target.value;
                const index = Number(value);
                const selected = snapshot.assets[index];
                updateDraft({ ...draft, selection: value === '' ? null : { revision: snapshot.revision, index, title: selected.title, excerpt: selected.excerpt } });
              }}>
                <option value="">{t('从名片中选择')}</option>
                {snapshot.assets.map((entry, index) => <option key={index} value={index}>{entry.title}</option>)}
              </select>
            </label>
            <label>{t('本次交付')}
              <textarea rows={4} maxLength={4000} required placeholder={t('写明对方会收到什么，以及如何验收。')} value={draft.deliverable} onChange={event => updateDraft({ ...draft, deliverable: event.target.value })} />
            </label>
            <label>{t('合作条件（选填）')}
              <textarea rows={3} maxLength={2000} placeholder={t('例如所需资料、协作分工、周期或交换条件。')} value={draft.terms} onChange={event => updateDraft({ ...draft, terms: event.target.value })} />
            </label>
          </fieldset>
        </div>
        <section className="exchange-paper" aria-label={t('协作说明预览')}>
          <div className="exchange-paper-top"><FileText size={16} aria-hidden="true" />{t('协作说明预览')}</div>
          {asset ? <>
            <h2>{asset.title}</h2>
            <dl>{sections.map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
          </> : <p className="exchange-paper-prompt">{t('选择能力后，可以在这里核对对方将看到的内容。')}</p>}
          <div className="exchange-paper-footer">
            <Button variant="primary" icon={Download} data-export-kind="exchange" disabled={!asset || !draft.deliverable.trim() || resource.loading || Boolean(resource.error)} busy={busy} onClick={download}>{t('导出协作说明')}</Button>
            <small>{t('文本文件 · 由你自行发送')}</small>
          </div>
        </section>
      </div>)}
      <p role="status" className="exchange-notice">{notice ? t(notice) : stale ? t('名片已更新。请重新选择能力并核对预览。') : ''}</p>
      <ErrorNotice error={error} />
    </ResourceState>
  </div>;
}
