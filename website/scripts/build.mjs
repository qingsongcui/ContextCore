import { readFile, mkdir, cp, writeFile, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import brand from '../brand.config.mjs';
import { bundleCss } from './css-bundle.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const out = path.join(root, 'dist');
const RELEASE_VERSION = brand.releaseVersion;
const releaseDownloadUrl = `${brand.github}/releases/download/v${RELEASE_VERSION}/Senrelay-macOS.zip`;
const FALLBACK_LASTMOD = new Date().toISOString().slice(0, 10);
const rawClarityId = (process.env.CLARITY_ID || '').trim();
if (rawClarityId && !/^[a-zA-Z0-9]{6,24}$/.test(rawClarityId)) {
  throw new Error(`Invalid CLARITY_ID: ${rawClarityId}`);
}
const clarityId = rawClarityId;
const rawCfToken = (process.env.CF_ANALYTICS_TOKEN || '').trim();
if (rawCfToken && !/^[a-fA-F0-9]{16,64}$/.test(rawCfToken)) {
  throw new Error('Invalid CF_ANALYTICS_TOKEN format');
}
const cfToken = rawCfToken;

function deploymentUrl(value) {
  if (!value) return null;
  const url = new URL(value);
  if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
    throw new Error('SITE_URL must be an http(s) URL without credentials, query, or hash.');
  }
  if (!url.pathname.endsWith('/')) url.pathname += '/';
  return url;
}

const site = deploymentUrl(brand.siteUrl);
const indexNowKey = (process.env.INDEXNOW_KEY || '').trim();
if (indexNowKey && !/^[a-zA-Z0-9-]{8,128}$/.test(indexNowKey)) {
  throw new Error('INDEXNOW_KEY must be 8-128 ASCII alphanumeric or hyphen characters.');
}

const escapeHtml = (value) =>
  String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('"', '&quot;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;');

const decodeHtml = (value) =>
  String(value)
    .replaceAll('&nbsp;', ' ')
    .replaceAll('&quot;', '"')
    .replaceAll('&#39;', "'")
    .replaceAll('&lt;', '<')
    .replaceAll('&gt;', '>')
    .replaceAll('&amp;', '&');

const escapeJson = (value) => JSON.stringify(value).replaceAll('<', '\\u003c');

function stripHtmlTags(fragment) {
  return decodeHtml(fragment.replace(/<[^>]+>/g, ' ')).replace(/\s+/g, ' ').trim();
}

function applyBrandTokens(source, { htmlEscaped = true } = {}) {
  const wrap = htmlEscaped ? escapeHtml : (v) => v;
  return source
    .replaceAll('https://github.com/qingsongcui/ContextCore', '__BRAND_GITHUB_URL__')
    .replaceAll('Senrelay', wrap(brand.name))
    .replaceAll('SENRELAY', wrap(brand.name.toUpperCase()))
    .replaceAll('续境', wrap(brand.chineseName))
    .replaceAll('换个 Agent，思路接着走。', wrap(brand.tagline))
    .replaceAll('__BRAND_GITHUB_URL__', wrap(brand.github));
}

function gitLastmod(relFiles) {
  try {
    const dirty = execFileSync('git', ['status', '--porcelain', '--', ...relFiles], {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    if (dirty) return FALLBACK_LASTMOD;
    const outDate = execFileSync('git', ['log', '-1', '--format=%cs', '--', ...relFiles], {
      cwd: root,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    }).trim();
    if (/^\d{4}-\d{2}-\d{2}$/.test(outDate)) return outDate;
  } catch {
    // Fallback when building in an isolated temp directory outside git.
  }
  return FALLBACK_LASTMOD;
}

const voidTags = new Set([
  'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input',
  'link', 'meta', 'param', 'source', 'track', 'wbr',
]);

function replaceI18nHtmlBlocks(source, htmlEn) {
  let result = '';
  let cursor = 0;
  const openTagRe = /<([a-zA-Z][\w:-]*)\b[^>]*\bdata-i18n-html="([^"]+)"[^>]*>/g;
  let match;
  while ((match = openTagRe.exec(source)) !== null) {
    if (match.index < cursor) continue;
    const tagName = match[1].toLowerCase();
    const key = match[2];
    const openEnd = match.index + match[0].length;
    result += source.slice(cursor, openEnd);

    let depth = 1;
    const tagScan = new RegExp(`<\\/?${tagName}\\b[^>]*>`, 'gi');
    tagScan.lastIndex = openEnd;
    let scan;
    let closeStart = openEnd;
    let closeEnd = openEnd;
    while (depth > 0 && (scan = tagScan.exec(source)) !== null) {
      if (scan[0].startsWith('</')) {
        depth -= 1;
        if (depth === 0) {
          closeStart = scan.index;
          closeEnd = scan.index + scan[0].length;
        }
      } else if (!voidTags.has(tagName) && !scan[0].endsWith('/>')) {
        depth += 1;
      }
    }
    const replacementInner = htmlEn[key] ?? source.slice(openEnd, closeStart);
    result += replacementInner + source.slice(closeStart, closeEnd);
    cursor = closeEnd;
    openTagRe.lastIndex = cursor;
  }
  result += source.slice(cursor);
  return result;
}

function removeElementByAttr(source, tagName, attrSnippet) {
  const lowerTag = tagName.toLowerCase();
  const openRe = new RegExp(`<${lowerTag}\\b[^>]*\\b${attrSnippet.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[^>]*>`, 'i');
  const match = openRe.exec(source);
  if (!match) return source;
  const start = match.index;
  const openEnd = start + match[0].length;
  let depth = 1;
  const scanRe = new RegExp(`<\\/?${lowerTag}\\b[^>]*>`, 'gi');
  scanRe.lastIndex = openEnd;
  let scan;
  while (depth > 0 && (scan = scanRe.exec(source)) !== null) {
    if (scan[0].startsWith('</')) {
      depth -= 1;
      if (depth === 0) {
        return source.slice(0, start) + source.slice(scan.index + scan[0].length);
      }
    } else if (!voidTags.has(lowerTag) && !scan[0].endsWith('/>')) {
      depth += 1;
    }
  }
  return source;
}

function translateStaticTextNodes(source, textEn) {
  const stack = [];
  let outStr = '';
  let cursor = 0;
  for (const match of source.matchAll(/<\/?([a-zA-Z][\w:-]*)\b[^>]*>/g)) {
    const rawText = source.slice(cursor, match.index);
    const skipping = stack.some((entry) => entry.skip);
    if (!skipping && rawText.trim()) {
      const decodedCore = decodeHtml(rawText.trim());
      if (Object.hasOwn(textEn, decodedCore)) {
        const lead = rawText.match(/^\s*/)[0];
        const trail = rawText.match(/\s*$/)[0];
        outStr += lead + escapeHtml(textEn[decodedCore]) + trail;
      } else {
        outStr += rawText;
      }
    } else {
      outStr += rawText;
    }

    const tag = match[1].toLowerCase();
    const rawTag = match[0];
    if (rawTag.startsWith('</')) {
      const idx = stack.findLastIndex((entry) => entry.tag === tag);
      if (idx !== -1) stack.splice(idx);
    } else if (!voidTags.has(tag) && !rawTag.endsWith('/>')) {
      const shouldSkip =
        skipping ||
        tag === 'script' ||
        tag === 'style' ||
        /\bdata-i18n-html=/.test(rawTag) ||
        /\bclass="language-switch"/.test(rawTag);
      stack.push({ tag, skip: shouldSkip });
    }
    outStr += rawTag;
    cursor = match.index + rawTag.length;
  }
  outStr += source.slice(cursor);
  return outStr;
}

function translateAttributes(source, attrEn) {
  return source.replace(/\b(aria-label|title|placeholder)="([^"]*)"/g, (full, attr, val) => {
    const decoded = decodeHtml(val);
    if (Object.hasOwn(attrEn, decoded)) {
      return `${attr}="${escapeHtml(attrEn[decoded])}"`;
    }
    return full;
  });
}

function extractFaqEntities(faqBlockHtml) {
  const entities = [];
  for (const [, body] of faqBlockHtml.matchAll(/<details\b[^>]*>([\s\S]*?)<\/details>/g)) {
    const summaryMatch = body.match(/<summary\b[^>]*>([\s\S]*?)<\/summary>/);
    const answerMatches = [...body.matchAll(/<p\b[^>]*>([\s\S]*?)<\/p>/g)];
    if (!summaryMatch || answerMatches.length === 0) continue;
    const rawQuestion = summaryMatch[1].replace(/<span\b[^>]*aria-hidden="true"[^>]*>[\s\S]*?<\/span>/g, '');
    const question = stripHtmlTags(rawQuestion);
    const answer = answerMatches.map((m) => stripHtmlTags(m[1])).join(' ');
    if (question && answer) {
      entities.push({
        '@type': 'Question',
        name: question,
        acceptedAnswer: {
          '@type': 'Answer',
          text: answer,
        },
      });
    }
  }
  return entities;
}

function buildPersonNode() {
  const creatorId = site ? `${site.href}#creator` : '#creator';
  return {
    '@type': 'Person',
    '@id': creatorId,
    name: "Qingsong Cui (George O'Nair)",
    alternateName: ['Qingsong Cui', "George O'Nair", 'George_ONair', 'qingsongcui'],
    url: 'https://github.com/qingsongcui',
    sameAs: ['https://github.com/qingsongcui', 'https://x.com/George_ONair'],
  };
}

