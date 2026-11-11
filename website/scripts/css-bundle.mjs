import { readFile, readdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

// Modules are numbered; filename order is cascade order (later files may override earlier ones).
const cssDir = fileURLToPath(new URL('../assets/css/', import.meta.url));
// Authored CSS contains no strings with braces or comment markers, so a whitespace minifier is safe.
export function minifyCss(css) {
  return css
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .replace(/\s+/g, ' ')
    .replace(/\s*([{};,>])\s*/g, '$1')
    .replace(/:\s+/g, ':')
    .replace(/;}/g, '}')
    .replace(/\)and\(/g, ') and (')
    .trim() + '\n';
}

export async function bundleCss() {
  const names = (await readdir(cssDir)).filter((name) => name.endsWith('.css')).sort();
  const parts = await Promise.all(names.map((name) => readFile(path.join(cssDir, name), 'utf8')));
  return minifyCss(parts.join('\n'));
}
