(() => {
  const lang = () =>
    (document.documentElement.getAttribute('lang') || '').toLowerCase().startsWith('zh') ? 'zh' : 'en';

  const track = (name, extra = {}) => {
    const payload = { event: name, lang: lang(), path: location.pathname, ...extra };
    try {
      window.dataLayer = window.dataLayer || [];
      window.dataLayer.push(payload);
    } catch {
      /* optional */
    }
    try {
      if (typeof window.clarity === 'function') {
        window.clarity('event', name);
        window.clarity('set', 'event_lang', payload.lang);
      }
    } catch {
      /* Clarity is optional in preview builds */
    }
  };

  document.addEventListener(
    'click',
    (event) => {
      const anchor = event.target?.closest?.('[data-macos-download]');
      if (!anchor) return;
      track('download_click', { href: anchor.getAttribute('href') || '' });
    },
    true,
  );

  if (/(?:^|\/)guide\.html$/.test(location.pathname || '')) {
    track('install_guide_view');
  }
})();