function buildHomepageSchema({ lang, pageUrl, siteDesc, appDesc, faqEntities, lastmod }) {
  const zhFullName = `${brand.name} · ${brand.chineseName}`;
  const displayName = lang === 'en' ? brand.name : zhFullName;
  const altNames = [...new Set([brand.name, brand.chineseName, zhFullName, 'ContextCore'].filter(Boolean))];
  const releaseTagUrl = `${brand.github}/releases/tag/v${RELEASE_VERSION}`;
  const orgId = site ? `${site.href}#organization` : '#organization';
  const websiteId = site ? `${pageUrl}#website` : '#website';
  const appId = site ? `${site.href}#software` : '#software';
  const personNode = buildPersonNode();
  const creatorId = personNode['@id'];

  const isEn = lang === 'en';
  const webSiteKeywords = isEn
    ? [brand.name, 'ContextCore', 'cross-agent conversation relay', 'MCP config sync', 'Claude Code to Codex', 'Cursor MCP', 'macOS Apple Silicon developer workbench', 'coding agent management', 'Claude Code and Codex usage', 'AGENTS.md']
    : [brand.name, brand.chineseName, 'ContextCore', '跨 Agent 对话接力', 'Claude Code 接力 Codex', 'MCP 配置同步', 'AI 编程助手配置管理', 'Claude Code 与 Codex 用量', 'macOS 开发工作台'];
  const disambiguation = isEn
    ? `Local-first Apple Silicon macOS developer workbench by Qingsong Cui (github.com/qingsongcui/ContextCore) for /contextcore cross-agent relay and multi-host MCP sync; distinct from unrelated cloud context-core.dev, PyPI contextcore packages, and hardware relay parts.`
    : `由 Qingsong Cui（github.com/qingsongcui/ContextCore）维护的 Apple Silicon macOS 本地多 AI 编程助手接力与 MCP 配置工作台；与汽车继电器（relay sen）及第三方 context-core.dev、PyPI contextcore 包无关。`;

  const webSiteNode = {
    '@type': 'WebSite',
    '@id': websiteId,
    name: displayName,
    alternateName: altNames,
    ...(site ? { url: pageUrl } : {}),
    inLanguage: isEn ? 'en' : 'zh-CN',
    description: siteDesc,
    keywords: webSiteKeywords,
    disambiguatingDescription: disambiguation,
    publisher: { '@id': orgId },
  };

  const appNode = {
    '@type': 'SoftwareApplication',
    '@id': appId,
    name: displayName,
    alternateName: altNames,
    applicationCategory: 'DeveloperApplication',
    operatingSystem: 'macOS 14.0+ (Apple Silicon arm64)',
    softwareVersion: RELEASE_VERSION,
    dateModified: lastmod,
    isAccessibleForFree: true,
    offers: {
      '@type': 'Offer',
      price: '0',
      priceCurrency: 'USD',
    },
    description: appDesc,
    keywords: webSiteKeywords,
    disambiguatingDescription: disambiguation,
    ...(site
      ? {
          url: pageUrl,
          image: new URL('assets/icon-192.png', site).href,
        }
      : {}),
    downloadUrl: releaseDownloadUrl,
    codeRepository: brand.github,
    sameAs: [brand.github, releaseTagUrl],
    license: `${brand.github}/blob/main/LICENSE`,
    author: [{ '@id': orgId }, { '@id': creatorId }],
  };

  const orgNode = {
    '@type': 'Organization',
    '@id': orgId,
    name: displayName,
    alternateName: altNames,
    founder: { '@id': creatorId },
    ...(site
      ? {
          url: site.href,
          logo: {
            '@type': 'ImageObject',
            url: new URL('assets/icon-192.png', site).href,
            width: 192,
            height: 192,
          },
        }
      : {}),
    sameAs: [brand.github, 'https://github.com/qingsongcui', 'https://x.com/George_ONair'],
  };

  const faqNode = {
    '@type': 'FAQPage',
    ...(site ? { '@id': `${pageUrl}#faq`, url: pageUrl } : {}),
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    mainEntity: faqEntities,
  };

  return {
    '@context': 'https://schema.org',
    '@graph': [webSiteNode, appNode, orgNode, personNode, faqNode],
  };
}

function buildGuideSchema({
  lang,
  pageUrl,
  homeUrl,
  headline,
  description,
  faqEntities = [],
  lastmod,
  published = '2026-11-11',
  proficiencyLevel = 'Beginner',
}) {
  const zhFullName = `${brand.name} · ${brand.chineseName}`;
  const displayName = lang === 'en' ? brand.name : zhFullName;
  const orgId = site ? `${site.href}#organization` : '#organization';
  const appId = site ? `${site.href}#software` : '#software';
  const personNode = buildPersonNode();
  const creatorId = personNode['@id'];

  const articleNode = {
    '@type': 'TechArticle',
    ...(site
      ? {
          '@id': `${pageUrl}#article`,
          url: pageUrl,
          mainEntityOfPage: pageUrl,
          image: new URL('assets/icon-192.png', site).href,
        }
      : {}),
    headline,
    description,
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    datePublished: published,
    dateModified: lastmod,
    proficiencyLevel,
    author: { '@type': 'Person', '@id': creatorId, name: personNode.name },
    publisher: { '@type': 'Organization', '@id': orgId, name: displayName },
    about: { '@type': 'SoftwareApplication', '@id': appId, name: displayName },
  };

  const faqNode = {
    '@type': 'FAQPage',
    ...(site ? { '@id': `${pageUrl}#faq`, url: `${pageUrl}#faq` } : {}),
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    mainEntity: faqEntities,
  };

  const breadcrumbNode = {
    '@type': 'BreadcrumbList',
    itemListElement: [
      {
        '@type': 'ListItem',
        position: 1,
        name: lang === 'en' ? `${brand.name} Home` : `${zhFullName} 首页`,
        ...(site ? { item: homeUrl } : {}),
      },
      {
        '@type': 'ListItem',
        position: 2,
        name: headline,
        ...(site ? { item: pageUrl } : {}),
      },
    ],
  };

  return {
    '@context': 'https://schema.org',
    '@graph': [articleNode, faqNode, breadcrumbNode, personNode],
  };
}

function buildCompareSchema({ lang, pageUrl, homeUrl, headline, description, faqEntities, lastmod }) {
  const zhFullName = `${brand.name} · ${brand.chineseName}`;
  const displayName = lang === 'en' ? brand.name : zhFullName;
  const orgId = site ? `${site.href}#organization` : '#organization';
  const appId = site ? `${site.href}#software` : '#software';
  const personNode = buildPersonNode();
  const creatorId = personNode['@id'];

  const articleNode = {
    '@type': 'TechArticle',
    ...(site
      ? {
          '@id': `${pageUrl}#article`,
          url: pageUrl,
          mainEntityOfPage: pageUrl,
          image: new URL('assets/icon-192.png', site).href,
        }
      : {}),
    headline,
    description,
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    datePublished: '2026-11-11',
    dateModified: lastmod,
    author: { '@type': 'Person', '@id': creatorId, name: personNode.name },
    publisher: { '@type': 'Organization', '@id': orgId, name: displayName },
    about: { '@type': 'SoftwareApplication', '@id': appId, name: displayName },
  };

  const optionUrl = (n) => (site ? `${pageUrl}#option-${n}` : `#option-${n}`);
  const itemListNode = {
    '@type': 'ItemList',
    ...(site ? { '@id': `${pageUrl}#options`, url: `${pageUrl}#options` } : {}),
    name:
      lang === 'en'
        ? '5 Ways to Share Context, Memory & MCP Across AI Coding Agents (2026)'
        : '2026 多 AI 编程助手共享上下文、记忆与同步 MCP 的 5 种方案对比',
    itemListOrder: 'https://schema.org/ItemListOrderAscending',
    numberOfItems: 5,
    itemListElement:
      lang === 'en'
        ? [
            {
              '@type': 'ListItem',
              position: 1,
              url: optionUrl(1),
              name: 'Repo-Committed Instruction Files (CLAUDE.md + AGENTS.md + .cursor/rules)',
              description: '$0 cross-platform standard for static Git-versioned conventions; no live session relay or global MCP sync.',
            },
            {
              '@type': 'ListItem',
              position: 2,
              url: optionUrl(2),
              name: 'Cloud or Docker-Hosted Memory MCP Servers (Mem0 MCP / OpenMemory)',
              description: 'Cross-device semantic fact store across Windows, Linux, and Mac; does not index native local chats or host configs.',
            },
            {
              '@type': 'ListItem',
              position: 3,
              url: optionUrl(3),
              name: `${brand.name} (ContextCore Local macOS Multi-Agent Workbench)`,
              description: '$0 MIT single-Mac (macOS 14.0+ arm64) workbench combining /contextcore full-transcript relay, shared memory, and 6+9 host MCP/Skills sync.',
              item: { '@type': 'SoftwareApplication', '@id': appId, name: displayName },
            },
            {
              '@type': 'ListItem',
              position: 4,
              url: optionUrl(4),
              name: 'Dotfiles Symlinks & Custom Config Scripts (GNU Stow / jq-toml Scripts)',
              description: '$0 DIY terminal sync for static files; prone to JSON/TOML/JSON5 conflicts and lacks session relay.',
            },
            {
              '@type': 'ListItem',
              position: 5,
              url: optionUrl(5),
              name: 'Single Multi-Model IDE or API Router (Cursor / OpenCode + OpenRouter)',
              description: 'Zero client switching by swapping LLMs inside one harness; context stays isolated when rate limits force tool switches.',
            },
          ]
        : [
            {
              '@type': 'ListItem',
              position: 1,
              url: optionUrl(1),
              name: '仓库内静态指令文件（CLAUDE.md + AGENTS.md + .cursor/rules）',
              description: '$0 全平台随 Git 共享静态规则；不支持中途会话接力或全局 MCP 配置同步。',
            },
            {
              '@type': 'ListItem',
              position: 2,
              url: optionUrl(2),
              name: '云端或 Docker 自托管记忆 MCP 服务（Mem0 MCP / OpenMemory）',
              description: '适合跨 Windows/Linux/Mac 多设备同步语义事实；不索引本地原生会话也不管宿主配置文件。',
            },
            {
              '@type': 'ListItem',
              position: 3,
              url: optionUrl(3),
              name: `${zhFullName}（ContextCore 本机多 Agent 接力与配置工作台）`,
              description: '$0 MIT 开源单机 macOS 14.0+ arm64 工作台，整合 /contextcore 全文接力、伴生共享记忆与 6+9 宿主 MCP/Skills 同步。',
              item: { '@type': 'SoftwareApplication', '@id': appId, name: displayName },
            },
            {
              '@type': 'ListItem',
              position: 4,
              url: optionUrl(4),
              name: 'Dotfiles 软链接与自定义配置同步脚本（GNU Stow / Shell 脚本）',
              description: '$0 极客手写脚本同步全局规则；跨 JSON/TOML/JSON5 易冲突且不支持对话接力。',
            },
            {
              '@type': 'ListItem',
              position: 5,
              url: optionUrl(5),
              name: '单一多模型 IDE 或聚合路由（Cursor / OpenCode + OpenRouter）',
              description: '固定单一外壳切换多模型；遇限流换工具时上下文仍中断。',
            },
          ],
  };

  const faqNode = {
    '@type': 'FAQPage',
    ...(site ? { '@id': `${pageUrl}#faq`, url: `${pageUrl}#faq` } : {}),
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    mainEntity: faqEntities,
  };

  const breadcrumbNode = {
    '@type': 'BreadcrumbList',
    itemListElement: [
      {
        '@type': 'ListItem',
        position: 1,
        name: lang === 'en' ? `${brand.name} Home` : `${zhFullName} 首页`,
        ...(site ? { item: homeUrl } : {}),
      },
      {
        '@type': 'ListItem',
        position: 2,
        name: headline,
        ...(site ? { item: pageUrl } : {}),
      },
    ],
  };

  return {
    '@context': 'https://schema.org',
    '@graph': [articleNode, itemListNode, faqNode, breadcrumbNode, personNode],
  };
}

