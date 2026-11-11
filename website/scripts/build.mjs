import { readFile, mkdir, cp, writeFile, rm } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import brand from '../brand.config.mjs';
import { bundleCss } from './css-bundle.mjs';

const root = fileURLToPath(new URL('../', import.meta.url));
const out = path.join(root, 'dist');
const RELEASE_VERSION = '1.0.0-beta.1';
const FALLBACK_LASTMOD = '2026-11-11';

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

function buildHomepageSchema({ lang, pageUrl, siteDesc, appDesc, faqEntities, lastmod }) {
  const zhFullName = `${brand.name} · ${brand.chineseName}`;
  const displayName = lang === 'en' ? brand.name : zhFullName;
  const altNames = [...new Set([brand.name, brand.chineseName, zhFullName, 'ContextCore'].filter(Boolean))];
  const releaseTagUrl = `${brand.github}/releases/tag/v${RELEASE_VERSION}`;
  const orgId = site ? `${site.href}#organization` : '#organization';
  const websiteId = site ? `${pageUrl}#website` : '#website';
  const appId = site ? `${site.href}#software` : '#software';

  const webSiteNode = {
    '@type': 'WebSite',
    '@id': websiteId,
    name: displayName,
    alternateName: altNames,
    ...(site ? { url: pageUrl } : {}),
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    description: siteDesc,
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
    ...(site
      ? {
          url: pageUrl,
          screenshot: new URL('assets/social-original-v1.png', site).href,
        }
      : {}),
    downloadUrl: releaseTagUrl,
    codeRepository: brand.github,
    sameAs: [brand.github, releaseTagUrl],
    license: `${brand.github}/blob/main/LICENSE`,
    author: { '@id': orgId },
  };

  const orgNode = {
    '@type': 'Organization',
    '@id': orgId,
    name: zhFullName,
    alternateName: altNames,
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
    sameAs: [brand.github],
  };

  const faqNode = {
    '@type': 'FAQPage',
    ...(site ? { '@id': `${pageUrl}#faq`, url: pageUrl } : {}),
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    mainEntity: faqEntities,
  };

  return {
    '@context': 'https://schema.org',
    '@graph': [webSiteNode, appNode, orgNode, faqNode],
  };
}

function buildGuideSchema({ lang, pageUrl, homeUrl, headline, description, lastmod }) {
  const zhFullName = `${brand.name} · ${brand.chineseName}`;
  const orgId = site ? `${site.href}#organization` : '#organization';
  const appId = site ? `${site.href}#software` : '#software';

  const articleNode = {
    '@type': 'TechArticle',
    ...(site ? { '@id': `${pageUrl}#article`, url: pageUrl } : {}),
    headline,
    description,
    inLanguage: lang === 'en' ? 'en' : 'zh-CN',
    dateModified: lastmod,
    proficiencyLevel: 'Beginner',
    author: { '@type': 'Organization', '@id': orgId, name: zhFullName },
    publisher: { '@type': 'Organization', '@id': orgId, name: zhFullName },
    about: { '@type': 'SoftwareApplication', '@id': appId, name: lang === 'en' ? brand.name : zhFullName },
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
    '@graph': [articleNode, breadcrumbNode],
  };
}

function renderDeploymentHead({ canonicalHref, zhHref, enHref, altOgLocale, imageAlt }) {
  if (!site) return '';
  const socialImg = escapeHtml(new URL('assets/social-original-v1.png', site).href);
  const llmsHref = escapeHtml(new URL('llms.txt', site).href);
  const llmsFullHref = escapeHtml(new URL('llms-full.txt', site).href);
  return [
    `<link rel="canonical" href="${escapeHtml(canonicalHref)}">`,
    `<link rel="alternate" hreflang="zh-CN" href="${escapeHtml(zhHref)}">`,
    `<link rel="alternate" hreflang="zh" href="${escapeHtml(zhHref)}">`,
    `<link rel="alternate" hreflang="en" href="${escapeHtml(enHref)}">`,
    `<link rel="alternate" hreflang="x-default" href="${escapeHtml(enHref)}">`,
    `<link rel="alternate" type="text/plain" href="${llmsHref}" title="LLMs.txt">`,
    `<link rel="alternate" type="text/plain" href="${llmsFullHref}" title="LLMs-full.txt">`,
    `<meta property="og:locale:alternate" content="${escapeHtml(altOgLocale)}">`,
    `<meta property="og:url" content="${escapeHtml(canonicalHref)}">`,
    `<meta property="og:image" content="${socialImg}">`,
    `<meta property="og:image:width" content="1536">`,
    `<meta property="og:image:height" content="1024">`,
    `<meta property="og:image:alt" content="${escapeHtml(imageAlt)}">`,
    `<meta name="twitter:image" content="${socialImg}">`,
    `<meta name="twitter:image:alt" content="${escapeHtml(imageAlt)}">`,
  ].join('\n  ');
}

