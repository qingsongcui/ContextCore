import { t } from "../lib/i18n.jsx";
import { useEffect, useState } from 'react';
import { Pause, Play, Plug, RefreshCw, Search, Unplug } from 'lucide-react';
import { errorFeedback, mutate, request } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { Badge, Button, EmptyState, ErrorNotice, Field, Modal, PageHeader, ResourceState, formatTime } from '../components/ui.jsx';
import { ConnectionPlanDialog } from '../components/ConnectionPlanDialog.jsx';
import { safeEvidence } from '../lib/management.js';
const CAPABILITIES = {
  history: '读历史记录',
  capture: '采集新记录',
  memory_read: '读记忆',
  memory_write: '写记忆',
  handoff: '跨助手接力'
};
const STATES = {
  available: '已实现，待实测',
  partial: '部分可用，有前提',
  not_configured: '尚未配置',
  unsupported: '暂不可用',
  error: '异常',
  verified: '已验证'
};
function cleanPathList(values) {
  return Array.from(new Set((values || []).map(value => String(value || '').trim()).filter(Boolean)));
}
function mergedConnectionPaths(item) {
  const configured = Array.isArray(item?.paths) ? cleanPathList(item.paths) : [];
  if (item?.enabled && configured.length > 0) return configured;
  return cleanPathList([...configured, ...(item?.discovery?.suggested_paths || [])]);
}
function discoveredExtraPaths(item, activePaths) {
  const activeSet = new Set(activePaths);
  const seen = new Set();
  const extras = [];
  for (const raw of item?.discovery?.suggested_paths || []) {
    const p = String(raw || '').trim();
    if (!p || activeSet.has(p) || seen.has(p)) continue;
    seen.add(p);
    extras.push({ path: p, symlink: false });
  }
  for (const ev of item?.discovery?.evidence || []) {
    const p = String(ev?.path || '').trim();
    if (!p || ev?.status !== 'symlink_requires_authorization' || activeSet.has(p) || seen.has(p)) continue;
    seen.add(p);
    extras.push({ path: p, symlink: true });
  }
  return extras;
}
export function ConsolePage({
  revision,
  onChanged,
  agentId,
  navigate,
  returnAgent,
  embedded = false,
  managementAvailable = true
}) {
  const resource = useResource('/api/connections', revision);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(null);
  const [connect, setConnect] = useState(null);
  const [disconnect, setDisconnect] = useState(null);
  const [paths, setPaths] = useState('');
  const [pathsTouched, setPathsTouched] = useState(false);
  const [consent, setConsent] = useState(false);
  const [setup, setSetup] = useState(null);
  function openConnectDialog(item) {
    setConnect(item);
    setPaths(mergedConnectionPaths(item).join('\n'));
    setPathsTouched(false);
    setConsent(false);
    setError(null);
  }
  function changed() {
    resource.reload();
    onChanged();
  }
  // Discover clears its busy flag before the reloaded path list arrives.
  useEffect(() => {
    if (!connect || pathsTouched) return;
    const latest = resource.data?.find(item => item.id === connect.id);
    if (!latest) return;
    const next = mergedConnectionPaths(latest).join('\n');
    setPaths(current => current === next ? current : next);
  }, [connect, resource.data, pathsTouched]);
  async function action(path, method, body, id) {
    if (busy) return null;
    setBusy(id);
    setError(null);
    setNotice('');
    try {
      const result = await mutate(path, method, body);
      changed();
      return result;
    } catch (failure) {
      setError(failure);
      return null;
    } finally {
      setBusy(null);
    }
  }
  async function enable() {
    const latest = resource.data?.find(item => item.id === connect.id) || connect;
    const defaultPaths = mergedConnectionPaths(latest);
    const submitted = paths.split('\n').map(value => value.trim()).filter(Boolean);
    if (!pathsTouched && defaultPaths.length && submitted.length === 0) {
      setPaths(defaultPaths.join('\n'));
      return;
    }
    if (latest?.enabled && submitted.length === 0) {
      setError(new Error(t("至少保留一个授权目录，或先停止接入")));
      return;
    }
    const result = await action(`/api/connections/${connect.id}`, 'PATCH', {
      enabled: true,
      paths: submitted,
      confirmed: true
    }, connect.id);
    if (!result) return;
    if (!result.enabled || result.error) {
      setError(new Error(result.error ? errorFeedback(500, result.error) : t("服务没有确认接入已生效，请检查下面的能力状态。")));
      return;
    }
    setNotice(t("{name} 的可读范围已保存。写进它的配置要另外确认；每项能力以实际结果为准。", { name: connect.label }));
    setConnect(null);
  }
  async function showSetup(item) {
    if (busy) return;
    setBusy(item.id);
    setError(null);
    try {
      setSetup({
        id: item.id,
        label: item.label,
        mode: 'preview',
        data: await request(`/api/connections/${item.id}/setup`)
      });
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(null);
    }
  }
  const selected = resource.data?.find(item => item.id === agentId);
  const visible = agentId ? resource.data?.filter(item => item.id === agentId) : resource.data;
  const latestConnect = connect ? (resource.data?.find(item => item.id === connect.id) || connect) : null;
  const parsedPaths = paths.split('\n').map(value => value.trim()).filter(Boolean);
  const extraSuggestedPaths = latestConnect ? discoveredExtraPaths(latestConnect, parsedPaths) : [];
  const emptyEnabledPaths = Boolean(latestConnect?.enabled && parsedPaths.length === 0);
  return <>
    <PageHeader headingLevel={embedded ? 2 : 1} title={selected ? t("{name} 的接入与权限", { name: selected.id === 'omp' ? 'Oh My Pi (OMP)' : selected.label }) : t("助手接入")} description={t("读历史与新增聊天、写入助手配置与安装受管组件各自独立确认。每项能力都要单独核对。")} actions={<>{navigate ? managementAvailable ? <Button onClick={() => navigate('agents', { agent: returnAgent || agentId || null, section: null })}>{t("返回我的助手")}</Button> : <Button onClick={() => navigate('projects')}>{t("返回项目工作台")}</Button> : null}<Button icon={Search} disabled={!!busy} busy={busy === 'discover'} onClick={() => action('/api/connections/discover', 'POST', {}, 'discover')}>{t("发现本机 Agent")}</Button></>} />
    <ErrorNotice error={error} />{notice ? <p className="operation-notice" role="status">{notice}</p> : null}
    <ResourceState resource={resource}>{visible?.length ? <div className="connections-grid">{visible.map(item => <section className="connection-panel" key={item.id}>
      <header><span className="connection-monogram">{item.label.slice(0, 1)}</span><div><h2>{item.label}</h2><p>{item.enabled ? item.paused ? t("采集已暂停") : t("已授权读取聊天") : item.discovered ? t("已发现 · 未授权读取聊天") : t("尚未发现")}</p></div><span className={`status-dot ${item.enabled && !item.paused && !item.error ? 'online' : ''}`} /></header>
      <p className="sync-time">{t("上下文工具：{state}", { state: item.authorizations?.context_tools === true ? t("已授权") : t("未授权") })} · {t("生命周期通知：{state}", { state: item.authorizations?.lifecycle === true ? t("已授权") : t("未授权") })}</p>
      <div className="capability-list group">{Object.entries(CAPABILITIES).map(([key, label]) => {
              const capability = item.capabilities?.[key];
              return <div key={key}><span>{t(label)}</span><Badge tone={capability?.status === 'verified' ? 'success' : capability?.status === 'error' ? 'warning' : ''}>{t(STATES[capability?.status] || "尚未验证")}</Badge>{capability?.detail ? <><small>{capability.status === 'unsupported' ? t("这个助手暂不支持此操作。请选择已支持的方式，或查看助手的能力说明。") : capability.status === 'error' ? t(errorFeedback(500, capability.detail)) : t("能力说明以当前本机检查为准；配置存在不代表助手已实际使用。")}</small><details className="capability-detail"><summary>{t("能力检查详情")}</summary><pre>{safeEvidence(capability.detail)}</pre></details></> : null}</div>;
            })}</div>
      {item.error ? <ErrorNotice error={new Error(t(errorFeedback(500, item.error)))} /> : null}<p className="sync-time">{t("最近同步：")}{formatTime(item.last_sync)}</p>
      <footer>{!item.enabled ? <Button disabled={!!busy} icon={Plug} variant="primary" onClick={() => openConnectDialog(item)}>{t("确认接入")}</Button> : <>
        <Button disabled={!!busy} icon={item.paused ? Play : Pause} busy={busy === item.id} onClick={async () => {
                const result = await action(`/api/connections/${item.id}`, 'PATCH', {
                  paused: !item.paused,
                  confirmed: true
                }, item.id);
                if (result) setNotice(`${item.label}：${result.paused ? t("采集已暂停，读记忆的权限不变。") : t("已恢复采集。实际能不能采到，看上面每项状态。")}`);
              }}>{item.paused ? t("恢复采集") : t("暂停采集")}</Button>
        <Button icon={RefreshCw} disabled={item.paused || !!busy} busy={busy === item.id} onClick={async () => {
                const result = await action(`/api/connections/${item.id}/sync`, 'POST', {}, item.id);
                if (result?.error) setError(new Error(errorFeedback(500, result.error)));else if (result) setNotice(t("{name} 已经发出同步请求。看看最近同步时间和各项能力；这不代表记忆已经整理好。", { name: item.label }));
              }}>{t("同步")}</Button>
        <Button disabled={!!busy} onClick={() => openConnectDialog(item)}>{t("授权读取的记录位置")}</Button>
        <Button icon={Unplug} disabled={!!busy} aria-label={t("解除 {name} 接入", { name: item.label })} onClick={() => {
                setDisconnect(item);
                setError(null);
              }} />
      </>}<Button disabled={!!busy} onClick={() => showSetup(item)} busy={busy === item.id}>{t("接入说明与配置")}</Button></footer>
    </section>)}</div> : <EmptyState title={agentId ? t("这个助手暂没有聊天接入") : t("发现你的协作工具")} body={agentId ? t("配置管理和聊天接入是两项能力。当前列表没有这个助手的来源接口；不会替它接入其他助手，也不会自动授权。") : t("只找出可用的工具和记录位置。真要读，还需要你确认范围。")} />}</ResourceState>
    <aside className="quiet-note">{t("本机记录里可能有机密信息，会按你的规则原样保存。读取聊天、上下文工具与生命周期通知分别授权；暂停采集不会撤销已授权的上下文工具。")}</aside>
    {connect ? <Modal title={t("接入 {name}", { name: connect.label })} busy={!!busy} onClose={() => setConnect(null)}><div className="editor-form"><Field label={t("授权读取的记录位置")} hint={t("一行一个路径。只读这些位置里支持读取的记录，不会翻遍整台电脑。留空不等于能读全部。")}><textarea rows={5} value={paths} onChange={event => { setPathsTouched(true); setPaths(event.target.value); }} /></Field>{extraSuggestedPaths.length > 0 ? <div className="suggested-paths toolbar-actions" role="group" aria-label={t("可添加的建议路径")}>{extraSuggestedPaths.map(candidate => <Button key={candidate.path} onClick={() => { setPathsTouched(true); setPaths(current => cleanPathList([...current.split('\n'), candidate.path]).join('\n')); }}>{candidate.symlink ? t("+ 添加建议路径：{path}（符号链接需确认）", { path: candidate.path }) : t("+ 添加建议路径：{path}", { path: candidate.path })}</Button>)}</div> : null}{emptyEnabledPaths ? <p className="muted" role="status">{t("至少保留一个授权目录，或先停止接入")}</p> : null}<label className="check-label consent"><input type="checkbox" checked={consent} onChange={event => setConsent(event.target.checked)} /><span>{t("允许读取上面这些位置的历史和新增记录，保存原文与记忆，供已接入的 Agent 使用。我知道记录不会自动打码。这次授权只允许读取记录，不包括修改它的配置文件。")}</span></label><ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setConnect(null)}>{t("取消")}</Button>{emptyEnabledPaths ? <Button onClick={() => { const target = latestConnect; setConnect(null); setDisconnect(target); setError(null); }}>{t("停止接入")}</Button> : null}<Button variant="primary" disabled={!consent || resource.loading || emptyEnabledPaths} busy={busy === connect.id} onClick={enable}>{t("确认授权范围")}</Button></footer></div></Modal> : null}
    {disconnect ? <Modal title={t("解除 {name} 接入？", { name: disconnect.label })} busy={!!busy} onClose={() => setDisconnect(null)}><p className="modal-copy">{t("停止采集，并断开通过 Senrelay 读写记忆的权限。已经存下的聊天和记忆仍保留；已经交出去的内容收不回来。这个操作不会删它的配置文件，需要的话可以另外预览并确认移除。")}</p><ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setDisconnect(null)}>{t("取消")}</Button><Button variant="danger" busy={busy === disconnect.id} onClick={async () => {
          const result = await action(`/api/connections/${disconnect.id}`, 'PATCH', {
            enabled: false,
            confirmed: true
          }, disconnect.id);
          if (result && !result.enabled) {
            setNotice(t("{name} 已解除接入，已有聊天与记忆保留。", { name: disconnect.label }));
            setDisconnect(null);
          } else if (result) setError(new Error(t("服务未确认已解除接入，请重新读取状态。")));
        }}>{t("解除接入")}</Button></footer></Modal> : null}
    {setup ? <ConnectionPlanDialog key={setup.id} setup={setup} onClose={() => setSetup(null)} onChanged={changed} /> : null}
  </>;
}