function renderAnalyticsHeadTags() {
  if (!site) return [];
  const tags = [];
  if (clarityId) {
    tags.push(
      `<script>(function(c,l,a,r,i,t,y){c[a]=c[a]||function(){(c[a].q=c[a].q||[]).push(arguments)};t=l.createElement(r);t.async=1;t.src="https://www.clarity.ms/tag/"+i;y=l.getElementsByTagName(r)[0];y.parentNode.insertBefore(t,y);})(window,document,"clarity","script","${escapeHtml(clarityId)}");</script>`,
    );
  }
  if (cfToken) {
    tags.push(
      `<script defer src="https://static.cloudflareinsights.com/beacon.min.js" data-cf-beacon='{"token":"${escapeHtml(cfToken)}"}'></script>`,
    );
  }
  return tags;
}

function renderDeploymentHead({ canonicalHref, zhHref, enHref, mdHref, altOgLocale, imageAlt }) {
  if (!site) return '';
  const socialImg = escapeHtml(new URL('assets/icon-192.png', site).href);
  const llmsHref = escapeHtml(new URL('llms.txt', site).href);
  const llmsFullHref = escapeHtml(new URL('llms-full.txt', site).href);
  const pricingMdHref = escapeHtml(new URL('pricing.md', site).href);
  return [
    `<link rel="canonical" href="${escapeHtml(canonicalHref)}">`,
    `<link rel="alternate" hreflang="zh-CN" href="${escapeHtml(zhHref)}">`,
    `<link rel="alternate" hreflang="zh" href="${escapeHtml(zhHref)}">`,
    `<link rel="alternate" hreflang="en" href="${escapeHtml(enHref)}">`,
    `<link rel="alternate" hreflang="x-default" href="${escapeHtml(enHref)}">`,
    ...(mdHref ? [`<link rel="alternate" type="text/markdown" href="${escapeHtml(mdHref)}" title="Markdown Mirror">`] : []),
    `<link rel="alternate" type="text/markdown" href="${pricingMdHref}" title="Pricing &amp; License (Markdown)">`,
    `<link rel="alternate" type="text/plain" href="${llmsHref}" title="LLMs.txt">`,
    `<link rel="alternate" type="text/plain" href="${llmsFullHref}" title="LLMs-full.txt">`,
    `<meta property="og:locale:alternate" content="${escapeHtml(altOgLocale)}">`,
    `<meta property="og:url" content="${escapeHtml(canonicalHref)}">`,
    `<meta property="og:image" content="${socialImg}">`,
    `<meta property="og:image:width" content="192">`,
    `<meta property="og:image:height" content="192">`,
    `<meta property="og:image:alt" content="${escapeHtml(`${brand.name} logo`)}">`,
    `<meta name="twitter:image" content="${socialImg}">`,
    `<meta name="twitter:image:alt" content="${escapeHtml(`${brand.name} logo`)}">`,
    ...renderAnalyticsHeadTags(),
  ].join('\n  ');
}

function renderLegacyEnRedirect({ enTitle, targetCanonical, relTarget }) {
  const robotsValue = site ? 'noindex, follow' : 'noindex, nofollow';
  const refreshTarget = site ? targetCanonical : relTarget;
  const canonicalTag = site ? `\n  <link rel="canonical" href="${escapeHtml(targetCanonical)}">` : '';
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#121412">
  <meta name="robots" content="${robotsValue}">${canonicalTag}
  <meta http-equiv="refresh" content="0; url=${escapeHtml(refreshTarget)}">
  <title>${escapeHtml(enTitle)}</title>
  <script>
    (function () {
      var dest = ${JSON.stringify(relTarget)} + (window.location.search || '') + (window.location.hash || '');
      window.location.replace(dest);
    })();
  </script>
</head>
<body>
  <main id="main">
    <h1>Moved to Primary English Edition</h1>
    <p>Redirecting to <a href="${escapeHtml(relTarget)}">${escapeHtml(enTitle)}</a>…</p>
  </main>
</body>
</html>
`;
}

function convertInlineHtmlToMd(fragment, basePageHref) {
  return decodeHtml(
    fragment
      .replace(/<span\b[^>]*\bclass="sr-only"[^>]*>[\s\S]*?<\/span>/gi, '')
      .replace(/<(?:span|b|i)\b[^>]*\baria-hidden="true"[^>]*>[\s\S]*?<\/(?:span|b|i)>/gi, '')
      .replace(/<code\b[^>]*>([\s\S]*?)<\/code>/gi, (_, inner) => `\`${stripHtmlTags(inner)}\``)
      .replace(/<(?:strong|b)\b[^>]*>([\s\S]*?)<\/(?:strong|b)>/gi, (_, inner) => `**${stripHtmlTags(inner)}**`)
      .replace(/<a\b[^>]*\bhref="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi, (_, href, inner) => {
        const label = stripHtmlTags(inner);
        if (!label) return '';
        let resolvedHref = decodeHtml(href);
        if (site && (resolvedHref.startsWith('./') || resolvedHref.startsWith('../') || resolvedHref.startsWith('#'))) {
          resolvedHref = new URL(resolvedHref, basePageHref).href;
        }
        return `[${label}](${resolvedHref})`;
      })
      .replace(/<br\s*\/?>/gi, ' ')
      .replace(/<[^>]+>/g, ' '),
  )
    .replace(/\s+/g, ' ')
    .trim();
}

function htmlMainToMarkdown({ docHtml, title, canonicalHref, lastmod, lang = 'en' }) {
  const mainMatch = docHtml.match(/<main\b[^>]*>([\s\S]*?)<\/main>/i);
  let body = mainMatch ? mainMatch[1] : docHtml;
  const basePageHref = site ? canonicalHref : './';

  // Strip purely visual/decorative blocks that add noise for coding agents
  for (const attr of [
    'class="hero-visual"',
    'class="project-visual"',
    'class="memory-visual"',
    'class="document-visual"',
    'class="agent-workflow-preview"',
    'class="picker-grid"',
  ]) {
    let prev;
    do {
      prev = body;
      body = removeElementByAttr(body, 'div', attr);
    } while (body !== prev);
  }
  body = body.replace(/<svg\b[\s\S]*?<\/svg>/gi, '');

  const codeBlocks = [];
  body = body.replace(/<pre\b[^>]*>\s*<code\b[^>]*>([\s\S]*?)<\/code>\s*<\/pre>/gi, (_, rawCode) => {
    const cleanCode = decodeHtml(rawCode.replace(/<[^>]+>/g, '')).trim();
    const idx = codeBlocks.length;
    codeBlocks.push(`\n\n\`\`\`bash\n${cleanCode}\n\`\`\`\n\n`);
    return `\n\n__MD_CODE_BLOCK_${idx}__\n\n`;
  });

  // Convert HTML tables into GFM Markdown tables
  body = body.replace(/<table\b[^>]*>([\s\S]*?)<\/table>/gi, (_, tableInner) => {
    const captionMatch = tableInner.match(/<caption\b[^>]*>([\s\S]*?)<\/caption>/i);
    const caption = captionMatch ? `**${convertInlineHtmlToMd(captionMatch[1], basePageHref)}**\n\n` : '';
    const rows = [];
    for (const [, trInner] of tableInner.matchAll(/<tr\b[^>]*>([\s\S]*?)<\/tr>/gi)) {
      const cells = [...trInner.matchAll(/<(?:th|td)\b[^>]*>([\s\S]*?)<\/(?:th|td)>/gi)].map((m) =>
        convertInlineHtmlToMd(m[1], basePageHref).replace(/\|/g, '\\|'),
      );
      if (cells.length > 0) rows.push(cells);
    }
    if (rows.length === 0) return '';
    const colCount = Math.max(...rows.map((r) => r.length));
    const padRow = (r) => {
      const copy = [...r];
      while (copy.length < colCount) copy.push('');
      return `| ${copy.join(' | ')} |`;
    };
    const headerLine = padRow(rows[0]);
    const dividerLine = `| ${Array(colCount).fill('---').join(' | ')} |`;
    const dataLines = rows.slice(1).map(padRow);
    return `\n\n${caption}${[headerLine, dividerLine, ...dataLines].join('\n')}\n\n`;
  });

  // Convert <details><summary>Q</summary>A</details>
  body = body.replace(/<details\b[^>]*>\s*<summary\b[^>]*>([\s\S]*?)<\/summary>([\s\S]*?)<\/details>/gi, (_, sumHtml, restHtml) => {
    const heading = convertInlineHtmlToMd(sumHtml, basePageHref);
    return `\n\n### ${heading}\n\n${restHtml}\n\n`;
  });

  // Convert headings, list items, and paragraphs
  body = body
    .replace(/<h1\b[^>]*>([\s\S]*?)<\/h1>/gi, (_, h) => `\n\n# ${convertInlineHtmlToMd(h, basePageHref)}\n\n`)
    .replace(/<h2\b[^>]*>([\s\S]*?)<\/h2>/gi, (_, h) => `\n\n## ${convertInlineHtmlToMd(h, basePageHref)}\n\n`)
    .replace(/<h3\b[^>]*>([\s\S]*?)<\/h3>/gi, (_, h) => `\n\n### ${convertInlineHtmlToMd(h, basePageHref)}\n\n`)
    .replace(/<h4\b[^>]*>([\s\S]*?)<\/h4>/gi, (_, h) => `\n\n#### ${convertInlineHtmlToMd(h, basePageHref)}\n\n`)
    .replace(/<li\b[^>]*>([\s\S]*?)<\/li>/gi, (_, li) => `\n- ${convertInlineHtmlToMd(li, basePageHref)}`)
    .replace(/<p\b[^>]*>([\s\S]*?)<\/p>/gi, (_, p) => `\n\n${convertInlineHtmlToMd(p, basePageHref)}\n\n`)
    .replace(/<\/(?:section|article|div|header|footer|aside|ul|ol)>/gi, '\n\n')
    .replace(/<(?:section|article|div|header|footer|aside|ul|ol)\b[^>]*>/gi, '\n');

  // Strip remaining inline HTML tags while preserving line breaks
  body = decodeHtml(
    body
      .replace(/<span\b[^>]*\bclass="sr-only"[^>]*>[\s\S]*?<\/span>/gi, '')
      .replace(/<(?:span|b|i)\b[^>]*\baria-hidden="true"[^>]*>[\s\S]*?<\/(?:span|b|i)>/gi, '')
      .replace(/<\/strong>(?=[A-Za-z0-9`\u4e00-\u9fff])/gi, '</strong> — ')
      .replace(/<code\b[^>]*>([\s\S]*?)<\/code>/gi, (_, inner) => `\`${stripHtmlTags(inner)}\``)
      .replace(/<(?:strong|b)\b[^>]*>([\s\S]*?)<\/(?:strong|b)>/gi, (_, inner) => `**${stripHtmlTags(inner)}**`)
      .replace(/<a\b[^>]*\bhref="([^"]+)"[^>]*>([\s\S]*?)<\/a>/gi, (_, href, inner) => {
        const label = stripHtmlTags(inner);
        if (!label) return '';
        let resolvedHref = decodeHtml(href);
        if (site && (resolvedHref.startsWith('./') || resolvedHref.startsWith('../') || resolvedHref.startsWith('#'))) {
          resolvedHref = new URL(resolvedHref, basePageHref).href;
        }
        return `[${label}](${resolvedHref})`;
      })
      .replace(/<br\s*\/?>/gi, '\n')
      .replace(/<[^>]+>/g, ' '),
  );

  body = body
    .split('\n')
    .map((line) => line.replace(/[ \t]+/g, ' ').trim())
    .join('\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();

  codeBlocks.forEach((snippet, idx) => {
    body = body.replace(`__MD_CODE_BLOCK_${idx}__`, snippet.trim());
  });
  body = body.replace(/\n{3,}/g, '\n\n').trim();

  const pricingRef = site ? new URL('pricing.md', site).href : './pricing.md';
  const llmsFullRef = site ? new URL('llms-full.txt', site).href : './llms-full.txt';
  if (lang === 'zh') {
    const zhPricing = site ? pricingRef : '../pricing.md';
    const zhReference = site ? llmsFullRef : '../llms-full.txt';
    return `> 原文：${canonicalHref}
> ${brand.name} · ${brand.chineseName} · v${RELEASE_VERSION} · 更新于 ${lastmod}
> 免费开源 · MIT · [版本与适用范围](${zhPricing}) · [资料索引](${zhReference})

${body}
`;
  }
  return `> **Machine-Readable Markdown Mirror** · Canonical HTML: ${canonicalHref}
> **Product**: ${brand.name} (${brand.chineseName} · \`qingsongcui/ContextCore\`) · **Version**: \`v${RELEASE_VERSION}\` · **Updated**: \`${lastmod}\`
> **Pricing & License**: **$0 (100% Free & Open Source under the MIT License, no signup)** · [Pricing & Limits](${pricingRef}) · [Extended Spec](${llmsFullRef})

${body}
`;
}