// 1. Prepare clean dist/ and copy static assets + bundled CSS + transformed i18n.js
await rm(out, { recursive: true, force: true });
await mkdir(path.join(out, 'en'), { recursive: true });

const cssSource = path.join(root, 'assets', 'css');
await cp(path.join(root, 'assets'), path.join(out, 'assets'), {
  recursive: true,
  filter: (source) =>
    source !== cssSource &&
    !source.startsWith(cssSource + path.sep) &&
    path.basename(source) !== 'social.png',
});
await cp(path.join(root, 'assets', 'favicon.ico'), path.join(out, 'favicon.ico'));
await writeFile(path.join(out, 'assets', 'site.css'), await bundleCss());

const rawI18n = await readFile(path.join(root, 'assets', 'i18n.js'), 'utf8');
const transformedI18n = applyBrandTokens(rawI18n, { htmlEscaped: false });
await writeFile(path.join(out, 'assets', 'i18n.js'), transformedI18n);

const { attrEn, htmlEn, metaEn, textEn } = await import(
  `${pathToFileURL(path.join(out, 'assets', 'i18n.js')).href}?v=${Date.now()}`
);

const homeLastmod = gitLastmod(['index.html', 'assets/i18n.js', 'assets/site.js', 'brand.config.mjs']);
const guideLastmod = gitLastmod(['guide.html', 'brand.config.mjs']);

const zhHomeHref = site ? site.href : './';
const enHomeHref = site ? new URL('en/', site).href : './en/';
const zhGuideHref = site ? new URL('guide.html', site).href : './guide.html';
const enGuideHref = site ? new URL('en/guide.html', site).href : './en/guide.html';

// 2. Build Chinese Homepage (dist/index.html) and Static English Homepage (dist/en/index.html)
const baseIndexHtml = applyBrandTokens(await readFile(path.join(root, 'index.html'), 'utf8'));
const baseSchema = JSON.parse(baseIndexHtml.match(/<script type="application\/ld\+json">(.*?)<\/script>/s)[1]);
const zhSiteDesc = baseSchema['@graph'][0].description;
const zhAppDesc = baseSchema['@graph'][1].description;
const zhFaqBlock = baseIndexHtml.match(/<div class="faq-list" data-i18n-html="faq">([\s\S]*?)<\/div>\s*<\/section>/)[1];

const zhSchema = buildHomepageSchema({
  lang: 'zh',
  pageUrl: zhHomeHref,
  siteDesc: zhSiteDesc,
  appDesc: zhAppDesc,
  faqEntities: extractFaqEntities(zhFaqBlock),
  lastmod: homeLastmod,
});

let zhHtml = baseIndexHtml.replace(
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
    altOgLocale: 'en_US',
    imageAlt: `${brand.name} · ${brand.chineseName} — ${brand.tagline}`,
  }),
);
await writeFile(path.join(out, 'index.html'), zhHtml);

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
  .replaceAll('href="./favicon.ico"', 'href="../favicon.ico"')
  .replaceAll('href="./assets/', 'href="../assets/')
  .replaceAll('src="./assets/', 'src="../assets/')
  .replace(
    /<div class="language-switch"[^>]*>[\s\S]*?<\/div>/,
    '<div class="language-switch" role="group" aria-label="Language"><a href="../" data-lang="zh" lang="zh-CN">中</a><a href="./" data-lang="en" aria-current="page" lang="en">EN</a></div>',
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
    altOgLocale: 'zh_CN',
    imageAlt: metaEn.ogTitle,
  }),
);
await writeFile(path.join(out, 'en', 'index.html'), enHtml);

// 3. Build Chinese Guide (dist/guide.html) and Static English Guide (dist/en/guide.html)
const baseGuideHtml = applyBrandTokens(await readFile(path.join(root, 'guide.html'), 'utf8'));
const zhGuideTitle = `${brand.name} · ${brand.chineseName} — 本地运行与使用指南`;
const zhGuideDesc = decodeHtml(baseGuideHtml.match(/<meta name="description" content="([^"]+)">/)[1]);

