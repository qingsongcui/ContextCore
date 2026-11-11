// Page-wide navigation must not silently retain a previous assistant or project filter.
export function nextLocation(current, next, options = {}) {
  const patch = { ...(typeof next === 'string' ? { page: next } : next), ...options };
  const resetScope = typeof next === 'string' || Boolean(patch.page && patch.page !== current.page);
  const result = { ...current, ...(resetScope ? { agent: '', section: '' } : {}), ...patch };
  if (result.page !== 'projects' && !('project' in patch)) delete result.project;
  return result;
}
