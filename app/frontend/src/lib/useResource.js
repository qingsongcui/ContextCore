import { useCallback, useEffect, useRef, useState } from 'react';
import { request } from './api.js';

/** Never show a previous project's response as the newly selected project's data. */
export function useResource(path, revision = 0) {
  const [state, setState] = useState({ path: null, data: null, loading: Boolean(path), error: null });
  const [refresh, setRefresh] = useState(0);
  const sequence = useRef(0);
  useEffect(() => {
    const current = ++sequence.current;
    if (!path) return;
    const controller = new AbortController();
    setState(previous => ({ path, data: previous.path === path ? previous.data : null, loading: true, error: null }));
    request(path, { signal: controller.signal }).then(data => {
      if (sequence.current === current) setState({ path, data, loading: false, error: null });
    }).catch(error => {
      if (error.name !== 'AbortError' && sequence.current === current) {
        setState(previous => ({ path, data: previous.path === path ? previous.data : null, loading: false, error }));
      }
    });
    return () => controller.abort();
  }, [path, revision, refresh]);
  const reload = useCallback(() => setRefresh(value => value + 1), []);
  return { data: state.path === path ? state.data : null, loading: Boolean(path) && (state.path !== path || state.loading), error: state.path === path ? state.error : null, reload };
}

export function queryString(values) {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(values)) {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value));
  }
  const encoded = query.toString();
  return encoded ? `?${encoded}` : '';
}
