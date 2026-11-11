import { useEffect, useState } from 'react';
import { StatusBadge } from '../ManagementUI.jsx';

const deadlineFor = item => item?.status === 'authorizing' ? item.authorization_expires_at ?? (item.account_id ? item.expires_at : null) : item?.expires_at;

export function accountStatusAt(item, now = Date.now()) {
  const deadline = deadlineFor(item);
  const watchesExpiry = (!item?.kind || item.kind === 'oauth') && ['authorized', 'authorizing'].includes(item?.status) && typeof deadline === 'number' && Number.isFinite(deadline);
  return watchesExpiry && deadline * 1000 <= now ? 'expired' : item?.status;
}

export function useAccountStatus(item) {
  const [now, setNow] = useState(Date.now);
  const deadline = deadlineFor(item);
  const watchesExpiry = (!item?.kind || item.kind === 'oauth') && ['authorized', 'authorizing'].includes(item?.status) && typeof deadline === 'number' && Number.isFinite(deadline);
  useEffect(() => {
    if (!watchesExpiry || deadline * 1000 <= now) return;
    const timer = setTimeout(() => setNow(Date.now()), Math.min(2147483647, Math.max(1, deadline * 1000 - Date.now())));
    return () => clearTimeout(timer);
  }, [watchesExpiry, deadline, now]);
  return accountStatusAt(item, now);
}

export function AccountStatus({ item }) {
  const status = useAccountStatus(item);
  return <span role="status"><StatusBadge value={status} /></span>;
}
