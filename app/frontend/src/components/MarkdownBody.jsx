import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';

const remarkPlugins = [remarkGfm];
const rehypePlugins = [rehypeHighlight];
const noHighlight = [];

export default memo(function MarkdownBody({ text, onInternalLink }) {
  return <ReactMarkdown rehypePlugins={text.length <= 4000 ? rehypePlugins : noHighlight} remarkPlugins={remarkPlugins} components={{
    a: ({ href = '', children, ...props }) => {
      delete props.node;
      if (href.startsWith('#contextcore-wiki/')) {
        let target;
        try { target = decodeURIComponent(href.slice('#contextcore-wiki/'.length)); }
        catch { return <span>{children}</span>; }
        return <a {...props} href={href} className="wiki-link" onClick={event => { event.preventDefault(); onInternalLink?.(target); }}>{children}</a>;
      }
      return <a {...props} href={href} target="_blank" rel="noreferrer">{children}</a>;
    }
  }}>{text}</ReactMarkdown>;
});
