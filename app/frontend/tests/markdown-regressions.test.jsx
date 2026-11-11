import { describe, expect, it } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { Markdown, MARKDOWN_LIMIT } from '../src/components/Markdown.jsx';
import MarkdownBody from '../src/components/MarkdownBody.jsx';
describe('actual Markdown rendering', () => {
  it('renders malformed wiki links locally without throwing or unsafe navigation', () => {
    const html = renderToStaticMarkup(<MarkdownBody text="[bad](#contextcore-wiki/%E0%A4%A)" />);
    expect(html).toContain('<span>bad</span>'); expect(html).not.toContain('href=');
  });
  it('preserves valid wiki links', () => {
    expect(renderToStaticMarkup(<MarkdownBody text="[good](#contextcore-wiki/hello%20world)" />)).toContain('href="#contextcore-wiki/hello%20world"');
  });
  it('bounds long-body rendering with text sections and exposes navigation', () => {
    const body = '# Heading\n```js\nconst x = 1;\n```\n'.repeat(2000) + 'LAST_SENTINEL';
    const started = performance.now();
    const html = renderToStaticMarkup(<Markdown>{body}</Markdown>);
    const elements = (html.match(/<[a-z][^>]*>/g) || []).length;
    console.log(JSON.stringify({ bodyCharacters: body.length, sectionCharacters: MARKDOWN_LIMIT, htmlElements: elements, renderMs: performance.now() - started }));
    expect(elements).toBeLessThan(20); expect(html).toContain('下一页'); expect(html).not.toContain('hljs');
    expect(html.length).toBeLessThan(MARKDOWN_LIMIT * 2); expect(html).not.toContain('LAST_SENTINEL');
  });
});
