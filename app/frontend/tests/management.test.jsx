import { afterEach, describe, expect, it, vi } from 'vitest';
import { number, safeEvidence, usageDateFilters, backendCompatibility } from '../src/lib/management.js';
import { errorFeedback, request } from '../src/lib/api.js';
afterEach(() => vi.unstubAllGlobals());

describe('usage display boundaries', () => {
  it('does not turn absent, invalid, blank or boolean observations into zero', () => {
    for (const value of [null, undefined, '', true, false, 'invalid', NaN, Infinity]) expect(number(value)).toBe('未知');
    expect(number(0)).toBe('0');
    expect(number('0')).toBe('0');
    expect(number('12.50', { maximumFractionDigits: 2 })).toBe('12.5');
  });
  it('includes the full selected local end day and preserves other filters', () => {
    const input = { start: '2026-10-23', end: '2026-10-24', host_id: 'fixture' };
    const actual = usageDateFilters(input);
    expect(new Date(actual.start).getHours()).toBe(0);
    expect(new Date(actual.end).getDate()).toBe(25);
    expect(actual.host_id).toBe('fixture');
    expect(input.end).toBe('2026-10-24');
  });
  it('does not create an invalid-date request', () => {
    expect(usageDateFilters({ start: 'invalid' })).toEqual({});
  });
});

describe('evidence secret boundaries', () => {
  it('redacts nested credential values while preserving usage and opaque references', () => {
    const input = { credential_ref: 'vault-ref', input_tokens: 12, config: { env: { OPENAI_API_KEY: 'fixture-key' }, access_token: 'fixture-token' }, list: [{ password: 'fixture-password' }] };
    const output = safeEvidence(input);
    expect(output.credential_ref).toBe('vault-ref');
    expect(output.input_tokens).toBe(12);
    expect(output.config.env.OPENAI_API_KEY).toBe('[redacted]');
    expect(output.config.access_token).toBe('[redacted]');
    expect(output.list[0].password).toBe('[redacted]');
    expect(input.config.access_token).toBe('fixture-token');
  });
  it('withholds credential-bearing free text in diagnostics without hiding ordinary token or secret terminology', () => {
    const ordinary = { notes: 'A token budget is not a fee cap; secret is only a field label.', credential_ref: 'keychain:fixture', input_tokens: 30 };
    expect(safeEvidence(ordinary)).toEqual(ordinary);
    const syntheticPrivateKey = ['-----BEGIN', 'PRIVATE KEY-----\\nfixture'].join(' ');
    for (const value of ['Authorization: Bearer fixture-private-value', '{\"api_key\":\"fixture-private-value\"}', 'PASSWORD=fixture-private-value', 'https://name:password@example.invalid/path', 'https://example.invalid/callback?code=fixture-private-value', syntheticPrivateKey]) {
      expect(safeEvidence({ diff: value }).diff).toBe('[redacted]');
    }
    expect(safeEvidence({ authorization_url: 'https://example.invalid/authorize?state=private' }).authorization_url).toBe('[redacted]');
    expect(safeEvidence({ accessToken: 'private' }).accessToken).toBe('[redacted]');
  });
  it('sanitizes internal compatibility wire keys and reference commits in safeEvidence without mutating input', () => {
    const input = {
      id: 'prov_1',
      cc_switch_current_hosts: ['codex', 'claude-code'],
      reference_commit: 'deadbeef0123456789',
      source: {
        type: 'cc-switch-migration',
        format: 'cc-switch-sqlite',
        alt_format: 'cc-switch-json',
        origin: 'cc-switch',
        basename: 'cc-switch.db',
        reference_commit: 'deadbeef0123456789',
      },
    };
    const output = safeEvidence(input);
    expect(output.local_active_hosts).toEqual(['codex', 'claude-code']);
    expect(output).not.toHaveProperty('cc_switch_current_hosts');
    expect(output).not.toHaveProperty('reference_commit');
    expect(output.source).toEqual({
      type: 'local-config',
      format: 'sqlite',
      alt_format: 'json',
      origin: 'local-config',
      basename: 'local-config.db',
    });
    expect(JSON.stringify(output)).not.toContain('cc-switch');
    expect(JSON.stringify(output)).not.toContain('cc_switch');
    expect(JSON.stringify(output)).not.toContain('reference_commit');
    expect(input.cc_switch_current_hosts).toEqual(['codex', 'claude-code']);
  });
});


