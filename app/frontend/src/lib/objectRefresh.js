import { createContext, useContext, useRef } from 'react';

export const ObjectRefreshContext = createContext(null);

export function eventAffectsObject(event, kind, id) {
  if (!event || event.type === 'changed' || event.type === 'message') return true;
  const data = event.data || {};
  if (event.type === 'audit.created') {
    const action = data.action || '';
    if (!action) return true;
    // Project changes can invalidate ownership and handoff sources.
    if (action.startsWith('project.')) return true;
    if (!action.startsWith(`${kind}.`)) return false;
    const target = data.target;
    return typeof target !== 'string' || !target || target === id || target.startsWith(`${id}@`) || target.startsWith(`${id}->`);
  }
  if (event.type !== `${kind}.changed`) return false;
  const target = data.id || data[`${kind}_id`];
  return typeof target !== 'string' || !target || target === id;
}

export function useObjectRevision(fallback, kind, id) {
  const batch = useContext(ObjectRefreshContext);
  const seen = useRef(null);
  const tick = useRef(0);
  if (batch && batch !== seen.current) {
    if (!seen.current || batch.manual !== seen.current.manual || batch.events.some(event => eventAffectsObject(event, kind, id))) tick.current += 1;
    seen.current = batch;
  }
  return batch ? tick.current : fallback;
}
