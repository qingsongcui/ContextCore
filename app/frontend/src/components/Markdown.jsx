import { Component, lazy, Suspense, useState } from 'react';
import { t, getLang } from '../lib/i18n.jsx';
import { Pagination } from './Pagination.jsx';

const ParsedBody = lazy(() => import('./MarkdownBody.jsx'));

// Bound parsing work per body. Every character remains reachable in plain-text pages.
export const MARKDOWN_LIMIT = 12000;
class BodyBoundary extends Component {
  state = { failed: false };
  static getDerivedStateFromError() { return { failed: true }; }
  render() {
    return this.state.failed ? <div><p role="status">{t('正文排版暂时不可用，以下显示原文。')}</p><PlainBody text={this.props.text} /></div> : this.props.children;
  }
}
function PlainBody({ text }) {
  const [offset, setOffset] = useState(0);
  const boundary = index => index > 0 && /[\uDC00-\uDFFF]/.test(text[index] || '') ? index - 1 : index;
  return <><pre className="markdown-plain">{text.slice(boundary(offset), boundary(offset + MARKDOWN_LIMIT))}</pre>{text.length > MARKDOWN_LIMIT ? <Pagination label={t('第 {page} / {pages} 段', { page: Math.floor(offset / MARKDOWN_LIMIT) + 1, pages: Math.ceil(text.length / MARKDOWN_LIMIT) })} total={text.length} offset={offset} limit={MARKDOWN_LIMIT} onChange={setOffset} /> : null}</>;
}
export function Markdown({ children, onInternalLink }) {
  const text = typeof children === 'string' ? children : '';
  if (!text.trim()) return <div className="markdown empty"><em>{t('暂无正文')}</em></div>;
  return <div className="markdown"><BodyBoundary key={text} text={text}>{text.length > MARKDOWN_LIMIT ? <><p className="muted">{t('正文较长，按段显示原文。使用上一页、下一页可阅读全部内容。')}</p><PlainBody key={text} text={text} /></> : <Suspense fallback={<div aria-busy="true"><PlainBody text={text} /></div>}><ParsedBody text={text} onInternalLink={onInternalLink} lang={getLang()} /></Suspense>}</BodyBoundary></div>;
}