let zhGuide = removeElementByAttr(baseGuideHtml, 'div', 'data-guide-section="en"').replace(
  /\s*<hr class="lang-divider">\s*/g,
  '\n',
);
const zhGuideSchema = buildGuideSchema({
  lang: 'zh',
  pageUrl: zhGuideHref,
  homeUrl: zhHomeHref,
  headline: zhGuideTitle,
  description: zhGuideDesc,
  lastmod: guideLastmod,
});
if (site) {
  zhGuide = zhGuide.replace('content="noindex, nofollow"', 'content="index, follow"');
}
zhGuide = zhGuide
  .replace(
    '<!-- deployment-metadata -->',
    renderDeploymentHead({
      canonicalHref: zhGuideHref,
      zhHref: zhGuideHref,
      enHref: enGuideHref,
      altOgLocale: 'en_US',
      imageAlt: zhGuideTitle,
    }),
  )
  .replace('</head>', `  <script type="application/ld+json">${escapeJson(zhGuideSchema)}</script>\n</head>`);
await writeFile(path.join(out, 'guide.html'), zhGuide);

const enGuideTitle = `${brand.name} — Local Setup & Workflow Guide`;
const enGuideDesc = `${brand.name} local macOS service and workbench guide: Homebrew Cask and release setup, multi-host configuration management, companion source-linked auto-memory, and cross-agent conversation relay.`;

