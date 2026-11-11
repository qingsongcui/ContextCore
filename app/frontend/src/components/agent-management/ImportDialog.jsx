import { useRef, useState } from 'react';
import { t } from '../../lib/i18n.jsx';
import { mutate } from '../../lib/api.js';
import { Button, ErrorNotice, Field, Modal } from '../ui.jsx';
import { EvidenceDetails, StatusBadge } from '../ManagementUI.jsx';
import { SkillInstallMode } from '../AssetTools.jsx';

export function ImportDialog({ agent, agentId, category, target, onClose, onChanged, isCurrent = () => true }) {
  const [format, setFormat] = useState(target?.source?.type === 'github' ? 'skill-github' : target?.source?.type === 'registry' ? 'skill-registry' : target?.source?.type === 'zip' ? 'skill-zip' : target ? 'skill-local' : category === 'skill' ? 'skill-zip' : 'senrelay');
  const [mode, setMode] = useState(target?.install_mode || 'copy');
  const [values, setValues] = useState({ 'skill-local': target?.source?.path || '', 'skill-github': { url: target?.source?.url || '', ref: target?.source?.ref || '', path: target?.source?.path || '' }, 'skill-registry': { manifest_url: target?.source?.manifest_url || '' } });
  const [filename, setFilename] = useState('');
  const [preview, setPreview] = useState(null);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const fileGeneration = useRef(0);
  const submitting = useRef(false);
  function change(value) { setValues(previous => ({ ...previous, [format]: value })); setPreview(null); setResult(null); setError(null); }
  async function readFile(file) {
    if (!file) return;
    const generation = ++fileGeneration.current;
    change(''); setFilename('');
    if (file.size > 2 * 1024 * 1024) { setError(new Error(t('文件不能超过 2 MiB。'))); return; }
    try {
      const value = format === 'skill-zip' ? await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(',')[1]); reader.onerror = reject; reader.readAsDataURL(file); }) : await file.text();
      if (!isCurrent() || generation !== fileGeneration.current) return;
      change(value); setFilename(file.name);
    } catch { if (isCurrent() && generation === fileGeneration.current) setError(new Error(t('文件无法读取，请重新选择。'))); }
  }
  async function submit(event) {
    event.preventDefault(); if (submitting.current || busy || !isCurrent()) return;
    setError(null);
    try {
      let data = values[format];
      if (['senrelay', 'cc-switch'].includes(format)) {
        if (!data || (typeof data === 'string' && !data.trim())) throw new Error(t('请先填写来源或选择文件。'));
        try { data = typeof data === 'string' ? JSON.parse(data) : data; }
        catch { throw new Error(t('JSON 格式不正确，请检查后再预览。')); }
      }
      if (!data || (typeof data === 'string' && !data.trim())) throw new Error(t('请先填写来源或选择文件。'));
      const payload = { format, data, ...(agentId ? { agent_id: agentId } : {}), ...(format.startsWith('skill-') ? { install_mode: mode } : {}), ...(target ? { target_id: target.id, expected_version: target.version, name: target.name } : {}) };
      if (!preview) { setPreview(payload); return; }
      submitting.current = true; setBusy(true);
      const output = await mutate('/api/manage/assets/import', 'POST', preview);
      if (!isCurrent()) return;
      setResult(output); onChanged();
    } catch (failure) { if (isCurrent()) setError(failure); }
    finally { submitting.current = false; setBusy(false); }
  }
  const value = values[format];
  return <Modal wide title={`${agent} · ${target ? t('更新技能来源') : t('导入共享定义')}`} busy={busy} onClose={onClose}><form className="management-form agent-form" onSubmit={submit}><fieldset disabled={busy} className="agent-import-fields">
    <p>{t('导入只保存共享资料，不会应用到任何助手、运行脚本或授权读取聊天。确认后才会读取目录或下载指定来源。')}</p>{agentId ? <p>{t('成功导入的资料会加入 {agent} 的清单；仍需单独确认应用。', { agent })}</p> : null}
    <Field label={t('来源类型')}><select value={format} disabled={busy} onChange={event => { fileGeneration.current += 1; setFormat(event.target.value); setPreview(null); setResult(null); setFilename(''); setError(null); }}>{!target ? <><option value="senrelay">Senrelay JSON</option><option value="cc-switch">{t('兼容配置 JSON')}</option></> : null}<option value="skill-local">{t('本地技能目录')}</option><option value="skill-zip">{t('技能 ZIP 文件')}</option><option value="skill-github">GitHub</option><option value="skill-registry">{t('技能清单网址')}</option></select></Field>
    {format.startsWith('skill-') ? <SkillInstallMode value={mode} onChange={next => { setMode(next); setPreview(null); setResult(null); }} /> : null}
    {format === 'skill-local' ? <Field label={t('技能目录的完整路径')} hint={t('选择含 SKILL.md 的本机目录；这里只会在确认导入后读取。')}><input required value={value || ''} onChange={event => change(event.target.value)} placeholder="/Users/you/Skills/my-skill" /></Field> : format === 'skill-github' ? <><Field label={t('GitHub 仓库网址')}><input type="url" required value={value?.url || ''} onChange={event => change({ ...value, url: event.target.value })} placeholder="https://github.com/owner/repository" /></Field><Field label={t('固定版本（40 位提交编号）')} hint={t('为防止来源悄悄改变，不能使用 main 或分支名。')}><input required pattern="[a-fA-F0-9]{40}" value={value?.ref || ''} onChange={event => change({ ...value, ref: event.target.value })} /></Field><Field label={t('仓库内的技能目录（可选）')}><input value={value?.path || ''} onChange={event => change({ ...value, path: event.target.value })} /></Field></> : format === 'skill-registry' ? <Field label={t('技能清单网址')}><input type="url" required value={value?.manifest_url || ''} onChange={event => change({ ...value, manifest_url: event.target.value })} /></Field> : <><Field label={format === 'skill-zip' ? t('选择 ZIP 文件') : t('选择导出的配置文件')}><input key={format} type="file" accept={format === 'skill-zip' ? '.zip,application/zip' : '.json,application/json'} onChange={event => readFile(event.target.files?.[0])} /></Field>{filename ? <p>{filename}</p> : null}{format !== 'skill-zip' ? <details className="agent-advanced"><summary>{t('高级：粘贴导出内容')}</summary><Field label={t('导入内容')}><textarea rows={10} value={value || ''} onChange={event => change(event.target.value)} /></Field></details> : null}</>}
    {preview ? <section className="agent-change-summary" aria-live="polite"><h3>{t('将保存到共享资料库')}</h3><p>{t('不会修改 {agent} 的配置。保存后还需要单独预览并确认应用。', { agent })}</p><p>{format === 'skill-zip' ? `${filename} · ${t('技能 ZIP 文件')}` : format === 'skill-local' ? preview.data : format === 'skill-github' ? `${preview.data.url} · ${preview.data.ref}` : format === 'skill-registry' ? preview.data.manifest_url : t('已解析配置文件，确认后逐项报告支持与不支持的内容。')}</p><EvidenceDetails value={format === 'skill-zip' ? { format, file: filename, encoded_bytes: preview.data.length, install_mode: mode } : preview} title={t('高级：导入参数')} /></section> : null}
    {result ? <><h3>{t('导入处理结果')}</h3>{(result.imported || []).map(item => <p key={item.id}>{item.name} · {item.kind} · {t('版本')} {item.version}</p>)}{(result.unsupported || []).length ? <p role="status">{t('有内容未能导入，请查看详情后补全。')}</p> : null}{result.embedded_text_review_required ? <p role="status">{t('导入的内容包含未验证的说明或脚本，请在共享或应用前仔细核对。')}</p> : null}<StatusBadge value={result.status || ((result.imported || []).length ? 'configured' : 'unknown')} /><EvidenceDetails value={result} title={t('导入处理结果')} /></> : null}
    </fieldset><ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={onClose}>{result ? t('关闭') : t('取消')}</Button>{!result ? <Button type="submit" variant="primary" busy={busy}>{preview ? t('确认导入定义') : t('解析预览')}</Button> : null}</footer>
  </form></Modal>;
}