// 1. Prepare clean dist/ and copy static assets + bundled CSS + transformed i18n.js
await rm(out, { recursive: true, force: true });
await mkdir(path.join(out, 'zh'), { recursive: true });
await mkdir(path.join(out, 'en'), { recursive: true });

const cssSource = path.join(root, 'assets', 'css');
await cp(path.join(root, 'assets'), path.join(out, 'assets'), {
  recursive: true,
  filter: (source) =>
    source !== cssSource &&
    !source.startsWith(cssSource + path.sep) &&
    !['social.png', 'social-original-v1.png'].includes(path.basename(source)),
});
await cp(path.join(root, 'assets', 'favicon.ico'), path.join(out, 'favicon.ico'));
await writeFile(path.join(out, 'assets', 'site.css'), await bundleCss());

const rawI18n = await readFile(path.join(root, 'assets', 'i18n.js'), 'utf8');
const transformedI18n = applyBrandTokens(rawI18n, { htmlEscaped: false });
await writeFile(path.join(out, 'assets', 'i18n.js'), transformedI18n);

const { attrEn, htmlEn, metaEn, textEn } = await import(
  `${pathToFileURL(path.join(out, 'assets', 'i18n.js')).href}?v=${Date.now()}`
);

const homeLastmod = gitLastmod(['index.html', 'assets/i18n.js', 'assets/site.js', 'brand.config.mjs', 'scripts/build.mjs']);
const guideLastmod = gitLastmod(['guide.html', 'brand.config.mjs', 'scripts/build.mjs']);
const compareLastmod = gitLastmod(['compare.html', 'brand.config.mjs', 'scripts/build.mjs']);
const hostsLastmod = gitLastmod(['hosts.html', 'brand.config.mjs', 'scripts/build.mjs']);
const relayLastmod = gitLastmod(['relay-workflow.html', 'brand.config.mjs', 'scripts/build.mjs']);

const enHomeHref = site ? site.href : './';
const zhHomeHref = site ? new URL('zh/', site).href : './zh/';
const enGuideHref = site ? new URL('guide.html', site).href : './guide.html';
const zhGuideHref = site ? new URL('zh/guide.html', site).href : './zh/guide.html';
const enCompareHref = site ? new URL('compare.html', site).href : './compare.html';
const zhCompareHref = site ? new URL('zh/compare.html', site).href : './zh/compare.html';
const enHostsHref = site ? new URL('hosts.html', site).href : './hosts.html';
const zhHostsHref = site ? new URL('zh/hosts.html', site).href : './zh/hosts.html';
const enRelayHref = site ? new URL('relay-workflow.html', site).href : './relay-workflow.html';
const zhRelayHref = site ? new URL('zh/relay-workflow.html', site).href : './zh/relay-workflow.html';
const homeMdHref = site ? new URL('index.md', site).href : './index.md';
const zhHomeMdHref = site ? new URL('zh/index.md', site).href : './zh/index.md';
const guideMdHref = site ? new URL('guide.md', site).href : './guide.md';
const compareMdHref = site ? new URL('compare.md', site).href : './compare.md';
const hostsMdHref = site ? new URL('hosts.md', site).href : './hosts.md';
const relayMdHref = site ? new URL('relay-workflow.md', site).href : './relay-workflow.md';
const pricingMdUrl = site ? new URL('pricing.md', site).href : './pricing.md';

