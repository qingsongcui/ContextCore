// Server diagnostics may contain host paths, account responses or credential values.
// Keep them out of routine notices; retain only structured status for safe recovery.
export function errorFeedback(status, detail) {
  const text = typeof detail === 'string' ? detail : '';
  if (status === 507 && /^Insufficient disk space for the model download/i.test(text)) return '模型下载空间不足。请释放磁盘空间后重新确认下载，词法检索仍可使用。';
  if (status === 502 && /^Model download interrupted/i.test(text)) return '模型下载连接中断。请检查网络后重新确认下载，词法检索仍可使用。';
  if (/^skipped_damaged:/i.test(text)) return '部分聊天来源格式无法读取，已跳过损坏文件。其余记录是当前的，不是整台助手同步失败。';
  if (/^partial_sync:/i.test(text)) return '部分聊天记录未能读取。已保存可读记录；请修复损坏来源，或等正在写入的文件稳定后再同步。';
  if (/legacy selection receipt/i.test(text)) return '旧版模型选择记录不完整，请先选择并启用当前模型，再继续管理连接。';
  if (/object is referenced|remove bindings and dependent objects/i.test(text)) return '这份资料仍被模型、账号或配置组合使用。请先修改或移除对应引用，再删除定义。';
  if (/context_window|model_context_windows|context window|model capacity/i.test(text)) return '请核对模型的上下文容量，按服务商说明填写正整数，再重新预览。';
  if (/reattach.*(?:credential|key)|imported credential/i.test(text)) return '这份定义需要重新绑定本机凭据。请先设置密钥或选择账号。';
  if (/credential_source|bound account|selected account.*(?:credential|key)/i.test(text)) return '所选凭据来源尚不可用。请检查绑定账号或密钥；不会自动换用另一份凭据。';
  if (/current model selection.*uninstall|active model selection.*uninstall/i.test(text)) return '请先切换或恢复当前模型选择，再移除正在使用的服务商配置。';
  if (status === 409 && /OMP model roles, fallbacks and task\/preset selectors/i.test(text)) return '这个服务商仍被 OMP 的模型角色、备用链或任务预设引用。请先修改或恢复这些选择，再移除服务商。';
  if (status === 422 && /OMP legacy settings\.json/i.test(text)) return '请先通过 OMP 官方迁移旧 settings.json，再预览模型选择；现有设置未被改写。';
  if (status === 422 && /OMP legacy models\.json/i.test(text)) return '请先通过 OMP 官方迁移旧 models.json，再管理服务商；不会创建会遮蔽旧服务商的并行配置。';
  if (status === 422 && /OMP model identifier conflicts with native selector syntax/i.test(text)) return '模型编号与 OMP 选择器语法冲突，请核对服务商的原始模型编号；不会猜测其他模型。';
  if (status === 422 && /(?:SKILL\.md frontmatter|Skill description in YAML frontmatter|Cursor Skill name)/i.test(text)) return '请编辑技能的 SKILL.md 元数据：使用闭合的 YAML 头部和非空 description；Cursor 还要求合法的 name 与文件夹一致。';
  if (status === 422 && /Antigravity truncates rules above 24000/i.test(text)) return 'Antigravity 会截断超过 24000 字节的规则。请缩短这份使用规则，再重新预览；没有写入截断内容。';
  if (status === 422 && /Cursor documents API-key setup/i.test(text)) return '请在 Cursor Settings → Models 中添加或移除密钥并选择模型；这里只保存或导出定义，不会改写 Cursor 私有存储。';
  if (status === 422 && /Antigravity Desktop documents plan-dependent models/i.test(text)) return '请在 Antigravity 对话的模型菜单中选择当前账号可用模型；本页不会写入自定义服务商或借用私有账号凭据。';
  if (status === 503 && /Native (?:yaml|json5) format runtime unavailable/i.test(text)) return '本机服务缺少保留原格式所需的配置解析组件。请安装 requirements-managed-formats.txt 对应依赖后重试；原文件未被改写。';
  if (status === 422 && /skill zip|skill directory|SKILL\.md/i.test(text)) return '这个 ZIP 需要包含一份 SKILL.md 和对应技能文件。请将单个技能目录压缩后导入，避免混入多份技能或重名文件。';
  if (status === 422 && /does not declare.*required.*host: responses/i.test(text)) return '这个服务商尚未声明支持所选 Responses 接口。请核对并编辑接口格式。';
  if (status === 409 && /pending assistant connection|connection recovery/i.test(text)) return '有未完成的连接更改。转发已暂停，请先检查并恢复。';
  if (status === 422 && /matching native protocols|required by this host|support for this endpoint|selected native gateway API/i.test(text)) return '所选服务与助手的接口不匹配。请核对服务商支持的接口，再预览连接更改。';
  if (status === 409 && /uninstall owned host entries|restore the active host selection/i.test(text)) return '先从助手移除配置或恢复原选择，再删除资料。';
  if (status === 409 && /referenced provider does not exist|stale route references/i.test(text)) return '所选路由包含已删除或不存在的服务商。请刷新服务商列表并移除失效引用后再保存。';
  if (status === 409 && /route.*default.*delet|referenced definition/i.test(text)) return '这份定义仍被连接设置引用。请先更换对应线路或默认模型，再删除定义。';
  if (status === 422 && /wire_api|supported_endpoints|endpoint declaration|API preference/i.test(text)) return '接口声明需要核对。请在服务商定义中选择支持的接口格式，保持首选格式与支持范围一致。';
  if (/keychain|钥匙串/i.test(text)) return '无法访问系统钥匙串。请在本机检查权限或解锁钥匙串，再重试；不会改用明文保存。';
  if (/symlink.*target/i.test(text)) return '配置位置包含符号链接，暂不能直接写入。请在助手中确认真实配置位置后再试。';
  if (/unsupported|not supported|不支持/i.test(text)) return '这个助手暂不支持此操作。请选择已支持的方式，或查看助手的能力说明。';
  if (/oauth|account|authentication|credential|账号|凭据/i.test(text) && [400, 401, 403, 409, 422, 502].includes(status)) return '账号或授权状态需要检查。请回到该助手的模型与账号页核对，不要重复提交密钥。';
  if ([404, 410].includes(status)) return '这条内容或功能现在不可用了。请重新读取列表，已有草稿不会被自动覆盖。';
  if (status === 409) return '内容或设置已在别处变化。请重新读取并比较，再决定是否提交；你的草稿仍保留。';
  if (status === 401 || status === 403) return '这次操作尚未获得授权。请检查所选范围和权限，必要时刷新本机连接后重试。';
  if (status === 422 || status === 400) return '有些内容不符合要求。请检查必填项、所选内容和输入格式后再试。';
  if (status === 429) return '请求过于频繁或额度暂不可用。请稍后重试，并检查对应助手的账号状态。';
  if (status >= 500) return '本机服务暂时未完成操作。请重新读取状态；确认结果前不要重复提交。';
  return '操作没有完成，请检查本机服务后重试。';
}
export class ApiError extends Error {
  constructor(message, status, detail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}
let csrfToken = null;
let bootstrap = null;

async function csrf() {
  if (csrfToken) return csrfToken;
  if (!bootstrap) {
    bootstrap = fetch('/api/session', { credentials: 'same-origin' }).then(async response => {
      if (!response.ok) throw new ApiError('无法初始化本机连接，请刷新后重试。', response.status);
      const data = await response.json();
      csrfToken = data.csrf_token;
      if (!csrfToken) throw new ApiError('本机服务没有提供有效的会话凭据。', 403);
      return csrfToken;
    }).catch(failure => {
      if (failure instanceof ApiError) throw failure;
      throw new ApiError('无法连接本机服务。已有内容不会因此丢失，请检查服务状态。', 0);
    }).finally(() => { bootstrap = null; });
  }
  return bootstrap;
}

export async function request(path, options = {}) {
  const method = (options.method || 'GET').toUpperCase();
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) headers.set('X-CSRF-Token', await csrf());
  let response;
  try {
    response = await fetch(path, { ...options, method, headers, credentials: 'same-origin' });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('无法连接本机服务。已有内容不会因此丢失，请检查服务状态。', 0);
  }
  if (response.status === 204) return null;
  let data;
  try { data = await response.json(); }
  catch {
    if (response.ok) throw new ApiError('服务返回了无法读取的内容。请检查本机服务版本后重试，不会把它当成空列表。', response.status);
    data = null;
  }
  if (!response.ok) {
    if (response.status === 403) csrfToken = null;
    const detail = data?.detail;
    const selection = detail?.code === 'ambiguous_task' && Array.isArray(detail.candidates)
      ? { code: 'ambiguous_task', candidates: detail.candidates.filter(item => typeof item.id === 'string' && typeof item.title === 'string').map(({ id, title, status }) => ({ id, title, status })) }
      : undefined;
    throw new ApiError(selection ? '找到多个未完成任务，请明确选择要接续的那一个。' : errorFeedback(response.status, detail), response.status, selection);
  }
  return data;
}
export function mutate(path, method, data) {
  return request(path, { method, ...(data === undefined ? {} : { body: JSON.stringify(data) }) });
}
