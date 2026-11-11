import { describe, it, expect } from 'vitest';
import { nextLocation } from '../src/lib/navigation.js';
describe('assistant and project navigation scope', () => {
  const current = { page: 'projects', agent: 'codex', section: 'mcp', project: 'p1' };
  it('clears assistant and project filters for global navigation and preserves explicit filters', () => {
    expect(nextLocation(current, 'usage')).toEqual({ page: 'usage', agent: '', section: '' });
    expect(nextLocation(current, 'settings')).toEqual({ page: 'settings', agent: '', section: '' });
    expect(nextLocation(current, 'usage', { project: 'p1' })).toEqual({ page: 'usage', agent: '', section: '', project: 'p1' });
    expect(nextLocation(current, 'agents').agent).toBe('');
    expect(nextLocation(current, 'agents').project).toBeUndefined();
    expect(nextLocation(current, 'usage', { agent: 'omp' }).agent).toBe('omp');
    expect(nextLocation({ page: 'agents', agent: 'codex', section: 'mcp' }, { section: 'skill' })).toEqual({ page: 'agents', agent: 'codex', section: 'skill' });
    expect(nextLocation(current, { project: 'p2' })).toEqual({ ...current, project: 'p2' });
  });
});