describe('legacy backend compatibility', () => {
  it('blocks unsupported endpoints and HTML fallback payloads', () => {
    expect(backendCompatibility({ error: { status: 404 } })).toBe('upgrade_required');
    expect(backendCompatibility({ data: null, loading: false })).toBe('upgrade_required');
    expect(backendCompatibility({ data: { ok: true }, loading: false })).toBe('upgrade_required');
  });
  it('keeps verified capability data during refresh but never trusts stale data after errors', () => {
    const data = { counts: {}, capabilities: { plan_apply_readback: true } };
    expect(backendCompatibility({ data, loading: true })).toBe('ready');
    expect(backendCompatibility({ data, error: { status: 404 } })).toBe('upgrade_required');
    expect(backendCompatibility({ data, error: { status: 0 } })).toBe('unavailable');
    expect(backendCompatibility({ data: null, loading: true })).toBe('checking');
  });
});

describe('API feedback safety and ambiguity recovery', () => {
  it('does not turn an unreadable successful response into an empty successful result', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.reject(new SyntaxError('invalid JSON')) }));
    await expect(request('/api/content/index')).rejects.toMatchObject({ name: 'ApiError', status: 200 });
  });
  it('retains only actionable task choices from structured errors, never raw server diagnostics', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 422, json: async () => ({ detail: { code: 'ambiguous_task', candidates: [{ id: 'a', title: 'Task A', status: 'in_progress', private_key: 'PRIVATE_SENTINEL' }], message: 'PRIVATE_SENTINEL' } }) }));
    try { await request('/api/handoffs'); throw new Error('Expected failure'); }
    catch (error) {
      expect(error.detail).toEqual({ code: 'ambiguous_task', candidates: [{ id: 'a', title: 'Task A', status: 'in_progress' }] });
      expect(error.message).not.toContain('PRIVATE_SENTINEL'); expect(JSON.stringify(error.detail)).not.toContain('PRIVATE_SENTINEL');
    }
  });
  it('maps skipped damaged sources and partial sync separately from a whole-host outage', () => {
    expect(errorFeedback(500, 'skipped_damaged: 1 damaged sources skipped (unsupported_format); readable history is current')).toBe('部分聊天来源格式无法读取，已跳过损坏文件。其余记录是当前的，不是整台助手同步失败。');
    expect(errorFeedback(500, 'partial_sync: readable sessions processed; 1 unreadable sources require repair: /tmp/broken.jsonl')).toBe('部分聊天记录未能读取。已保存可读记录；请修复损坏来源，或等正在写入的文件稳定后再同步。');
  });
  it('maps deletion and routing reference errors to actionable recovery guidance', () => {
    expect(errorFeedback(409, 'Uninstall owned host entries before deleting this object')).toBe('先从助手移除配置或恢复原选择，再删除资料。');
    expect(errorFeedback(409, 'Restore the active host selection before deleting its provider')).toBe('先从助手移除配置或恢复原选择，再删除资料。');
    expect(errorFeedback(409, 'Change or clear the independent client routing provider, fallbacks or model routes before deleting a referenced definition')).toBe('这份定义仍被连接设置引用。请先更换对应线路或默认模型，再删除定义。');
    expect(errorFeedback(409, 'Referenced provider does not exist in shared definitions; reload providers or remove stale route references before saving')).toBe('所选路由包含已删除或不存在的服务商。请刷新服务商列表并移除失效引用后再保存。');
  });
});

describe('native management failure feedback', () => {
  it.each([
    [422, 'Native SKILL.md frontmatter is not closed; edit the shared definition before applying', 'SKILL.md 元数据'],
    [422, 'This host requires a non-empty Skill description in YAML frontmatter', '非空 description'],
    [422, 'Cursor Skill name must contain lowercase letters, digits and single hyphens and match its folder', 'name 与文件夹一致'],
    [422, "OMP legacy settings.json requires OMP's official settings migration before changing model selection; existing settings were preserved", '官方迁移旧 settings.json'],
    [422, "OMP legacy models.json requires OMP's official models migration before management; creating a parallel models.yml would shadow existing providers", '遮蔽旧服务商'],
    [409, 'Change or restore OMP model roles, fallbacks and task/preset selectors before uninstalling their provider', '模型角色、备用链或任务预设'],
    [422, 'Antigravity truncates rules above 24000 UTF-8 bytes; shorten this prompt before applying', '没有写入截断内容'],
    [422, 'Cursor documents API-key setup in Cursor Settings > Models, not a supported configuration file API', 'Cursor Settings → Models'],
    [422, 'Antigravity Desktop documents plan-dependent models selected in the conversation model menu', '当前账号可用模型'],
    [503, 'Native yaml format runtime unavailable; install requirements-managed-formats.txt', '原文件未被改写'],
  ])('reports native failure %s without misleading ZIP advice or raw diagnostics', async (status, detail, expected) => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status, json: async () => ({ detail }) }));
    try {
      await request('/api/manage/hosts/synthetic/state');
      throw new Error('Expected request failure');
    } catch (error) {
      expect(error.name).toBe('ApiError');
      expect(error.status).toBe(status);
      expect(error.message).toContain(expected);
      expect(error.message).not.toContain('这个 ZIP');
      expect(error.detail).toBeUndefined();
    }
  });
});
