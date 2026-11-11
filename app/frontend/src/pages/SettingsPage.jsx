import { t } from "../lib/i18n.jsx";
import { useState } from 'react';
import { Monitor, Moon, Power, Sun } from 'lucide-react';
import { mutate } from '../lib/api.js';
import { useResource } from '../lib/useResource.js';
import { useTheme } from '../lib/theme.js';
import { Badge, Button, ErrorNotice, Modal, PageHeader, ResourceState } from '../components/ui.jsx';
import { RetrievalSettings } from '../components/RetrievalSettings.jsx';
import { ProcessingStatus } from '../components/ProcessingStatus.jsx';
import { LanguageSwitch } from '../components/LanguageSwitch.jsx';
import { AuditPage } from './AuditPage.jsx';
export function SettingsPage({
  revision,
  status,
  onChanged,
  managementAvailable = false,
  navigate
}) {
  const resource = useResource('/api/settings', revision);
  const {
    theme,
    setTheme
  } = useTheme();
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [backingUp, setBackingUp] = useState(false);
  const [backupResult, setBackupResult] = useState(null);
  const [backupError, setBackupError] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [showAudit, setShowAudit] = useState(false);
  const stopStatus = status?.service?.stop_status;
  const serviceRunning = status?.ok === true && status?.service?.running === true;
  async function autostart(value) {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      await mutate('/api/settings', 'PATCH', {
        autostart: value,
        install_launch_agent: true
      });
      resource.reload();
      onChanged();
      setNotice(t("你的选择已保存；实际装没装，看下面这一项。"));
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  async function stop() {
    if (busy) return;
    setBusy(true);
    setError(null);
    setNotice('');
    try {
      await mutate('/api/service/stop', 'POST', {
        confirmed: true
      });
      setStopping(false);
      setNotice(t("已经发出停止请求；要再用，得重新启动本机服务。"));
      onChanged();
    } catch (failure) {
      setError(failure);
    } finally {
      setBusy(false);
    }
  }
  async function backupDatabase() {
    if (busy || backingUp) return;
    setBackingUp(true);
    setBackupError(null);
    setError(null);
    setNotice('');
    try {
      const res = await mutate('/api/settings/backup', 'POST', {});
      setBackupResult(res);
    } catch (failure) {
      setBackupError(failure);
    } finally {
      setBackingUp(false);
    }
  }
  const autostartInstalled = resource.data?.autostart_installed ?? status?.service?.autostart_installed;
  return <>
    <PageHeader title={t("设置")} description={t("调整外观和语言，查看本机服务状态、数据库备份与变更记录。")} />
    <ErrorNotice error={error} />{notice ? <p className="operation-notice" role="status">{notice}</p> : null}
    <section className="settings-section group"><header><h2>{t("外观")}</h2><p>{t("界面清爽，内容清楚。")}</p></header><div className="theme-options" role="group" aria-label={t("界面主题")}>{[['light', Sun, t("浅色")], ['dark', Moon, t("深色")], ['system', Monitor, t("跟随系统")]].map(([value, Icon, label]) => <button className={theme === value ? 'selected' : ''} key={value} aria-pressed={theme === value} onClick={() => setTheme(value)}><span className={`theme-preview theme-preview-${value}`}><i /><i /><i /></span><span><Icon size={16} />{label}</span></button>)}</div></section>
    <section className="settings-section group"><header><h2>{t("语言")}</h2><p>{t("只改变界面文字，不会翻译或改写你的聊天和记忆。")}</p></header><LanguageSwitch /></section>
    <section className="settings-section group"><header><h2>{t("权限分开管理")}</h2><p>{t("读取哪些聊天、修改哪些助手配置，均在所选助手中独立确认。登记项目目录不等于允许读取。")}</p></header><Button onClick={() => navigate?.('agents')}>{t("选择助手并管理接入")}</Button></section>
    <ResourceState resource={resource}>{resource.data ? <>
      <section className="settings-section group"><header><h2>{t("本机运行")}</h2><p>{t("关掉网页不会停止采集；只有明确停止服务，后台才会真正停下。")}</p></header>
        <div className="setting-row"><div><h3>{t("开机自动启动")}</h3><p>{t("建议开启。打开后会写入并注册本机登录项；下面显示实际是否已安装。")}</p></div><label className="switch"><input type="checkbox" aria-label={t("开机自动启动")} checked={resource.data.autostart ?? false} disabled={busy || resource.loading || !!resource.error || typeof resource.data.autostart !== 'boolean'} onChange={event => autostart(event.target.checked)} /><span /></label></div>
        <div className="setting-row"><div><h3>{t("实际启动项")}</h3><p>{!status && typeof resource.data?.autostart_installed !== 'boolean' ? t("读不到服务状态，暂时判断不了启动项装没装。") : autostartInstalled ? t("系统启动项已安装。") : t("还没检测到启动项。需要在本机服务里执行安装命令——网页不会替你绕过系统授权。")}</p></div><Badge>{!status && typeof resource.data?.autostart_installed !== 'boolean' ? t("未知") : autostartInstalled ? t("已安装") : t("未安装")}</Badge></div>
        <div className="setting-row"><div><h3>{t("本机服务")}</h3><p>{stopStatus === 'pending' ? t("正在停，等后台回话。") : stopStatus === 'failed' ? t("服务没有完成停止，请检查运行状态后再决定是否重试。") : stopStatus === 'stopped' ? t("后台已经报了停止。要再用，得重新启动服务。") : serviceRunning ? t("正在运行，只读你授权过的那几个位置。") : status?.ok ? t("服务有回应，但还不能确认这个进程在运行。") : t("连不上。连不上不代表服务已经停了。")}</p></div><Button icon={Power} disabled={busy || !serviceRunning || stopStatus === 'pending' || stopStatus === 'stopped'} onClick={() => {
              setError(null);
              setStopping(true);
            }}>{stopStatus === 'pending' ? t("正在停止…") : stopStatus === 'failed' ? t("重新请求停止") : t("停止服务")}</Button></div>
      </section>
      <section className="settings-section group"><header><h2>{t("本地数据库备份")}</h2><p>{t("在升级或整理前为本机 SQLite 数据库创建一致性快照，并自动校验备份完整性。")}</p></header>
        <div className="setting-row"><div><h3>{t("创建数据库快照")}</h3><p>{backupResult?.backup_path || backupResult?.path ? <><span>{t("最新备份路径：")}</span><code>{backupResult.backup_path || backupResult.path}</code></> : t("备份文件保存在本机数据目录的 backups 文件夹中，不会上传到外部服务。")}</p>{backupResult ? <p className="operation-notice" role="status">{backupResult.integrity === 'ok' ? t("完整性校验已通过（quick_check: ok）") : t("完整性校验状态：{status}", { status: backupResult.integrity || t("未知") })}</p> : null}</div><Button busy={backingUp} disabled={busy || backingUp} onClick={backupDatabase}>{t("立即备份数据库")}</Button></div>
        <ErrorNotice error={backupError} />
      </section>
      {managementAvailable ? <details className="consumer-advanced"><summary>{t("高级：增强搜索与处理诊断")}</summary><RetrievalSettings revision={revision} /><ProcessingStatus revision={revision} /></details> : null}<section className="settings-section group"><header><h2>{t("已确定的数据规则")}</h2></header><dl className="policy-list"><div><dt>{t("共享范围")}</dt><dd>{t("接入的 Agent 都能读写所有项目的内容。")}</dd></div><div><dt>{t("凭据处理")}</dt><dd>{t("原始记录不会自动打码，里面可能有密钥和登录信息。")}</dd></div><div><dt>{t("内容保存")}</dt><dd>{t("一直保留，直到你主动删除。聊天和记忆分开删，删了就没了，没有回收站。")}</dd></div><div><dt>{t("变更记录")}</dt><dd>{t("只记谁在什么时候改了什么，不复制正文和密钥；平常查看不会逐条记。")}</dd></div><div><dt>{t("通知")}</dt><dd>{t("只有要紧的事才在网页上提示，平常不打扰你。")}</dd></div></dl><Button onClick={() => setShowAudit(value => !value)}>{t("查看变更记录")}</Button>{showAudit ? <div className="embedded-audit"><AuditPage revision={revision} /></div> : null}</section>
    </> : null}</ResourceState>
    {stopping ? <Modal title={t("停止本机服务？")} busy={busy} onClose={() => setStopping(false)}><p className="modal-copy">{t("对话采集会暂停，网页也会断开与本机服务的连接。已保存的内容仍在；再次使用前请重新启动服务。")}</p><ErrorNotice error={error} /><footer className="modal-actions"><Button onClick={() => setStopping(false)}>{t("取消")}</Button><Button variant="danger" busy={busy} onClick={stop}>{t("确认停止")}</Button></footer></Modal> : null}
  </>;
}
