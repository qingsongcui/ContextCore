import { useEffect, useRef, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { mutate } from '../../lib/api.js';
import { useResource } from '../../lib/useResource.js';
import { Button, ErrorNotice, Modal, ResourceState } from '../ui.jsx';
import { DefinitionFields } from './DefinitionFields.jsx';

const resourcePath = '/api/manage/assets';
const kinds = { mcp: 'MCP', skill: 'Skill', prompt: '使用规则' };

function initialDraft(kind) {
  const fields = kind === 'mcp' ? { config: { transport: 'http', url: '' } }
    : kind === 'skill' ? { install_mode: 'copy', files: { 'SKILL.md': '' } } : { content: '' };
  return { ...fields, kind, name: '', source: { type: 'manual' }, agent_ids: [] };
}

function AssetForm({ kind, original, onReload, onSaved, onClose }) {
  const [draft, setDraft] = useState(() => original ? structuredClone(original) : initialDraft(kind));
  const [valid, setValid] = useState(true);
  const [deleting, setDeleting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState(null);
  const active = useRef(true);
  const pending = useRef(false);
  useEffect(() => {
    active.current = true;
    return () => { active.current = false; };
  }, []);

  async function write(event) {
    event.preventDefault();
    if (pending.current || (!deleting && (!valid || !draft.name?.trim()))) return;
    pending.current = true;
    setBusy(true);
    setFailure(null);
    try {
      const path = original ? resourcePath + '/' + encodeURIComponent(original.id) : resourcePath;
      let saved = null;
      if (deleting) {
        await mutate(path + '?expected_version=' + original.version, 'DELETE');
      } else {
        const body = { ...draft, kind };
        for (const key of ['id', 'version', 'created_at', 'updated_at', '_summary', 'has_credential', 'credential_ref', 'quota', 'expected_version']) delete body[key];
        if (original) body.expected_version = original.version;
        // Assignment is managed separately in the distribution flow. An unscoped
        // legacy record keeps its original scope; a new definition starts unassigned.
        if (original && !Array.isArray(original.agent_ids)) delete body.agent_ids;
        else body.agent_ids = original?.agent_ids ?? [];
        saved = await mutate(path, original ? 'PATCH' : 'POST', body);
      }
      if (active.current) onSaved(saved);
    } catch (error) {
      if (active.current) setFailure(error);
    } finally {
      pending.current = false;
      if (active.current) setBusy(false);
    }
  }

  const kindLabel = kind === 'prompt' ? t('使用规则') : kinds[kind];
  const title = deleting ? t('删除共享定义') : original ? t('编辑定义') : t('新建定义');
  return <Modal wide title={kindLabel + ' · ' + title} busy={busy} onClose={onClose}>
    <form className="management-form agent-form" onSubmit={write}>
      {deleting ? <p className="modal-copy">{t('删除 {name} 的管理定义。宿主文件中的自有条目需要先通过卸载计划移除。', { name: original.name })}</p> :
        <fieldset className="global-definition-fields" disabled={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0, display: 'grid', gap: '1rem' }}>
          <DefinitionFields kind={kind} draft={draft} onChange={setDraft} onValidityChange={setValid} isEditing={Boolean(original)} />
        </fieldset>}
      <ErrorNotice error={failure} />
      {failure?.status === 409 && original && <Button disabled={busy} onClick={onReload}>{t('放弃本次编辑并读取最新版')}</Button>}
      <footer className="modal-actions">
        <Button disabled={busy} onClick={() => { if (deleting) { setDeleting(false); setFailure(null); } else onClose(); }}>{t('取消')}</Button>
        {original && !deleting && <Button variant="danger" disabled={busy} onClick={() => { setDeleting(true); setFailure(null); }}>{t('删除共享定义')}</Button>}
        <Button variant={deleting ? 'danger' : 'primary'} type="submit" busy={busy} disabled={!deleting && (!valid || !draft.name?.trim())}>{deleting ? t('确认删除定义') : t('保存共享定义')}</Button>
      </footer>
    </form>
  </Modal>;
}

export function GlobalAssetEditor({ kind, item, onClose, onSaved }) {
  const path = item?.id ? resourcePath + '/' + encodeURIComponent(item.id) : null;
  const loaded = useResource(path);
  if (!Object.hasOwn(kinds, kind)) return null;
  const detail = loaded.data;
  const mismatch = detail && (detail.id !== item?.id || detail.kind !== kind || detail._summary || !Number.isInteger(detail.version));
  if (path && (!detail || loaded.loading || loaded.error || mismatch)) {
    return <Modal title={t('编辑定义')} onClose={onClose}>
      {mismatch ? <><ErrorNotice error={new Error(t('资料详情与当前选择不一致，请重新读取列表。'))} /><Button onClick={loaded.reload}>{t('重新读取')}</Button></> :
        <ResourceState resource={{ ...loaded, data: null }} />}
    </Modal>;
  }
  return <AssetForm key={path ? kind + ':' + detail.id + ':' + detail.version : kind} kind={kind} original={path ? detail : null} onReload={loaded.reload} onClose={onClose} onSaved={onSaved} />;
}
