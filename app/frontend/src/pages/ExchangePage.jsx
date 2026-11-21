import { useEffect, useRef, useState } from 'react';
import { ArrowUpRight, Download, FileText } from 'lucide-react';
import { Button, ErrorNotice, PageHeader, ResourceState, formatTime } from '../components/ui.jsx';
import { request } from '../lib/api.js';
import { downloadText } from '../lib/downloadText.js';
import { t } from '../lib/i18n.jsx';
import { useResource } from '../lib/useResource.js';
import '../styles/exchange.css';

const exportPath = '/api/workbench/profile/export';
const briefsPath = '/api/workbench/briefs';
const newDraft = () => ({ id: crypto.randomUUID(), revision: 0, selection: null, deliverable: '', terms: '', dirty: false });

function payload(draft) {
  return {
    revision: draft.revision,
    profile_revision: draft.selection?.revision ?? null,
    asset_index: draft.selection?.index ?? null,
    deliverable: draft.deliverable,
    terms: draft.terms,
  };
}

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
  const [offset, setOffset] = useState(0);
  const library = useResource(briefsPath + '?limit=10&offset=' + offset, revision);
  const total = library.data?.total;
  useEffect(() => {
    if (total !== undefined && offset > 0 && offset >= total) setOffset(Math.max(0, Math.floor((total - 1) / 10) * 10));
  }, [offset, total]);
  const [draft, setDraft] = useState(() => draftStore?.current || newDraft());
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState(null);
  const pending = useRef(null);
  const [confirm, setConfirm] = useState(null);
  useEffect(() => {
    if (!draft.dirty) return;
    const warn = event => { event.preventDefault(); event.returnValue = ''; };
    window.addEventListener('beforeunload', warn);
    return () => window.removeEventListener('beforeunload', warn);
  }, [draft.dirty]);
  const visibleSnapshot = useRef(snapshot);
  useEffect(() => { visibleSnapshot.current = snapshot; }, [snapshot]);

  useEffect(() => () => pending.current?.abort(), []);

  const asset = selectedAsset(snapshot, draft.selection);
  const sections = asset ? briefSections(snapshot, asset, draft) : [];
  const stale = Boolean(snapshot && draft.selection && !asset);
  const alreadySaved = draft.revision > 0 || library.data?.items?.some(item => item.id === draft.id);
  const toProfile = () => navigate({ page: 'personal', section: 'profile' });
  const toAssets = () => navigate({ page: 'personal', section: 'assets' });

  function replaceDraft(next) {
    if (draftStore) draftStore.current = next;
    setDraft(next);
    setNotice('');
    setError(null);
  }

  function updateDraft(next) {
    replaceDraft({ ...next, dirty: true });
    setConfirm(null);
  }

  async function perform(work) {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setNotice('');
    setError(null);
    setConfirm(null);
    try {
      await work(controller.signal);
    } catch (failure) {
      if (!controller.signal.aborted) {
        setError(failure);
        if (failure.status === 404 || failure.status === 409) {
          resource.reload();
          library.reload();
        }
      }
    } finally {
      pending.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  function save() {
    return perform(async signal => {
      const value = await request(briefsPath + '/' + draft.id, {
        method: 'PUT', body: JSON.stringify(payload(draft)), signal
      });
      if (signal.aborted) return;
      replaceDraft({ ...value, dirty: false });
      setNotice('协作草稿已保存到本机。');
      setOffset(0);
      library.reload();
    });
  }

  function open(action) {
    if (action.kind === 'new') {
      replaceDraft({...newDraft(), composing: true});
      setConfirm(null);
      return;
    }
    return perform(async signal => {
      const path = briefsPath + '/' + encodeURIComponent(action.id);
      if (action.kind === 'delete') {
        await request(path + '?revision=' + action.revision, {method: 'DELETE', signal});
        if (signal.aborted) return;
        if (action.id === draft.id) replaceDraft(newDraft());
        setNotice('草稿已删除。');
        library.reload();
        return;
      }
      const value = await request(path, {cache: 'no-store', signal});
      if (signal.aborted) return;
      replaceDraft({ ...value, dirty: false });
      resource.reload();
    });
  }

  function chooseAction(action) {
    if (action.kind === 'delete' || draft.dirty) setConfirm(action);
    else open(action);
  }

  function download() {
    if (!asset || !draft.deliverable.trim()) return;
    return perform(async signal => {
      const body = { ...payload(draft), ...(draft.revision ? {id: draft.id} : {}) };
      const receipt = await request(briefsPath + '/export', { method: 'POST', body: JSON.stringify(body), signal });
      if (signal.aborted) return;
      if (receipt.profile_revision !== draft.selection.revision || receipt.title !== asset.title ||
          receipt.excerpt !== asset.excerpt || receipt.provider !== snapshot.name ||
          !selectedAsset(visibleSnapshot.current, draft.selection)) {
        updateDraft({ ...draft, selection: null, selection_stale: true });
        setNotice('名片已更新。请重新选择能力并核对预览。');
        resource.reload();
        return;
      }
      const sections = briefSections({name: receipt.provider}, receipt, receipt);
      downloadText('Senrelay-collaboration.txt', [receipt.title, ...sections.map(([label, value]) => label + '\n' + value)].join('\n\n') + '\n');
      setNotice('已请求导出协作说明。');
    });
  }

  return <div className="exchange-workspace">
    <PageHeader title={t('协作')} description={t('从已保存的名片中选择一项能力，写清交付内容，导出给合作者。')} />
    <ResourceState resource={resource}>
      {snapshot && (!snapshot.assets.length && !draft.revision && !draft.dirty && !draft.composing ? <section className="exchange-empty">
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
                updateDraft({ ...draft, selection_stale: false, selection: value === '' ? null : { revision: snapshot.revision, index, title: selected.title, excerpt: selected.excerpt } });
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
            <div className="toolbar-actions">
              {stale && <Button onClick={() => updateDraft({...draft, selection: null, selection_stale: false})}>{t('移除已失效的选择')}</Button>}
              <Button disabled={!draft.dirty || stale || !(draft.selection || draft.deliverable.trim() || draft.terms.trim())} onClick={save}>{t('保存协作草稿')}</Button>
              {draft.dirty && <small>{t('有未保存的编辑')}</small>}
            </div>
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
      <p role="status" className="exchange-notice">{notice ? t(notice) : stale || draft.selection_stale ? t('名片已更新。请重新选择能力并核对预览。') : ''}</p>
    </ResourceState>
    <ErrorNotice error={error} />
    {error && [404,409].includes(error.status) && alreadySaved && <div className="toolbar-actions">
      <Button onClick={() => chooseAction({kind: 'open', id: draft.id})}>{t('重新读取已保存草稿')}</Button>
      <Button onClick={() => updateDraft({...draft, id: crypto.randomUUID(), revision: 0})}>{t('将当前文字另存为新草稿')}</Button>
    </div>}
    <section className="exchange-library" aria-label={t('本机草稿')}>
      <div className="exchange-heading"><h2>{t('本机草稿')}</h2>
        <Button disabled={busy} onClick={() => chooseAction({kind: 'new'})}>{t('新草稿')}</Button>
      </div>
      <ResourceState resource={library}>
        {library.data?.items?.length === 0 && <p>{t('保存后的协作说明会出现在这里。')}</p>}
        {(library.data?.items || []).map(item => <div className="exchange-draft-row" key={item.id}>
          <button className="exchange-draft-open" disabled={busy} aria-current={draft.id === item.id ? 'true' : undefined}
            onClick={() => chooseAction({kind: 'open', id: item.id})}>
            <strong>{item.deliverable || t('协作草稿')}</strong>
            <small>{formatTime(item.updated_at)}</small>
          </button>
          <Button disabled={busy} onClick={() => chooseAction({kind: 'delete', id: item.id, revision: (draft.id === item.id && draft.revision) || item.revision})}>{t('删除草稿')}</Button>
        </div>)}
        {(offset > 0 || total > 10) && <div className="toolbar-actions">
          <Button disabled={busy || !offset} onClick={() => setOffset(offset - 10)}>{t('上一页')}</Button>
          <Button disabled={busy || offset + 10 >= total} onClick={() => setOffset(offset + 10)}>{t('下一页')}</Button>
        </div>}
      </ResourceState>
      {confirm && <div role="alert" className="toolbar-actions exchange-confirm">
        <p>{confirm.kind === 'delete' ? t('永久删除这份草稿？') : t('放弃当前未保存的修改？')}</p>
        <Button onClick={() => open(confirm)}>{confirm.kind === 'delete' ? t('确认删除') : t('放弃修改')}</Button>
        <Button onClick={() => setConfirm(null)}>{t('取消')}</Button>
      </div>}
    </section>
  </div>;
}
