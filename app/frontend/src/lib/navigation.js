// Page-wide navigation must not silently retain a previous assistant or project filter.
export function nextLocation(current, next, options = {}) {
  const patch = { ...(typeof next === 'string' ? { page: next } : next), ...options };
  const resetScope = typeof next === 'string' || Boolean(patch.page && patch.page !== current.page);
  const result = { ...current, ...(resetScope ? { agent: '', section: '' } : {}), ...patch };
  if (resetScope || ('project' in patch && patch.project !== current.project) || 'section' in patch) {
    for (const field of ['record', 'view']) if (!(field in patch)) delete result[field];
    if ('project' in patch && patch.project !== current.project && !('section' in patch) && ['memory','document','conversations','overview'].includes(current.section)) delete result.section;
  }
  if (result.page !== 'usage' || !('request' in patch)) delete result.request;
  if (result.page !== 'projects' && !('project' in patch)) delete result.project;
  return result;
}