let enGuide = removeElementByAttr(baseGuideHtml, 'div', 'data-guide-section="zh"')
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
  .replace(/<title>.*?<\/title>/s, `<title>${escapeHtml(enGuideTitle)}</title>`)
  .replace(/(<meta name="description" content=")[^"]*(")/, `$1${escapeHtml(enGuideDesc)}$2`)
  .replace(/(<meta property="og:locale" content=")[^"]*(")/, '$1en_US$2')
  .replace(/(<meta property="og:site_name" content=")[^"]*(")/, `$1${escapeHtml(brand.name)}$2`)
  .replace(/(<meta property="og:title" content=")[^"]*(")/, `$1${escapeHtml(enGuideTitle)}$2`)
  .replace(/(<meta property="og:description" content=")[^"]*(")/, `$1${escapeHtml(enGuideDesc)}$2`)
  .replace(/(<meta name="twitter:title" content=")[^"]*(")/, `$1${escapeHtml(enGuideTitle)}$2`)
  .replace(/(<meta name="twitter:description" content=")[^"]*(")/, `$1${escapeHtml(enGuideDesc)}$2`)
  .replaceAll('href="./favicon.ico"', 'href="../favicon.ico"')
  .replaceAll('href="./assets/', 'href="../assets/')
  .replaceAll('src="./assets/', 'src="../assets/')
  .replace(
    /<div class="language-switch"[^>]*>[\s\S]*?<\/div>/,
    '<div class="language-switch" role="group" aria-label="Language" data-aria-zh="语言" data-aria-en="Language"><a href="../guide.html" data-lang="zh" lang="zh-CN">中</a><a href="./guide.html" data-lang="en" aria-current="page" lang="en">EN</a></div>',
  );

const enGuideSchema = buildGuideSchema({
  lang: 'en',
  pageUrl: enGuideHref,
  homeUrl: enHomeHref,
  headline: enGuideTitle,
  description: enGuideDesc,
  lastmod: guideLastmod,
});
if (site) {
  enGuide = enGuide.replace('content="noindex, nofollow"', 'content="index, follow"');
}
enGuide = enGuide
  .replace(
    '<!-- deployment-metadata -->',
    renderDeploymentHead({
      canonicalHref: enGuideHref,
      zhHref: zhGuideHref,
      enHref: enGuideHref,
      altOgLocale: 'zh_CN',
      imageAlt: enGuideTitle,
    }),
  )
  .replace('</head>', `  <script type="application/ld+json">${escapeJson(enGuideSchema)}</script>\n</head>`);
await writeFile(path.join(out, 'en', 'guide.html'), enGuide);

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
  'OAI-SearchBot',
  'ChatGPT-User',
  'Claude-SearchBot',
  'Claude-User',
  'PerplexityBot',
  'Google-Extended',
  'Applebot-Extended',
  'GPTBot',
  'ClaudeBot',
  'Bytespider',
  '*',
];

const robotsTxt = site
  ? `${aiCrawlerAgents.map((ua) => `User-agent: ${ua}\nAllow: ${site.pathname}`).join('\n\n')}\n\nSitemap: ${new URL('sitemap.xml', site).href}\n`
  : 'User-agent: *\nDisallow: /\n';
await writeFile(path.join(out, 'robots.txt'), robotsTxt);

if (site) {
  const sitemapEntries = [
    { loc: zhHomeHref, lastmod: homeLastmod, zh: zhHomeHref, en: enHomeHref },
    { loc: enHomeHref, lastmod: homeLastmod, zh: zhHomeHref, en: enHomeHref },
    { loc: zhGuideHref, lastmod: guideLastmod, zh: zhGuideHref, en: enGuideHref },
    { loc: enGuideHref, lastmod: guideLastmod, zh: zhGuideHref, en: enGuideHref },
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

// 6. Build GEO-ready llms.txt & llms-full.txt
const llmsFullUrl = site ? new URL('llms-full.txt', site).href : './llms-full.txt';
const homeZhRef = site ? zhHomeHref : './index.html';
const homeEnRef = site ? enHomeHref : './en/index.html';
const guideZhRef = site ? zhGuideHref : './guide.html';
const guideEnRef = site ? enGuideHref : './en/guide.html';
const releaseTagHref = `${brand.github}/releases/tag/v${RELEASE_VERSION}`;

const llmsContent = `# ${brand.name} · ${brand.chineseName} (ContextCore)

> ${brand.name} (open-source repository \`ContextCore\`) is a free, MIT-licensed, local-first macOS workbench and loopback service (\`127.0.0.1:8420\`) for cross-agent conversation relay (\`/contextcore\`), companion source-linked auto-memory, and unified multi-host AI coding assistant configuration management.

${brand.name} (${brand.chineseName}) runs natively on macOS 14.0+ (Sonoma or later, Apple Silicon \`arm64\`) as \`Senrelay.app\` or from source via Python 3.11+ and Node 22/24 LTS. Pricing: **$0 (free and open source under the MIT License)**.

> **Package Disambiguation**: Install the macOS app via \`brew install --cask qingsongcui/tap/senrelay\` or GitHub Releases (\`Senrelay-macOS.zip\`). Do **NOT** run \`pip install contextcore\`—the PyPI package of that name is an unrelated third-party project.

## Core Capabilities

- **Cross-Agent Conversation Relay (\`/contextcore\`)**: Type \`/contextcore\` (or say "接着做" / "接力" / "continue" / "relay") inside any of the 6 core context hosts—Claude Code, Codex, Cursor, Antigravity, Oh My Pi (OMP), or OpenCode—to list numbered recent cross-host conversation candidates with source agent, project, title, and summary. Selecting only a candidate number loads context with \`must_not_execute=true\` so the new agent briefs status and awaits instructions; attaching a concrete task continues execution immediately.
- **Companion Source-Linked Auto-Memory**: Companion MCP tools and Skills capture structured memories across six categories (Preferences, Background, Constraints, Tasks, Lessons, References) with exact conversation-turn or document provenance and a brief \`💡\` receipt, plus core-rule pinning (\`📌\`), conflict inbox review, version rollback, \`AGENTS.md\` handbook preview, and multi-format document imports (Markdown, TXT, JSON, PDF, DOCX, DOC).
- **Unified Multi-Host Configuration Management**: Switch across 6 core context hosts and 9 extended configuration hosts from a permanent top bar, run One-Click Connect, apply provider presets, test endpoint latency, isolate API keys in macOS Keychain, and sync MCP servers, Skills, and system prompts with automatic rollback snapshots and external-edit Diff protection.

## System Requirements & Privacy Boundaries

- **Platform & Pricing**: macOS 14.0+ on Apple Silicon (\`arm64\`) · Public Beta \`v${RELEASE_VERSION}\` · Free (\`$0\`, MIT License).
- **Quick Install**: \`brew install --cask qingsongcui/tap/senrelay\` (first launch for ad-hoc signed bundle: \`xattr -cr /Applications/Senrelay.app\`).
- **Privacy & Storage**: Loopback-only HTTP/MCP service (\`127.0.0.1:8420\`, dev \`127.0.0.1:18420\`), local SQLite + FTS5 full-text search (\`~/Library/Application Support/ContextCore\`, directory mode \`0700\`, DB mode \`0600\`), online SQLite backups (\`contextcore backup\`), and macOS Keychain secret isolation. No mandatory cloud account or background cloud upload.

## Resources

- [English Homepage](${homeEnRef}): Static English product overview, host capability matrix, and FAQ.
- [Chinese Homepage (中文首页)](${homeZhRef}): Primary Simplified Chinese product overview and interactive walkthrough.
- [English Setup & Workflow Guide](${guideEnRef}): Homebrew Cask install, first launch (\`xattr -cr\`), source build, and daily workflows.
- [Chinese Setup & Workflow Guide (中文运行指南)](${guideZhRef}): 本地安装、首次启动、源码运行与三大工作流指南。
- [Extended LLM Reference](${llmsFullUrl}): Complete 6-point relay specification, 6+9 host support matrix, CLI commands, and bilingual FAQ.
- [GitHub Repository](${brand.github}): Source code, architecture contracts, and automated verification gates.
- [Release v${RELEASE_VERSION}](${releaseTagHref}): Prebuilt \`Senrelay-macOS.zip\` and \`SHA256SUMS\`.
- [License](${brand.github}/blob/main/LICENSE): MIT License.
`;

const llmsFullContent = `${llmsContent}
## 6-Point Cross-Agent Relay Pack Specification

When \`/contextcore\` loads a selected conversation across Claude Code, Codex, Cursor, Antigravity, OMP, or OpenCode, it assembles a structured 6-point relay pack backed by recent verbatim transcript turns:

1. **01 — Project & Task Goal**: What the session is building and what is explicitly out of scope.
2. **02 — Current Progress**: Completed work vs remaining items.
3. **03 — Confirmed Decisions & Constraints**: Chosen architecture and boundaries that must not be altered.
4. **04 — Working Scene & Evidence**: Linked original conversation turns, project records, and imported documents.
5. **05 — Blockers & Open Questions**: Unresolved issues and assumptions requiring verification.
6. **06 — Next Action Plan**: Exact resume point and verification criteria.

**Execution Safety Contract**:
- **Number-only reply** (e.g. \`1\`): Sets \`must_not_execute=true\`; the target agent summarizes the handoff state and waits for your next prompt without editing files or running destructive commands.
- **Number + task reply** (e.g. \`1, finish the unit tests\`): Loads the 6-point pack and executes the requested follow-up task directly.

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

### 1. macOS App Bundle (Homebrew Cask or Release Archive)
\`\`\`bash
# Option A: Homebrew Cask
brew install --cask qingsongcui/tap/senrelay

# Option B: Manual Senrelay-macOS.zip first launch (ad-hoc signed, macOS 14.0+ arm64)
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
Connect your hosts once via **Agent Management → One-Click Connect**. When switching assistants, type \`/contextcore\` (or "接力" / "continue") in the target agent. It queries local authorized histories and lists numbered recent sessions. Reply \`1\` to load context (\`must_not_execute=true\`) and get a status summary without modifying files, or \`1, finish the unit tests\` to continue immediately.

### 2. How is implicit auto-memory triggered during chats? / 隐式自动记忆是如何触发的？
During everyday chats, companion MCP tools and Skills capture preferences, architectural decisions, constraints, milestones, and lessons learned as structured records linked to exact source turns, returning a brief \`💡\` receipt. Conflicting updates route to Pending Conflicts, and you can pin \`📌\` core rules or preview \`AGENTS.md\` anytime.

### 3. How does multi-host MCP and Skills sync avoid overwriting manual config edits? / 跨宿主同步如何防止覆盖手改配置？
Every configuration write records ownership hashes and an automatic rollback snapshot. If a target host file (\`~/.claude.json\`, \`~/.codex/config.toml\`, Cursor MCP config, etc.) was modified externally, ${brand.name} pauses auto-apply and opens a side-by-side Diff confirmation modal instead of silently overwriting user changes.

### 4. Will changing one assistant affect my other assistants? / 修改单个宿主会影响其他助手吗？
Never silently. You can configure a single host independently or explicitly select multiple hosts to sync shared MCP servers, Skills, and prompts, with full diff preview and one-click snapshot rollback.

### 5. Where are conversations, memories, and API keys stored? / 会话记录与 API Key 存储在哪里？
All session indexes, versioned memories, imported documents, and config snapshots stay in your local Mac SQLite + FTS5 database (\`0600\` permissions). Provider API keys live separately in macOS Keychain and are stripped from configuration exports by default.

### 6. Can I install ContextCore via \`pip install contextcore\`? / 可以用 \`pip install contextcore\` 安装吗？
No. The \`contextcore\` package on public PyPI is an unrelated third-party project. Install ${brand.name} on macOS via \`brew install --cask qingsongcui/tap/senrelay\`, download \`Senrelay-macOS.zip\` from \`${releaseTagHref}\`, or clone \`${brand.github}\` and run \`pip install -e 'app/backend[test]'\`.
`;

await writeFile(path.join(out, 'llms.txt'), llmsContent);
await writeFile(path.join(out, 'llms-full.txt'), llmsFullContent);
console.log(
  `Built ${out}\nMode: ${
    site ? `deployment-ready (${site.href}${indexNowKey ? ', IndexNow enabled' : ''})` : 'local preview (noindex; no canonical or sitemap)'
  }`,
);