// 2. Build Primary Static English Homepage (dist/index.html), Simplified Chinese Homepage (dist/zh/index.html), and Legacy Redirect (dist/en/index.html)
const baseIndexHtml = applyBrandTokens(await readFile(path.join(root, 'index.html'), 'utf8'));
const baseSchema = JSON.parse(baseIndexHtml.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
const zhSiteDesc = baseSchema['@graph'][0].description;
const zhAppDesc = baseSchema['@graph'][1].description;
const zhFaqBlock = baseIndexHtml.match(/<div class="faq-list" data-i18n-html="faq">([\s\S]*?)<\/div>\s*<\/section>/)[1];

let zhHtml = baseIndexHtml
  .replace('<html lang="zh-CN">', '<html lang="zh-CN" data-static-locale="zh">')
  .replaceAll('href="./favicon.ico"', 'href="../favicon.ico"')
  .replaceAll('href="./assets/', 'href="../assets/')
  .replaceAll('src="./assets/', 'src="../assets/')
  .replaceAll('href="./pricing.md"', 'href="../pricing.md"')
  .replace(
    /<div class="language-switch"[^>]*>[\s\S]*?<\/div>/,
    '<div class="language-switch" role="group" aria-label="语言"><a href="../" data-lang="en" lang="en">EN</a><a href="./" data-lang="zh" aria-current="page" lang="zh-CN">中</a></div>',
  );

const zhSchema = buildHomepageSchema({
  lang: 'zh',
  pageUrl: zhHomeHref,
  siteDesc: zhSiteDesc,
  appDesc: zhAppDesc,
  faqEntities: extractFaqEntities(zhFaqBlock),
  lastmod: homeLastmod,
});

zhHtml = zhHtml.replace(
  /<script type="application\/ld\+json">.*?<\/script>/s,
  `<script type="application/ld+json">${escapeJson(zhSchema)}</script>`,
);
if (site) {
  zhHtml = zhHtml.replace('content="noindex, nofollow"', 'content="index, follow"');
}
zhHtml = zhHtml.replace(
  '<!-- deployment-metadata -->',
  renderDeploymentHead({
    canonicalHref: zhHomeHref,
    zhHref: zhHomeHref,
    enHref: enHomeHref,
    mdHref: zhHomeMdHref,
    altOgLocale: 'en_US',
    imageAlt: `${brand.name} · ${brand.chineseName} — ${brand.tagline}`,
  }),
);
await writeFile(path.join(out, 'zh', 'index.html'), zhHtml);
await writeFile(
  path.join(out, 'zh', 'index.md'),
  htmlMainToMarkdown({
    docHtml: zhHtml,
    title: decodeHtml(baseIndexHtml.match(/<title>(.*?)<\/title>/s)[1]),
    canonicalHref: zhHomeHref,
    lastmod: homeLastmod,
    lang: 'zh',
  }),
);

let enHtml = baseIndexHtml.replace('<html lang="zh-CN">', '<html lang="en" data-static-locale="en">');
enHtml = replaceI18nHtmlBlocks(enHtml, htmlEn);
enHtml = translateAttributes(enHtml, attrEn);
enHtml = translateStaticTextNodes(enHtml, textEn);
enHtml = enHtml
  .replace(/<title>.*?<\/title>/s, `<title>${escapeHtml(metaEn.title)}</title>`)
  .replace(/(<meta name="description" content=")[^"]*(")/, `$1${escapeHtml(metaEn.description)}$2`)
  .replace(/(<meta property="og:locale" content=")[^"]*(")/, `$1${escapeHtml(metaEn.locale)}$2`)
  .replace(/(<meta property="og:site_name" content=")[^"]*(")/, `$1${escapeHtml(brand.name)}$2`)
  .replace(/(<meta property="og:title" content=")[^"]*(")/, `$1${escapeHtml(metaEn.ogTitle)}$2`)
  .replace(/(<meta property="og:description" content=")[^"]*(")/, `$1${escapeHtml(metaEn.ogDescription)}$2`)
  .replace(/(<meta name="twitter:title" content=")[^"]*(")/, `$1${escapeHtml(metaEn.ogTitle)}$2`)
  .replace(/(<meta name="twitter:description" content=")[^"]*(")/, `$1${escapeHtml(metaEn.twitterDescription)}$2`)
  .replace(
    /<div class="language-switch"[^>]*>[\s\S]*?<\/div>/,
    '<div class="language-switch" role="group" aria-label="Language"><a href="./" data-lang="en" aria-current="page" lang="en">EN</a><a href="./zh/" data-lang="zh" lang="zh-CN">中</a></div>',
  );

const enSchema = buildHomepageSchema({
  lang: 'en',
  pageUrl: enHomeHref,
  siteDesc: metaEn.siteDescription,
  appDesc: metaEn.jsonDescription,
  faqEntities: extractFaqEntities(htmlEn.faq),
  lastmod: homeLastmod,
});
enHtml = enHtml.replace(
  /<script type="application\/ld\+json">.*?<\/script>/s,
  `<script type="application/ld+json">${escapeJson(enSchema)}</script>`,
);
if (site) {
  enHtml = enHtml.replace('content="noindex, nofollow"', 'content="index, follow"');
}
enHtml = enHtml.replace(
  '<!-- deployment-metadata -->',
  renderDeploymentHead({
    canonicalHref: enHomeHref,
    zhHref: zhHomeHref,
    enHref: enHomeHref,
    mdHref: homeMdHref,
    altOgLocale: 'zh_CN',
    imageAlt: metaEn.ogTitle,
  }),
);
await writeFile(path.join(out, 'index.html'), enHtml);
await writeFile(
  path.join(out, 'index.md'),
  htmlMainToMarkdown({
    docHtml: enHtml,
    title: metaEn.title,
    canonicalHref: enHomeHref,
    lastmod: homeLastmod,
  }),
);
await writeFile(
  path.join(out, 'en', 'index.html'),
  renderLegacyEnRedirect({
    enTitle: metaEn.title,
    targetCanonical: enHomeHref,
    relTarget: '../',
  }),
);

async function buildBilingualDocPagePair({
  fileName,
  mdFileName,
  zhTitle,
  enTitle,
  enDesc,
  zhHref,
  enHref,
  mdHref,
  lastmod,
  published,
  buildSchema,
  proficiencyLevel = 'Beginner',
}) {
  const baseDocHtml = applyBrandTokens(await readFile(path.join(root, fileName), 'utf8'));
  const zhDesc = decodeHtml(baseDocHtml.match(/<meta name="description" content="([^"]+)">/)[1]);
  const zhMdHref = site ? new URL(mdFileName, zhHref).href : './' + mdFileName;

  let zhDoc = removeElementByAttr(baseDocHtml, 'div', 'data-guide-section="en"')
    .replace(/\s*<hr class="lang-divider">\s*/g, '\n')
    .replaceAll('href="./favicon.ico"', 'href="../favicon.ico"')
    .replaceAll('href="./assets/', 'href="../assets/')
    .replaceAll('src="./assets/', 'src="../assets/')
    .replaceAll('href="./pricing.md"', 'href="../pricing.md"')
    .replace(
      /<div class="language-switch"[^>]*>[\s\S]*?<\/div>/,
      `<div class="language-switch" role="group" aria-label="语言" data-aria-zh="语言" data-aria-en="Language"><a href="../${fileName}" data-lang="en" lang="en">EN</a><a href="./${fileName}" data-lang="zh" aria-current="page" lang="zh-CN">中</a></div>`,
    );
  const zhSchema = buildSchema({
    lang: 'zh',
    pageUrl: zhHref,
    homeUrl: zhHomeHref,
    headline: zhTitle,
    description: zhDesc,
    faqEntities: extractFaqEntities(zhDoc),
    lastmod,
    published,
    proficiencyLevel,
  });
  if (site) {
    zhDoc = zhDoc.replace('content="noindex, nofollow"', 'content="index, follow"');
  }
  zhDoc = zhDoc
    .replace(
      '<!-- deployment-metadata -->',
      renderDeploymentHead({
        canonicalHref: zhHref,
        zhHref,
        enHref,
        mdHref: zhMdHref,
        altOgLocale: 'en_US',
        imageAlt: zhTitle,
      }),
    )
    .replace('</head>', `  <script type="application/ld+json">${escapeJson(zhSchema)}</script>\n</head>`);
  await writeFile(path.join(out, 'zh', fileName), zhDoc);
  await writeFile(path.join(out, 'zh', mdFileName), htmlMainToMarkdown({
    docHtml: zhDoc, title: zhTitle, canonicalHref: zhHref, lastmod, lang: 'zh',
  }));

  let enDoc = removeElementByAttr(baseDocHtml, 'div', 'data-guide-section="zh"')
    .replace(/\s*<hr class="lang-divider">\s*/g, '\n')
    .replace('<html lang="zh-CN">', '<html lang="en">')
    .replace(
      /<([a-zA-Z][\w:-]*)\b([^>]*?)\bdata-en="([^"]*)"([^>]*)>[\s\S]*?<\/\1>/g,
      (full, tag, before, enVal, after) => `<${tag}${before}data-en="${enVal}"${after}>${enVal}</${tag}>`,
    )
    .replace(
      /<([a-zA-Z][\w:-]*)\b([^>]*?)\bdata-aria-en="([^"]*)"([^>]*)>/g,
      (full, tag, before, enAria, after) => {
        const combined = `${before}data-aria-en="${enAria}"${after}`.replace(
          /\baria-label="[^"]*"/,
          `aria-label="${enAria}"`,
        );
        return `<${tag}${combined}>`;
      },
    )
    .replace(/<title>.*?<\/title>/s, `<title>${escapeHtml(enTitle)}</title>`)
    .replace(/(<meta name="description" content=")[^"]*(")/, `$1${escapeHtml(enDesc)}$2`)
    .replace(/(<meta property="og:locale" content=")[^"]*(")/, '$1en_US$2')
    .replace(/(<meta property="og:site_name" content=")[^"]*(")/, `$1${escapeHtml(brand.name)}$2`)
    .replace(/(<meta property="og:title" content=")[^"]*(")/, `$1${escapeHtml(enTitle)}$2`)
    .replace(/(<meta property="og:description" content=")[^"]*(")/, `$1${escapeHtml(enDesc)}$2`)
    .replace(/(<meta name="twitter:title" content=")[^"]*(")/, `$1${escapeHtml(enTitle)}$2`)
    .replace(/(<meta name="twitter:description" content=")[^"]*(")/, `$1${escapeHtml(enDesc)}$2`)
    .replace(
      /<div class="language-switch"[^>]*>[\s\S]*?<\/div>/,
      `<div class="language-switch" role="group" aria-label="Language" data-aria-zh="语言" data-aria-en="Language"><a href="./${fileName}" data-lang="en" aria-current="page" lang="en">EN</a><a href="./zh/${fileName}" data-lang="zh" lang="zh-CN">中</a></div>`,
    );

  const enSchema = buildSchema({
    lang: 'en',
    pageUrl: enHref,
    homeUrl: enHomeHref,
    headline: enTitle,
    description: enDesc,
    faqEntities: extractFaqEntities(enDoc),
    lastmod,
    published,
    proficiencyLevel,
  });
  if (site) {
    enDoc = enDoc.replace('content="noindex, nofollow"', 'content="index, follow"');
  }
  enDoc = enDoc
    .replace(
      '<!-- deployment-metadata -->',
      renderDeploymentHead({
        canonicalHref: enHref,
        zhHref,
        enHref,
        mdHref,
        altOgLocale: 'zh_CN',
        imageAlt: enTitle,
      }),
    )
    .replace('</head>', `  <script type="application/ld+json">${escapeJson(enSchema)}</script>\n</head>`);
  await writeFile(path.join(out, fileName), enDoc);
  await writeFile(
    path.join(out, mdFileName),
    htmlMainToMarkdown({
      docHtml: enDoc,
      title: enTitle,
      canonicalHref: enHref,
      lastmod,
    }),
  );
  await writeFile(
    path.join(out, 'en', fileName),
    renderLegacyEnRedirect({
      enTitle,
      targetCanonical: enHref,
      relTarget: `../${fileName}`,
    }),
  );
}

// 3. Build Bilingual Documentation Pages (guide, compare, hosts, relay-workflow)
const docPages = [
  {
    fileName: 'guide.html',
    mdFileName: 'guide.md',
    zhTitle: `${brand.name} · ${brand.chineseName} — 本地运行与使用指南`,
    enTitle: `${brand.name} — Local Setup & Workflow Guide`,
    enDesc: `${brand.name} macOS Apple Silicon local guide ($0 MIT): macOS ZIP download, xattr first launch, /contextcore relay, shared memory, and MCP sync.`,
    zhHref: zhGuideHref,
    enHref: enGuideHref,
    mdHref: guideMdHref,
    lastmod: guideLastmod,
    buildSchema: buildGuideSchema,
  },
  {
    fileName: 'compare.html',
    mdFileName: 'compare.md',
    zhTitle: `${brand.name} · ${brand.chineseName} — 2026 多 Agent 上下文接力与 MCP 方案对比`,
    enTitle: `${brand.name} — 5 Ways to Share AI Agent Context (2026)`,
    enDesc: `Compare 5 ways to share context, memory, and sync MCP across Claude Code, Cursor & Codex: AGENTS.md, Mem0, ${brand.name} ($0 MIT), Dotfiles, and routers.`,
    zhHref: zhCompareHref,
    enHref: enCompareHref,
    mdHref: compareMdHref,
    lastmod: compareLastmod,
    buildSchema: buildCompareSchema,
  },
  {
    fileName: 'hosts.html',
    mdFileName: 'hosts.md',
    zhTitle: `Claude Code、Cursor、Codex MCP 配置与路径指南 — ${brand.name}`,
    enTitle: `${brand.name} — Claude Code, Cursor & Codex MCP Config`,
    enDesc: 'MCP configuration paths for Claude Code, Cursor and Codex, with JSON and TOML examples, Skills locations and support across 15 coding agents.',
    zhHref: zhHostsHref,
    enHref: enHostsHref,
    mdHref: hostsMdHref,
    lastmod: hostsLastmod,
    buildSchema: buildGuideSchema,
    proficiencyLevel: 'Intermediate',
  },
  {
    fileName: 'relay-workflow.html',
    mdFileName: 'relay-workflow.md',
    zhTitle: `Claude Code 转 Codex 或 Cursor 继续对话指南 — ${brand.name}`,
    enTitle: `${brand.name} — Continue Claude Code in Codex or Cursor`,
    enDesc: 'Continue a Claude Code conversation in Codex or Cursor with Senrelay. Select a source session, read the transcript, review context and resume the task.',
    zhHref: zhRelayHref,
    enHref: enRelayHref,
    mdHref: relayMdHref,
    lastmod: relayLastmod,
    buildSchema: buildGuideSchema,
    proficiencyLevel: 'Intermediate',
  },
];

const featureGuides = ['agent-management.html', 'ai-coding-usage-tracker.html'];
for (const fileName of ['claude-code-memory.html', 'mcp-json-vs-toml-config-guide.html', ...featureGuides]) {
  const source = await readFile(path.join(root, fileName), 'utf8');
  const enTitle = decodeHtml(source.match(/<h1[^>]*data-en="([^"]+)"/)[1]);
  const enDesc = decodeHtml(source.match(/class="guide-lead"[^>]*data-en="([^"]+)"/)[1]);
  const href = (prefix) => site ? new URL(prefix + fileName, site).href : './' + prefix + fileName;
  const mdFileName = fileName.replace('.html', '.md');
  docPages.push({
    fileName,
    mdFileName,
    zhTitle: applyBrandTokens(decodeHtml(source.match(/<title>(.*?)<\/title>/)[1]), { htmlEscaped: false }),
    enTitle: applyBrandTokens(enTitle, { htmlEscaped: false }),
    enDesc: applyBrandTokens(enDesc, { htmlEscaped: false }),
    zhHref: href('zh/'),
    enHref: href(''),
    mdHref: site ? new URL(mdFileName, site).href : './' + mdFileName,
    ...(featureGuides.includes(fileName) ? { published: '2026-11-19' } : {}),
    lastmod: gitLastmod([fileName, 'scripts/build.mjs']),
    buildSchema: buildGuideSchema,
  });
}

for (const docPage of docPages) {
  await buildBilingualDocPagePair(docPage);
}

// 4. Build Custom 404 Page (dist/404.html)
let notFoundHtml = applyBrandTokens(await readFile(path.join(root, '404.html'), 'utf8'));
if (site) {
  notFoundHtml = notFoundHtml.replace(/(href|src)="\.\//g, `$1="${escapeHtml(site.pathname)}`);
}
await writeFile(path.join(out, '404.html'), notFoundHtml);

// 5. Build robots.txt, sitemap.xml, IndexNow key file, and CNAME
const aiCrawlerAgents = [
  'Googlebot',
  'Bingbot',
  'Applebot',
  'Bravebot',
  'DuckAssistBot',
  'OAI-SearchBot',
  'ChatGPT-User',
  'Claude-SearchBot',
  'Claude-User',
  'PerplexityBot',
  'Google-Extended',
  'Applebot-Extended',
  'Meta-ExternalAgent',
  'GPTBot',
  'ClaudeBot',
  'Bytespider',
  'Baiduspider',
  '*',
];

const robotsTxt = site
  ? `${aiCrawlerAgents.map((ua) => `User-agent: ${ua}\nAllow: ${site.pathname}`).join('\n\n')}\n\nSitemap: ${new URL('sitemap.xml', site).href}\n`
  : 'User-agent: *\nDisallow: /\n';
await writeFile(path.join(out, 'robots.txt'), robotsTxt);

if (site) {
  const sitemapEntries = [
    { loc: enHomeHref, lastmod: homeLastmod, zh: zhHomeHref, en: enHomeHref },
    { loc: zhHomeHref, lastmod: homeLastmod, zh: zhHomeHref, en: enHomeHref },
    ...docPages.flatMap((page) => [page.enHref, page.zhHref].map((loc) => ({
      loc,
      lastmod: page.lastmod,
      zh: page.zhHref,
      en: page.enHref,
    }))),
  ];
  const sitemapXml = `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:xhtml="http://www.w3.org/1999/xhtml">\n${sitemapEntries
    .map(
      (item) => `  <url>
    <loc>${escapeHtml(item.loc)}</loc>
    <lastmod>${escapeHtml(item.lastmod)}</lastmod>
    <xhtml:link rel="alternate" hreflang="zh-CN" href="${escapeHtml(item.zh)}"/>
    <xhtml:link rel="alternate" hreflang="zh" href="${escapeHtml(item.zh)}"/>
    <xhtml:link rel="alternate" hreflang="en" href="${escapeHtml(item.en)}"/>
    <xhtml:link rel="alternate" hreflang="x-default" href="${escapeHtml(item.en)}"/>
  </url>`,
    )
    .join('\n')}\n</urlset>\n`;
  await writeFile(path.join(out, 'sitemap.xml'), sitemapXml);

  if (indexNowKey) {
    await writeFile(path.join(out, `${indexNowKey}.txt`), `${indexNowKey}\n`);
  }
  if (!site.hostname.endsWith('.github.io')) {
    await writeFile(path.join(out, 'CNAME'), `${site.hostname}\n`);
  }
}

// 6. Build Agent-Ready pricing.md, llms.txt & llms-full.txt
const llmsFullUrl = site ? new URL('llms-full.txt', site).href : './llms-full.txt';
const homeEnRef = site ? enHomeHref : './index.html';
const homeZhRef = site ? zhHomeHref : './zh/index.html';
const guideEnRef = site ? enGuideHref : './guide.html';
const guideZhRef = site ? zhGuideHref : './zh/guide.html';
const compareEnRef = site ? enCompareHref : './compare.html';
const compareZhRef = site ? zhCompareHref : './zh/compare.html';
const hostsEnRef = site ? enHostsHref : './hosts.html';
const hostsZhRef = site ? zhHostsHref : './zh/hosts.html';
const relayEnRef = site ? enRelayHref : './relay-workflow.html';
const relayZhRef = site ? zhRelayHref : './zh/relay-workflow.html';
const releaseTagHref = `${brand.github}/releases/tag/v${RELEASE_VERSION}`;

const pricingMdContent = `# ${brand.name} (${brand.chineseName} · ContextCore) — Pricing, License & System Requirements

> **Price**: **$0 / month ($0 forever)** · **License**: [MIT License](${brand.github}/blob/main/LICENSE) · **Account Signup**: None required · **Telemetry / Cloud Upload**: Zero (100% local loopback \`127.0.0.1:8420\`)

## Summary Table (Verified 2026-11-15 · Updated ${homeLastmod})

| Dimension | Official Specification | Notes |
|---|---|---|
| **Price** | **$0** (100% Free & Open Source) | No free-trial expiration, no paid Pro/Enterprise tiers, no cloud paywall. |
| **Open-Source License** | **MIT License** | Source repository: [${brand.github}](${brand.github}). |
| **Account & Telemetry** | **No account · Zero telemetry** | Runs strictly on \`http://127.0.0.1:8420\` (dev \`18420\`); never uploads transcripts or code. |
| **Model API Costs** | **BYOK (Bring Your Own Key)** | You configure your own provider API keys in your AI coding clients; keys stay isolated in macOS Keychain. |
| **Supported OS & CPU** | **macOS 14.0+ · Apple Silicon (\`arm64\`)** | Sonoma or later on M-series Macs only. Not supported on Windows, Linux, or Intel (\`x86_64\`) Macs. |
| **Current Release** | **\`v${RELEASE_VERSION}\`** (Public Beta) | Download [\`Senrelay-macOS.zip\`](${releaseDownloadUrl}). |
| **First-Launch Gatekeeper Note** | \`xattr -cr /Applications/Senrelay.app\` | Public Beta bundles are ad-hoc signed prior to Apple Developer ID notarization. |
| **Package Disambiguation** | **Never run \`pip install contextcore\`** | Public PyPI \`contextcore\` and \`context-core.dev\` are unrelated third-party projects. Host MCP server key is \`contextcore\` at \`http://127.0.0.1:8420/mcp/\` (never \`senrelay-mcp\`). Official MCP Registry ID: \`io.github.qingsongcui/contextcore\`. |

## Included Capabilities at $0 (MIT)

- **6 Core Context Hosts (\`Claude Code\`, \`Codex\`, \`Cursor\`, \`Antigravity\`, \`OMP\`, \`OpenCode\`)**:
  - Native local session discovery with worktree and UUID disambiguation.
  - \`/contextcore\` short-brief candidate selection and sequential \`read_conversation_messages\` full-transcript in-context compaction (\`must_not_execute=true\` read-only stand-by on number-only selection).
  - Dual-tier working memory (\`working_tasks\` + 6 categories of version-governed source-linked memories), \`AGENTS.md\` preview, and document imports (\`MD\`, \`TXT\`, \`JSON\`, \`PDF\`, \`DOCX\`, \`DOC\`).
  - Provider presets, latency checks, Keychain secret isolation, and multi-host MCP / Skills / prompt sync with automatic rollback snapshots and external-edit Diff guards.
- **9 Extended Configuration Hosts (\`Claude Desktop\`, \`Gemini CLI\`, \`Grok Build\`, \`Kimi Code\`, \`MiniMax Code\`, \`Pi\`, \`Hermes\`, \`OpenClaw\`, \`agy\`)**:
  - Previewed configuration writes, format conversion (\`JSON\`, \`JSON5\`, \`TOML\`, \`YAML\`), and rollback snapshots only (no session capture or \`/contextcore\` conversation relay).

## When NOT to Use ${brand.name}

1. **Windows, Linux, or Intel (\`x86_64\`) macOS environments**: Start with Git-tracked \`CLAUDE.md\` + \`AGENTS.md\` ($0) instead.
2. **Multi-device cloud memory sync or hosted team SaaS**: Pair \`AGENTS.md\` with a cloud/self-hosted Memory MCP server (e.g., Mem0 / OpenMemory).
3. **Single-IDE workflows where you never switch agent harnesses**: Use a single IDE with an LLM router.

See the full [5-Way Comparison & Stack Picker](${compareEnRef}) and [15-Host Config Reference](${hostsEnRef}).
`;
await writeFile(path.join(out, 'pricing.md'), pricingMdContent);

const resourceHref = path => site ? new URL(path, site).href : './' + path;

const llmsContent = `# ${brand.name} · ${brand.chineseName} (ContextCore)

> ${brand.name} (open-source repository \`qingsongcui/ContextCore\`) is a free, MIT-licensed, local-first macOS workbench and loopback service (\`127.0.0.1:8420\`) for cross-agent conversation relay (\`/contextcore\`), companion source-linked auto-memory, and unified multi-host AI coding assistant configuration management. Distinct from unrelated third-party \`context-core.dev\`, PyPI \`contextcore\`, and automotive relay hardware.

${brand.name} (${brand.chineseName}) runs natively on macOS 14.0+ (Sonoma or later, Apple Silicon \`arm64\`) as \`Senrelay.app\` or from source via Python 3.11+ and Node 22/24 LTS. Pricing: **$0 (free and open source under the MIT License)**.

> **Package & MCP Disambiguation**: Install from the published [macOS ZIP](${releaseDownloadUrl}). Do **NOT** run \`pip install contextcore\`—the PyPI package of that name is an unrelated third-party project. In AI host configurations, Senrelay registers under the MCP server key \`contextcore\` pointing to \`http://127.0.0.1:8420/mcp/\` alongside the companion \`/contextcore\` skill (never \`senrelay-mcp\`). Official MCP Registry ID: \`io.github.qingsongcui/contextcore\`.

## Core Capabilities

- **Cross-Agent Conversation Relay (\`/contextcore\`)**: Type \`/contextcore\` (or say "接着做" / "接力" / "continue" / "relay") inside any of the 6 core context hosts—Claude Code, Codex, Cursor, Antigravity, Oh My Pi (OMP), or OpenCode—to inspect short recognizable candidate briefs with native session and worktree identity without calling a model. Upon selection, \`relay_conversation\` returns \`transcript_paging\` so the receiving agent pages through the full transcript via \`read_conversation_messages\` (bounded per MCP page to ~48KB) and compacts the entire conversation directly in context instead of emitting lossy handoff cards. Number-only replies set \`must_not_execute=true\` to brief and stand by; attaching a concrete task continues execution immediately.
- **Dual-Tier Working Memory & Companion Auto-Memory**: Companion MCP tools and Skills maintain active working tasks (\`working_tasks\`) and source-backed lesson promotions alongside six categories of version-governed long-term memories (Preferences, Background, Constraints, Tasks, Lessons, References) with exact conversation-turn provenance and a concise receipt, \`outdated\` lifecycle and supersession version guards, core-rule pinning, conflict inbox review, directory-free project creation, \`AGENTS.md\` handbook preview, and multi-format document imports (Markdown, TXT, JSON, PDF, DOCX, DOC).
- **Unified Multi-Host Configuration Management**: Switch across 6 core context hosts and 9 extended configuration hosts from a permanent top bar, run One-Click Connect (configuring the \`contextcore\` server at \`http://127.0.0.1:8420/mcp/\`), apply provider presets, test endpoint latency, isolate API keys in macOS Keychain, and sync MCP servers, Skills, and system prompts with automatic rollback snapshots, external-edit Diff protection, and zero silent Skill overwrites during startup or local config import.

## System Requirements, Pricing & Real Boundaries (Checked on 2026-11-15)

- **Pricing & License**: **$0 (100% Free & Open Source under the MIT License)**. No account registration, no subscription tiers, and no cloud paywall.
- **Platform & Release**: macOS 14.0+ on Apple Silicon (\`arm64\`) · Public Beta \`v${RELEASE_VERSION}\`.
- **Quick Install**: Download [\`Senrelay-macOS.zip\`](${releaseDownloadUrl}), verify its checksum on the [release page](${releaseTagHref}), and follow the [first-launch guide](${guideEnRef}).
- **Known Limitations (When NOT to Use ${brand.name})**:
  1. **No Windows, Linux, or Intel Mac support**: Requires macOS 14.0+ on Apple Silicon (\`arm64\`).
  2. **Single-machine local architecture only**: Runs strictly on \`127.0.0.1:8420\` without multi-device cloud sync or hosted team SaaS collaboration.
  3. **9 Extended Hosts are config-only**: Session capture, auto-memory, and \`/contextcore\` relay work on the 6 Core Context Hosts only.
  4. **Public Beta ad-hoc signing**: Requires a one-time \`xattr -cr /Applications/Senrelay.app\` command on first launch before Apple Developer ID notarization is added.
- **Privacy, Storage & Creator**: Loopback-only HTTP/MCP service (\`127.0.0.1:8420\`, dev \`127.0.0.1:18420\`), local SQLite + FTS5 full-text search (\`~/Library/Application Support/ContextCore\`, directory mode \`0700\`, DB mode \`0600\`), online SQLite backups (\`contextcore backup\`), and macOS Keychain secret isolation. Created and maintained by **Qingsong Cui (George O'Nair)** (\`https://github.com/qingsongcui\` · \`https://x.com/George_ONair\`).

## Resources

- [English Homepage](${homeEnRef}) ([Markdown Mirror](${homeMdHref})): Primary static English product overview, host capability matrix, and FAQ.
- [Pricing, License & System Requirements (Markdown)](${pricingMdUrl}): Machine-readable $0 MIT pricing, BYOK policy, Apple Silicon requirements, and scope limits.
- [English Host Config Reference (15 AI Agents)](${hostsEnRef}) ([Markdown Mirror](${hostsMdHref})): Verified MCP, Skills, and instruction paths across 6 core and 9 extended agents (JSON, TOML, JSON5, YAML) plus safety boundaries.
- [English Cross-Agent Relay Workflow](${relayEnRef}) ([Markdown Mirror](${relayMdHref})): Step-by-step workflow to continue Claude Code sessions in Codex or Cursor with short briefs, paged reading, and stand-by safeguards.
- [Claude Code Memory & Context](${site ? new URL('claude-code-memory.html', site).href : './claude-code-memory.html'}): Memory, instructions and cross-agent continuation.
- [MCP JSON to TOML Guide](${site ? new URL('mcp-json-vs-toml-config-guide.html', site).href : './mcp-json-vs-toml-config-guide.html'}): Claude Code, Cursor and Codex configuration examples.
- [AI Coding Agent Management](${resourceHref('agent-management.html')}) · [English Markdown](${resourceHref('agent-management.md')}) · [中文指南](${resourceHref('zh/agent-management.html')}) · [中文 Markdown](${resourceHref('zh/agent-management.md')}). Six Agent workspaces, native configuration import, supported provider switches, and MCP and Skills sharing with file diffs and recovery.
- [Claude Code and Codex Usage](${resourceHref('ai-coding-usage-tracker.html')}) · [English Markdown](${resourceHref('ai-coding-usage-tracker.md')}) · [中文指南](${resourceHref('zh/ai-coding-usage-tracker.html')}) · [中文 Markdown](${resourceHref('zh/ai-coding-usage-tracker.md')}). Observed token use, logical requests and retries, estimated API costs, and supported account quotas.
- [English Setup & Workflow Guide](${guideEnRef}) ([Markdown Mirror](${guideMdHref})): ZIP download, first launch (\`xattr -cr\`), source build, and daily workflows.
- [English 5-Way Comparison & Stack Picker](${compareEnRef}) ([Markdown Mirror](${compareMdHref})): Objective comparison of \`CLAUDE.md\`/\`AGENTS.md\`, Cloud Memory MCP, ${brand.name}, Dotfiles scripts, and single-IDE routers (with interactive picker & real downsides).
- [Chinese Homepage (中文首页)](${homeZhRef}): 简体中文产品概览、能力矩阵与常见问题。
- [Chinese Host Config Reference (15 宿主配置路径指南)](${hostsZhRef}): 6 类核心接力宿主与 9 类扩展宿主的 MCP/Skills/规则文件路径与写前保护边界。
- [Chinese Cross-Agent Relay Workflow (跨 Agent 接力流程)](${relayZhRef}): Claude Code 切 Codex/Cursor 的六步接力协议、短简介选号与双层记忆协同。
- [Chinese Setup & Workflow Guide (中文运行指南)](${guideZhRef}): 本地安装、首次启动、源码运行与三大工作流指南。
- [Chinese 5-Way Comparison & Stack Picker (5 种方案客观对比)](${compareZhRef}): 跨 AI 编程助手共享上下文、记忆与同步 MCP 的 5 种方案深度横评与按环境交互选型。
- [Extended LLM Reference](${llmsFullUrl}): Complete full-transcript relay & compaction specification, 6+9 host support matrix, CLI commands, and bilingual FAQ.
- [GitHub Repository](${brand.github}): Source code, architecture contracts, and automated verification gates.
- [Release v${RELEASE_VERSION}](${releaseTagHref}): [Download the macOS ZIP](${releaseDownloadUrl}) and review \`SHA256SUMS\`.
- [License](${brand.github}/blob/main/LICENSE): MIT License.
`;

const llmsFullContent = `${llmsContent}
## Full-Transcript Cross-Agent Relay & Compaction Specification

The \`/contextcore\` relay workflow is available for connected core hosts. Historical continuation was exercised in four live hosts (Codex CLI, OMP CLI, Antigravity Desktop and OpenCode) and at the protocol level in Cursor Desktop and Claude Code. Memory writes have synthetic coverage across all six. See the [recorded host evidence](${brand.github}/blob/main/docs/evidence.md#recorded-host-continuation).

The relay protocol has six steps:

1. **01 — Short-Brief Candidate Discovery**: Candidate lists display recognizable titles and short briefs without calling a model or pre-writing summaries.
2. **02 — Native Session & Worktree Disambiguation**: Preserves native session UUIDs and worktree/cwd identity so same-title or unassigned sessions stay distinct; refreshes only the selected authorized source on demand.
3. **03 — Bounded Sequential Transcript Paging**: \`relay_conversation\` returns \`transcript_paging\` (\`next_args\` at \`offset=0\`) so the receiving agent calls \`read_conversation_messages\` until \`next_offset\` is \`null\`, with each MCP response page bounded (~48KB page cap / 24KB single-item trim) to protect context budgets.
4. **04 — Direct In-Context Compaction**: The receiving agent compacts the complete read transcript inside its own context window (like \`/compact\`), without emitting five-field or six-element handoff cards.
5. **05 — Working Tasks & Fail-Closed Constraints**: Carries active \`working_tasks\` state and locked project constraints; if protected rules cannot fit the requested budget, responses fail closed without silent clipping.
6. **06 — Execution Safety Contract**:
   - **Number- or ID-only reply** (e.g. \`1\`): Enforces \`must_not_execute=true\`; the receiving agent reports its compacted full-conversation summary and waits for your next prompt without editing files or running destructive commands.
   - **Number + explicit task reply** (e.g. \`1, finish the unit tests\`): Continues immediately using the full compacted context and preserved constraints.

## Host Support Matrix (6 Core Context Hosts + 9 Extended Configuration Hosts)

${brand.name} strictly separates configuration file writes, synthetic/local tests, and live host process reloads:

| Tier | Host | Surface | Session Capture, Auto-Memory & \`/contextcore\` Relay | Config Management Scope |
|---|---|---|---|---|
| Core Context Host | Claude Code | CLI | Supported | Providers, MCP (\`~/.claude.json\`), Skills, prompts, snapshots & diff guard |
| Core Context Host | Codex | Desktop / CLI | Supported (surfaces tracked separately) | Providers, MCP (\`~/.codex/config.toml\`), Skills, prompts, snapshots & diff guard |
| Core Context Host | Cursor | IDE / CLI | Supported (surfaces tracked separately) | Providers, MCP, Skills, rules, snapshots & diff guard |
| Core Context Host | Antigravity | Desktop | Supported (separate from \`agy\` CLI) | Providers, MCP, Skills, rules, snapshots & diff guard |
| Core Context Host | OMP (Oh My Pi) | CLI | Supported (separate from \`Pi\`) | Providers, MCP, Skills, prompts, snapshots & diff guard |
| Core Context Host | OpenCode | CLI | Supported | Providers, MCP, Skills, prompts, snapshots & diff guard |
| Extended Config Host | Claude Desktop | Desktop | Not supported (config only) | Declared MCP JSON path; direct remote MCP & model activation rejected as unverified |
| Extended Config Host | Gemini CLI | CLI | Not supported (config only) | Declared \`.gemini/settings.json\` MCP path; model activation rejected as unverified |
| Extended Config Host | Grok Build | CLI | Not supported (config only) | Native TOML MCP entry; changing global default model unsupported |
| Extended Config Host | Kimi Code | CLI | Not supported (config only) | Renders MCP & appends provider without altering existing default (requires context window) |
| Extended Config Host | MiniMax Code | CLI | Not supported (config only) | Maps MCP entry & writes provider keeping native default; rejects over-long prompts |
| Extended Config Host | Pi | CLI | Not supported (config only; separate from OMP) | Renders MCP & provider; refuses to rewrite existing JSON5 |
| Extended Config Host | Hermes | CLI | Not supported (config only) | Renders YAML MCP & named provider; supports activation preview |
| Extended Config Host | OpenClaw | CLI | Not supported (config only) | Writes provider JSON5 preserving comments; native MCP sync unsupported |
| Extended Config Host | agy | CLI | Not supported (config only; separate from Antigravity Desktop) | Renders MCP; provider credentials stay in process env/private profile |

## Installation, Source Development & CLI Reference

### 1. macOS App Bundle

Download [\`Senrelay-macOS.zip\`](${releaseDownloadUrl}) for macOS 14.0+ on Apple Silicon. Verify its checksum on the [release page](${releaseTagHref}), extract it, and use the commands below if macOS blocks first launch.

\`\`\`bash
# Run from the directory containing the extracted Senrelay.app.
mv Senrelay.app /Applications/
xattr -cr /Applications/Senrelay.app
\`\`\`

### 2. Run from Source (Repository Root)
\`\`\`bash
# Never run "pip install contextcore" from PyPI (unrelated third-party package).
python3.12 -m venv app/backend/.venv
app/backend/.venv/bin/python -m pip install -e 'app/backend[test]'
npm --prefix app/frontend ci --ignore-scripts
npm --prefix app/frontend run build
app/backend/.venv/bin/contextcore serve --host 127.0.0.1 --port 18420 --data-dir "$HOME/.contextcore/dev-data" --frontend-dir app/frontend/dist-staging
\`\`\`

### 3. Backup & Lifecycle
- Default data directory: \`~/Library/Application Support/ContextCore\` (\`0700\` directory permissions, \`0600\` SQLite database files).
- Online backup command: \`contextcore backup\`.
- Closing the macOS window (\`Cmd+W\`) keeps the background service alive on \`127.0.0.1:8420\`; use Settings or \`contextcore stop\` to stop the daemon explicitly.

## Frequently Asked Questions (EN / 中文)

### 1. How do I hand off a coding session from Claude Code to Cursor or Codex? / 如何在不同 AI 编程助手间无缝接力对话？
Connect your hosts once via **Agent Management → One-Click Connect**. When switching assistants, type \`/contextcore\` (or "接力" / "continue") in the target agent to see short recognizable candidate briefs without calling a model. Pick a candidate (e.g. \`1\`) so the receiving agent calls \`relay_conversation\` + sequential \`read_conversation_messages\` pages until \`next_offset\` is \`null\` and compacts the entire conversation directly in context—reporting in read-only stand-by (\`must_not_execute=true\`) on number-only replies, or continuing immediately when you attach a task like \`1, finish the unit tests\`.

### 2. How is implicit auto-memory triggered during chats? / 隐式自动记忆是如何触发的？
During everyday chats, companion MCP tools and Skills maintain working tasks (\`working_tasks\`) and six categories of version-governed long-term memories linked to exact source turns, returning a concise source receipt. Superseding decisions mark old records \`outdated\` with version guards instead of overwriting history, conflicting updates route to Pending Conflicts, lesson promotions verify same-project source dialogue provenance, and you can pin core rules or preview \`AGENTS.md\` anytime.

### 3. How does multi-host MCP and Skills sync avoid overwriting manual config edits? / 跨宿主同步如何防止覆盖手改配置？
Every configuration write records ownership hashes and an automatic rollback snapshot, and startup or local config import never silently overwrites installed host Skills. If a target host file (\`~/.claude.json\`, \`~/.codex/config.toml\`, Cursor MCP config, etc.) was modified externally, ${brand.name} pauses auto-apply and opens a side-by-side Diff confirmation modal instead of silently overwriting user changes.

### 4. Will changing one assistant affect my other assistants? / 修改单个宿主会影响其他助手吗？
Never silently. You can configure a single host independently or explicitly select multiple hosts to sync shared MCP servers, Skills, and prompts, with full diff preview and one-click snapshot rollback.

### 5. Where are conversations, memories, and API keys stored? / 会话记录与 API Key 存储在哪里？
All session indexes, versioned memories, imported documents, and config snapshots stay in your local Mac SQLite + FTS5 database (\`0600\` permissions). Provider API keys live separately in macOS Keychain and are stripped from configuration exports by default.

### 6. Can I install ContextCore via \`pip install contextcore\`? / 可以用 \`pip install contextcore\` 安装吗？
No. The \`contextcore\` package on public PyPI is an unrelated third-party project. Install ${brand.name} from [\`Senrelay-macOS.zip\`](${releaseDownloadUrl}) or clone \`${brand.github}\` and run \`pip install -e 'app/backend[test]'\`.

### 7. What MCP server key should appear in ~/.claude.json or ~/.codex/config.toml? / 宿主配置中的 MCP 键名是什么？
Registered hosts connect to local \`http://127.0.0.1:8420/mcp/\` (source dev port \`18420\`) under the MCP key \`contextcore\`, paired with the \`/contextcore\` slash skill. Do **NOT** use \`senrelay-mcp\`.

## 5-Way Comparison & Architecture Layering (2026)

| Approach | OS Support | Price | Mid-Task Relay | Source-Linked Memory | MCP & Skills Sync | Storage |
|---|---|---|---|---|---|---|
| 1. \`CLAUDE.md\` / \`AGENTS.md\` | Win / Mac / Linux | $0 | Static rules only | Manual Markdown | Does not sync globals | Git repo |
| 2. Cloud / Docker Memory MCP | All OSes | $0 self-host / Cloud plans | Fragment search only | Semantic facts only | Manual per-host setup | Cloud or Docker |
| 3. ${brand.name} (ContextCore) | macOS 14.0+ arm64 | $0 (MIT · no signup) | Short-brief pick + full transcript page & compact | Working tasks + versioned memory | 6+9 hosts with diff & rollback | Local SQLite (\`0600\`) + Keychain |
| 4. Dotfiles / GNU Stow | macOS / Linux | $0 (DIY) | No | No | Fragile across JSON/TOML/JSON5 | Dotfiles repo |
| 5. Single IDE + Router | Depends on IDE | IDE plan + tokens | Within same app only | Depends on single IDE | Single host only | Local + router API |

- **How \`AGENTS.md\`, \`MCP\`, and Session Relay Fit Together**: \`AGENTS.md\` holds Git-versioned static repo conventions; \`MCP\` connects assistants to tools and data; cross-agent session relay (${brand.name} \`/contextcore\`) preserves live mid-task conversation threads, working tasks, and host configs locally on a single Apple Silicon Mac.
- **When NOT to Choose ${brand.name}**: Choose Option 1 (\`CLAUDE.md\` / \`AGENTS.md\`) if your team uses Windows, Linux, or Intel Macs; choose Option 2 (Mem0 / OpenMemory) if you need multi-device cloud memory sync; choose Option 5 if you stay inside one IDE and only switch LLM APIs.

## 中文核心摘要（${brand.name} · ${brand.chineseName} / ContextCore）

- **实体定义**：${brand.name}（中文名「${brand.chineseName}」，开源仓库 \`ContextCore\`，宿主 MCP 键名为 \`contextcore\`，不适用 \`senrelay-mcp\`）是免费开源（**$0 · MIT 协议 · 免注册**）的 \`macOS 14.0+\` Apple Silicon（\`arm64\`）本机工作台，仅绑定 \`127.0.0.1:8420\`。
- **核心场景覆盖**：**跨 Agent 对话接力**（在 Claude Code、Codex、Cursor、Antigravity、OMP、OpenCode 中输入 \`/contextcore\` 或说「接力」，按短简介选场后通过 \`read_conversation_messages\` 分页读完完整原文并原地压缩；仅选序号设 \`must_not_execute=true\` 只读待命）、**共享记忆与工作任务**（双层机制与六类带出处长期记忆、版本留痕与核心规则置顶），以及 **MCP 管理与多宿主配置同步**（6 大主力 + 9 类扩展宿主的 JSON/TOML/JSON5/YAML 预览写入、钥匙串密钥隔离、快照回滚与外部修改 Diff 提醒）。
- **明确不适用场景**：不支持 Windows、Linux 或 Intel（\`x86_64\`）Mac；无多设备云同步（换机需用 \`contextcore backup\`）；9 类扩展宿主仅管配置不做对话接力；Public Beta 首次启动需执行 \`xattr -cr /Applications/Senrelay.app\`。
`;

await writeFile(path.join(out, 'llms.txt'), llmsContent);
await writeFile(path.join(out, 'llms-full.txt'), llmsFullContent);
console.log(
  `Built ${out}\nMode: ${
    site ? `deployment-ready (${site.href}${indexNowKey ? ', IndexNow enabled' : ''})` : 'local preview (noindex; no canonical or sitemap)'
  }`,
);
